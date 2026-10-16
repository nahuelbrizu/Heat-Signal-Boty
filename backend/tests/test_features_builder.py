"""Tests de build_feature_set: composicion, eleccion de fuente de precio y data_gaps explicitos."""

from __future__ import annotations

import random

import pytest

from features_helpers import SYM, liq, ticker, trade
from heatsignal.features.builder import build_feature_set
from heatsignal.features.heatmap import HEATMAP_MODEL_ID
from heatsignal.schema.events import Exchange, Side
from heatsignal.schema.features import FeatureSet

M = 60_000
NOW = 20 * M          # la barra 19 cierra justo en NOW
WIN = 300             # ventana (15 min, 20 min]
KW = dict(bar_s=60, atr_period=3, regime_lookback_bars=3)


def _scenario():
    # Binance: 1 trade por minuto, precio 100+i -> barras planas, TR = 1 -> ATR = 1
    trades = [trade(100 + i, 1, Side.BUY if i % 2 else Side.SELL, Exchange.BINANCE, ts=i * M + 30_000)
              for i in range(20)]
    # Bybit: pocos trades con base +50 (si se mezclaran inflarian el ATR)
    trades += [trade(170, 1, Side.BUY, Exchange.BYBIT, ts=18 * M + 1), trade(171, 1, Side.SELL, Exchange.BYBIT,
                                                                                 ts=19 * M + 1)]
    trades += [trade(170, 5, Side.BUY, Exchange.BYBIT, ts=19 * M + 2, block=True)]
    trades += [trade(1, 1, Side.BUY, Exchange.BINANCE, ts=NOW + 1)]  # futuro
    liqs = [
        liq(118.6, 1000, Side.SELL, Exchange.BINANCE, ts=NOW - 10_000),
        liq(119.6, 500, Side.SELL, Exchange.OKX, ts=NOW - 5_000),
        liq(90.0, 99999, Side.BUY, Exchange.BYBIT, ts=NOW - WIN * 1000),  # fuera de ventana
    ]
    tickers = [
        ticker(Exchange.BINANCE, NOW - 1_000, mark=119.0, funding=0.0001, oi_usd=1_000_000),
        ticker(Exchange.BYBIT, NOW - 100_000, mark=119.2, funding=0.0002, oi_usd=500_000),
        ticker(Exchange.BYBIT, NOW - 500, mark=119.5, oi_usd=510_000),
        ticker(Exchange.OKX, NOW - 200_000, mark=118.0, funding=-0.0001),  # stale y sin OI
    ]
    return liqs, trades, tickers


def _build(liqs, trades, tickers, **kw):
    return build_feature_set(SYM, liqs, trades, tickers, NOW, WIN, **{**KW, **kw})


def test_full_composition_mixed_exchanges() -> None:
    fs = _build(*_scenario())
    assert isinstance(fs, FeatureSet)
    assert fs.ts_ms == NOW

    # ATR solo de binance (fuente con mas trades), sin contaminar con la base de bybit
    assert fs.context.atr == pytest.approx(1.0)
    assert fs.heatmap.atr == pytest.approx(1.0)
    assert fs.context.mark_price == 119.5
    assert fs.context.atr_pct == pytest.approx(1 / 119.5 * 100)
    assert fs.context.regime == "trend_up"  # +3 ATR en 3 barras
    assert fs.context.open_interest_usd == pytest.approx(1_510_000)
    assert fs.context.oi_change_pct == pytest.approx(2.0)  # bybit 500k -> 510k

    # heatmap: w = 0.25 -> bins 474 (118.5-118.75) y 478 (119.5-119.75), separados
    hm = fs.heatmap
    assert hm.heatmap_model_id == HEATMAP_MODEL_ID
    assert [(c.price_low, c.price_high, c.notional_usd) for c in hm.clusters] == [
        (pytest.approx(118.5), pytest.approx(118.75), 1000), (pytest.approx(119.5), pytest.approx(119.75), 500)]
    assert hm.clusters[0].distance_atr == pytest.approx(118.625 - 119.5)
    assert all(c.partial_coverage_share == 1.0 for c in hm.clusters)

    # order flow en la ventana: binance minutos 15..19 (impar buy, par sell) + bybit 2 trades no-block
    of = fs.order_flow
    assert of.buy_volume_usd == pytest.approx(115 + 117 + 119 + 170)
    assert of.sell_volume_usd == pytest.approx(116 + 118 + 171)
    assert of.cvd_usd == pytest.approx(116)
    assert of.n_trades == 7
    assert of.block_trade_volume_usd == pytest.approx(850)
    assert of.sources == [Exchange.BINANCE, Exchange.BYBIT]

    gaps = fs.data_gaps
    assert "liquidaciones binance/okx PARTIAL (<=1/seg/simbolo): subrepresentan el notional real" in gaps
    assert "liquidaciones: sin eventos de bybit en la ventana" in gaps
    assert "trades: sin datos de okx en la ventana" in gaps
    assert "ticker okx: stale, ultimo hace 200s (> 120s)" in gaps
    assert "open_interest: sin dato de okx (la suma es parcial)" in gaps
    assert not any(g.startswith("atr") for g in gaps)
    assert not any("fallback" in g for g in gaps)
    assert not any(g.startswith(("ticker binance", "ticker bybit", "funding", "regime", "mark_price")) for g in gaps)


def test_deterministic_under_shuffle() -> None:
    liqs, trades, tickers = _scenario()
    a = _build(liqs, trades, tickers)
    rng = random.Random(3)
    for xs in (liqs, trades, tickers):
        rng.shuffle(xs)
    assert _build(liqs, trades, tickers) == a


def test_atr_falls_back_to_mark_bars_and_declares_it() -> None:
    tickers = [ticker(Exchange.BYBIT, i * M + 1, mark=100 + 2 * i, funding=0.0, oi_usd=1.0) for i in range(6)]
    fs = _build([], [], tickers, expected_exchanges=[Exchange.BYBIT])
    assert fs.context.atr == pytest.approx(2.0)
    assert any(g.startswith("atr: calculado con mark price de bybit") for g in fs.data_gaps)


def test_atr_unavailable_triggers_heatmap_fallback_gaps() -> None:
    liqs = [liq(100.05, 10, ts=NOW - 1), liq(100.25, 10, ts=NOW - 2)]
    tickers = [ticker(Exchange.BYBIT, NOW - 1, mark=100.0)]
    trades = [trade(100, 1, ts=NOW - 1)]
    fs = _build(liqs, trades, tickers, expected_exchanges=[Exchange.BYBIT, Exchange.BINANCE])
    assert fs.context.atr is None
    assert fs.context.regime == "unknown"
    assert all(c.distance_atr is None for c in fs.heatmap.clusters)
    assert len(fs.heatmap.clusters) == 2  # w = 0.1 -> bins 1000 y 1002
    gaps = fs.data_gaps
    assert any(g.startswith("atr: no disponible") for g in gaps)
    assert any("heatmap: sin ATR, bin por fallback" in g for g in gaps)
    assert any(g.startswith("regime: unknown") for g in gaps)


def test_holes_in_price_bars_are_declared() -> None:
    trades = [trade(100, 1, ts=i * M + 1) for i in (0, 1, 2, 5, 6)]   # faltan barras 3 y 4
    fs = _build([], trades, [], expected_exchanges=[Exchange.BINANCE])
    assert fs.context.atr is not None
    assert "atr: 2 barras vacias en la serie (trades de binance); el TR que cruza el hueco puede inflarse" in fs.data_gaps


def test_everything_empty_is_explicit_not_silent() -> None:
    fs = _build([], [], [])
    assert fs.heatmap.clusters == []
    assert fs.order_flow.imbalance == 0.0
    assert fs.context.regime == "unknown"
    gaps = fs.data_gaps
    for expected in (
        "liquidaciones: 0 eventos en la ventana (sin datos o sin liquidaciones: no distinguible)",
        "order_flow: ventana sin trades (no-block); imbalance=0 no implica equilibrio",
        "trades: sin datos de binance en la ventana",
        "trades: sin datos de bybit en la ventana",
        "trades: sin datos de okx en la ventana",
        "ticker: sin datos de binance",
        "ticker: sin datos de bybit",
        "ticker: sin datos de okx",
        "mark_price: no disponible",
        "open_interest: ningun exchange lo informa",
    ):
        assert expected in gaps
    assert any(g.startswith("atr: no disponible") for g in gaps)
    assert any(g.startswith("oi_change_pct: no disponible") for g in gaps)


def test_funding_missing_for_exchange_with_ticker() -> None:
    tickers = [ticker(Exchange.OKX, NOW - 1, mark=100.0, oi_usd=5.0)]
    fs = _build([], [], tickers, expected_exchanges=[Exchange.OKX])
    assert "funding: sin dato de okx" in fs.data_gaps
