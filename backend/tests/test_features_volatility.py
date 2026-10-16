"""Tests de barras OHLC y ATR de Wilder con valores calculados a mano."""

from __future__ import annotations

import pytest

from features_helpers import ticker, trade
from heatsignal.features.volatility import (
    Bar,
    bars_from_marks,
    bars_from_trades,
    count_missing_bars,
    true_ranges,
    wilder_atr,
)
from heatsignal.schema.events import Exchange

M = 60_000


def _bar(i: int, o: float, h: float, low: float, c: float) -> Bar:
    return Bar(start_ms=i * M, open=o, high=h, low=low, close=c, n=1)


# TR a mano: b1 max(2,2,0)=2 ; b2 max(1,.5,.5)=1 ; b3 max(3,3,0)=3 ; b4 max(5,0,5)=5 ; b5 max(1,1,0)=1
SERIES = [
    _bar(0, 10, 11, 9, 10),
    _bar(1, 10, 12, 10, 11),
    _bar(2, 11, 11.5, 10.5, 11),
    _bar(3, 11, 14, 11, 13),
    _bar(4, 13, 13, 8, 9),
    _bar(5, 9, 10, 9, 9.5),
]


def test_true_ranges_hand_computed() -> None:
    assert true_ranges(SERIES) == [2, 1, 3, 5, 1]


def test_true_range_uses_previous_close_gap() -> None:
    bars = [_bar(0, 10, 10, 10, 10), _bar(1, 19, 20, 19, 20)]
    assert true_ranges(bars) == [10]  # |H - Cprev| = 10 domina sobre H-L = 1


def test_wilder_atr_seed_is_simple_mean() -> None:
    # period 3, 4 barras -> 3 TR (2,1,3) -> media = 2
    assert wilder_atr(SERIES[:4], period=3) == pytest.approx(2.0)


def test_wilder_atr_recursion() -> None:
    # (2*2 + 5)/3 = 3 ; (3*2 + 1)/3 = 7/3
    assert wilder_atr(SERIES[:5], period=3) == pytest.approx(3.0)
    assert wilder_atr(SERIES, period=3) == pytest.approx(7 / 3)


def test_wilder_atr_insufficient_data_returns_none() -> None:
    assert wilder_atr(SERIES[:3], period=3) is None  # 3 barras -> 2 TR < 3
    assert wilder_atr([], period=3) is None
    assert wilder_atr(SERIES[:1], period=1) is None


def test_wilder_atr_rejects_bad_period() -> None:
    with pytest.raises(ValueError):
        wilder_atr(SERIES, period=0)


def test_bars_from_trades_ohlc_and_bucketing() -> None:
    trades = [
        trade(101, 1, ts=30_000),
        trade(100, 1, ts=0),
        trade(103, 1, ts=59_999),
        trade(99, 1, ts=45_000),
        trade(105, 1, ts=60_000),   # siguiente bucket
        trade(200, 1, ts=61_000, block=True),  # block: excluido
    ]
    bars = bars_from_trades(trades, M)
    assert bars[0] == Bar(start_ms=0, open=100, high=103, low=99, close=103, n=4)
    assert bars[1] == Bar(start_ms=M, open=105, high=105, low=105, close=105, n=1)
    with_block = bars_from_trades(trades, M, include_block_trades=True)
    assert with_block[1].high == 200


def test_bars_from_trades_excludes_open_bar_with_end_ms() -> None:
    trades = [trade(100, 1, ts=0), trade(101, 1, ts=M + 1)]
    assert [b.start_ms for b in bars_from_trades(trades, M, end_ms=2 * M - 1)] == [0]
    assert [b.start_ms for b in bars_from_trades(trades, M, end_ms=2 * M)] == [0, M]


def test_bars_do_not_fill_gaps_and_holes_are_counted() -> None:
    trades = [trade(100, 1, ts=0), trade(101, 1, ts=3 * M)]
    bars = bars_from_trades(trades, M)
    assert len(bars) == 2
    assert count_missing_bars(bars, M) == 2
    assert count_missing_bars(bars[:1], M) == 0


def test_bars_same_ts_tiebreak_is_deterministic() -> None:
    a = trade(100, 1, ts=0, source=Exchange.OKX)
    b = trade(101, 1, ts=0, source=Exchange.BINANCE)
    # binance antes que okx a igual ts -> open 101, close 100 sin importar el orden de entrada
    assert bars_from_trades([a, b], M) == bars_from_trades([b, a], M)
    assert bars_from_trades([a, b], M)[0].open == 101


def test_bars_from_marks_ignores_none() -> None:
    tks = [ticker(Exchange.BYBIT, 0, mark=100), ticker(Exchange.BYBIT, 10, mark=None, funding=0.1),
           ticker(Exchange.BYBIT, 20, mark=98)]
    assert bars_from_marks(tks, M) == [Bar(start_ms=0, open=100, high=100, low=98, close=98, n=2)]
