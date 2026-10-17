"""
Almacenamiento por niveles: caliente en la VM, frio en Drive. Corre EN LA VM (sin pandas).

    python -m heatsignal.history.archive check                 # uso de disco + que se archivaria (solo lectura)
    python -m heatsignal.history.archive run                   # archiva si el disco esta >= umbral (default 80 %)
    python -m heatsignal.history.archive run --dry-run         # = check, pero con la decision de `run`
    python -m heatsignal.history.archive status                # JSON para el panel / scripts
    python -m heatsignal.history.archive vacuum [--dry-run]    # migracion unica a auto_vacuum=INCREMENTAL

Opciones: --threshold 80 --target 70 --keep-days 60 --klines-keep-days N --checkpoint-keep-days 30
          --export-keep-days 3 --vacuum auto|never --json (y --db/--checkpoints-db/--export-dir/--remote).

Que hace `run` (lo dispara heatsignal-archive.timer cada hora): si el uso del sistema de archivos de data/ es
>= umbral, archiva lo mas viejo hasta que el uso PROYECTADO baje al objetivo, en este orden:
  a) export      carpetas locales data/export/YYYY/MM/DD de dias ya subidos: se verifica cada archivo contra
                 Drive (tamano + md5) y recien ahi se borra la copia local. Si falta en Drive, no se toca.
  b) klines      velas 1m mas viejas que --klines-keep-days (default = --keep-days). Son re-descargables de
                 Binance, pero igual se respaldan: un .csv.gz por mes (recortado al corte).
  c) history     bars_1m / liquidations / news mas viejos que --keep-days (default 60): un .csv.gz por dia y
                 tabla, mismo formato que el export diario.
  d) checkpoints checkpoints del grafo LangGraph mas viejos que --checkpoint-keep-days (default 30): un .sqlite
                 por dia con las filas de checkpoints y writes de esos hilos. OJO: solo son reconstruibles en
                 parte (el cycle_log guarda reporte y decisiones, no el estado intermedio de cada nodo): por
                 eso se respaldan antes de borrar y el panel los vuelve a leer desde Drive si hace falta.
NUNCA se tocan heatsignal_eval.sqlite (estrategias, hipotesis, ciclos, cola del lab: solo-agregar) ni
freqtrade_dryrun.sqlite: el archivador se niega a arrancar si alguna ruta configurada apunta a ellos.

Garantias por unidad (un dia / un mes / una carpeta):
  1. se escribe el archivo frio local (export.write_csv_gz, gzip determinista) y se cuentan sus filas;
  2. se sube con `rclone copyto --immutable` a gdrive:archive/... (nunca pisa un archivo frio distinto);
  3. se VERIFICA: tamano y md5 que informa Drive (o bajandolo si el backend no da md5) contra el local;
  4. recien entonces, en UNA transaccion: DELETE de esas filas (si el numero borrado no coincide con el
     exportado, ROLLBACK y error) + alta en `archive_catalog`. Si algo falla antes, no se borra nada.
Si una unidad falla se corta la corrida (status error) y se informa; lo ya archivado queda consistente.

Espacio: un DELETE no achica el archivo SQLite; las paginas quedan libres (freelist) y se reusan para datos
nuevos. El uso proyectado descuenta esa freelist. Para devolver el espacio al disco:
  - base con auto_vacuum=INCREMENTAL: `PRAGMA incremental_vacuum` por tandas + checkpoint del WAL (barato);
  - base sin auto_vacuum (la de hoy): un VACUUM completo necesita ~2x el tamano de la base libre (copia
    temporal + WAL). `run` lo hace una sola vez, si alcanza el espacio, y de paso deja la base en
    auto_vacuum=INCREMENTAL (migracion). Si no alcanza, avisa y el espacio queda para reuso.
  - migracion manual: `archive vacuum` (mejor con la ingesta parada, porque bloquea sus escrituras mientras
    dura; la ingesta reintenta y no pierde barras, pero el panel puede tardar).
"""

from __future__ import annotations

import argparse
import contextlib
import csv
import gzip
import json
import logging
import os
import shutil
import sqlite3
import sys
import tempfile
import time
import uuid
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from . import catalog as cat
from .export import TABLES, day_dir, table_sql, write_csv_gz
from .remote import RcloneRemote, RemoteError, join_remote, md5_file, verify_remote
from .store import SCHEMA

log = logging.getLogger("heatsignal.history.archive")

DAY_MS = 86_400_000
PROTECTED_NAMES = ("heatsignal_eval.sqlite", "freqtrade_dryrun.sqlite")
HISTORY_TABLES = ("bars_1m", "liquidations", "news")
STAGES = ("export", "klines", "history", "checkpoints")
STATUS_FILE = "archive_status.json"
LOCK_FILE = ".archive.lock"
VACUUM_MARGIN_BYTES = 256 * 1024 * 1024
INCREMENTAL_STEP_PAGES = 2048  # ~8 MB por tanda con paginas de 4 KB


class ArchiveError(Exception):
    pass


class SkipUnit(Exception):
    """La unidad no se puede archivar todavia (p.ej. un export que no esta en Drive): se saltea sin borrar."""


# ---- configuracion ---------------------------------------------------------------------------------

def _env_float(name: str, default: float) -> float:
    v = os.environ.get(name)
    return float(v) if v not in (None, "") else default


def _env_int(name: str, default: int) -> int:
    v = os.environ.get(name)
    return int(v) if v not in (None, "") else default


@dataclass
class ArchiveConfig:
    history_db: Path
    checkpoints_db: Path | None = None
    export_dir: Path | None = None
    remote_base: str | None = None        # HEATSIGNAL_DRIVE_REMOTE, p.ej. "gdrive:"
    archive_prefix: str = "archive"       # -> gdrive:archive/...
    threshold_pct: float = 80.0
    target_pct: float = 70.0
    keep_days: int = 60
    klines_keep_days: int | None = None   # None = keep_days
    checkpoint_keep_days: int = 30
    export_keep_days: int = 3             # dias recientes de data/export que nunca se borran
    vacuum: str = "auto"                  # auto | never
    stages: tuple[str, ...] = STAGES

    @property
    def data_dir(self) -> Path:
        return self.history_db.parent

    @classmethod
    def from_env(cls, **over: Any) -> ArchiveConfig:
        data = Path(os.environ.get("HEATSIGNAL_HISTORY_DB", "data/heatsignal_history.sqlite")).parent
        base = dict(
            history_db=Path(os.environ.get("HEATSIGNAL_HISTORY_DB", "data/heatsignal_history.sqlite")),
            checkpoints_db=Path(os.environ.get("HEATSIGNAL_CHECKPOINT_DB", str(data / "heatsignal_checkpoints.sqlite"))),
            export_dir=Path(os.environ.get("HEATSIGNAL_EXPORT_DIR", str(data / "export"))),
            remote_base=os.environ.get("HEATSIGNAL_DRIVE_REMOTE") or None,
            threshold_pct=_env_float("HEATSIGNAL_ARCHIVE_THRESHOLD", 80.0),
            target_pct=_env_float("HEATSIGNAL_ARCHIVE_TARGET", 70.0),
            keep_days=_env_int("HEATSIGNAL_ARCHIVE_KEEP_DAYS", 60),
            checkpoint_keep_days=_env_int("HEATSIGNAL_ARCHIVE_CHECKPOINT_KEEP_DAYS", 30),
        )
        base.update({k: v for k, v in over.items() if v is not None})
        return cls(**base)

    def validate(self) -> None:
        if not 0 < self.target_pct < self.threshold_pct <= 100:
            raise ArchiveError(f"se necesita 0 < objetivo ({self.target_pct}) < umbral ({self.threshold_pct}) <= 100")
        if self.keep_days < 1 or self.checkpoint_keep_days < 1 or (self.klines_keep_days or 1) < 1:
            raise ArchiveError("los dias a conservar tienen que ser >= 1")
        if self.vacuum not in ("auto", "never"):
            raise ArchiveError("--vacuum tiene que ser auto o never")
        for p in (self.history_db, self.checkpoints_db):
            if p is not None and p.name in PROTECTED_NAMES:
                raise ArchiveError(f"{p} es una base protegida (nunca se archiva ni se borra)")
        if self.history_db is not None and self.checkpoints_db is not None \
                and self.history_db.resolve() == self.checkpoints_db.resolve():
            raise ArchiveError("history_db y checkpoints_db no pueden ser la misma base")


# ---- disco -----------------------------------------------------------------------------------------

DiskFn = Callable[[Path], tuple[int, int, int]]  # (total, used, free) en bytes


def _disk_usage(path: Path) -> tuple[int, int, int]:
    du = shutil.disk_usage(path)
    return du.total, du.used, du.free


def disk_info(path: Path, disk: DiskFn = _disk_usage) -> dict[str, Any]:
    """Uso como lo muestra `df`: used / (used + disponible) (excluye los bloques reservados para root)."""
    total, used, free = disk(path)
    return {"path": str(path), "total_bytes": total, "used_bytes": used, "free_bytes": free,
            "used_pct": round(100.0 * used / (used + free), 2) if used + free else 0.0}


# ---- utilidades ------------------------------------------------------------------------------------

def _now_ms() -> int:
    return int(time.time() * 1000)


def day_start_ms(d: date) -> int:
    return int(datetime(d.year, d.month, d.day, tzinfo=timezone.utc).timestamp() * 1000)


def utc_day(ms: int) -> date:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).date()


def iso(ms: int | None) -> str | None:
    return None if ms is None else datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat()


_UUID_EPOCH_100NS = 0x01B21DD213814000  # 1582-10-15 -> 1970-01-01 en intervalos de 100 ns


def uuid6_ms(value: str) -> int | None:
    """Epoch ms de un UUID v6 (los checkpoint_id de LangGraph); None si no es v6."""
    try:
        u = uuid.UUID(value)
    except ValueError:
        return None
    h = u.hex
    if h[12] != "6":
        return None
    ts = int(h[:12] + h[13:16], 16)
    return (ts - _UUID_EPOCH_100NS) // 10_000


def thread_ms(thread_id: str, last_checkpoint_id: str | None) -> int | None:
    """Momento de un hilo del grafo: thread_id = "<simbolos>:<run_id>" con run_id cycle-<ms> / manual-<ms>;
    si no, el UUID v6 del ultimo checkpoint."""
    run_id = thread_id.rsplit(":", 1)[-1]
    tail = run_id.rsplit("-", 1)[-1]
    if tail.isdigit() and len(tail) == 13:
        return int(tail)
    return uuid6_ms(last_checkpoint_id) if last_checkpoint_id else None


def count_csv_rows(path: Path) -> int:
    """Filas de datos de un .csv.gz (sin encabezado). Lee todo el archivo: valida tambien el gzip."""
    with gzip.open(path, "rt", encoding="utf-8", newline="") as fh:
        return max(sum(1 for _ in csv.reader(fh)) - 1, 0)


def _dir_size(p: Path) -> int:
    return sum(f.stat().st_size for f in p.rglob("*") if f.is_file())


def _db_files_size(p: Path) -> int:
    return sum(x.stat().st_size for x in p.parent.glob(p.name + "*") if x.is_file())


def _open_ro(path: Path, timeout: float = 30.0) -> sqlite3.Connection:
    from urllib.parse import quote

    return sqlite3.connect(f"file:{quote(str(path.resolve()))}?mode=ro", uri=True, timeout=timeout)


def _open_rw(path: Path, timeout: float = 60.0) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path), timeout=timeout, isolation_level=None)  # transacciones explicitas
    return conn


def _pragma(conn: sqlite3.Connection, name: str) -> Any:
    return conn.execute(f"PRAGMA {name}").fetchone()[0]


def freelist_bytes(conn: sqlite3.Connection) -> int:
    return int(_pragma(conn, "freelist_count")) * int(_pragma(conn, "page_size"))


def _has_table(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None


def _bytes_per_row(conn: sqlite3.Connection, table: str, total_rows: int) -> float:
    """Bytes en disco por fila (tabla + sus indices) segun dbstat; si no esta compilado, una cota gruesa."""
    if total_rows <= 0:
        return 0.0
    names = [table] + [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE tbl_name = ? AND type = 'index'", (table,))]
    try:
        q = f"SELECT SUM(pgsize) FROM dbstat WHERE name IN ({','.join('?' * len(names))})"
        size = conn.execute(q, names).fetchone()[0] or 0
    except sqlite3.OperationalError:
        size = int(_pragma(conn, "page_count")) * int(_pragma(conn, "page_size")) / 4  # sin dbstat: grueso
    return size / total_rows


# ---- unidades --------------------------------------------------------------------------------------

@dataclass
class Unit:
    stage: str
    label: str
    start_ms: int
    end_ms: int
    rows: dict[str, int] = field(default_factory=dict)
    est_bytes: int = 0
    path: str | None = None          # export: carpeta local
    threads: list[str] = field(default_factory=list)  # checkpoints

    def brief(self) -> dict[str, Any]:
        return {"stage": self.stage, "label": self.label, "start_ms": self.start_ms, "end_ms": self.end_ms,
                "start_at": iso(self.start_ms), "end_at": iso(self.end_ms), "rows": self.rows,
                "est_bytes": self.est_bytes}


# ---- archivador ------------------------------------------------------------------------------------

class Archiver:
    def __init__(
        self,
        cfg: ArchiveConfig,
        *,
        remote: RcloneRemote | None = None,
        disk: DiskFn = _disk_usage,
        clock_ms: Callable[[], int] = _now_ms,
    ) -> None:
        cfg.validate()
        self.cfg = cfg
        self._remote = remote
        self._disk = disk
        self._clock_ms = clock_ms
        self.run_id = "arch-" + str(clock_ms())

    # ---- lectura (check / plan) ----

    def _today(self) -> date:
        return utc_day(self._clock_ms())

    def _cutoff(self, keep_days: int) -> int:
        return day_start_ms(self._today() - timedelta(days=keep_days))

    def _units_export(self, conn: sqlite3.Connection | None) -> list[Unit]:
        out = self.cfg.export_dir
        if out is None or not out.is_dir():
            return []
        last_ok = self._today() - timedelta(days=self.cfg.export_keep_days)
        units = []
        for p in sorted(out.glob("[0-9][0-9][0-9][0-9]/[0-9][0-9]/[0-9][0-9]")):
            try:
                d = date(int(p.parent.parent.name), int(p.parent.name), int(p.name))
            except ValueError:
                continue
            if d >= last_ok or not p.is_dir():
                continue
            files = sorted(f for f in p.iterdir() if f.is_file() and f.name.endswith(".csv.gz"))
            if not files:
                continue
            units.append(Unit("export", f"export {d}", day_start_ms(d), day_start_ms(d) + DAY_MS,
                              rows={"files": len(files)}, est_bytes=_dir_size(p), path=str(p)))
        return units

    def _units_rows(self, conn: sqlite3.Connection, stage: str) -> list[Unit]:
        """Unidades de filas: history = un dia (3 tablas); klines = un mes recortado al corte."""
        if stage == "history":
            tables, cutoff = HISTORY_TABLES, self._cutoff(self.cfg.keep_days)
        else:
            tables, cutoff = ("klines_1m",), self._cutoff(self.cfg.klines_keep_days or self.cfg.keep_days)
        per_day: dict[int, dict[str, int]] = {}
        bpr_total: dict[str, float] = {}
        for t in tables:
            table = "klines" if t == "klines_1m" else t
            if not _has_table(conn, table):
                continue
            _, ts_col = TABLES[t]
            extra = " AND interval = '1m'" if t == "klines_1m" else ""
            rows = conn.execute(
                f"SELECT {ts_col} / {DAY_MS}, COUNT(*) FROM {table} WHERE {ts_col} < ?{extra} GROUP BY 1",
                (cutoff,)).fetchall()
            total = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            bpr_total[t] = _bytes_per_row(conn, table, total)
            for d, n in rows:
                per_day.setdefault(int(d) * DAY_MS, {})[t] = n
        if not per_day:
            return []
        units: list[Unit] = []
        if stage == "history":
            for start in sorted(per_day):
                rows = {t: per_day[start].get(t, 0) for t in tables}
                units.append(Unit("history", f"history {utc_day(start)}", start, start + DAY_MS, rows=rows,
                                  est_bytes=int(sum(n * bpr_total.get(t, 0) for t, n in rows.items()))))
            return units
        by_month: dict[tuple[int, int], list[int]] = {}
        for start in sorted(per_day):
            d = utc_day(start)
            by_month.setdefault((d.year, d.month), []).append(start)
        for (y, m), starts in sorted(by_month.items()):
            a, b = starts[0], starts[-1] + DAY_MS
            n = sum(per_day[s].get("klines_1m", 0) for s in starts)
            units.append(Unit("klines", f"klines {y:04d}-{m:02d} ({utc_day(a)} a {utc_day(b - 1)})", a, b,
                              rows={"klines_1m": n}, est_bytes=int(n * bpr_total.get("klines_1m", 0))))
        return units

    def _units_checkpoints(self) -> list[Unit]:
        p = self.cfg.checkpoints_db
        if p is None or not p.is_file():
            return []
        conn = _open_ro(p)
        try:
            if not _has_table(conn, "checkpoints"):
                return []
            threads = conn.execute(
                "SELECT thread_id, MAX(checkpoint_id), COUNT(*) FROM checkpoints GROUP BY thread_id").fetchall()
            total = sum(n for _, _, n in threads)
            bpr = _bytes_per_row(conn, "checkpoints", total)
            has_writes = _has_table(conn, "writes")
            wtotal = conn.execute("SELECT COUNT(*) FROM writes").fetchone()[0] if has_writes else 0
            wbpr = _bytes_per_row(conn, "writes", wtotal) if has_writes else 0.0
            wcount = dict(conn.execute("SELECT thread_id, COUNT(*) FROM writes GROUP BY thread_id").fetchall()) \
                if has_writes else {}
        finally:
            conn.close()
        cutoff = self._cutoff(self.cfg.checkpoint_keep_days)
        by_day: dict[int, Unit] = {}
        for tid, last_cid, n in threads:
            ms = thread_ms(tid, last_cid)
            if ms is None or ms >= cutoff:
                continue  # sin fecha reconocible: no se archiva (se informa en el plan como conservado)
            start = ms - ms % DAY_MS
            u = by_day.setdefault(start, Unit("checkpoints", f"checkpoints {utc_day(start)}", start, start + DAY_MS,
                                              rows={"checkpoints": 0, "writes": 0}))
            u.threads.append(tid)
            u.rows["checkpoints"] += n
            u.rows["writes"] += wcount.get(tid, 0)
            u.est_bytes += int(n * bpr + wcount.get(tid, 0) * wbpr)
        return [by_day[k] for k in sorted(by_day)]

    def candidates(self, conn: sqlite3.Connection | None) -> list[Unit]:
        units: list[Unit] = []
        for stage in self.cfg.stages:
            if stage == "export":
                units += self._units_export(conn)
            elif stage in ("klines", "history") and conn is not None:
                units += self._units_rows(conn, stage)
            elif stage == "checkpoints":
                units += self._units_checkpoints()
        return units

    def _reclaimable(self, hconn: sqlite3.Connection | None) -> int:
        n = freelist_bytes(hconn) if hconn is not None else 0
        p = self.cfg.checkpoints_db
        if p is not None and p.is_file():
            c = _open_ro(p)
            try:
                n += freelist_bytes(c)
            finally:
                c.close()
        return n

    def _projected_pct(self, disk: dict[str, Any], reclaim: int, extra_freed: int = 0) -> float:
        cap = disk["used_bytes"] + disk["free_bytes"]
        return round(100.0 * max(disk["used_bytes"] - reclaim - extra_freed, 0) / cap, 2) if cap else 0.0

    def _vacuum_info(self, conn: sqlite3.Connection | None, path: Path | None, disk: dict[str, Any]) -> dict | None:
        if conn is None or path is None:
            return None
        size = _db_files_size(path)
        need = 2 * size + VACUUM_MARGIN_BYTES
        mode = {0: "none", 1: "full", 2: "incremental"}.get(int(_pragma(conn, "auto_vacuum")), "?")
        return {"path": str(path), "size_bytes": size, "auto_vacuum": mode, "freelist_bytes": freelist_bytes(conn),
                "full_vacuum_needs_bytes": need, "full_vacuum_possible": disk["free_bytes"] >= need}

    def check(self, *, mode: str = "check") -> dict[str, Any]:
        """Solo lectura: no abre nada en escritura (ni crea el catalogo)."""
        cfg = self.cfg
        disk = disk_info(cfg.data_dir, self._disk)
        hconn = _open_ro(cfg.history_db) if cfg.history_db.is_file() else None
        try:
            reclaim = self._reclaimable(hconn)
            units = self.candidates(hconn)
            vac = self._vacuum_info(hconn, cfg.history_db, disk)
            cold = cat.summary(hconn) if hconn is not None else {}
        finally:
            if hconn is not None:
                hconn.close()
        projected = self._projected_pct(disk, reclaim)
        plan, freed = [], 0
        for u in units:
            if self._projected_pct(disk, reclaim, freed) <= cfg.target_pct:
                break
            plan.append(u.brief())
            freed += u.est_bytes
        by_stage: dict[str, dict[str, Any]] = {}
        for u in units:
            s = by_stage.setdefault(u.stage, {"units": 0, "est_bytes": 0, "rows": {}, "first": u.label, "last": u.label})
            s["units"] += 1
            s["est_bytes"] += u.est_bytes
            s["last"] = u.label
            for k, v in u.rows.items():
                s["rows"][k] = s["rows"].get(k, 0) + v
        would_run = disk["used_pct"] >= cfg.threshold_pct
        warnings = []
        if not cfg.remote_base:
            warnings.append("HEATSIGNAL_DRIVE_REMOTE sin definir: `run` no puede archivar (no hay a donde subir)")
        if vac and vac["auto_vacuum"] != "incremental":
            warnings.append(
                "la base historica no tiene auto_vacuum=INCREMENTAL: lo borrado queda como espacio libre dentro "
                "de la base hasta un VACUUM completo (necesita ~" + _fmt_bytes(vac["full_vacuum_needs_bytes"])
                + " libres; correr `archive vacuum` o lo hace `run` la primera vez si alcanza)")
        if units and len(plan) == len(units) and self._projected_pct(disk, reclaim, freed) > cfg.target_pct:
            warnings.append("aun archivando todo lo elegible no se llega al objetivo: bajar --keep-days o liberar otra cosa")
        return {
            "mode": mode, "ts_ms": self._clock_ms(), "ts_at": iso(self._clock_ms()),
            "disk": disk, "threshold_pct": cfg.threshold_pct, "target_pct": cfg.target_pct,
            "keep_days": cfg.keep_days, "klines_keep_days": cfg.klines_keep_days or cfg.keep_days,
            "checkpoint_keep_days": cfg.checkpoint_keep_days, "export_keep_days": cfg.export_keep_days,
            "reclaimable_bytes": reclaim, "projected_used_pct": projected,
            "would_run": would_run,
            "plan_note": None if would_run else (
                f"uso {disk['used_pct']} % < umbral {cfg.threshold_pct} %: hoy `run` no archiva nada. El plan de abajo "
                "es solo informativo (lo que haria para bajar al objetivo)."),
            "plan": plan, "plan_freed_est_bytes": freed,
            "plan_projected_used_pct": self._projected_pct(disk, reclaim, freed),
            "candidates": by_stage, "vacuum": vac, "cold": cold,
            "remote": join_remote(cfg.remote_base, cfg.archive_prefix) if cfg.remote_base else None,
            "protected": list(PROTECTED_NAMES), "warnings": warnings,
        }

    # ---- escritura (run) ----

    def _require_remote(self) -> RcloneRemote:
        if not self.cfg.remote_base:
            raise ArchiveError("HEATSIGNAL_DRIVE_REMOTE sin definir: no hay a donde archivar")
        if self._remote is None:
            try:
                self._remote = RcloneRemote()
            except RemoteError as exc:
                raise ArchiveError(str(exc)) from exc
        return self._remote

    def _put(self, local: Path, rel: str, work: Path) -> tuple[str, str, int]:
        """Sube `local` a <remoto>/<prefijo>/<rel> y lo verifica. Devuelve (ruta remota, md5, bytes).

        Si ya hay un archivo identico (corrida anterior que no llego a borrar), se reutiliza. Si hay uno distinto,
        se sube con otro nombre (nunca se pisa un archivo frio)."""
        remote = self._require_remote()
        md5, size = md5_file(local), local.stat().st_size
        full = join_remote(self.cfg.remote_base or "", f"{self.cfg.archive_prefix}/{rel}")
        existing = remote.stat(full)
        if existing is not None:
            if existing.size == size and existing.md5 == md5:
                log.info("ya estaba en Drive (identico): %s", full)
                return full, md5, size
            stem, _, ext = rel.partition(".")
            full = join_remote(self.cfg.remote_base or "", f"{self.cfg.archive_prefix}/{stem}.{self.run_id}.{ext}")
            log.warning("en Drive ya hay otro archivo con ese nombre: se sube como %s", full)
        remote.upload(local, full)
        verify_remote(remote, local, full, workdir=work)
        log.info("subido y verificado: %s (%d B, md5 %s)", full, size, md5)
        return full, md5, size

    def _catalog_insert(self, conn: sqlite3.Connection, kind: str, table: str, symbols: str, start: int, end: int,
                        rows: int, remote_path: str, size: int, md5: str) -> None:
        conn.execute(
            "INSERT INTO archive_catalog (kind, table_name, symbols, start_ms, end_ms, rows, remote_path, bytes, md5, "
            "archived_ms, run_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (kind, table, symbols, start, end, rows, remote_path, size, md5, self._clock_ms(), self.run_id))

    def _exec_export(self, conn: sqlite3.Connection, u: Unit, work: Path) -> None:
        remote = self._require_remote()
        day_path = Path(u.path or "")
        rel_day = day_path.relative_to(self.cfg.export_dir or day_path).as_posix()
        verified = []
        for f in sorted(x for x in day_path.iterdir() if x.is_file()):
            if not f.name.endswith(".csv.gz"):
                raise ArchiveError(f"{f}: archivo inesperado en la carpeta de export (no se borra la carpeta)")
            full = join_remote(self.cfg.remote_base or "", f"{rel_day}/{f.name}")
            try:
                got = verify_remote(remote, f, full, workdir=work)
            except RemoteError as exc:
                raise SkipUnit(f"{u.label}: {f.name} no esta verificado en Drive ({exc}); la copia local queda") from exc
            verified.append((f, full, got, count_csv_rows(f)))
        conn.execute("BEGIN IMMEDIATE")
        try:
            for f, full, got, n in verified:
                self._catalog_insert(conn, "export_local", "export", "", u.start_ms, u.end_ms, n, full, got.size, got.md5 or "")
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        shutil.rmtree(day_path)
        for parent in (day_path.parent, day_path.parent.parent):  # carpetas de mes/anio vacias
            with contextlib.suppress(OSError):
                parent.rmdir()

    def _exec_rows(self, conn: sqlite3.Connection, u: Unit, work: Path) -> None:
        tables = HISTORY_TABLES if u.stage == "history" else ("klines_1m",)
        d = utc_day(u.start_ms)
        prepared = []
        for t in tables:
            cols, ts_col = TABLES[t]
            sql, where = table_sql(t)
            local = work / f"{t}.csv.gz"
            n = write_csv_gz(conn.execute(sql, (u.start_ms, u.end_ms)), cols, local)
            if count_csv_rows(local) != n:
                raise ArchiveError(f"{u.label}/{t}: el archivo local no tiene las {n} filas exportadas")
            table = "klines" if t == "klines_1m" else t
            symbols = "" if t == "news" else ",".join(r[0] for r in conn.execute(
                f"SELECT DISTINCT symbol FROM {table} WHERE {where} ORDER BY 1", (u.start_ms, u.end_ms)))
            if u.stage == "history":
                rel = f"history/{d:%Y/%m/%d}/{t}.csv.gz"
            else:
                rel = f"klines_1m/{d:%Y/%m}/klines_1m_{d:%Y%m%d}-{utc_day(u.end_ms - 1):%Y%m%d}.csv.gz"
            prepared.append((t, table, where, local, n, symbols, rel))
        uploaded = []
        for t, table, where, local, n, symbols, rel in prepared:
            full, md5, size = self._put(local, rel, work)
            uploaded.append((t, table, where, n, symbols, full, md5, size))
        # todo subido y verificado: borrar + catalogar en UNA transaccion
        conn.execute("BEGIN IMMEDIATE")
        try:
            for t, table, where, n, symbols, full, md5, size in uploaded:
                cur = conn.execute(f"DELETE FROM {table} WHERE {where}", (u.start_ms, u.end_ms))
                if cur.rowcount != n:
                    raise ArchiveError(f"{u.label}/{t}: se iban a borrar {n} filas y el DELETE toco {cur.rowcount} "
                                       "(llegaron datos nuevos al rango?); ROLLBACK, no se borro nada")
                self._catalog_insert(conn, "table", t, symbols, u.start_ms, u.end_ms, n, full, size, md5)
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise

    def _exec_checkpoints(self, conn: sqlite3.Connection, u: Unit, work: Path) -> None:
        src_path = self.cfg.checkpoints_db
        assert src_path is not None
        local = work / "checkpoints.sqlite"
        local.unlink(missing_ok=True)
        src = _open_rw(src_path)
        try:
            ddl = [r[0] for r in src.execute(
                "SELECT sql FROM sqlite_master WHERE type = 'table' AND name IN ('checkpoints', 'writes') AND sql IS NOT NULL")]
            dst = sqlite3.connect(str(local), isolation_level=None)
            try:
                dst.execute("BEGIN")
                for s in ddl:
                    dst.execute(s)
                has_writes = _has_table(src, "writes")
                n_ck = n_wr = 0
                for tid in u.threads:
                    rows = src.execute("SELECT * FROM checkpoints WHERE thread_id = ?", (tid,)).fetchall()
                    if rows:
                        dst.executemany(f"INSERT INTO checkpoints VALUES ({','.join('?' * len(rows[0]))})", rows)
                    n_ck += len(rows)
                    if has_writes:
                        wr = src.execute("SELECT * FROM writes WHERE thread_id = ?", (tid,)).fetchall()
                        if wr:
                            dst.executemany(f"INSERT INTO writes VALUES ({','.join('?' * len(wr[0]))})", wr)
                        n_wr += len(wr)
                dst.execute("COMMIT")
                got_ck = dst.execute("SELECT COUNT(*) FROM checkpoints").fetchone()[0]
                got_wr = dst.execute("SELECT COUNT(*) FROM writes").fetchone()[0] if has_writes else 0
            finally:
                dst.close()
            if (got_ck, got_wr) != (n_ck, n_wr):
                raise ArchiveError(f"{u.label}: la copia tiene {got_ck}/{got_wr} filas y se esperaban {n_ck}/{n_wr}")
            d = utc_day(u.start_ms)
            full, md5, size = self._put(local, f"checkpoints/{d:%Y/%m}/checkpoints_{d:%Y%m%d}.sqlite", work)
            # catalogo primero: si el borrado falla, el hilo sigue local y el panel lo lee de aca (sin duplicar)
            conn.execute("BEGIN IMMEDIATE")
            self._catalog_insert(conn, "checkpoints", "checkpoints", "", u.start_ms, u.end_ms, n_ck, full, size, md5)
            conn.execute("COMMIT")
            src.execute("BEGIN IMMEDIATE")
            try:
                marks = ",".join("?" * len(u.threads))
                dw = src.execute(f"DELETE FROM writes WHERE thread_id IN ({marks})", u.threads).rowcount if has_writes else 0
                dc = src.execute(f"DELETE FROM checkpoints WHERE thread_id IN ({marks})", u.threads).rowcount
                if (dc, dw) != (n_ck, n_wr):
                    raise ArchiveError(f"{u.label}: el DELETE toco {dc}/{dw} filas y se esperaban {n_ck}/{n_wr}; ROLLBACK")
                src.execute("COMMIT")
            except BaseException:
                src.execute("ROLLBACK")
                raise
            self._reclaim(src, "checkpoints")
        finally:
            src.close()

    def _reclaim(self, conn: sqlite3.Connection, name: str) -> None:
        """Con auto_vacuum=INCREMENTAL devuelve las paginas libres al disco por tandas (WAL acotado)."""
        if int(_pragma(conn, "auto_vacuum")) != 2:
            return
        while int(_pragma(conn, "freelist_count")) > 0:
            before = int(_pragma(conn, "freelist_count"))
            conn.execute(f"PRAGMA incremental_vacuum({INCREMENTAL_STEP_PAGES})").fetchall()
            with contextlib.suppress(sqlite3.OperationalError):
                conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchall()
            if int(_pragma(conn, "freelist_count")) >= before:
                log.warning("%s: incremental_vacuum no avanzo (lectores activos?); se sigue en la proxima corrida", name)
                break

    def full_vacuum(self, path: Path, *, dry_run: bool = False) -> dict[str, Any]:
        """VACUUM completo + auto_vacuum=INCREMENTAL (migracion). Solo si hay ~2x el tamano de la base libre."""
        if path.name in PROTECTED_NAMES:
            raise ArchiveError(f"{path} es una base protegida")
        conn = _open_ro(path) if dry_run else _open_rw(path)
        try:
            return self._vacuum_with(conn, path, dry_run=dry_run)
        finally:
            conn.close()

    def run(self, *, dry_run: bool = False) -> dict[str, Any]:
        cfg = self.cfg
        if dry_run:
            return self.check(mode="run --dry-run")
        started = self._clock_ms()
        disk0 = disk_info(cfg.data_dir, self._disk)
        report: dict[str, Any] = {"mode": "run", "run_id": self.run_id, "ts_ms": started, "ts_at": iso(started),
                                  "disk_before": disk0, "threshold_pct": cfg.threshold_pct, "target_pct": cfg.target_pct,
                                  "archived": [], "errors": [], "vacuum": []}
        if disk0["used_pct"] < cfg.threshold_pct:
            report["status"] = "skipped"
            report["note"] = f"uso {disk0['used_pct']} % < umbral {cfg.threshold_pct} %: nada que archivar"
            log.info(report["note"])
            return report
        log.warning("uso de disco %.1f %% >= umbral %.1f %%: archivando hasta %.1f %%", disk0["used_pct"],
                    cfg.threshold_pct, cfg.target_pct)
        conn = _open_rw(cfg.history_db)
        status = "ok"
        try:
            conn.execute("PRAGMA journal_mode=WAL").fetchall()
            conn.executescript(SCHEMA)
            self._require_remote()
            units = self.candidates(conn)
            with tempfile.TemporaryDirectory(prefix="archive-", dir=str(cfg.data_dir)) as tmp:
                work = Path(tmp)
                for u in units:
                    disk = disk_info(cfg.data_dir, self._disk)
                    projected = self._projected_pct(disk, self._reclaimable(conn))
                    if projected <= cfg.target_pct:
                        log.info("uso proyectado %.1f %% <= objetivo %.1f %%: listo", projected, cfg.target_pct)
                        break
                    log.info("archivando %s (%s)", u.label, u.rows)
                    t0 = time.time()
                    try:
                        if u.stage == "export":
                            self._exec_export(conn, u, work)
                        elif u.stage == "checkpoints":
                            self._exec_checkpoints(conn, u, work)
                        else:
                            self._exec_rows(conn, u, work)
                            self._reclaim(conn, "history")
                    except SkipUnit as exc:
                        log.warning("se saltea %s", exc)
                        report.setdefault("skipped", []).append(str(exc))
                        continue
                    except (ArchiveError, RemoteError, sqlite3.Error, OSError) as exc:
                        msg = f"{u.label}: {type(exc).__name__}: {exc}"
                        log.error("FALLO %s - no se borro nada de esa unidad; se corta la corrida", msg)
                        report["errors"].append(msg)
                        status = "error"
                        break
                    finally:
                        for f in work.iterdir():
                            f.unlink(missing_ok=True)
                    report["archived"].append({**u.brief(), "seconds": round(time.time() - t0, 1)})
                else:
                    if units:
                        log.info("se archivo todo lo elegible")
            if status == "ok" and cfg.vacuum == "auto":
                report["vacuum"] = self._final_vacuum(conn)
        except (ArchiveError, RemoteError, sqlite3.Error, OSError) as exc:
            report["errors"].append(f"{type(exc).__name__}: {exc}")
            status = "error"
            log.error("archivado abortado: %s", exc)
        finally:
            disk1 = disk_info(cfg.data_dir, self._disk)
            report["disk_after"] = disk1
            report["projected_used_pct"] = self._projected_pct(disk1, self._reclaimable(conn))
            report["freed_bytes"] = max(disk0["used_bytes"] - disk1["used_bytes"], 0)
            report["status"] = status if (status != "ok" or report["archived"] or report["vacuum"]) else "nothing"
            if report["status"] == "ok" and report["projected_used_pct"] > cfg.target_pct:
                report["status"] = "partial"
                report["errors"].append("no se llego al objetivo con lo elegible (bajar --keep-days?)")
            with contextlib.suppress(sqlite3.Error):
                conn.execute(
                    "INSERT INTO archive_runs (run_id, started_ms, finished_ms, status, threshold_pct, target_pct, "
                    "used_pct_before, used_pct_after, freed_bytes, detail) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (self.run_id, started, self._clock_ms(), report["status"], cfg.threshold_pct, cfg.target_pct,
                     disk0["used_pct"], disk1["used_pct"], report["freed_bytes"],
                     json.dumps({"archived": [a["label"] for a in report["archived"]], "errors": report["errors"],
                                 "vacuum": report["vacuum"]})))
            conn.close()
        return report

    def _final_vacuum(self, conn: sqlite3.Connection) -> list[dict[str, Any]]:
        """Bases sin auto_vacuum incremental con espacio libre interno: VACUUM completo si alcanza el disco."""
        out = []
        targets = [(self.cfg.history_db, conn)]
        if self.cfg.checkpoints_db is not None and self.cfg.checkpoints_db.is_file():
            targets.append((self.cfg.checkpoints_db, None))
        for path, c in targets:
            probe = c or _open_ro(path)
            try:
                needs = int(_pragma(probe, "auto_vacuum")) != 2 and freelist_bytes(probe) > 0
            finally:
                if c is None:
                    probe.close()
            if not needs:
                continue
            info = self._vacuum_with(c, path) if c is not None else self.full_vacuum(path)
            if "sin espacio" in str(info.get("result")):
                log.warning("%s: %s (necesita %s libres)", path, info["result"], _fmt_bytes(info["needs_bytes"]))
            out.append(info)
        return out

    def _vacuum_with(self, conn: sqlite3.Connection, path: Path, *, dry_run: bool = False) -> dict[str, Any]:
        disk = disk_info(self.cfg.data_dir, self._disk)
        size = _db_files_size(path)
        need = 2 * size + VACUUM_MARGIN_BYTES
        info: dict[str, Any] = {"path": str(path), "needs_bytes": need, "free_bytes": disk["free_bytes"],
                                "before": {"size_bytes": size, "freelist_bytes": freelist_bytes(conn),
                                           "auto_vacuum": int(_pragma(conn, "auto_vacuum"))}}
        if disk["free_bytes"] < need:
            info["result"] = "sin espacio: no se hace (el espacio libre interno se reusa para datos nuevos)"
            return info
        if dry_run:
            info["result"] = "dry-run: alcanzaria el espacio"
            return info
        t0 = time.time()
        conn.execute("PRAGMA auto_vacuum = INCREMENTAL")
        conn.execute("VACUUM")
        with contextlib.suppress(sqlite3.OperationalError):
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchall()
        info["after"] = {"size_bytes": _db_files_size(path), "auto_vacuum": int(_pragma(conn, "auto_vacuum"))}
        info["result"] = f"ok en {time.time() - t0:.1f} s"
        log.info("VACUUM %s: %s -> %s", path, _fmt_bytes(size), _fmt_bytes(info["after"]["size_bytes"]))
        return info


# ---- status ----------------------------------------------------------------------------------------

def status(cfg: ArchiveConfig, *, disk: DiskFn = _disk_usage, cache_dir: Path | None = None) -> dict[str, Any]:
    """Estado para el panel: disco, caliente (base) vs frio (catalogo), ultimas corridas y ultimo chequeo."""
    from .store import summarize

    d = disk_info(cfg.data_dir, disk)
    hot: dict[str, Any] = {}
    cold: dict[str, Any] = {}
    runs: list[dict[str, Any]] = []
    vac = None
    if cfg.history_db.is_file():
        conn = _open_ro(cfg.history_db)
        try:
            hot = summarize(conn, cfg.history_db)
            cold = cat.summary(conn)
            runs = cat.last_runs(conn)
            vac = {"auto_vacuum": {0: "none", 1: "full", 2: "incremental"}.get(int(_pragma(conn, "auto_vacuum"))),
                   "freelist_bytes": freelist_bytes(conn)}
        finally:
            conn.close()
    return {
        "disk": {**d, "threshold_pct": cfg.threshold_pct, "target_pct": cfg.target_pct,
                 "over_threshold": d["used_pct"] >= cfg.threshold_pct},
        "hot": hot, "cold": cold, "runs": runs, "last_check": read_status_file(cfg.data_dir),
        "history_db": vac, "cache": cache_stats(cache_dir or cfg.data_dir / "cold_cache"),
        "remote": join_remote(cfg.remote_base, cfg.archive_prefix) if cfg.remote_base else None,
        "keep_days": cfg.keep_days, "protected": list(PROTECTED_NAMES),
    }


def cache_stats(cache_dir: Path) -> dict[str, Any]:
    files = [f for f in cache_dir.glob("*") if f.is_file() and not f.name.endswith(".part")] if cache_dir.is_dir() else []
    return {"dir": str(cache_dir), "files": len(files), "bytes": sum(f.stat().st_size for f in files)}


def write_status_file(data_dir: Path, report: dict[str, Any]) -> None:
    path = data_dir / STATUS_FILE
    tmp = path.with_suffix(".json.tmp")
    try:
        tmp.write_text(json.dumps(report, default=str))
        tmp.replace(path)
    except OSError:
        log.warning("no se pudo escribir %s", path, exc_info=True)


def read_status_file(data_dir: Path) -> dict[str, Any] | None:
    path = data_dir / STATUS_FILE
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return {"error": f"{STATUS_FILE} ilegible"}
    return {k: data.get(k) for k in ("mode", "run_id", "ts_ms", "ts_at", "status", "note", "errors", "archived",
                                     "freed_bytes", "projected_used_pct", "disk_after", "disk_before")}


# ---- CLI -------------------------------------------------------------------------------------------

def _fmt_bytes(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if abs(n) < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} GB"


def _print_check(r: dict[str, Any]) -> None:
    d = r["disk"]
    print(f"disco ({d['path']}): {d['used_pct']} % usado - {_fmt_bytes(d['used_bytes'])} usados, "
          f"{_fmt_bytes(d['free_bytes'])} libres de {_fmt_bytes(d['total_bytes'])}")
    print(f"umbral {r['threshold_pct']} % / objetivo {r['target_pct']} % -> "
          + ("`run` ARCHIVARIA ahora" if r["would_run"] else "`run` no archivaria nada hoy"))
    print(f"espacio libre dentro de las bases (freelist): {_fmt_bytes(r['reclaimable_bytes'])}; "
          f"uso proyectado {r['projected_used_pct']} %")
    print("elegible para archivar (mas viejo que lo que se conserva):")
    if not r["candidates"]:
        print("  nada")
    for stage, s in r["candidates"].items():
        print(f"  {stage:12s} {s['units']:4d} unidades ~{_fmt_bytes(s['est_bytes'])} filas={s['rows']} "
              f"({s['first']} ... {s['last']})")
    if r.get("plan_note"):
        print(r["plan_note"])
    print(f"plan para llegar al objetivo ({len(r['plan'])} unidades, ~{_fmt_bytes(r['plan_freed_est_bytes'])}, "
          f"uso proyectado final {r['plan_projected_used_pct']} %):")
    for u in r["plan"][:40]:
        print(f"  - {u['label']:45s} ~{_fmt_bytes(u['est_bytes'])} {u['rows']}")
    if len(r["plan"]) > 40:
        print(f"  ... y {len(r['plan']) - 40} mas")
    if r.get("vacuum"):
        v = r["vacuum"]
        print(f"base historica: {_fmt_bytes(v['size_bytes'])}, auto_vacuum={v['auto_vacuum']}, "
              f"VACUUM completo necesita {_fmt_bytes(v['full_vacuum_needs_bytes'])} libres "
              f"({'alcanza' if v['full_vacuum_possible'] else 'NO alcanza'})")
    for w in r["warnings"]:
        print("AVISO:", w)
    print("destino frio:", r["remote"] or "(sin remoto)", "| protegidas:", ", ".join(r["protected"]))


@contextlib.contextmanager
def _lock(data_dir: Path) -> Iterator[None]:
    import fcntl

    fh = open(data_dir / LOCK_FILE, "w")  # noqa: SIM115
    try:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ArchiveError("ya hay otro archivado corriendo") from None
        yield
    finally:
        fh.close()


def main(argv: Iterable[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Almacenamiento por niveles: caliente en la VM, frio en Drive.")
    p.add_argument("command", nargs="?", default="check", choices=("check", "run", "status", "vacuum"))
    p.add_argument("--threshold", type=float, default=None, help="umbral de uso de disco en %% (default 80)")
    p.add_argument("--target", type=float, default=None, help="uso objetivo tras archivar en %% (default 70)")
    p.add_argument("--keep-days", type=int, default=None, help="dias de bars/liquidaciones/noticias en la VM (60)")
    p.add_argument("--klines-keep-days", type=int, default=None, help="dias de velas 1m en la VM (= keep-days)")
    p.add_argument("--checkpoint-keep-days", type=int, default=None, help="dias de checkpoints del grafo (30)")
    p.add_argument("--export-keep-days", type=int, default=None, help="dias recientes de data/export a conservar (3)")
    p.add_argument("--vacuum", choices=("auto", "never"), default=None)
    p.add_argument("--dry-run", action="store_true", help="no sube ni borra nada")
    p.add_argument("--json", action="store_true", help="salida JSON")
    p.add_argument("--db", default=None)
    p.add_argument("--checkpoints-db", default=None)
    p.add_argument("--export-dir", default=None)
    p.add_argument("--remote", default=None, help="remoto rclone (default $HEATSIGNAL_DRIVE_REMOTE)")
    a = p.parse_args(list(argv) if argv is not None else None)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        cfg = ArchiveConfig.from_env(
            history_db=Path(a.db) if a.db else None, checkpoints_db=Path(a.checkpoints_db) if a.checkpoints_db else None,
            export_dir=Path(a.export_dir) if a.export_dir else None, remote_base=a.remote,
            threshold_pct=a.threshold, target_pct=a.target, keep_days=a.keep_days,
            klines_keep_days=a.klines_keep_days, checkpoint_keep_days=a.checkpoint_keep_days,
            export_keep_days=a.export_keep_days, vacuum=a.vacuum)
        if a.command == "status":
            print(json.dumps(status(cfg), default=str, indent=None if a.json else 2))
            return 0
        arch = Archiver(cfg)
        if a.command == "check" or (a.command == "run" and a.dry_run):
            r = arch.run(dry_run=True) if a.command == "run" else arch.check()
            print(json.dumps(r, default=str)) if a.json else _print_check(r)
            return 0
        with _lock(cfg.data_dir):
            if a.command == "vacuum":
                out = [arch.full_vacuum(cfg.history_db, dry_run=a.dry_run)]
                if cfg.checkpoints_db is not None and cfg.checkpoints_db.is_file():
                    out.append(arch.full_vacuum(cfg.checkpoints_db, dry_run=a.dry_run))
                print(json.dumps(out, default=str, indent=2))
                return 0
            r = arch.run()
            write_status_file(cfg.data_dir, r)
            print(json.dumps({k: r.get(k) for k in ("status", "note", "errors", "freed_bytes", "projected_used_pct")},
                             default=str))
            return 1 if r["status"] == "error" else 0
    except ArchiveError as exc:
        log.error("%s", exc)
        return 2


if __name__ == "__main__":
    sys.exit(main())
