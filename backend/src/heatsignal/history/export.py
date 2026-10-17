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

Subida: `rclone copy data/export <HEATSIGNAL_DRIVE_REMOTE>` si la variable esta definida (p.ej.
"gdrive:" con root_folder_id = la carpeta de Drive del usuario). `copy` nunca borra en el destino. Sin
la variable, o sin rclone, se exporta igual y se avisa (sin fallos silenciosos).
"""

from __future__ import annotations

import argparse
import csv
import gzip
import logging
import os
import shutil
import subprocess
import sys
import time
from collections.abc import Iterable
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


def export_day(store: HistoryStore, out: Path, d: date) -> dict[str, int]:
    """Escribe los 4 archivos del dia `d` (UTC). Escritura atomica: .tmp + rename."""
    start, end = _day_start_ms(d), _day_start_ms(d) + DAY_MS
    target = day_dir(out, d)
    target.mkdir(parents=True, exist_ok=True)
    counts: dict[str, int] = {}
    for name, (cols, ts_col) in TABLES.items():
        table = "klines" if name == "klines_1m" else name
        extra = " AND interval='1m'" if name == "klines_1m" else ""
        rows = store.query(
            f"SELECT {', '.join(cols)} FROM {table} WHERE {ts_col} >= ? AND {ts_col} < ?{extra} ORDER BY {ts_col}",
            (start, end))
        path = target / f"{name}.csv.gz"
        tmp = path.with_suffix(".gz.tmp")
        with gzip.open(tmp, "wt", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(cols)
            w.writerows(rows)
        tmp.replace(path)
        counts[name] = len(rows)
    return counts


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
    days = []
    d = first
    while d < today:
        if not (day_dir(out, d) / "bars_1m.csv.gz").exists():
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
    args = parser.parse_args(list(argv) if argv is not None else None)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    out = Path(args.out)
    store = HistoryStore(args.db)
    try:
        today = datetime.fromtimestamp(time.time(), tz=timezone.utc).date()
        days = [datetime.strptime(args.day, "%Y-%m-%d").date()] if args.day else pending_days(store, out, today)
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
