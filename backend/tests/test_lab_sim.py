"""Tests del simulador del laboratorio: ejecucion sin look-ahead, funding, costos y descomposicion."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from heatsignal.lab.sim import Costs, simulate

T0 = pd.Timestamp("2026-01-01 00:00", tz="UTC")
MIN = pd.Timedelta(minutes=1)
ZERO = Costs(fee_bps=0.0, default_slippage_bps=0.0)


def bars(opens, closes=None, start=T0, index=None) -> pd.DataFrame:
    o = np.asarray(opens, dtype=float)
    c = o.copy() if closes is None else np.asarray(closes, dtype=float)
    idx = index if index is not None else pd.date_range(start, periods=len(o), freq="1min")
    return pd.DataFrame(
        {"open": o, "high": np.maximum(o, c) + 0.5, "low": np.minimum(o, c) - 0.5, "close": c},
        index=idx,
    )


def flat(price: float, n: int, start=T0) -> pd.DataFrame:
    return bars([price] * n, start=start)


# ---------------------------------------------------------------- (a) ejecucion al open siguiente

def test_decision_at_bar_close_executes_at_next_open():
    df = bars([100, 101, 102, 103, 104], [100.5, 101.5, 102.5, 103.5, 104.5])
    # decision al cierre de la vela 1 (= apertura de la vela 2)
    sig = pd.Series([1.0], index=[T0 + 2 * MIN])
    res = simulate({"X": df}, {"X": sig}, None, ZERO)
    assert res.fills.loc[0, "time"] == T0 + 2 * MIN
    assert res.fills.loc[0, "price"] == 102.0


def test_decision_inside_bar_executes_at_next_open():
    df = bars([100, 101, 102, 103])
    sig = pd.Series([1.0], index=[T0 + MIN + pd.Timedelta(seconds=30)])
    res = simulate({"X": df}, {"X": sig}, None, ZERO)
    assert res.fills.loc[0, "time"] == T0 + 2 * MIN
    assert res.fills.loc[0, "price"] == 102.0


def test_decision_in_gap_executes_at_next_available_bar():
    idx = pd.DatetimeIndex([T0, T0 + MIN, T0 + 10 * MIN, T0 + 11 * MIN])
    df = bars([100, 101, 110, 111], index=idx)
    sig = pd.Series([1.0], index=[T0 + 5 * MIN])
    res = simulate({"X": df}, {"X": sig}, None, ZERO)
    assert res.fills.loc[0, "time"] == T0 + 10 * MIN
    assert res.fills.loc[0, "price"] == 110.0


def test_perfect_close_signal_gets_no_lookahead_gain():
    # Velas alternadas: sube 1 / baja 1, cada open = close anterior. Una senal "perfecta" que mira el
    # close de la vela que acaba de cerrar (long si subio) se ejecuta en la vela siguiente, que baja:
    # pierde exactamente 1 por unidad en cada trade. Un simulador con look-ahead ganaria.
    n = 21
    d = np.array([1.0 if i % 2 == 0 else -1.0 for i in range(n)])
    opens = 1000.0 + np.r_[0.0, np.cumsum(d)[:-1]]
    closes = opens + d
    df = bars(opens, closes)
    up = closes > opens
    decision_times = df.index + MIN  # cierre de cada vela
    sig = pd.Series(np.where(up, 1.0, 0.0), index=decision_times)
    res = simulate({"X": df}, {"X": sig}, None, ZERO, capital=1.0)
    trades = res.trades
    assert len(trades) == n // 2  # una por cada vela alcista con vela siguiente
    # cada trade: entra al open de una vela bajista y sale en la siguiente: pierde 1 por unidad
    units = trades["notional_entry"] / trades["entry_price"]
    np.testing.assert_allclose(trades["pnl_price"], -units, rtol=1e-12)
    assert res.breakdown["precio"] < 0


def test_iid_perfect_close_signal_has_no_edge_vs_oracle():
    rng = np.random.default_rng(7)
    n = 5000
    r = rng.normal(0, 0.001, n)
    closes = 100 * np.exp(np.cumsum(r))
    opens = np.r_[100.0, closes[:-1]]
    df = bars(opens, closes)
    sig = pd.Series(np.sign(closes - opens), index=df.index + MIN)
    res = simulate({"X": df}, {"X": sig}, None, ZERO, capital=100.0)
    oracle = np.abs(closes - opens).sum()  # lo que ganaria con look-ahead (1 unidad)
    assert abs(res.breakdown["precio"]) < 0.1 * oracle


# ---------------------------------------------------------------- (b) funding

@pytest.mark.parametrize("side,expected_sign", [(1.0, -1.0), (-1.0, 1.0)])
def test_funding_sign_long_pays_short_receives(side, expected_sign):
    df = flat(100.0, 120)
    sig = pd.Series([side], index=[T0])
    fund = pd.Series([0.001], index=[T0 + 60 * MIN])
    res = simulate({"X": df}, {"X": sig}, {"X": fund}, ZERO, capital=1000.0)
    # nocional 1000, tasa 0.1% -> 1.0
    assert res.breakdown["funding"] == pytest.approx(expected_sign * 1.0)
    assert res.trades.loc[0, "funding"] == pytest.approx(expected_sign * 1.0)


def test_negative_funding_long_receives():
    df = flat(100.0, 120)
    res = simulate(
        {"X": df}, {"X": pd.Series([1.0], index=[T0])},
        {"X": pd.Series([-0.0005], index=[T0 + 60 * MIN])}, ZERO, capital=1000.0,
    )
    assert res.breakdown["funding"] == pytest.approx(0.5)


def test_funding_only_when_position_open_at_settlement():
    df = flat(100.0, 600)
    # long entre 00:10 y 02:00 (ejecuta al open de esas velas)
    sig = pd.Series([1.0, 0.0], index=[T0 + 10 * MIN, T0 + 120 * MIN])
    fund = pd.Series(
        [0.01, 0.001, 0.01, 0.01],
        index=[T0 + 5 * MIN, T0 + 60 * MIN, T0 + 120 * MIN, T0 + 480 * MIN],
    )
    res = simulate({"X": df}, {"X": sig}, {"X": fund}, ZERO, capital=1000.0)
    # solo cuenta la de 01:00. La de 02:00 coincide con la salida al open de 02:00: la posicion
    # estaba abierta JUSTO ANTES de la liquidacion, asi que paga.
    assert res.breakdown["funding"] == pytest.approx(-1000 * (0.001 + 0.01))


def test_entry_exactly_at_settlement_does_not_pay_that_funding():
    df = flat(100.0, 200)
    sig = pd.Series([1.0], index=[T0 + 60 * MIN])  # ejecuta al open de 01:00
    fund = pd.Series([0.01], index=[T0 + 60 * MIN])
    res = simulate({"X": df}, {"X": sig}, {"X": fund}, ZERO, capital=1000.0)
    assert res.breakdown["funding"] == 0.0


# ---------------------------------------------------------------- (c) costos

def test_costs_on_every_change_including_reversal():
    df = flat(100.0, 100)
    sig = pd.Series([1.0, -1.0, 0.0], index=[T0, T0 + 10 * MIN, T0 + 20 * MIN])
    costs = Costs(fee_bps=5.0, default_slippage_bps=2.0)
    res = simulate({"X": df}, {"X": sig}, None, costs, capital=1000.0)
    # nocional operado: 1000 (entrada) + 2000 (reversion) + 1000 (salida)
    assert res.pnl_1m["turnover"].sum() == pytest.approx(4000.0)
    assert res.breakdown["comisiones"] == pytest.approx(4000 * 5e-4)
    assert res.breakdown["slippage"] == pytest.approx(4000 * 2e-4)
    assert len(res.fills) == 3
    assert res.fills.loc[1, "notional"] == pytest.approx(2000.0)
    # la reversion reparte: cada trade paga 1000 de entrada + 1000 de salida
    assert len(res.trades) == 2
    np.testing.assert_allclose(res.trades["fees"], [2000 * 5e-4, 2000 * 5e-4])
    assert list(res.trades["direction"]) == ["long", "short"]


def test_repeated_same_value_does_not_trade():
    df = flat(100.0, 50)
    sig = pd.Series([1.0, 1.0, 1.0], index=[T0, T0 + 10 * MIN, T0 + 20 * MIN])
    res = simulate({"X": df}, {"X": sig}, None, Costs(), capital=1.0)
    assert len(res.fills) == 1


def test_fractional_resize_pays_only_delta_and_is_same_trade():
    df = flat(100.0, 50)
    sig = pd.Series([0.5, 0.8, 0.0], index=[T0, T0 + 10 * MIN, T0 + 20 * MIN])
    res = simulate({"X": df}, {"X": sig}, None, Costs(fee_bps=10.0, default_slippage_bps=0.0), 1000.0)
    assert res.pnl_1m["turnover"].sum() == pytest.approx(500 + 300 + 800)
    assert len(res.trades) == 1


def test_slippage_per_symbol_and_multiplier():
    df = flat(100.0, 50)
    sig = pd.Series([1.0, 0.0], index=[T0, T0 + 10 * MIN])
    costs = Costs(
        fee_bps=0.0,
        slippage_bps={"X": 3.0},
        slippage_mult_fn=lambda sym, ts: 3.0 if ts == T0 + 10 * MIN else 1.0,
    )
    res = simulate({"X": df}, {"X": sig}, None, costs, capital=1000.0)
    assert res.breakdown["slippage"] == pytest.approx(1000 * 3e-4 + 1000 * 9e-4)
    t = res.trades.iloc[0]
    assert t["entry_price"] == pytest.approx(100 * (1 + 3e-4))
    assert t["exit_price"] == pytest.approx(100 * (1 - 9e-4))


# ---------------------------------------------------------------- (d) descomposicion

def _random_case(seed: int):
    rng = np.random.default_rng(seed)
    out_p, out_s, out_f = {}, {}, {}
    for k, sym in enumerate(["AAA", "BBB", "CCC"]):
        n = 3000
        idx = pd.date_range(T0, periods=n, freq="1min")
        keep = rng.random(n) > 0.03  # huecos
        keep[0] = True
        idx = idx[keep]
        m = len(idx)
        closes = (50 + 30 * k) * np.exp(np.cumsum(rng.normal(0, 0.002, m)))
        opens = np.r_[closes[0], closes[:-1]] * np.exp(rng.normal(0, 0.0005, m))
        out_p[sym] = bars(opens, closes, index=idx)
        dec = pd.date_range(T0, periods=n // 37, freq="37min") + pd.Timedelta(seconds=13)
        out_s[sym] = pd.Series(rng.choice([-1.0, -0.5, 0.0, 0.3, 1.0], size=len(dec)), index=dec)
        out_f[sym] = pd.Series(rng.normal(0.0001, 0.0003, 7), index=pd.date_range(T0, periods=7, freq="8h"))
    return out_p, out_s, out_f


def test_breakdown_sums_to_total_and_matches_equity():
    p, s, f = _random_case(1)
    costs = Costs(fee_bps=5.0, slippage_bps={"AAA": 1.0}, default_slippage_bps=3.0)
    res = simulate(p, s, f, costs, capital=10_000.0)
    b = res.breakdown
    assert b["total"] == pytest.approx(b["precio"] + b["funding"] - b["comisiones"] - b["slippage"], abs=1e-9)
    assert res.equity.iloc[-1] - 10_000.0 == pytest.approx(b["total"], abs=1e-7)
    assert res.daily_returns.sum() * 10_000.0 == pytest.approx(b["total"], abs=1e-7)
    # por simbolo suma al total
    assert res.breakdown_by_symbol["total"].sum() == pytest.approx(b["total"], abs=1e-9)
    # los trades reparten todo el PnL (precio, funding y costos)
    tr = res.trades
    assert tr["pnl_price"].sum() == pytest.approx(b["precio"], abs=1e-7)
    assert tr["funding"].sum() == pytest.approx(b["funding"], abs=1e-9)
    assert tr["fees"].sum() == pytest.approx(b["comisiones"], abs=1e-9)
    assert tr["slippage"].sum() == pytest.approx(b["slippage"], abs=1e-9)
    assert tr["pnl_net"].sum() == pytest.approx(b["total"], abs=1e-7)
    assert b["funding"] != 0 and b["comisiones"] > 0


def test_equal_allocation_splits_capital():
    df = flat(100.0, 30)
    sig = pd.Series([1.0], index=[T0])
    res = simulate({"A": df, "B": df}, {"A": sig, "B": sig}, None, ZERO, capital=1000.0)
    assert res.fills["notional"].tolist() == [500.0, 500.0]
    res2 = simulate({"A": df, "B": df}, {"A": sig * 0.5, "B": sig * 0.5}, None, ZERO, 1000.0, allocation="total")
    assert res2.fills["notional"].tolist() == [500.0, 500.0]


def test_mark_to_market_and_gap_pnl():
    idx = pd.DatetimeIndex([T0, T0 + MIN, T0 + 5 * MIN])
    df = bars([100, 102, 110], [101, 104, 111], index=idx)
    res = simulate({"X": df}, {"X": pd.Series([1.0], index=[T0])}, None, ZERO, capital=100.0)
    # 1 unidad desde 100 hasta 111
    assert res.breakdown["precio"] == pytest.approx(11.0)
    assert res.equity.index[0] == T0 + MIN  # equity fechada al cierre de la vela
    tr = res.trades.iloc[0]
    assert bool(tr["is_open"]) and tr["mfe"] == pytest.approx(111.5 / 100 - 1)
    assert tr["mae"] == pytest.approx(99.5 / 100 - 1)


def test_rejects_naive_index():
    df = flat(100.0, 5)
    df.index = df.index.tz_localize(None)
    with pytest.raises(ValueError):
        simulate({"X": df}, {"X": pd.Series([1.0], index=[T0])}, None)


def test_mixed_datetime_resolutions():
    df = flat(100.0, 120)
    df.index = df.index.as_unit("s")
    sig = pd.Series([1.0], index=pd.DatetimeIndex([T0 + 2 * MIN]).as_unit("ms"))
    fund = pd.Series([0.001], index=pd.DatetimeIndex([T0 + 60 * MIN]).as_unit("us"))
    res = simulate({"X": df}, {"X": sig}, {"X": fund}, ZERO, capital=1000.0)
    assert res.fills.loc[0, "time"] == T0 + 2 * MIN
    assert res.breakdown["funding"] == pytest.approx(-1.0)
