"""Tests del modelo de heatmap liqcluster-v1 con bins y clusters calculados a mano."""

from __future__ import annotations

import pytest

from features_helpers import SYM, liq
from heatsignal.features.heatmap import HEATMAP_MODEL_ID, build_heatmap, resolve_bin_width
from heatsignal.schema.events import Coverage, Exchange, Side

NOW = 10_000
WIN = 60


def _hm(events, mark=100.0, atr=4.0, **kw):
    return build_heatmap(SYM, events, NOW, WIN, mark, atr, **kw)


def test_model_id_is_versioned() -> None:
    assert HEATMAP_MODEL_ID == "liqcluster-v1"
    assert _hm([]).snapshot.heatmap_model_id == "liqcluster-v1"


def test_known_bins_mixed_exchanges() -> None:
    # ATR=4 -> w = 0.25*4 = 1.0 ; mark = 100
    events = [
        liq(100.2, 1000, Side.SELL, Exchange.BYBIT),     # bin 100, FULL
        liq(100.7, 500, Side.SELL, Exchange.BINANCE),    # bin 100, PARTIAL
        liq(101.5, 300, Side.BUY, Exchange.OKX),         # bin 101, PARTIAL (contiguo)
        liq(103.5, 200, Side.BUY, Exchange.BYBIT),       # bin 103 (102 vacio -> cluster aparte)
        liq(95.1, 2000, Side.BUY, Exchange.BYBIT),       # bin 95
    ]
    res = _hm(events)
    assert res.bin_width == pytest.approx(1.0)
    assert res.bin_basis == "atr"
    assert res.data_gaps == []
    assert res.partial_sources == [Exchange.BINANCE, Exchange.OKX]
    c = res.snapshot.clusters
    assert [x.notional_usd for x in c] == [2000, 1800, 200]

    big = c[0]
    assert (big.price_low, big.price_high, big.price_mid) == (95, 96, 95.5)
    assert big.dominant_side == Side.BUY
    assert big.partial_coverage_share == 0.0
    assert big.distance_atr == pytest.approx((95.5 - 100) / 4)  # -1.125

    mid = c[1]
    assert (mid.price_low, mid.price_high, mid.price_mid) == (100, 102, 101)
    assert mid.n_events == 3
    assert mid.dominant_side == Side.SELL                      # 1500/1800 = 0.833 >= 0.6
    assert mid.sources == [Exchange.BINANCE, Exchange.BYBIT, Exchange.OKX]
    assert mid.partial_coverage_share == pytest.approx(800 / 1800)
    assert mid.distance_atr == pytest.approx(0.25)

    assert c[2].price_mid == 103.5
    assert c[2].distance_atr == pytest.approx(0.875)


def test_peak_seeded_clusters_respect_radius() -> None:
    # bins 10..20 contiguos; pico en 15. radius=2 -> [13..17], luego [10..12] (empata con 18,
    # gana el de menor precio) y [18..20].
    weights = {10: 1, 11: 2, 12: 3, 13: 4, 14: 5, 15: 100, 16: 5, 17: 4, 18: 3, 19: 2, 20: 1}
    events = [liq(i + 0.5, n) for i, n in weights.items()]
    c = _hm(events, mark=15.0, atr=4.0).snapshot.clusters
    assert [(x.price_low, x.price_high) for x in c] == [(13, 18), (10, 13), (18, 21)]
    assert [x.notional_usd for x in c] == [118, 6, 6]
    # ancho maximo = (2*radius + 1) * w
    assert all(x.price_high - x.price_low <= 5 for x in c)


def test_radius_zero_means_one_bin_per_cluster() -> None:
    events = [liq(10.5, 10), liq(11.5, 20)]
    c = _hm(events, atr=4.0, max_radius_bins=0).snapshot.clusters
    assert [(x.price_low, x.notional_usd) for x in c] == [(11, 20), (10, 10)]


def test_dominance_threshold_edges() -> None:
    balanced = [liq(100.5, 55, Side.SELL), liq(100.5, 45, Side.BUY)]
    assert _hm(balanced).snapshot.clusters[0].dominant_side is None
    exact = [liq(100.5, 60, Side.BUY), liq(100.5, 40, Side.SELL)]
    assert _hm(exact).snapshot.clusters[0].dominant_side == Side.BUY  # 0.6 >= 0.6
    assert _hm(exact, dominance_threshold=0.7).snapshot.clusters[0].dominant_side is None
    with pytest.raises(ValueError):
        _hm(exact, dominance_threshold=0.5)


def test_partial_share_uses_coverage_field_not_exchange() -> None:
    events = [liq(100.5, 300, source=Exchange.BYBIT, coverage=Coverage.PARTIAL),
              liq(100.5, 100, source=Exchange.BYBIT, coverage=Coverage.FULL)]
    assert _hm(events).snapshot.clusters[0].partial_coverage_share == pytest.approx(0.75)


def test_fallback_pct_of_mark_when_no_atr_is_declared() -> None:
    res = _hm([liq(100.05, 10), liq(100.25, 10)], mark=100.0, atr=None)
    assert res.bin_basis == "pct_mark"
    assert res.bin_width == pytest.approx(0.1)
    assert any("sin ATR" in g and "fallback" in g for g in res.data_gaps)
    assert all(c.distance_atr is None for c in res.snapshot.clusters)
    assert res.snapshot.atr is None


def test_fallback_liq_vwap_when_no_atr_no_mark() -> None:
    res = _hm([liq(100, 100), liq(200, 300)], mark=None, atr=None)
    # VWAP = (100*100 + 200*300)/400 = 175 -> w = 0.175
    assert res.bin_basis == "pct_liq_vwap"
    assert res.bin_width == pytest.approx(0.175)
    assert any("VWAP" in g for g in res.data_gaps)


def test_no_fallback_gap_when_no_events() -> None:
    res = _hm([], atr=None)
    assert res.snapshot.clusters == []
    assert res.data_gaps == []


def test_resolve_bin_width_chain() -> None:
    assert resolve_bin_width(4.0, 100.0, 50.0) == (1.0, "atr")
    assert resolve_bin_width(0.0, 100.0, 50.0) == (pytest.approx(0.1), "pct_mark")
    assert resolve_bin_width(None, None, None) == (None, None)


def test_window_symbol_and_future_filtering() -> None:
    events = [
        liq(100.5, 1, ts=NOW - WIN * 1000),        # borde inferior: excluido
        liq(100.5, 2, ts=NOW - WIN * 1000 + 1),    # incluido
        liq(100.5, 4, ts=NOW),                     # incluido
        liq(100.5, 8, ts=NOW + 1),                 # futuro: excluido
        liq(100.5, 16, symbol="BTC-USDT"),         # otro simbolo
    ]
    res = _hm(events)
    assert res.n_events_used == 2
    assert res.snapshot.clusters[0].notional_usd == 6


def test_invalid_events_dropped_and_declared() -> None:
    res = _hm([liq(100.5, 10), liq(100.5, 0.0)])
    assert res.dropped_events == 1
    assert res.n_events_used == 1
    assert any("descartadas" in g for g in res.data_gaps)


def test_top_n_and_tiebreak() -> None:
    events = [liq(10.5 + 3 * i, 100) for i in range(5)]   # 5 clusters iguales, separados
    c = _hm(events, top_n=3).snapshot.clusters
    assert len(c) == 3
    assert [x.price_mid for x in c] == [10.5, 13.5, 16.5]   # empate -> menor precio primero


def test_deterministic_regardless_of_input_order() -> None:
    events = [liq(100.2, 10, ts=1), liq(101.3, 20, ts=2, source=Exchange.OKX), liq(97.9, 5, ts=3)]
    assert _hm(events).snapshot == _hm(list(reversed(events))).snapshot
