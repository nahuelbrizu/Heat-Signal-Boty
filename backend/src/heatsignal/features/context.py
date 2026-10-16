"""
Contexto de mercado -> MarketContext (mark, funding, OI, ATR, regimen).

Reglas (todas point-in-time: solo tickers con ts <= now)
--------------------------------------------------------
- mark_price: el mark no-None mas reciente entre todos los exchanges
  (desempate a igual ts: binance, bybit, okx).
- funding_rate_by_exchange: ultimo funding no-None de cada exchange.
- OI en USD de una observacion: open_interest_usd si viene; si solo viene
  open_interest (activo base) se convierte con el mark_price de ESE ticker, o
  su last_price si no hay mark. Si no hay precio, la observacion se ignora.
- open_interest_usd: suma del ultimo OI-USD de cada exchange que lo informa.
  None si ningun exchange lo informa.
- oi_change_pct: por exchange, primer y ultimo OI-USD dentro de la ventana
  (now - window, now]; solo entran exchanges con >= 2 observaciones a distinto ts
  (mismo conjunto de exchanges arriba y abajo: no se mezcla el alta de una fuente
  con un cambio de OI). (sum_fin - sum_ini) / sum_ini * 100. None si no hay datos.
- atr_pct = ATR / mark * 100 (None si falta cualquiera).

Clasificador de regimen (deterministico, `regime-v1`)
-----------------------------------------------------
Sobre las ultimas `lookback_bars` barras cerradas (se requieren lookback_bars + 1
barras y ATR > 0; si no -> "unknown"):
    move_atr = (close[-1] - close[-1 - lookback_bars]) / ATR
    move_atr >=  trend_atr_mult -> "trend_up"
    move_atr <= -trend_atr_mult -> "trend_down"
    resto                       -> "range"
Defaults: lookback_bars = 20, trend_atr_mult = 2.0. Bajo un paseo aleatorio el
desplazamiento tipico en N barras es ~ATR * sqrt(N) (~4.5 ATR para N=20), asi que
2 ATR es un umbral deliberadamente sensible; recalibrar = nuevo REGIME_MODEL_ID.
"""

from __future__ import annotations

from collections.abc import Iterable

from ..schema.events import Exchange, TickerEvent
from ..schema.features import MarketContext, Regime
from ._window import EXCHANGE_ORDER, in_window, select
from .volatility import Bar

REGIME_MODEL_ID = "regime-v1"
DEFAULT_REGIME_LOOKBACK_BARS = 20
DEFAULT_TREND_ATR_MULT = 2.0


def oi_usd(t: TickerEvent) -> float | None:
    if t.open_interest_usd is not None:
        return t.open_interest_usd
    if t.open_interest is not None:
        px = t.mark_price if t.mark_price is not None else t.last_price
        if px is not None:
            return t.open_interest * px
    return None


def classify_regime(
    bars: list[Bar],
    atr: float | None,
    lookback_bars: int = DEFAULT_REGIME_LOOKBACK_BARS,
    trend_atr_mult: float = DEFAULT_TREND_ATR_MULT,
) -> Regime:
    if lookback_bars <= 0:
        raise ValueError("lookback_bars debe ser > 0")
    if atr is None or atr <= 0 or len(bars) < lookback_bars + 1:
        return "unknown"
    move_atr = (bars[-1].close - bars[-1 - lookback_bars].close) / atr
    if move_atr >= trend_atr_mult:
        return "trend_up"
    if move_atr <= -trend_atr_mult:
        return "trend_down"
    return "range"


def latest_mark(tickers: list[TickerEvent]) -> float | None:
    """tickers ya filtrados y ordenados por (ts, exchange): el ultimo mark no-None."""
    for t in reversed(tickers):
        if t.mark_price is not None:
            return t.mark_price
    return None


def build_market_context(
    symbol: str,
    tickers: Iterable[TickerEvent],
    bars: list[Bar],
    atr: float | None,
    now_ms: int,
    window_s: int,
    *,
    regime_lookback_bars: int = DEFAULT_REGIME_LOOKBACK_BARS,
    trend_atr_mult: float = DEFAULT_TREND_ATR_MULT,
) -> MarketContext:
    sel = select(tickers, symbol, now_ms)  # todo lo conocido hasta now
    mark = latest_mark(sel)

    funding: dict[Exchange, float] = {}
    last_oi: dict[Exchange, float] = {}
    oi_series: dict[Exchange, list[tuple[int, float]]] = {}
    for t in sel:  # orden ascendente: el ultimo valor gana
        if t.funding_rate is not None:
            funding[t.source] = t.funding_rate
        v = oi_usd(t)
        if v is not None:
            last_oi[t.source] = v
            if in_window(t.ts_event_ms, now_ms, window_s):
                oi_series.setdefault(t.source, []).append((t.ts_event_ms, v))

    open_interest = sum(last_oi.values()) if last_oi else None

    start_sum = end_sum = 0.0
    used = False
    for ex in EXCHANGE_ORDER:
        series = oi_series.get(ex, [])
        if len(series) >= 2 and series[0][0] != series[-1][0]:
            start_sum += series[0][1]
            end_sum += series[-1][1]
            used = True
    oi_change = (end_sum - start_sum) / start_sum * 100 if used and start_sum > 0 else None

    atr_pct = atr / mark * 100 if (atr is not None and mark is not None and mark > 0) else None

    return MarketContext(
        symbol=symbol,
        ts_ms=now_ms,
        mark_price=mark,
        funding_rate_by_exchange={ex: funding[ex] for ex in EXCHANGE_ORDER if ex in funding},
        open_interest_usd=open_interest,
        oi_change_pct=oi_change,
        atr=atr,
        atr_pct=atr_pct,
        regime=classify_regime(bars, atr, regime_lookback_bars, trend_atr_mult),
    )
