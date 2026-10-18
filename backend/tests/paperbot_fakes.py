"""Datos falsos para los tests del bot de paper trading (sin red)."""

from __future__ import annotations

import json
import urllib.parse
from pathlib import Path

import numpy as np

from heatsignal.history import klines as hk
from heatsignal.history.store import HistoryStore
from heatsignal.paperbot.store import PaperbotStore

MIN = 60_000
H = 3_600_000
DAY = 86_400_000
T0 = 1_792_000_000_000 - 1_792_000_000_000 % DAY  # medianoche UTC
SEVEN = ["BTCUSDT", "SOLUSDT", "XRPUSDT", "ETHUSDT", "XLMUSDT", "BNBUSDT", "TRXUSDT"]
BASE = {"BTCUSDT": 100_000.0, "SOLUSDT": 150.0, "XRPUSDT": 2.5, "ETHUSDT": 4000.0, "XLMUSDT": 0.4, "BNBUSDT": 900.0,
        "TRXUSDT": 0.33}


def walk(symbol: str, start_ms: int, end_ms: int, seed: int = 0, vol: float = 0.0008) -> np.ndarray:
    """Velas 1m [start, end): columnas t, o, h, l, c. Random walk determinista por simbolo."""
    n = (end_ms - start_ms) // MIN
    rng = np.random.default_rng(SEVEN.index(symbol) + 11 + 1000 * seed)
    r = rng.normal(0, vol, n)
    close = BASE[symbol] * np.exp(np.cumsum(r))
    open_ = np.r_[BASE[symbol], close[:-1]]
    hi = np.maximum(open_, close) * (1 + np.abs(rng.normal(0, vol / 2, n)))
    lo = np.minimum(open_, close) * (1 - np.abs(rng.normal(0, vol / 2, n)))
    t = start_ms + np.arange(n, dtype=np.int64) * MIN
    return np.column_stack([t, open_, hi, lo, close])


def fill_history(path: Path, symbols, start_ms: int, end_ms: int, *, skip: dict[str, list[int]] | None = None,
                 flat: dict[str, float] | None = None) -> HistoryStore:
    """Base historica con velas 1m de `symbols`. `skip`: minutos a omitir; `flat`: precio constante por simbolo."""
    store = HistoryStore(path)
    for s in symbols:
        if flat and s in flat:
            n = (end_ms - start_ms) // MIN
            t = start_ms + np.arange(n, dtype=np.int64) * MIN
            p = np.full(n, flat[s])
            arr = np.column_stack([t, p, p, p, p])
        else:
            arr = walk(s, start_ms, end_ms)
        omit = set(skip.get(s, [])) if skip else set()
        rows = [(hk.EXCHANGE, hk.internal_symbol(s), "1m", int(r[0]), float(r[1]), float(r[2]), float(r[3]), float(r[4]),
                 1.0, float(r[4]), 1, float(r[4]) / 2) for r in arr if int(r[0]) not in omit]
        store.add_klines(rows)
    return store


def add_kline(store: HistoryStore, symbol: str, t_ms: int, o: float, c: float | None = None) -> None:
    c = o if c is None else c
    store.add_klines([(hk.EXCHANGE, hk.internal_symbol(symbol), "1m", t_ms, o, max(o, c), min(o, c), c, 1.0, c, 1, c / 2)])


def fill_funding(path: Path, symbols, start_ms: int, end_ms: int, rate=lambda s, t: 0.0001, every_h: int = 8) -> None:
    store = PaperbotStore(path)
    rows = []
    t = start_ms - start_ms % (every_h * H)
    while t <= end_ms:
        for s in symbols:
            rows.append((s, t, float(rate(s, t)), None, t))
        t += every_h * H
    store.add_funding_rates(rows)
    store.close()


def fake_binance(hist_arrays: dict[str, np.ndarray], funding: dict[str, list[tuple[int, float]]], calls: list | None = None):
    """http_get falso para /fapi/v1/klines y /fapi/v1/fundingRate (formato de la doc oficial)."""

    def get(url: str, timeout: float):
        if calls is not None:
            calls.append(url)
        q = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))
        sym = q["symbol"]
        start = int(q.get("startTime", 0))
        limit = int(q.get("limit", 500))
        if "/fapi/v1/klines" in url:
            arr = hist_arrays[sym]
            sel = arr[arr[:, 0] >= start][:limit]
            body = [[int(r[0]), str(r[1]), str(r[2]), str(r[3]), str(r[4]), "1.0", int(r[0]) + MIN - 1, str(r[4]), 1,
                     "0.5", str(r[4] / 2), "0"] for r in sel]
            return 200, json.dumps(body).encode()
        if "/fapi/v1/fundingRate" in url:
            rows = [x for x in funding.get(sym, []) if x[0] >= start][:limit]
            body = [{"symbol": sym, "fundingRate": f"{r:.8f}", "fundingTime": t + 3, "markPrice": "1.0",
                     "rateType": "Regular"} for t, r in rows]
            return 200, json.dumps(body).encode()
        return 404, b"{}"

    return get
