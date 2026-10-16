"""Tests de MarketContext: mark, funding, OI multi-exchange, oi_change y clasificador de regimen."""

from __future__ import annotations

import pytest

from features_helpers import SYM, ticker
from heatsignal.features.context import build_market_context, classify_regime, oi_usd
from heatsignal.features.volatility import Bar
from heatsignal.schema.events import Exchange

NOW = 6_000
WIN = 5  # ventana (1000, 6000]


def _closes(*cs: float) -> list[Bar]:
    return [Bar(start_ms=i, open=c, high=c, low=c, close=c, n=1) for i, c in enumerate(cs)]


TICKERS = [
    ticker(Exchange.BINANCE, 1_000, mark=100, funding=0.0001, oi_usd=1_000_000),  # fuera de ventana
    ticker(Exchange.BYBIT, 2_000, mark=101, funding=0.0002, oi=1_000),            # 101_000 USD
    ticker(Exchange.OKX, 3_000, mark=None, last=99, funding=-0.0001, oi=10),       # 990 USD via last
    ticker(Exchange.BYBIT, 5_000, mark=102, funding=None, oi=1_000),              # 102_000 USD
    ticker(Exchange.BINANCE, 7_000, mark=999, funding=0.9, oi_usd=9e9),           # futuro: ignorado
]


def _ctx(tickers=TICKERS, bars=None, atr=None):
    return build_market_context(SYM, tickers, bars or [], atr, NOW, WIN, regime_lookback_bars=3)


def test_latest_mark_funding_and_oi_sum() -> None:
    ctx = _ctx()
    assert ctx.mark_price == 102
    assert ctx.funding_rate_by_exchange == {
        Exchange.BINANCE: 0.0001, Exchange.BYBIT: 0.0002, Exchange.OKX: -0.0001,
    }
    assert ctx.open_interest_usd == pytest.approx(1_000_000 + 102_000 + 990)


def test_oi_change_only_exchanges_with_two_obs_in_window() -> None:
    # binance: obs fuera de ventana ; okx: 1 obs -> solo bybit: (102000-101000)/101000*100
    assert _ctx().oi_change_pct == pytest.approx(1000 / 101_000 * 100)


def test_oi_change_none_without_enough_obs() -> None:
    assert _ctx(TICKERS[:3]).oi_change_pct is None


def test_oi_usd_conversion_rules() -> None:
    assert oi_usd(ticker(Exchange.OKX, 0, mark=2, last=3, oi=10)) == 20
    assert oi_usd(ticker(Exchange.OKX, 0, last=3, oi=10)) == 30
    assert oi_usd(ticker(Exchange.OKX, 0, oi=10)) is None
    assert oi_usd(ticker(Exchange.OKX, 0, mark=2, oi=10, oi_usd=7)) == 7


def test_atr_pct() -> None:
    ctx = _ctx(atr=2.04)
    assert ctx.atr == 2.04
    assert ctx.atr_pct == pytest.approx(2.0)
    assert _ctx(atr=None).atr_pct is None


def test_empty_tickers() -> None:
    ctx = _ctx([])
    assert ctx.mark_price is None
    assert ctx.funding_rate_by_exchange == {}
    assert ctx.open_interest_usd is None
    assert ctx.oi_change_pct is None
    assert ctx.regime == "unknown"


@pytest.mark.parametrize(
    ("closes", "atr", "expected"),
    [
        ((10, 11, 12, 13), 1.0, "trend_up"),      # move = +3 ATR
        ((13, 12, 11, 10), 1.0, "trend_down"),    # move = -3 ATR
        ((10, 11, 10, 11), 1.0, "range"),         # move = +1 ATR
        ((10, 15, 5, 12), 1.0, "trend_up"),       # exactamente +2 ATR: >= umbral
        ((12, 15, 5, 10), 1.0, "trend_down"),     # exactamente -2 ATR
        ((10, 11, 12, 13), 2.0, "range"),         # +3/2 = 1.5 ATR
        ((20, 10, 11, 12, 13), 1.0, "trend_up"),  # solo cuentan las ultimas lookback barras
        ((10, 11, 12), 1.0, "unknown"),           # < lookback + 1 barras
        ((10, 11, 12, 13), None, "unknown"),      # sin ATR
        ((10, 11, 12, 13), 0.0, "unknown"),
    ],
)
def test_classify_regime(closes, atr, expected) -> None:
    assert classify_regime(_closes(*closes), atr, lookback_bars=3, trend_atr_mult=2.0) == expected


def test_regime_via_context() -> None:
    assert _ctx(bars=_closes(10, 11, 12, 13), atr=1.0).regime == "trend_up"
