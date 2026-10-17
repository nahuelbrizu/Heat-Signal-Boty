"""
Lectura fria: cuando el panel pide un rango que ya no esta en la base local (lo archivo `history/archive.py`),
se buscan en el catalogo los archivos de Drive que lo cubren, se bajan con rclone a un cache local acotado
(`data/cold_cache/`, LRU con tope de tamano, default 300 MB) y se combinan con lo local.

Todo esto es bloqueante (rclone + gzip + sqlite): se llama SIEMPRE desde un hilo (`asyncio.to_thread`), nunca
desde el event loop. Cada archivo bajado se verifica contra el md5 del catalogo antes de usarse.

Sin fallos silenciosos: si un archivo no se puede leer, `ColdRead.errors` lo dice; el panel responde 503 (o
`partial: true` con el motivo si el cliente lo pidio explicitamente).

Como el catalogo y el DELETE de las filas se escriben en la misma transaccion, un rango esta en Drive o en la
base, nunca en los dos: combinar local + frio no duplica filas.
"""

from __future__ import annotations

import csv
import gzip
import logging
import os
import sqlite3
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import catalog as cat
from .export import TABLES
from .remote import RcloneRemote, RemoteError, md5_file
from .store import SCHEMA

log = logging.getLogger("heatsignal.history.cold")

DEFAULT_CACHE_BYTES = 300 * 1024 * 1024
MAX_PARALLEL_DOWNLOADS = 2

# tipos por columna para leer los .csv.gz (csv escribe None como "")
_INT = {"minute_ms", "n_trades", "n_liqs", "ts_ms", "is_primary", "published_ms", "fetched_ms", "market_wide",
        "open_time_ms"}
_TEXT = {"exchange", "symbol", "side", "coverage", "item_id", "source", "title", "summary", "url", "symbols",
         "interval"}


def _converter(col: str) -> Callable[[str], Any]:
    if col in _TEXT:
        return lambda v: v
    if col in _INT:
        return lambda v: int(v) if v != "" else None
    return lambda v: float(v) if v != "" else None


class ColdError(Exception):
    pass


class ColdCache:
    """Cache LRU en disco de archivos frios, por md5 (contenido). Thread-safe."""

    def __init__(self, directory: Path, cap_bytes: int = DEFAULT_CACHE_BYTES) -> None:
        self.dir = Path(directory)
        self.cap_bytes = cap_bytes
        self._lock = threading.Lock()
        self._file_locks: dict[str, threading.Lock] = {}
        self._sem = threading.BoundedSemaphore(MAX_PARALLEL_DOWNLOADS)

    def _path(self, entry: cat.CatalogEntry) -> Path:
        suffix = ".sqlite" if entry.remote_path.endswith(".sqlite") else ".csv.gz"
        return self.dir / f"{entry.md5}{suffix}"

    def get(self, entry: cat.CatalogEntry, remote: RcloneRemote) -> tuple[Path, bool]:
        """(ruta local, vino del cache). Baja de Drive si hace falta y verifica el md5."""
        path = self._path(entry)
        with self._lock:
            flock = self._file_locks.setdefault(entry.md5, threading.Lock())
        with flock:  # dos pedidos del mismo archivo: lo baja uno solo
            if path.is_file():
                os.utime(path)  # LRU: ultimo uso
                return path, True
            self.dir.mkdir(parents=True, exist_ok=True)
            part = path.with_name(path.name + ".part")
            try:
                with self._sem:
                    t0 = time.time()
                    remote.download(entry.remote_path, part)
                got = md5_file(part)
                if got != entry.md5:
                    raise ColdError(f"{entry.remote_path}: md5 bajado {got} != catalogo {entry.md5} (archivo alterado?)")
                part.replace(path)
                log.info("bajado de Drive %s (%d B, %.1f s)", entry.remote_path, path.stat().st_size, time.time() - t0)
            finally:
                part.unlink(missing_ok=True)
        self.evict(keep=path)
        return path, False

    def evict(self, keep: Path | None = None) -> int:
        """Borra los menos usados hasta quedar bajo el tope. Devuelve bytes liberados."""
        with self._lock:
            files = [f for f in self.dir.glob("*") if f.is_file() and not f.name.endswith(".part")]
            total = sum(f.stat().st_size for f in files)
            freed = 0
            for f in sorted(files, key=lambda x: x.stat().st_mtime):
                if total <= self.cap_bytes:
                    break
                if keep is not None and f == keep:
                    continue
                size = f.stat().st_size
                f.unlink(missing_ok=True)
                total -= size
                freed += size
            return freed

    def stats(self) -> dict[str, Any]:
        files = [f for f in self.dir.glob("*") if f.is_file() and not f.name.endswith(".part")] if self.dir.is_dir() else []
        return {"dir": str(self.dir), "files": len(files), "bytes": sum(f.stat().st_size for f in files),
                "cap_bytes": self.cap_bytes}


@dataclass
class ColdRead:
    entries: list[cat.CatalogEntry] = field(default_factory=list)
    cache_hits: int = 0
    downloaded: int = 0
    errors: list[str] = field(default_factory=list)
    seconds: float = 0.0

    def meta(self) -> dict[str, Any]:
        return {"files": len(self.entries), "cache_hits": self.cache_hits, "downloaded": self.downloaded,
                "seconds": round(self.seconds, 2), "errors": self.errors,
                "first_ms": min((e.start_ms for e in self.entries), default=None),
                "end_ms": max((e.end_ms for e in self.entries), default=None)}


def read_csv_rows(path: Path, table: str, keep: Callable[[tuple], bool] | None = None) -> list[tuple]:
    """Filas tipadas de un .csv.gz frio (mismo formato que el export), en el orden de columnas de `TABLES`."""
    cols = TABLES[table][0]
    with gzip.open(path, "rt", encoding="utf-8", newline="") as fh:
        reader = csv.reader(fh)
        header = next(reader, None)
        if header != list(cols):
            raise ColdError(f"{path.name}: encabezado inesperado {header!r}")
        conv = [_converter(c) for c in cols]
        out = []
        for raw in reader:
            row = tuple(f(v) for f, v in zip(conv, raw, strict=True))
            if keep is None or keep(row):
                out.append(row)
    return out


_TABLE_DDL = {
    "bars_1m": SCHEMA[SCHEMA.index("CREATE TABLE IF NOT EXISTS bars_1m"):SCHEMA.index("CREATE INDEX IF NOT EXISTS idx_bars_1m")],
    "liquidations": SCHEMA[SCHEMA.index("CREATE TABLE IF NOT EXISTS liquidations"):SCHEMA.index("CREATE INDEX IF NOT EXISTS idx_liquidations")],
}


def memory_db(table: str, rows: list[tuple]) -> sqlite3.Connection:
    """Base en memoria con la tabla `table` (mismo esquema que la base historica) y `rows` adentro."""
    conn = sqlite3.connect(":memory:")
    conn.execute(_TABLE_DDL[table])
    if rows:
        conn.executemany(f"INSERT OR REPLACE INTO {table} VALUES ({','.join('?' * len(rows[0]))})", rows)
    return conn


class ColdReader:
    """Lee rangos archivados. `remote` None = no hay remoto configurado (todo pedido frio da error)."""

    def __init__(self, history_db: Path, cache: ColdCache, remote: RcloneRemote | None,
                 *, remote_error: str | None = None) -> None:
        self.history_db = Path(history_db)
        self.cache = cache
        self.remote = remote
        self.remote_error = remote_error  # por que no hay remoto (para el 503)

    def entries(self, table: str, since_ms: int | None, until_ms: int | None, *, kind: str = "table") -> list[cat.CatalogEntry]:
        from .store import open_readonly

        if not self.history_db.is_file():
            return []
        conn = open_readonly(self.history_db)
        try:
            return cat.entries(conn, table, since_ms, until_ms, kind=kind)
        finally:
            conn.close()

    def summary(self) -> dict[str, Any]:
        from .store import open_readonly

        if not self.history_db.is_file():
            return {}
        conn = open_readonly(self.history_db)
        try:
            return cat.summary(conn)
        finally:
            conn.close()

    def fetch(self, entries: list[cat.CatalogEntry], *, allow_partial: bool = False) -> tuple[list[tuple[cat.CatalogEntry, Path]], ColdRead]:
        """Baja (o toma del cache) cada archivo. Sin `allow_partial`, el primer error corta con ColdError."""
        res = ColdRead(entries=list(entries))
        out: list[tuple[cat.CatalogEntry, Path]] = []
        t0 = time.time()
        for e in entries:
            try:
                if self.remote is None:
                    raise ColdError(self.remote_error or "no hay remoto de Drive configurado (HEATSIGNAL_DRIVE_REMOTE)")
                path, hit = self.cache.get(e, self.remote)
            except (ColdError, RemoteError, OSError) as exc:
                msg = f"{e.remote_path}: {exc}"
                res.errors.append(msg)
                if not allow_partial:
                    res.seconds = time.time() - t0
                    raise ColdError(msg) from exc
                continue
            res.cache_hits += int(hit)
            res.downloaded += int(not hit)
            out.append((e, path))
        res.seconds = time.time() - t0
        return out, res

    def per_file(self, table: str, entries: list[cat.CatalogEntry], keep: Callable[[tuple], bool],
                 fn: Callable[[sqlite3.Connection], list[tuple]], *, allow_partial: bool = False) -> tuple[list[tuple], ColdRead]:
        """Para cada archivo: filas filtradas -> base en memoria -> `fn(conn)`. Concatena los resultados.

        Pensado para agregaciones que no cruzan el borde de un archivo (barras de hasta 1 h sobre archivos de un
        dia UTC). Memoria acotada a un archivo por vez."""
        files, res = self.fetch(entries, allow_partial=allow_partial)
        out: list[tuple] = []
        for e, path in files:
            try:
                rows = read_csv_rows(path, table, keep)
            except (ColdError, OSError, ValueError, EOFError) as exc:
                self._bad_file(path, e, res, exc, allow_partial)
                continue
            mem = memory_db(table, rows)
            try:
                out += fn(mem)
            finally:
                mem.close()
        return out, res

    def combined(self, table: str, entries: list[cat.CatalogEntry], keep: Callable[[tuple], bool],
                 *, allow_partial: bool = False) -> tuple[sqlite3.Connection, ColdRead]:
        """Todas las filas filtradas de todos los archivos en UNA base en memoria (para tablas chicas)."""
        files, res = self.fetch(entries, allow_partial=allow_partial)
        rows: list[tuple] = []
        for e, path in files:
            try:
                rows += read_csv_rows(path, table, keep)
            except (ColdError, OSError, ValueError, EOFError) as exc:
                self._bad_file(path, e, res, exc, allow_partial)
        return memory_db(table, rows), res

    def checkpoint_file(self, run_ms: int) -> tuple[Path | None, ColdRead]:
        """Archivo frio de checkpoints que cubre el momento `run_ms` (o None si no hay)."""
        found = self.entries("checkpoints", run_ms, run_ms + 1, kind="checkpoints")
        if not found:
            return None, ColdRead()
        files, res = self.fetch(found[-1:])
        return (files[0][1] if files else None), res

    @staticmethod
    def _bad_file(path: Path, e: cat.CatalogEntry, res: ColdRead, exc: BaseException, allow_partial: bool) -> None:
        path.unlink(missing_ok=True)  # que el proximo pedido lo vuelva a bajar
        msg = f"{e.remote_path}: ilegible ({type(exc).__name__}: {exc})"
        res.errors.append(msg)
        if not allow_partial:
            raise ColdError(msg) from exc


def make_reader(history_db: str | Path, *, remote_base: str | None = None, cache_dir: Path | None = None,
                cap_bytes: int = DEFAULT_CACHE_BYTES, remote: RcloneRemote | None = None) -> ColdReader:
    """Lector frio del proceso de ingesta. Sin rclone o sin remoto, el lector existe igual y los pedidos que
    necesiten Drive responden 503 con el motivo."""
    history_db = Path(history_db)
    cache = ColdCache(cache_dir or history_db.parent / "cold_cache", cap_bytes)
    err = None
    if remote is None:
        if not remote_base:
            err = "HEATSIGNAL_DRIVE_REMOTE sin definir: no se puede leer lo archivado en Drive"
        else:
            try:
                remote = RcloneRemote()
            except RemoteError as exc:
                err = str(exc)
    return ColdReader(history_db, cache, remote, remote_error=err)
