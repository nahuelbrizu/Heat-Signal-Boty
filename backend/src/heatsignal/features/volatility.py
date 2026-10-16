"""
Barras OHLC y ATR (Wilder) a partir de eventos normalizados.

Barras
------
- Bucket fijo de `bar_ms` anclado a epoch: start = floor(ts / bar_ms) * bar_ms.
- open = primer precio del bucket, close = ultimo, high/low = max/min (orden por
  (ts_event_ms, exchange) — deterministico).
- Buckets sin eventos NO se rellenan (no se fabrica precio). Los huecos quedan
  visibles: `count_missing_bars` los cuenta para que el caller los declare.
- `end_ms`: si se pasa, solo se devuelven barras CERRADAS (start + bar_ms <= end_ms).
  La barra en curso queda afuera para no usar informacion incompleta.
- Por defecto los block trades se excluyen de las barras: se negocian fuera del
  libro y su precio no es representativo del rango de mercado.

ATR (Wilder)
------------
- TR_i = max(H_i - L_i, |H_i - C_{i-1}|, |L_i - C_{i-1}|), definido para i >= 1
  (necesita el cierre previo; convencion TA-Lib — la primera barra no aporta TR).
- Semilla: ATR_p = media simple de los primeros `period` TR.
- Recursion: ATR_t = (ATR_{t-1} * (period - 1) + TR_t) / period.
- Con menos de period + 1 barras devuelve None. Nunca se inventa un valor.
- Si hay huecos de barras, el TR que cruza el hueco usa el cierre previo
  disponible (puede inflar el TR); el caller debe declararlo en data_gaps.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from ..schema.events import TickerEvent, TradeEvent
from ._window import exchange_rank

DEFAULT_ATR_PERIOD = 14


@dataclass(frozen=True)
class Bar:
    start_ms: int
    open: float
    high: float
    low: float
    close: float
    n: int  # cantidad de observaciones de precio en la barra


def _bars_from_points(points: list[tuple[int, int, float]], bar_ms: int, end_ms: int | None) -> list[Bar]:
    """points = (ts_ms, desempate, precio). Construye barras ordenadas por start."""
    if bar_ms <= 0:
        raise ValueError("bar_ms debe ser > 0")
    points = sorted(points, key=lambda p: (p[0], p[1]))
    buckets: dict[int, list[float]] = {}
    for ts, _, price in points:
        start = (ts // bar_ms) * bar_ms
        buckets.setdefault(start, []).append(price)
    bars = []
    for start in sorted(buckets):
        if end_ms is not None and start + bar_ms > end_ms:
            continue
        px = buckets[start]
        bars.append(Bar(start_ms=start, open=px[0], high=max(px), low=min(px), close=px[-1], n=len(px)))
    return bars


def bars_from_trades(
    trades: Iterable[TradeEvent],
    bar_ms: int,
    end_ms: int | None = None,
    include_block_trades: bool = False,
) -> list[Bar]:
    """Barras OHLC desde trades (precio del trade). No filtra por simbolo/exchange: el caller elige."""
    pts = [
        (t.ts_event_ms, exchange_rank(t.source), t.price)
        for t in trades
        if (include_block_trades or not t.is_block_trade) and t.price > 0
    ]
    return _bars_from_points(pts, bar_ms, end_ms)


def bars_from_marks(tickers: Iterable[TickerEvent], bar_ms: int, end_ms: int | None = None) -> list[Bar]:
    """Barras OHLC desde mark_price de tickers (ignora tickers sin mark)."""
    pts = [
        (t.ts_event_ms, exchange_rank(t.source), t.mark_price)
        for t in tickers
        if t.mark_price is not None and t.mark_price > 0
    ]
    return _bars_from_points(pts, bar_ms, end_ms)  # type: ignore[arg-type]


def count_missing_bars(bars: list[Bar], bar_ms: int) -> int:
    """Cantidad de buckets vacios entre la primera y la ultima barra."""
    if len(bars) < 2:
        return 0
    span = (bars[-1].start_ms - bars[0].start_ms) // bar_ms + 1
    return int(span - len(bars))


def true_ranges(bars: list[Bar]) -> list[float]:
    """TR para i >= 1 (len = len(bars) - 1)."""
    out = []
    for prev, cur in zip(bars, bars[1:]):
        out.append(max(cur.high - cur.low, abs(cur.high - prev.close), abs(cur.low - prev.close)))
    return out


def wilder_atr(bars: list[Bar], period: int = DEFAULT_ATR_PERIOD) -> float | None:
    """ATR de Wilder sobre la serie completa; None si hay menos de period + 1 barras."""
    if period <= 0:
        raise ValueError("period debe ser > 0")
    trs = true_ranges(bars)
    if len(trs) < period:
        return None
    atr = sum(trs[:period]) / period
    for tr in trs[period:]:
        atr = (atr * (period - 1) + tr) / period
    return atr
