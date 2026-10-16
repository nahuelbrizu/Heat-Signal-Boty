"""Tests de order flow: volumenes por lado del taker, CVD, imbalance y exclusion de block trades."""

from __future__ import annotations

import pytest

from features_helpers import SYM, trade
from heatsignal.features.order_flow import build_order_flow
from heatsignal.schema.events import Exchange, Side

NOW = 30_000  # ventana (-30s, 30s] contiene el ts por defecto (1000) de los helpers
WIN = 60


def _of(trades):
    return build_order_flow(SYM, trades, NOW, WIN)


def test_volumes_cvd_imbalance_mixed_exchanges_with_block_excluded() -> None:
    of = _of([
        trade(100, 2, Side.BUY, Exchange.BINANCE),               # 200 buy
        trade(100, 1, Side.SELL, Exchange.BYBIT),                # 100 sell
        trade(100, 10, Side.BUY, Exchange.BYBIT, block=True),    # 1000 block, excluido
    ])
    assert of.buy_volume_usd == 200
    assert of.sell_volume_usd == 100
    assert of.cvd_usd == 100
    assert of.imbalance == pytest.approx(1 / 3)
    assert of.n_trades == 2
    assert of.block_trade_volume_usd == 1000
    assert of.sources == [Exchange.BINANCE, Exchange.BYBIT]


def test_zero_volume_gives_zero_imbalance() -> None:
    of = _of([])
    assert (of.buy_volume_usd, of.sell_volume_usd, of.cvd_usd, of.imbalance, of.n_trades) == (0, 0, 0, 0.0, 0)
    assert of.sources == []


def test_only_block_trades_is_zero_flow() -> None:
    of = _of([trade(50, 4, Side.SELL, Exchange.BYBIT, block=True)])
    assert of.imbalance == 0.0
    assert of.n_trades == 0
    assert of.block_trade_volume_usd == 200
    assert of.sources == [Exchange.BYBIT]


def test_one_sided_extremes() -> None:
    assert _of([trade(10, 1, Side.BUY)]).imbalance == 1.0
    assert _of([trade(10, 1, Side.SELL), trade(20, 1, Side.SELL, Exchange.OKX)]).imbalance == -1.0


def test_balanced_is_zero() -> None:
    of = _of([trade(10, 3, Side.BUY), trade(30, 1, Side.SELL, Exchange.OKX)])
    assert of.cvd_usd == 0
    assert of.imbalance == 0.0


def test_window_and_symbol_filter() -> None:
    of = _of([
        trade(1, 1, Side.BUY, ts=NOW - WIN * 1000),        # borde: excluido
        trade(1, 2, Side.BUY, ts=NOW - WIN * 1000 + 1),    # incluido
        trade(1, 4, Side.SELL, ts=NOW),                    # incluido
        trade(1, 8, Side.BUY, ts=NOW + 1),                 # futuro
        trade(1, 16, Side.BUY, symbol="BTC-USDT"),         # otro simbolo
    ])
    assert (of.buy_volume_usd, of.sell_volume_usd, of.n_trades) == (2, 4, 2)
    assert of.imbalance == pytest.approx(-1 / 3)
