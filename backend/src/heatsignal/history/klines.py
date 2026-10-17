"""
Descarga de velas historicas de Binance USDS-M Futures a `HistoryStore.klines`.

    python -m heatsignal.history.klines --symbols BTCUSDT,SOLUSDT,XRPUSDT --since 2025-10-01
    python -m heatsignal.history.klines --update        # continua desde la ultima vela guardada

Solo se baja 1m: 5m, 15m, 30m y 1h salen EXACTAS de 1m (`resample`), asi hay una sola fuente de verdad
para las simulaciones. Es reanudable: arranca desde la ultima vela guardada de cada simbolo y nunca
duplica (clave primaria por open_time).

Endpoint (verificado contra la API real el 2026-10-17, ver `api/klines.py`):
    GET https://fapi.binance.com/fapi/v1/klines?symbol=&interval=1m&startTime=&limit=1500
    [[openTime, open, high, low, close, volume, closeTime, quoteVolume, trades, takerBuyBase,
      takerBuyQuote, ignore], ...]
Solo se guardan velas CERRADAS (closeTime < ahora): la vela en curso cambia hasta cerrar.
El peso de klines con limit > 1000 es 10 y el limite es 2400/min por IP: con ~0.35 s entre pedidos
quedan ~1700/min, con margen para la ingesta.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
import urllib.parse
from collections.abc import Callable, Iterable, Sequence
from datetime import datetime, timezone

from ..evaluation.price_source import HttpGet, _urllib_get, to_binance_symbol
from .store import HistoryStore

log = logging.getLogger("heatsignal.history.klines")

BASE_URL = "https://fapi.binance.com"
EXCHANGE = "binance"
MINUTE_MS = 60_000
PAGE = 1500
DEFAULT_SLEEP_S = 0.35
DEFAULT_DB = "data/heatsignal_history.sqlite"
RESAMPLE_MINUTES = {"1m": 1, "5m": 5, "15m": 15, "30m": 30, "1h": 60}


class KlineDownloadError(Exception):
    pass


def internal_symbol(symbol: str) -> str:
    s = to_binance_symbol(symbol)
    for quote in ("USDT", "USDC"):
        if s.endswith(quote):
            return f"{s[: -len(quote)]}-{quote}"
    return s


def _page_url(symbol: str, start_ms: int) -> str:
    q = urllib.parse.urlencode({"symbol": to_binance_symbol(symbol), "interval": "1m", "startTime": start_ms,
                                "limit": PAGE})
    return f"{BASE_URL}/fapi/v1/klines?{q}"


def _rows(symbol: str, payload: list, now_ms: int) -> list[tuple]:
    out = []
    for k in payload:
        if int(k[6]) >= now_ms:
            continue  # vela abierta
        out.append((EXCHANGE, internal_symbol(symbol), "1m", int(k[0]), float(k[1]), float(k[2]), float(k[3]),
                    float(k[4]), float(k[5]), float(k[7]), int(k[8]), float(k[10])))
    return out


def download(
    store: HistoryStore,
    symbol: str,
    since_ms: int,
    *,
    http_get: HttpGet | None = None,
    sleep_s: float = DEFAULT_SLEEP_S,
    clock_ms: Callable[[], int] = lambda: int(time.time() * 1000),
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    """Baja 1m desde max(since, ultima guardada + 1m) hasta ahora. Devuelve velas nuevas."""
    get = http_get or _urllib_get
    last = store.last_kline_ms(EXCHANGE, internal_symbol(symbol), "1m")
    start = since_ms if last is None else max(since_ms, last + MINUTE_MS)
    start -= start % MINUTE_MS
    added = 0
    while True:
        now = clock_ms()
        if start >= now - MINUTE_MS:
            break
        status, body = get(_page_url(symbol, start), 15.0)
        if status == 429 or status == 418:
            raise KlineDownloadError(f"Binance limito los pedidos (HTTP {status}); reintentar mas tarde")
        if status != 200:
            raise KlineDownloadError(f"HTTP {status} de Binance: {body[:200]!r}")
        payload = json.loads(body)
        if not isinstance(payload, list):
            raise KlineDownloadError(f"respuesta inesperada: {body[:200]!r}")
        if not payload:
            break
        rows = _rows(symbol, payload, now)
        added += store.add_klines(rows)
        next_start = int(payload[-1][0]) + MINUTE_MS
        if next_start <= start or not rows:
            break
        start = next_start
        if len(payload) < PAGE:
            break
        sleep(sleep_s)
    return added


def resample(candles: Sequence[tuple], minutes: int) -> list[tuple]:
    """(open_time_ms, o, h, l, c, volume, quote_volume, n_trades, taker_buy_quote) de 1m -> N minutos.

    Agrupa por bloque alineado a UTC (open_time // N). Un bloque incompleto (faltan velas de 1m) se
    descarta: una vela de 1h armada con 50 minutos no es la vela de 1h real.
    """
    if minutes == 1:
        return list(candles)
    size = minutes * MINUTE_MS
    out: list[tuple] = []
    group: list[tuple] = []
    for c in candles:
        if group and c[0] // size != group[0][0] // size:
            if len(group) == minutes:
                out.append(_merge(group, size))
            group = []
        group.append(c)
    if len(group) == minutes:
        out.append(_merge(group, size))
    return out


def _merge(group: list[tuple], size: int) -> tuple:
    return (
        group[0][0] - group[0][0] % size,
        group[0][1],
        max(c[2] for c in group),
        min(c[3] for c in group),
        group[-1][4],
        sum(c[5] for c in group),
        sum(c[6] for c in group),
        sum(c[7] for c in group),
        sum(c[8] for c in group),
    )


def load(store: HistoryStore, symbol: str, interval: str, start_ms: int, end_ms: int) -> list[tuple]:
    """Velas de `interval` (1m..1h) entre [start, end), derivadas de 1m."""
    if interval not in RESAMPLE_MINUTES:
        raise ValueError(f"interval debe ser uno de {list(RESAMPLE_MINUTES)}")
    rows = store.query(
        "SELECT open_time_ms, open, high, low, close, volume, quote_volume, n_trades, taker_buy_quote "
        "FROM klines WHERE exchange=? AND symbol=? AND interval='1m' AND open_time_ms >= ? AND open_time_ms < ? "
        "ORDER BY open_time_ms", (EXCHANGE, internal_symbol(symbol), start_ms, end_ms))
    return resample(rows, RESAMPLE_MINUTES[interval])


def _parse_date(s: str) -> int:
    return int(datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp() * 1000)


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--symbols", default=os.environ.get("HEATSIGNAL_SYMBOLS", "BTCUSDT,SOLUSDT,XRPUSDT"))
    parser.add_argument("--since", default=None, help="YYYY-MM-DD (UTC). Por defecto: 365 dias atras")
    parser.add_argument("--update", action="store_true", help="solo continuar desde lo guardado")
    parser.add_argument("--db", default=os.environ.get("HEATSIGNAL_HISTORY_DB", DEFAULT_DB))
    args = parser.parse_args(list(argv) if argv is not None else None)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    since = _parse_date(args.since) if args.since else int(time.time() * 1000) - 365 * 86_400_000
    store = HistoryStore(args.db)
    rc = 0
    try:
        for sym in [s.strip() for s in args.symbols.split(",") if s.strip()]:
            if args.update and store.last_kline_ms(EXCHANGE, internal_symbol(sym), "1m") is None:
                log.info("%s: sin velas guardadas y --update: se omite (correr con --since)", sym)
                continue
            try:
                added = download(store, sym, since)
                log.info("%s: %d velas 1m nuevas", sym, added)
            except Exception as exc:  # noqa: BLE001 - un simbolo no frena a los demas
                log.error("%s: fallo la descarga: %s", sym, exc)
                rc = 1
    finally:
        store.close()
    return rc


if __name__ == "__main__":
    sys.exit(main())
