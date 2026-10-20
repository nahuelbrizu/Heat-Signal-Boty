"""
Velas DIARIAS para las estrategias diarias del lote 6 en el bot de paper (prueba hacia adelante, 2026-10-20).

Por que: el engine carga 64 dias de velas 1m (lo que necesitan las 7 que ya corrian). Las del lote 6 deciden con velas
diarias y algunas necesitan mucha mas historia (ruptura de 55 d, tsmom de 28 d con vol de 30 d, ATR/precio contra el
percentil de 365 d). Cargar 1m para eso son ~600 dias x 7 simbolos x 1440 filas: no entra en `MemoryMax=400M`. Una
vela diaria es UNA fila: 600 dias x 7 simbolos son 4200 filas.

Fuente de cada dia (en este orden, sin mezclar dentro de un dia):
  1. 1m de la base historica agregadas en SQL (GROUP BY dia UTC), solo dias COMPLETOS (1440 velas): es exactamente lo
     que hace `clock.resample_bars(k, "1D")` en el laboratorio (open de la primera, max/min, close de la ultima).
  2. Si el dia no esta completo en 1m (lo mas viejo se archiva a Drive), la vela diaria de Binance cacheada en
     `paperbot.sqlite` (`daily_klines`), bajada por REST:

         GET https://fapi.binance.com/fapi/v1/klines?symbol=BTCUSDT&interval=1d&startTime=<ms>&limit=1000
         [[openTime, open, high, low, close, volume, closeTime, quoteVolume, trades, takerBuyBase, takerBuyQuote,
           ignore], ...]   (orden ascendente; la vela en curso viene con closeTime >= ahora y NO se guarda)

     Doc oficial: developers.binance.com/docs/derivatives/usds-margined-futures/market-data/rest-api/
     Kline-Candlestick-Data (limit default 500, max 1000 segun el conector oficial `binance-futures-connector-python`
     2026-10; formato verificado contra la API real el 2026-10-20). Solo se pide lo que falta (cache incremental): en
     regimen, como mucho un pedido por simbolo por dia.

Historia minima por spec (`history_days`), en dias diarios CONTIGUOS que terminan en el ultimo dia cerrado:
  - ruptura N/H:  N cierres + CHAIN_MARGIN_D (hold_events es "una posicion por vez": la cadena de entradas depende
                  del pasado; 60 d de margen hacen que el estado en H coincida con el de la historia completa).
  - tsmom L:      L + 1 cierres (y vol_window + 1 si el tamano es por vol) + 5 d.
  - regimen ADX:  max(20, 2 x adx_n) + EWM_WARMUP_D (suavizado exponencial) + CHAIN_MARGIN_D.
  - regimen ATR%: atrp_window + 1 + atr_n + EWM_WARMUP_D + CHAIN_MARGIN_D  (~590 d: la mas larga).
  - familia:      la mayor de sus componentes.
El engine carga el MAXIMO de las estrategias activas y a cada una le exige su minimo: si no alcanza, esa estrategia
queda "sin datos" en esa corrida (lo dice el detalle de la corrida y el panel) y las demas corren igual.

La regla del laboratorio NO cambia: `daily_source` hace que `strategies._daily(md)` devuelva estas velas (mismo formato
que `resample_bars`: indice = CIERRE del dia, columnas open/high/low/close) en vez de re-agregar 1m.
"""

from __future__ import annotations

import json
import time
import urllib.parse
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any

import pandas as pd

from ..evaluation.price_source import HttpGet, _urllib_get
from ..history import klines as hk
from ..history.store import HistoryStore
from .store import PaperbotStore

DAILY_URL = "https://fapi.binance.com/fapi/v1/klines"
PAGE = 1000
DAY_MS = 86_400_000
MIN_PER_DAY = 1440
CHAIN_MARGIN_D = 60
EWM_WARMUP_D = 150


class DailyFetchError(Exception):
    pass


def history_days(spec: Any, rule: Any) -> int | None:
    """Dias diarios contiguos que necesita la estrategia, o None si no es diaria (usa la ventana 1m del engine)."""
    from ..lab import strategies as S

    p = spec.params
    if rule is S.l6_ruptura:
        return p["lookback_days"] + CHAIN_MARGIN_D
    if rule is S.l6_tsmom:
        need = p["lookback_days"] + 1
        if p["sizing"] == "vol":
            need = max(need, p["vol_window_days"] + 1)
        return need + 5
    if rule is S.l6_regimen:
        if p["filter"] == "adx":
            need = max(p["lookback_days"], 2 * p["adx_n"]) + EWM_WARMUP_D
        else:
            need = max(p["lookback_days"], p["atrp_window_days"] + 1 + p["atr_n"] + EWM_WARMUP_D)
        return need + CHAIN_MARGIN_D
    if rule is getattr(S, "l6_familia", None):
        comps = [history_days(*S.CATALOG[c]) for c in p["components"]]
        return None if not comps or any(c is None for c in comps) else max(comps)
    return None


# ---- fuentes -----------------------------------------------------------------------------------------------


def sql_daily(hist: HistoryStore, symbol: str, start_ms: int, end_ms: int) -> dict[int, tuple[float, float, float, float]]:
    """Dias COMPLETOS (1440 velas 1m) con apertura en [start, end), agregados en SQL. {open_ms: (o, h, l, c)}."""
    sym = hk.internal_symbol(symbol)
    rows = hist.query(
        "WITH g AS (SELECT open_time_ms / ? AS d, MIN(open_time_ms) AS t0, MAX(open_time_ms) AS t1, MAX(high) AS hi, "
        "MIN(low) AS lo, COUNT(*) AS n FROM klines WHERE exchange=? AND symbol=? AND interval='1m' AND open_time_ms >= ? "
        "AND open_time_ms < ? GROUP BY d) "
        "SELECT g.d, a.open, g.hi, g.lo, b.close, g.n FROM g "
        "JOIN klines a ON a.exchange=? AND a.symbol=? AND a.interval='1m' AND a.open_time_ms=g.t0 "
        "JOIN klines b ON b.exchange=? AND b.symbol=? AND b.interval='1m' AND b.open_time_ms=g.t1 ORDER BY g.d",
        (DAY_MS, hk.EXCHANGE, sym, start_ms, end_ms, hk.EXCHANGE, sym, hk.EXCHANGE, sym))
    return {int(d) * DAY_MS: (float(o), float(h), float(lo), float(c)) for d, o, h, lo, c, n in rows if n == MIN_PER_DAY}


def update_daily_cache(store: PaperbotStore, symbol: str, since_ms: int, until_ms: int, now_ms: int, *,
                       http_get: HttpGet | None = None, sleep: Callable[[float], None] = time.sleep) -> int:
    """Baja de Binance las velas diarias CERRADAS con apertura en [since, until) que no estan en la cache.
    Respuesta inesperada -> DailyFetchError (la estrategia queda sin datos; nunca se inventa una vela)."""
    get = http_get or _urllib_get
    have = {r[0] for r in store.daily_klines(symbol, since_ms, until_ms)}
    missing = [t for t in range(since_ms, until_ms, DAY_MS) if t not in have]
    if not missing:
        return 0
    start = missing[0]
    added = 0
    for _ in range(20):  # 20 paginas de 1000 dias: de sobra
        q = urllib.parse.urlencode({"symbol": symbol, "interval": "1d", "startTime": start, "limit": PAGE})
        status, body = get(f"{DAILY_URL}?{q}", 15.0)
        if status != 200:
            raise DailyFetchError(f"{symbol}: HTTP {status} de klines 1d: {body[:200]!r}")
        try:
            payload = json.loads(body)
        except ValueError as exc:
            raise DailyFetchError(f"{symbol}: respuesta no JSON de klines 1d") from exc
        if not isinstance(payload, list):
            raise DailyFetchError(f"{symbol}: respuesta inesperada de klines 1d: {body[:200]!r}")
        rows = []
        for k in payload:
            try:
                t, close_t = int(k[0]), int(k[6])
                vals = tuple(float(k[i]) for i in (1, 2, 3, 4))
            except (IndexError, TypeError, ValueError) as exc:
                raise DailyFetchError(f"{symbol}: vela diaria ilegible: {k!r}") from exc
            if t % DAY_MS:
                raise DailyFetchError(f"{symbol}: vela diaria que no abre a las 00:00 UTC: {t}")
            if close_t >= now_ms or t >= until_ms:
                continue  # vela en curso (o fuera del rango pedido)
            rows.append((symbol, t, *vals, now_ms))
        added += store.add_daily_klines(rows)
        if len(payload) < PAGE or not payload:
            return added
        nxt = int(payload[-1][0]) + DAY_MS
        if nxt <= start or nxt >= until_ms:
            return added
        start = nxt
        sleep(0.2)
    raise DailyFetchError(f"{symbol}: demasiadas paginas de klines 1d")


def contiguous_tail(days: dict[int, tuple], last_open_ms: int) -> list[int]:
    """Aperturas de la racha CONTIGUA de dias que termina en `last_open_ms` (vacia si ese dia falta). Las reglas usan
    ventanas por cantidad de filas: un dia faltante en el medio correria todo, asi que se corta ahi."""
    out = []
    t = last_open_ms
    while t in days:
        out.append(t)
        t -= DAY_MS
    return out[::-1]


def build_daily(hist: HistoryStore, store: PaperbotStore, symbol: str, H: int, days: int, *,
                fetch: Callable[[int, int], int] | None = None) -> tuple[pd.DataFrame, dict[str, Any]]:
    """Velas diarias cerradas en H (cierre <= H) de los ultimos `days` dias: 1m agregadas si el dia esta completo,
    si no la cache REST (y `fetch(since, until)` para completarla). Devuelve (barras de la racha contigua final, info)."""
    last_open = H - H % DAY_MS - DAY_MS
    start = last_open - (days - 1) * DAY_MS
    end = last_open + DAY_MS
    merged: dict[int, tuple] = dict(sql_daily(hist, symbol, start, end))
    n_sql = len(merged)
    info: dict[str, Any] = {"dias_pedidos": days, "dias_1m": n_sql}
    need_rest = [t for t in range(start, end, DAY_MS) if t not in merged]
    if need_rest and fetch is not None:
        try:
            info["rest_nuevas"] = fetch(need_rest[0], need_rest[-1] + DAY_MS)
        except Exception as exc:  # noqa: BLE001 - se informa; la estrategia queda sin datos si le falta historia
            info["rest_error"] = f"{type(exc).__name__}: {exc}"
    if need_rest:
        for t, o, h, lo, c in store.daily_klines(symbol, start, end):
            merged.setdefault(t, (o, h, lo, c))
    info["dias_rest"] = len(merged) - n_sql
    tail = contiguous_tail(merged, last_open)
    info["dias_contiguos"] = len(tail)
    info["primer_dia_ms"] = tail[0] if tail else None
    idx = pd.DatetimeIndex(pd.to_datetime([t + DAY_MS for t in tail], unit="ms", utc=True), name="time")
    bars = pd.DataFrame([merged[t] for t in tail], index=idx, columns=["open", "high", "low", "close"], dtype=float)
    return bars, info


@contextmanager
def daily_source(frames: dict[int, pd.DataFrame]) -> Iterator[None]:
    """Mientras dura, `strategies._daily(md)` devuelve `frames[id(md)]` (velas diarias ya armadas) para esos
    MarketData; para cualquier otro sigue agregando 1m como siempre. La regla y su resultado no cambian."""
    from ..lab import strategies as S

    orig = S._daily

    def _daily(md):  # noqa: ANN001
        f = frames.get(id(md))
        return f if f is not None else orig(md)

    S._daily = _daily
    try:
        yield
    finally:
        S._daily = orig
