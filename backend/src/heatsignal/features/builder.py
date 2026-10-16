"""
Composicion de features -> FeatureSet (lo unico que el grafo de agentes lee por simbolo).

Puro y deterministico: sin I/O, sin reloj (now_ms lo pasa el caller), sin LLM.

Pipeline
--------
1. Barras de precio para ATR/regimen (solo barras CERRADAS, start + bar <= now):
     a. trades no-block del exchange con mas trades del simbolo (desempate binance,
        bybit, okx). Se usa UNA sola fuente para no inflar el rango con la base
        entre exchanges.
     b. si con eso no alcanza para el ATR: mark price de tickers del exchange con
        mas marks. Se declara en data_gaps.
   Las listas de entrada pueden traer historia anterior a la ventana: el ATR la
   aprovecha (necesita atr_period + 1 barras); order flow, heatmap y oi_change
   usan solo la ventana.
2. ATR Wilder (volatility.py) -> heatmap (liqcluster-v1), order flow, contexto.
3. data_gaps: todo hueco o sesgo conocido se declara en texto explicito.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Sequence

from ..schema.events import Coverage, Exchange, LiquidationEvent, TickerEvent, TradeEvent
from ..schema.features import FeatureSet
from ._window import EXCHANGE_ORDER, exchange_rank, select
from .context import (
    DEFAULT_REGIME_LOOKBACK_BARS,
    DEFAULT_TREND_ATR_MULT,
    build_market_context,
    latest_mark,
)
from .heatmap import (
    DEFAULT_BIN_ATR_MULT,
    DEFAULT_DOMINANCE_THRESHOLD,
    DEFAULT_FALLBACK_BIN_PCT,
    DEFAULT_MAX_RADIUS_BINS,
    DEFAULT_TOP_N,
    build_heatmap,
)
from .order_flow import build_order_flow
from .volatility import (
    DEFAULT_ATR_PERIOD,
    Bar,
    bars_from_marks,
    bars_from_trades,
    count_missing_bars,
    wilder_atr,
)

DEFAULT_BAR_S = 60
DEFAULT_STALE_TICKER_S = 120


def _primary(counts: Counter[Exchange]) -> Exchange | None:
    if not counts:
        return None
    return sorted(counts, key=lambda ex: (-counts[ex], exchange_rank(ex)))[0]


def _price_bars(
    trades: list[TradeEvent],
    tickers: list[TickerEvent],
    bar_ms: int,
    now_ms: int,
    atr_period: int,
    gaps: list[str],
) -> tuple[list[Bar], float | None]:
    """Elige la serie de barras (trades -> marks) y calcula el ATR, declarando lo usado."""
    need = atr_period + 1
    tr_counts = Counter(t.source for t in trades if not t.is_block_trade)
    ex = _primary(tr_counts)
    trade_bars: list[Bar] = []
    if ex is not None:
        trade_bars = bars_from_trades([t for t in trades if t.source == ex], bar_ms, end_ms=now_ms)
        atr = wilder_atr(trade_bars, atr_period)
        if atr is not None:
            _declare_holes(trade_bars, bar_ms, f"trades de {ex.value}", gaps)
            return trade_bars, atr

    mark_bars: list[Bar] = []
    mk_counts = Counter(t.source for t in tickers if t.mark_price is not None)
    mex = _primary(mk_counts)
    if mex is not None:
        mark_bars = bars_from_marks([t for t in tickers if t.source == mex], bar_ms, end_ms=now_ms)
        atr = wilder_atr(mark_bars, atr_period)
        if atr is not None:
            gaps.append(
                f"atr: calculado con mark price de {mex.value} (trades insuficientes: "
                f"{len(trade_bars)} barras cerradas < {need})"
            )
            _declare_holes(mark_bars, bar_ms, f"mark de {mex.value}", gaps)
            return mark_bars, atr

    gaps.append(
        f"atr: no disponible (barras cerradas de {bar_ms // 1000}s: trades={len(trade_bars)}, "
        f"mark={len(mark_bars)} < {need} requeridas, periodo {atr_period})"
    )
    return trade_bars, None


def _declare_holes(bars: list[Bar], bar_ms: int, label: str, gaps: list[str]) -> None:
    holes = count_missing_bars(bars, bar_ms)
    if holes:
        gaps.append(f"atr: {holes} barras vacias en la serie ({label}); el TR que cruza el hueco puede inflarse")


def build_feature_set(
    symbol: str,
    liquidations: Iterable[LiquidationEvent],
    trades: Iterable[TradeEvent],
    tickers: Iterable[TickerEvent],
    now_ms: int,
    window_s: int,
    *,
    expected_exchanges: Sequence[Exchange] = EXCHANGE_ORDER,
    bar_s: int = DEFAULT_BAR_S,
    atr_period: int = DEFAULT_ATR_PERIOD,
    stale_ticker_s: int = DEFAULT_STALE_TICKER_S,
    bin_atr_mult: float = DEFAULT_BIN_ATR_MULT,
    fallback_bin_pct: float = DEFAULT_FALLBACK_BIN_PCT,
    max_radius_bins: int = DEFAULT_MAX_RADIUS_BINS,
    dominance_threshold: float = DEFAULT_DOMINANCE_THRESHOLD,
    top_n_clusters: int = DEFAULT_TOP_N,
    regime_lookback_bars: int = DEFAULT_REGIME_LOOKBACK_BARS,
    trend_atr_mult: float = DEFAULT_TREND_ATR_MULT,
) -> FeatureSet:
    liqs_all = select(liquidations, symbol, now_ms)
    trades_all = select(trades, symbol, now_ms)
    tickers_all = select(tickers, symbol, now_ms)
    gaps: list[str] = []

    # 1-2. Barras + ATR + contexto
    bars, atr = _price_bars(trades_all, tickers_all, bar_s * 1000, now_ms, atr_period, gaps)
    context = build_market_context(
        symbol, tickers_all, bars, atr, now_ms, window_s,
        regime_lookback_bars=regime_lookback_bars, trend_atr_mult=trend_atr_mult,
    )
    mark = latest_mark(tickers_all)

    # Heatmap
    hm = build_heatmap(
        symbol, liqs_all, now_ms, window_s, mark, atr,
        bin_atr_mult=bin_atr_mult, fallback_bin_pct=fallback_bin_pct,
        max_radius_bins=max_radius_bins, dominance_threshold=dominance_threshold, top_n=top_n_clusters,
    )

    # Order flow
    of = build_order_flow(symbol, trades_all, now_ms, window_s)

    # 3. data_gaps — liquidaciones
    liqs_win = select(liqs_all, symbol, now_ms, window_s)
    liq_sources = {e.source for e in liqs_win}
    if not liqs_win:
        gaps.append("liquidaciones: 0 eventos en la ventana (sin datos o sin liquidaciones: no distinguible)")
    else:
        for ex in expected_exchanges:
            if ex not in liq_sources:
                gaps.append(f"liquidaciones: sin eventos de {ex.value} en la ventana")
    partial = [ex.value for ex in EXCHANGE_ORDER if ex in {e.source for e in liqs_win if e.coverage == Coverage.PARTIAL}]
    if partial:
        gaps.append(f"liquidaciones {'/'.join(partial)} PARTIAL (<=1/seg/simbolo): subrepresentan el notional real")
    gaps.extend(hm.data_gaps)

    # Trades
    tr_sources = {t.source for t in select(trades_all, symbol, now_ms, window_s)}
    if of.n_trades == 0:
        gaps.append("order_flow: ventana sin trades (no-block); imbalance=0 no implica equilibrio")
    for ex in expected_exchanges:
        if ex not in tr_sources:
            gaps.append(f"trades: sin datos de {ex.value} en la ventana")

    # Tickers
    last_ticker: dict[Exchange, int] = {}
    for t in tickers_all:
        last_ticker[t.source] = t.ts_event_ms
    for ex in expected_exchanges:
        if ex not in last_ticker:
            gaps.append(f"ticker: sin datos de {ex.value}")
        elif now_ms - last_ticker[ex] > stale_ticker_s * 1000:
            age = (now_ms - last_ticker[ex]) / 1000
            gaps.append(f"ticker {ex.value}: stale, ultimo hace {age:.0f}s (> {stale_ticker_s}s)")
    if context.mark_price is None:
        gaps.append("mark_price: no disponible")
    for ex in expected_exchanges:
        if ex in last_ticker and ex not in context.funding_rate_by_exchange:
            gaps.append(f"funding: sin dato de {ex.value}")
    oi_sources = {t.source for t in tickers_all if t.open_interest_usd is not None
                  or (t.open_interest is not None and (t.mark_price is not None or t.last_price is not None))}
    if context.open_interest_usd is None:
        gaps.append("open_interest: ningun exchange lo informa")
    else:
        for ex in expected_exchanges:
            if ex in last_ticker and ex not in oi_sources:
                gaps.append(f"open_interest: sin dato de {ex.value} (la suma es parcial)")
    if context.oi_change_pct is None:
        gaps.append("oi_change_pct: no disponible (< 2 observaciones de OI en la ventana)")
    if context.regime == "unknown":
        gaps.append(f"regime: unknown (requiere ATR y {regime_lookback_bars + 1} barras cerradas)")

    return FeatureSet(
        symbol=symbol,
        ts_ms=now_ms,
        heatmap=hm.snapshot,
        order_flow=of,
        context=context,
        data_gaps=gaps,
    )
