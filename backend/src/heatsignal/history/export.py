"""
Exporta la base historica a archivos diarios comprimidos y los sube a Drive (respaldo).

    python -m heatsignal.history.export                  # dias cerrados que falten + subida
    python -m heatsignal.history.export --day 2026-10-17 # un dia puntual (se rehace)
    python -m heatsignal.history.export --no-upload

Estructura (UTC, un dia cerrado por carpeta; un dia ya exportado no se rehace salvo con --day):
    data/export/YYYY/MM/DD/bars_1m.csv.gz
                          liquidations.csv.gz
                          news.csv.gz
                          klines_1m.csv.gz

Los .csv.gz son deterministas (gzip con mtime=0): rehacer un dia sin cambios da el mismo md5, y el archivado
(`history/archive.py`) reutiliza `write_csv_gz` con el mismo formato.

Almacenamiento por niveles: cuando `archive.py` borra la carpeta local de un dia ya subido y verificado, ese dia
queda en `archive_catalog` y no se vuelve a exportar. Un dia cuyas filas ya se archivaron (y borraron de la base)
no se puede rehacer con --day: saldria vacio y pisaria la copia buena de Drive (se rechaza salvo --force).

Subida: `rclone copy data/export <HEATSIGNAL_DRIVE_REMOTE>` si la variable esta definida (p.ej.
"gdrive:" con root_folder_id = la carpeta de Drive del usuario). `copy` nunca borra en el destino. Sin
la variable, o sin rclone, se exporta igual y se avisa (sin fallos silenciosos).
"""

from __future__ import annotations

import argparse
import csv
import gzip
import io
import logging
import os
import shutil
import sqlite3
import subprocess
import sys
import time
from collections.abc import Iterable, Sequence
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from .store import BAR_COLUMNS, KLINE_COLUMNS, HistoryStore

log = logging.getLogger("heatsignal.history.export")

DEFAULT_DB = "data/heatsignal_history.sqlite"
DEFAULT_OUT = "data/export"
DAY_MS = 86_400_000

TABLES: dict[str, tuple[list[str], str]] = {
    "bars_1m": (BAR_COLUMNS, "minute_ms"),
    "liquidations": (["exchange", "symbol", "ts_ms", "side", "price", "qty", "notional_usd", "coverage"], "ts_ms"),
    "news": (["item_id", "source", "is_primary", "title", "summary", "url", "published_ms", "fetched_ms",
              "symbols", "market_wide"], "fetched_ms"),
    "klines_1m": (KLINE_COLUMNS, "open_time_ms"),
}


def _day_start_ms(d: date) -> int:
    return int(datetime(d.year, d.month, d.day, tzinfo=timezone.utc).timestamp() * 1000)


def day_dir(out: Path, d: date) -> Path:
    return out / f"{d:%Y}" / f"{d:%m}" / f"{d:%d}"


def table_sql(name: str) -> tuple[str, str]:
    """(SELECT de las columnas exportadas con rango [?, ?) ordenado por tiempo, condicion WHERE del rango)."""
    cols, ts_col = TABLES[name]
    table = "klines" if name == "klines_1m" else name
    where = f"{ts_col} >= ? AND {ts_col} < ?" + (" AND interval='1m'" if name == "klines_1m" else "")
    return f"SELECT {', '.join(cols)} FROM {table} WHERE {where} ORDER BY {ts_col}", where


def write_csv_gz(rows: Iterable[Sequence], cols: Sequence[str], path: Path) -> int:
    """CSV con encabezado, gzip determinista (mtime=0, sin nombre). Escritura atomica: .tmp + rename.

    `rows` puede ser un cursor: se escribe en streaming (no carga la tabla en memoria). Devuelve filas escritas.
    """
    tmp = path.with_suffix(path.suffix + ".tmp")
    n = 0
    with open(tmp, "wb") as raw, gzip.GzipFile(fileobj=raw, mode="wb", mtime=0, filename="") as gz, \
            io.TextIOWrapper(gz, encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(cols)
        for r in rows:
            w.writerow(r)
            n += 1
    tmp.replace(path)
    return n


def export_day(store: HistoryStore, out: Path, d: date) -> dict[str, int]:
    """Escribe los 4 archivos del dia `d` (UTC). Escritura atomica: .tmp + rename."""
    start, end = _day_start_ms(d), _day_start_ms(d) + DAY_MS
    target = day_dir(out, d)
    target.mkdir(parents=True, exist_ok=True)
    counts: dict[str, int] = {}
    for name, (cols, _) in TABLES.items():
        sql, _ = table_sql(name)
        counts[name] = write_csv_gz(store.query(sql, (start, end)), cols, target / f"{name}.csv.gz")
    return counts


def archived_days(store: HistoryStore, kind: str, table_name: str) -> set[date]:
    """Dias (UTC) que el catalogo de archivado registra para (kind, tabla). Vacio si no hay catalogo."""
    try:
        rows = store.query("SELECT start_ms, end_ms FROM archive_catalog WHERE kind = ? AND table_name = ?",
                           (kind, table_name))
    except sqlite3.OperationalError:
        return set()
    days: set[date] = set()
    for a, b in rows:
        t = a - a % DAY_MS
        while t < b:
            days.add(datetime.fromtimestamp(t / 1000, tz=timezone.utc).date())
            t += DAY_MS
    return days


def first_data_day(store: HistoryStore) -> date | None:
    row = store.query("SELECT MIN(m) FROM (SELECT MIN(minute_ms) AS m FROM bars_1m "
                      "UNION ALL SELECT MIN(fetched_ms) FROM news)")[0]
    if row[0] is None:
        return None
    return datetime.fromtimestamp(row[0] / 1000, tz=timezone.utc).date()


def pending_days(store: HistoryStore, out: Path, today: date) -> list[date]:
    """Dias cerrados (< hoy UTC) con datos propios que todavia no tienen carpeta exportada."""
    first = first_data_day(store)
    if first is None:
        return []
    pruned = archived_days(store, "export_local", "export")  # subidos, verificados y borrados en la VM
    days = []
    d = first
    while d < today:
        if not (day_dir(out, d) / "bars_1m.csv.gz").exists() and d not in pruned:
            days.append(d)
        d += timedelta(days=1)
    return days


def upload(out: Path, remote: str | None) -> bool:
    if not remote:
        log.warning("HEATSIGNAL_DRIVE_REMOTE sin definir: exportado solo en la VM (%s), sin respaldo en Drive", out)
        return False
    if shutil.which("rclone") is None:
        log.error("rclone no esta instalado: no se puede subir a %s", remote)
        return False
    cmd = ["rclone", "copy", str(out), remote, "--exclude", "*.tmp", "--transfers", "2"]
    res = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
    if res.returncode != 0:
        log.error("rclone fallo (rc=%d): %s", res.returncode, res.stderr.strip()[-500:])
        return False
    log.info("subido a %s", remote)
    _mark_upload(out, remote)
    return True


def _mark_upload(out: Path, remote: str) -> None:
    """Deja constancia del ultimo respaldo OK (lo lee el panel, /api/system). Fuera de `out`: no se sube."""
    import json

    try:
        (out.parent / "last_upload.json").write_text(json.dumps(
            {"ts_ms": int(time.time() * 1000), "remote": remote, "dir": str(out)}))
    except OSError:
        log.warning("no se pudo escribir la marca de ultimo respaldo", exc_info=True)


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Exporta la historia a archivos diarios y la sube a Drive.")
    parser.add_argument("--db", default=os.environ.get("HEATSIGNAL_HISTORY_DB", DEFAULT_DB))
    parser.add_argument("--out", default=os.environ.get("HEATSIGNAL_EXPORT_DIR", DEFAULT_OUT))
    parser.add_argument("--day", help="YYYY-MM-DD: exportar (o rehacer) solo ese dia")
    parser.add_argument("--no-upload", action="store_true")
    parser.add_argument("--force", action="store_true",
                        help="rehacer con --day aunque las filas de ese dia ya esten archivadas (saldria vacio)")
    args = parser.parse_args(list(argv) if argv is not None else None)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    out = Path(args.out)
    store = HistoryStore(args.db)
    try:
        today = datetime.fromtimestamp(time.time(), tz=timezone.utc).date()
        days = [datetime.strptime(args.day, "%Y-%m-%d").date()] if args.day else pending_days(store, out, today)
        if args.day and not args.force and days[0] in archived_days(store, "table", "bars_1m"):
            log.error("%s ya esta archivado en Drive y sus filas no estan en la base: el export saldria vacio y "
                      "pisaria la copia buena. Usar --force solo si se sabe lo que se hace.", days[0])
            return 2
        for d in days:
            log.info("%s: %s", d, export_day(store, out, d))
        if not days:
            log.info("nada nuevo para exportar")
    finally:
        store.close()
    ok = True if args.no_upload else upload(out, os.environ.get("HEATSIGNAL_DRIVE_REMOTE"))
    return 0 if ok or not os.environ.get("HEATSIGNAL_DRIVE_REMOTE") else 1


if __name__ == "__main__":
    sys.exit(main())
