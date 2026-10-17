"""
Lectura de la historia del laboratorio (parquet de `lab/vision.py`) como DataFrames con indice de tiempo.

Convencion de indices (la misma que espera `lab/sim.py`):
  - klines / premium: indice = APERTURA de la vela 1m (UTC). El dato se conoce al cierre (+1 min):
    eso lo resuelve `lab/clock.py`, no este modulo.
  - metrics: indice = instante del snapshot (UTC).
  - funding: Series indice = liquidacion (UTC), valor = tasa del intervalo.
"""

from __future__ import annotations

import json
import time
import urllib.parse
from pathlib import Path

import pandas as pd

from ..evaluation.price_source import _urllib_get
from .vision import lab_dir

FUNDING_URL = "https://fapi.binance.com/fapi/v1/fundingRate"


def _ts(value: str | pd.Timestamp | None) -> pd.Timestamp | None:
    if value is None:
        return None
    ts = pd.Timestamp(value)
    return ts.tz_localize("UTC") if ts.tzinfo is None else ts.tz_convert("UTC")


def _load(dataset: str, symbol: str, time_col: str, start=None, end=None, root: Path | None = None) -> pd.DataFrame:
    folder = (root or lab_dir()) / dataset / symbol.upper()
    files = sorted(folder.glob("*.parquet"))
    if not files:
        raise FileNotFoundError(f"sin datos de {dataset} para {symbol} en {folder} (correr lab.vision)")
    s, e = _ts(start), _ts(end)
    if s is not None or e is not None:  # solo los meses que tocan el rango
        lo = f"{s:%Y-%m}" if s is not None else "0000-00"
        hi = f"{e:%Y-%m}" if e is not None else "9999-99"
        files = [f for f in files if lo <= f.stem <= hi]
    df = pd.concat([pd.read_parquet(f) for f in files], ignore_index=True)
    idx = pd.to_datetime(df.pop(time_col), unit="ms", utc=True)
    df.index = idx
    df.index.name = "time"
    df = df[~df.index.duplicated()].sort_index()
    if s is not None:
        df = df[df.index >= s]
    if e is not None:
        df = df[df.index < e]
    return df


def load_klines(symbol: str, start=None, end=None, root: Path | None = None) -> pd.DataFrame:
    """Velas 1m: open, high, low, close, volume, close_time, quote_volume, count, taker_buy_volume,
    taker_buy_quote_volume. Indice = apertura."""
    return _load("klines", symbol, "open_time", start, end, root)


def load_premium(symbol: str, start=None, end=None, root: Path | None = None) -> pd.Series:
    return _load("premium", symbol, "open_time", start, end, root)["premium"]


def load_metrics(symbol: str, start=None, end=None, root: Path | None = None) -> pd.DataFrame:
    """Snapshots cada 5m: OI (contratos y USD), long/short top y global, ratio taker."""
    df = _load("metrics", symbol, "ts", start, end, root)
    return df.rename(columns={
        "sum_open_interest": "oi_contracts",
        "sum_open_interest_value": "oi_usd",
        "count_toptrader_long_short_ratio": "ls_top_accounts",
        "sum_toptrader_long_short_ratio": "ls_top_positions",
        "count_long_short_ratio": "ls_global",
        "sum_taker_long_short_vol_ratio": "taker_ratio",
    })


def load_funding(symbol: str, start=None, end=None, root: Path | None = None) -> pd.Series:
    df = _load("funding", symbol, "funding_time", start, end, root)
    return df["rate"]


# ---- funding del mes en curso (vision solo publica meses cerrados) ----------------------------------


def update_current_funding(symbol: str, root: Path | None = None, http_get=None, now_ms: int | None = None) -> int:
    """Baja por REST el funding desde el inicio del mes en curso y lo guarda como {YYYY-MM}.parquet.

    GET /fapi/v1/fundingRate?symbol&startTime&limit (max 1000; verificado 2026-10-17). Devuelve filas.
    """
    get = http_get or _urllib_get
    now = pd.Timestamp(now_ms, unit="ms", tz="UTC") if now_ms is not None else pd.Timestamp.now(tz="UTC")
    month_start = now.normalize().replace(day=1)
    q = urllib.parse.urlencode({"symbol": symbol.upper(), "startTime": int(month_start.timestamp() * 1000),
                                "limit": 1000})
    status, body = get(f"{FUNDING_URL}?{q}", 15.0)
    if status != 200:
        raise RuntimeError(f"HTTP {status} de Binance fundingRate: {body[:200]!r}")
    rows = json.loads(body)
    df = pd.DataFrame({
        "funding_time": [(int(r["fundingTime"]) + 30_000) // 60_000 * 60_000 for r in rows],
        "interval_h": [8] * len(rows),
        "rate": [float(r["fundingRate"]) for r in rows],
    }).drop_duplicates("funding_time").sort_values("funding_time", ignore_index=True)
    out = (root or lab_dir()) / "funding" / symbol.upper() / f"{month_start:%Y-%m}.parquet"
    out.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(out, index=False)
    time.sleep(0.1)
    return len(df)
