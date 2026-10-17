"""Tests de los baselines: buy & hold, canasta rebalanceada y entradas al azar."""

from __future__ import annotations

from collections import Counter

import numpy as np
import pandas as pd
import pytest

from heatsignal.lab.baselines import (
    buy_and_hold,
    empirical_pvalue,
    equal_weight_basket,
    extract_trades,
    random_entries,
    random_entries_null,
)
from heatsignal.lab.sim import Costs

T0 = pd.Timestamp("2026-01-01", tz="UTC")
ZERO = Costs(fee_bps=0.0, default_slippage_bps=0.0)


def _prices(path: np.ndarray) -> pd.DataFrame:
    idx = pd.date_range(T0, periods=len(path), freq="1min")
    o = np.r_[path[0], path[:-1]]
    return pd.DataFrame(
        {"open": o, "high": np.maximum(o, path), "low": np.minimum(o, path), "close": path}, index=idx
    )


def test_buy_and_hold_tracks_price():
    df = _prices(np.linspace(100, 110, 3 * 1440))
    res = buy_and_hold({"X": df}, costs=ZERO)["X"]
    # 1 unidad del capital (1.0) comprada a 100 -> retorno = 110/100 - 1
    assert res.breakdown["precio"] == pytest.approx(0.1)
    assert len(res.fills) == 1


def test_equal_weight_basket_rebalances_daily():
    n = 3 * 1440
    a = _prices(np.linspace(100, 200, n))  # sube mucho
    b = _prices(np.full(n, 50.0))  # quieto
    res = equal_weight_basket({"A": a, "B": b}, costs=Costs(fee_bps=5.0, default_slippage_bps=0.0))
    fills_a = res.fills[res.fills["symbol"] == "A"]
    # entrada + rebalanceos a las 00:00 de los dias 2 y 3
    assert len(fills_a) == 3
    assert (fills_a["units_to"].diff().dropna() < 0).all()  # A sube: se venden unidades al rebalancear
    assert len(res.fills[res.fills["symbol"] == "B"]) == 1  # B no cambia de precio: sin rebalanceo
    assert res.breakdown["comisiones"] > 0


def _signature(trades) -> tuple:
    return (
        len(trades.lengths),
        sorted(trades.lengths.tolist()),
        Counter(trades.directions.tolist()),
    )


def _example_signal() -> pd.Series:
    grid = pd.date_range(T0, periods=200, freq="4h")
    vals = np.zeros(200)
    vals[5:12] = 1.0
    vals[20:22] = -0.5
    vals[22:30] = 1.0  # reversion directa
    vals[50:51] = -1.0
    vals[80:95] = 0.7
    vals[120:126] = -1.0
    s = pd.Series(vals, index=grid)
    return s[s.diff().fillna(s) != 0]  # formato esparso (solo cambios)


def test_extract_trades():
    tr = extract_trades(_example_signal(), freq="4h", end=T0 + pd.Timedelta(hours=4 * 199))
    assert tr.lengths.tolist() == [7, 2, 8, 1, 15, 6]
    assert tr.directions.tolist() == [1, -1, 1, -1, 1, -1]


def test_random_entries_preserve_count_durations_and_direction():
    sig = _example_signal()
    end = T0 + pd.Timedelta(hours=4 * 199)
    ref = extract_trades(sig, freq="4h", end=end)
    perms = list(random_entries(sig, n_perm=200, seed=11, freq="4h", end=end))
    assert len(perms) == 200
    starts = set()
    for p in perms:
        tr = extract_trades(p, freq="4h", start=T0, end=end)
        assert _signature(tr) == _signature(ref)
        # los bloques de tamano se conservan tal cual
        assert sorted(tuple(b) for b in tr.blocks) == sorted(tuple(b) for b in ref.blocks)
        starts.add(tuple(tr.starts.tolist()))
    assert len(starts) > 150  # realmente aleatorio
    # reproducible con la misma semilla
    again = list(random_entries(sig, n_perm=3, seed=11, freq="4h", end=end))
    for x, y in zip(perms[:3], again):
        pd.testing.assert_series_equal(x, y)


def test_random_entries_infers_grid():
    sig = _example_signal()
    tr = extract_trades(sig)
    assert tr.grid.freq == pd.Timedelta(hours=4)


def test_random_entries_null_and_pvalue():
    rng = np.random.default_rng(2)
    n = 1440 * 30
    df = _prices(100 * np.exp(np.cumsum(rng.normal(0, 0.0005, n))))
    grid = pd.date_range(T0, periods=180, freq="4h")
    vals = np.zeros(180)
    vals[10:20] = 1.0
    vals[60:70] = -1.0
    vals[100:130] = 1.0
    sig = pd.Series(vals, index=grid)
    null = random_entries_null(df, sig, None, ZERO, n_perm=30, seed=1, freq="4h")
    assert null.shape == (30,) and np.isfinite(null).all()
    assert empirical_pvalue(np.inf, null) == pytest.approx(1 / 31)
    assert empirical_pvalue(-np.inf, null) == pytest.approx(1.0)


def test_random_shift_joint_keeps_hedge_and_durations_between_legs() -> None:
    from heatsignal.lab.baselines import random_shift_joint

    idx = pd.date_range("2024-01-01", periods=200, freq="1h", tz="UTC")
    eth = pd.Series(0.0, index=idx)
    eth.iloc[20:50] = 1.0
    eth.iloc[120:130] = -1.0
    btc = -0.8 * eth  # pata de cobertura
    sigs = {"ETHUSDT": eth[eth.diff().fillna(1) != 0], "BTCUSDT": btc[btc.diff().fillna(1) != 0]}
    perms = list(random_shift_joint(sigs, 50, seed=1, start=idx[0], end=idx[-1]))
    assert len(perms) == 50
    shifted_any = False
    for p in perms:
        e = p["ETHUSDT"].reindex(idx, method="ffill").fillna(0.0)
        b = p["BTCUSDT"].reindex(idx, method="ffill").fillna(0.0)
        assert np.allclose(b, -0.8 * e)  # en cada instante la cobertura sigue igual
        assert sorted(e.to_numpy()) == sorted(eth.to_numpy())  # mismo tiempo en cada posicion
        shifted_any |= not np.allclose(e, eth)
    assert shifted_any


def test_null_kind_is_joint_only_for_neutral_specs() -> None:
    from heatsignal.lab.run import null_kind
    from heatsignal.lab.strategies import CATALOG

    assert null_kind(CATALOG["pares-eth-btc-v1"][0]) == "joint_shift"
    assert null_kind(CATALOG["reversion-xs-1h-v1"][0]) == "joint_shift"
    assert null_kind(CATALOG["donchian-ensemble-v1"][0]) == "per_symbol"
    assert null_kind(CATALOG["tsmom-diario-v1"][0]) == "per_symbol"
