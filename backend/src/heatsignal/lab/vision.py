"""
Descarga de historia de Binance USDS-M desde data.binance.vision (Binance Public Data) a parquet.

    python -m heatsignal.lab.vision --symbols BTCUSDT,XRPUSDT --datasets klines,premium,metrics,funding
    python -m heatsignal.lab.vision                       # los 7 simbolos, los 4 datasets, toda la historia

Datasets (verificados el 2026-10-17 contra el bucket publico; ver research/binance-datos.md):
    klines    monthly/klines/{S}/1m/{S}-1m-{YYYY-MM}.zip     (+ daily/... para el mes en curso)
              open_time,open,high,low,close,volume,close_time,quote_volume,count,
              taker_buy_volume,taker_buy_quote_volume,ignore
    premium   monthly/premiumIndexKlines/{S}/1m/...        mismo formato; se guarda open_time + close
    metrics   daily/metrics/{S}/{S}-metrics-{YYYY-MM-DD}.zip  (cada 5m; desordenado: se ordena)
              create_time,symbol,sum_open_interest,sum_open_interest_value,
              count_toptrader_long_short_ratio,sum_toptrader_long_short_ratio,
              count_long_short_ratio,sum_taker_long_short_vol_ratio
    funding   monthly/fundingRate/{S}/{S}-fundingRate-{YYYY-MM}.zip
              calc_time,funding_interval_hours,last_funding_rate

Los CSV viejos no traen encabezado y los nuevos si: se detecta. Cada zip se verifica contra su
.CHECKSUM (sha256) antes de convertir. Un archivo que todavia no se publico (404) se saltea sin error
(se reintenta en la proxima corrida); un checksum que no coincide es error.

Salida: {LAB_DIR}/{dataset}/{SYMBOL}/{YYYY-MM}.parquet (un archivo por mes; metrics junta los dias del
mes). Un mes CERRADO ya convertido no se vuelve a bajar; el mes en curso se rehace en cada corrida.
Timestamps en ms UTC tal como los da Binance (open_time = APERTURA de la vela): el reloj de
disponibilidad lo aplica `lab/clock.py`, no este modulo.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import logging
import os
import sys
import time
import urllib.error
import urllib.request
import zipfile
from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

log = logging.getLogger("heatsignal.lab.vision")

BASE = "https://data.binance.vision/data/futures/um"
DEFAULT_SYMBOLS = ("BTCUSDT", "SOLUSDT", "XRPUSDT", "ETHUSDT", "XLMUSDT", "BNBUSDT", "TRXUSDT")
DATASETS = ("klines", "premium", "metrics", "funding")
# Primer mes con datos por simbolo (inicio del perpetuo; research/binance-datos.md).
LISTED = {"BTCUSDT": "2019-09", "ETHUSDT": "2019-11", "XRPUSDT": "2020-01", "TRXUSDT": "2020-01",
          "XLMUSDT": "2020-01", "BNBUSDT": "2020-02", "SOLUSDT": "2020-09"}
METRICS_FROM = "2020-09"  # metrics no existe antes (y para algunos simbolos arranca mas tarde)

KLINE_COLS = ["open_time", "open", "high", "low", "close", "volume", "close_time", "quote_volume", "count",
              "taker_buy_volume", "taker_buy_quote_volume", "ignore"]
METRICS_COLS = ["create_time", "symbol", "sum_open_interest", "sum_open_interest_value",
                "count_toptrader_long_short_ratio", "sum_toptrader_long_short_ratio",
                "count_long_short_ratio", "sum_taker_long_short_vol_ratio"]
FUNDING_COLS = ["calc_time", "funding_interval_hours", "last_funding_rate"]


def lab_dir() -> Path:
    return Path(os.environ.get("HEATSIGNAL_LAB_DIR", Path.home() / "heatsignal-lab" / "data"))


class VisionError(Exception):
    pass


# ---- descarga -------------------------------------------------------------------------------------


def _get(url: str, timeout: float = 60.0, retries: int = 3) -> bytes | None:
    """Bytes del recurso, None si es 404 (todavia no publicado). Reintenta errores transitorios."""
    req = urllib.request.Request(url, headers={"User-Agent": "heatsignal-lab/0.1"})
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read()
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return None
            if attempt == retries - 1:
                raise VisionError(f"HTTP {exc.code} en {url}") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            if attempt == retries - 1:
                raise VisionError(f"red: {exc} en {url}") from exc
        time.sleep(2 ** attempt)
    return None


def fetch_csv(url: str) -> list[list[str]] | None:
    """Baja zip + .CHECKSUM, verifica sha256 y devuelve las filas del CSV (sin encabezado)."""
    blob = _get(url)
    if blob is None:
        return None
    check = _get(url + ".CHECKSUM")
    if check is None:
        raise VisionError(f"falta {url}.CHECKSUM")
    expected = check.decode().split()[0].strip().lower()
    if hashlib.sha256(blob).hexdigest() != expected:
        raise VisionError(f"checksum no coincide: {url}")
    with zipfile.ZipFile(io.BytesIO(blob)) as zf:
        text = zf.read(zf.namelist()[0]).decode()
    rows = list(csv.reader(io.StringIO(text)))
    if rows and rows[0] and not rows[0][0][:1].isdigit():
        rows = rows[1:]  # encabezado (archivos nuevos)
    return [r for r in rows if r]


# ---- conversion -----------------------------------------------------------------------------------


def klines_frame(rows: list[list[str]]) -> pd.DataFrame:
    df = pd.DataFrame(rows, columns=KLINE_COLS).drop(columns=["ignore"])
    for c in ("open_time", "close_time", "count"):
        df[c] = df[c].astype("int64")
    for c in ("open", "high", "low", "close", "volume", "quote_volume", "taker_buy_volume", "taker_buy_quote_volume"):
        df[c] = df[c].astype("float64")
    return df.drop_duplicates("open_time").sort_values("open_time", ignore_index=True)


def premium_frame(rows: list[list[str]]) -> pd.DataFrame:
    df = pd.DataFrame(rows, columns=KLINE_COLS)[["open_time", "close"]]
    df["open_time"] = df["open_time"].astype("int64")
    df["close"] = df["close"].astype("float64")
    return df.rename(columns={"close": "premium"}).drop_duplicates("open_time").sort_values("open_time", ignore_index=True)


def metrics_frame(rows: list[list[str]]) -> pd.DataFrame:
    df = pd.DataFrame(rows, columns=METRICS_COLS).drop(columns=["symbol"])
    ts = pd.to_datetime(df.pop("create_time"), utc=True, format="%Y-%m-%d %H:%M:%S")
    df.insert(0, "ts", ts.dt.as_unit("ms").astype("int64"))  # ms explicito: pandas 3 infiere la unidad
    for c in METRICS_COLS[2:]:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    return df.drop_duplicates("ts").sort_values("ts", ignore_index=True)


def funding_frame(rows: list[list[str]]) -> pd.DataFrame:
    df = pd.DataFrame(rows, columns=FUNDING_COLS)
    # algunos calc_time traen milisegundos de offset (…005): se redondea al minuto
    df["funding_time"] = (df.pop("calc_time").astype("int64") + 30_000) // 60_000 * 60_000
    df["interval_h"] = df.pop("funding_interval_hours").astype("int64")
    df["rate"] = df.pop("last_funding_rate").astype("float64")
    return df.drop_duplicates("funding_time").sort_values("funding_time", ignore_index=True)


# ---- planificacion por mes -------------------------------------------------------------------------


def months(start: str, end: date) -> list[str]:
    y, m = map(int, start.split("-"))
    out = []
    while (y, m) <= (end.year, end.month):
        out.append(f"{y:04d}-{m:02d}")
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


def _days_of(month: str, until: date) -> list[str]:
    y, m = map(int, month.split("-"))
    d = date(y, m, 1)
    out = []
    while d.month == m and d <= until:
        out.append(d.isoformat())
        d += timedelta(days=1)
    return out


@dataclass(frozen=True)
class Job:
    dataset: str
    symbol: str
    month: str
    current: bool  # mes en curso: se arma con archivos diarios y se rehace siempre


def plan(symbols: Iterable[str], datasets: Iterable[str], today: date, root: Path, since: str | None = None) -> list[Job]:
    current = f"{today:%Y-%m}"
    jobs = []
    for ds in datasets:
        for sym in symbols:
            first = max(LISTED.get(sym, "2020-01"), METRICS_FROM if ds == "metrics" else "0000-00", since or "0000-00")
            for mo in months(first, today):
                is_current = mo == current
                if not is_current and (root / ds / sym / f"{mo}.parquet").exists():
                    continue
                if is_current and ds == "funding":
                    continue  # el funding del mes en curso no se publica: va por REST (lab/funding_hist.py)
                jobs.append(Job(ds, sym, mo, is_current))
    return jobs


def run_job(job: Job, root: Path, today: date) -> tuple[Job, int]:
    yesterday = today - timedelta(days=1)
    sym = job.symbol
    frames: list[pd.DataFrame] = []
    if job.dataset == "metrics" or job.current:
        # diarios: metrics siempre; klines/premium del mes en curso (hasta ayer)
        sub = {"klines": f"klines/{sym}/1m/{sym}-1m", "premium": f"premiumIndexKlines/{sym}/1m/{sym}-1m",
               "metrics": f"metrics/{sym}/{sym}-metrics"}[job.dataset]
        for d in _days_of(job.month, yesterday):
            rows = fetch_csv(f"{BASE}/daily/{sub}-{d}.zip")
            if rows:
                frames.append(_convert(job.dataset, rows))
    else:
        sub = {"klines": f"klines/{sym}/1m/{sym}-1m", "premium": f"premiumIndexKlines/{sym}/1m/{sym}-1m",
               "funding": f"fundingRate/{sym}/{sym}-fundingRate"}[job.dataset]
        rows = fetch_csv(f"{BASE}/monthly/{sub}-{job.month}.zip")
        if rows:
            frames.append(_convert(job.dataset, rows))
    if not frames:
        return job, 0
    df = pd.concat(frames, ignore_index=True)
    key = df.columns[0]
    df = df.drop_duplicates(key).sort_values(key, ignore_index=True)
    out = root / job.dataset / sym / f"{job.month}.parquet"
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(".parquet.tmp")
    df.to_parquet(tmp, index=False)
    tmp.replace(out)
    return job, len(df)


def _convert(dataset: str, rows: list[list[str]]) -> pd.DataFrame:
    return {"klines": klines_frame, "premium": premium_frame, "metrics": metrics_frame,
            "funding": funding_frame}[dataset](rows)


def repair_month(dataset: str, symbol: str, month: str, root: Path) -> int:
    """Completa con archivos DIARIOS los dias que faltan en un mes cerrado de klines/premium.

    Binance publico algunos mensuales incompletos (p.ej. 2022-02 y 2022-04 de SOL/XRP/XLM/TRX les faltan
    2-3 dias; verificado 2026-10-17). Devuelve cuantas filas agrego.
    """
    path = root / dataset / symbol / f"{month}.parquet"
    df = pd.read_parquet(path)
    days_have = set(pd.to_datetime(df["open_time"], unit="ms", utc=True).dt.strftime("%Y-%m-%d"))
    y, m = map(int, month.split("-"))
    first_day = date(y, m, 1)
    listed = LISTED.get(symbol, "2020-01")
    sub = {"klines": f"klines/{symbol}/1m/{symbol}-1m", "premium": f"premiumIndexKlines/{symbol}/1m/{symbol}-1m"}[dataset]
    extra = []
    for d in _days_of(month, date(y + (m == 12), m % 12 + 1, 1) - timedelta(days=1)):
        if d in days_have or (month == listed and d < f"{first_day}" ):
            continue
        rows = fetch_csv(f"{BASE}/daily/{sub}-{d}.zip")
        if rows:
            extra.append(_convert(dataset, rows))
    if not extra:
        return 0
    full = pd.concat([df, *extra], ignore_index=True)
    full = full.drop_duplicates(full.columns[0]).sort_values(full.columns[0], ignore_index=True)
    tmp = path.with_suffix(".parquet.tmp")
    full.to_parquet(tmp, index=False)
    tmp.replace(path)
    return len(full) - len(df)


def repair(symbols: Iterable[str], root: Path, today: date) -> int:
    """Revisa todos los meses cerrados de klines/premium y completa los que tienen dias faltantes."""
    added = 0
    current = f"{today:%Y-%m}"
    for ds in ("klines", "premium"):
        for sym in symbols:
            listed = LISTED.get(sym, "2020-01")
            for path in sorted((root / ds / sym).glob("*.parquet")):
                month = path.stem
                if month == current or month == listed:
                    continue  # mes en curso (diarios) y mes de lanzamiento (empieza a mitad de mes)
                n_rows = len(pd.read_parquet(path, columns=[pd.read_parquet(path).columns[0]]))
                y, m = map(int, month.split("-"))
                minutes = (date(y + (m == 12), m % 12 + 1, 1) - date(y, m, 1)).days * 1440
                if n_rows < minutes:
                    n = repair_month(ds, sym, month, root)
                    log.info("reparado %s %s %s: +%d filas (%d de %d minutos)", ds, sym, month, n, n_rows + n, minutes)
                    added += n
    return added


def main(argv: Iterable[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Historia de Binance USDS-M desde data.binance.vision a parquet.")
    p.add_argument("--symbols", default=",".join(DEFAULT_SYMBOLS))
    p.add_argument("--datasets", default=",".join(DATASETS))
    p.add_argument("--since", help="YYYY-MM: no bajar antes de este mes")
    p.add_argument("--workers", type=int, default=8)
    p.add_argument("--dir", default=None, help="por defecto $HEATSIGNAL_LAB_DIR o ~/heatsignal-lab/data")
    p.add_argument("--repair", action="store_true", help="completar meses incompletos desde archivos diarios")
    args = p.parse_args(list(argv) if argv is not None else None)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    root = Path(args.dir) if args.dir else lab_dir()
    today = datetime.now(timezone.utc).date()
    datasets = [d.strip() for d in args.datasets.split(",") if d.strip()]
    if bad := [d for d in datasets if d not in DATASETS]:
        p.error(f"datasets desconocidos: {bad}")
    jobs = plan([s.strip().upper() for s in args.symbols.split(",") if s.strip()], datasets, today, root, args.since)
    log.info("%d meses por bajar en %s", len(jobs), root)
    errors = 0
    done = 0
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = [pool.submit(run_job, j, root, today) for j in jobs]
        for fut in as_completed(futures):
            try:
                job, n = fut.result()
                done += 1
                if done % 50 == 0 or n == 0:
                    log.info("[%d/%d] %s %s %s: %d filas", done, len(jobs), job.dataset, job.symbol, job.month, n)
            except Exception as exc:  # noqa: BLE001 - un mes que falla no frena a los demas
                errors += 1
                log.error("fallo: %s", exc)
    log.info("listo: %d meses, %d errores", done, errors)
    if args.repair:
        log.info("reparacion: +%d filas", repair([s.strip().upper() for s in args.symbols.split(",") if s.strip()], root, today))
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
