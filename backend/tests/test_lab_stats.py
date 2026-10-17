"""Tests de las metricas del laboratorio: PSR/DSR/MinTRL contra valores de referencia y demas."""

from __future__ import annotations

import math

import numpy as np
import pandas as pd
import pytest
from scipy import stats as sps

from heatsignal.lab import stats as st
from heatsignal.lab.sim import Costs, simulate


# ---------------------------------------------------------------- (e) PSR / DSR / MinTRL

def test_dsr_matches_bailey_lopez_de_prado_2014_numerical_example():
    # Ejemplo del paper "The Deflated Sharpe Ratio" (2014): SR anual 2.5 en 5 anios de datos diarios
    # (T = 1250, 250 dias/anio), N = 100 intentos con V[SR] anual = 1/2, asimetria -3, curtosis 10.
    # El paper obtiene SR0 ~ 0.1132 (diario) y DSR ~ 0.9004.
    sr = 2.5 / math.sqrt(250)
    var_pp = 0.5 / 250
    assert st.expected_max_sharpe(100, var_pp) == pytest.approx(0.1132, abs=1e-4)
    dsr = st.deflated_sharpe_ratio(sr, 1250, 100, var_pp, skew=-3.0, kurt=10.0)
    assert dsr == pytest.approx(0.9004, abs=1e-4)


@pytest.mark.parametrize(
    "sr,sr_star,n,skew,kurt,expected",
    [
        # valores calculados con ml4t-diagnostic 0.1.9 (probabilistic_sharpe_ratio, MIT)
        (0.1, 0.0, 250, -0.5, 5.0, 0.937320574390259),
        (0.05, 0.02, 1000, 0.3, 4.0, 0.8300830231738849),
    ],
)
def test_psr_matches_reference_implementation(sr, sr_star, n, skew, kurt, expected):
    assert st.probabilistic_sharpe_ratio(sr, n, skew, kurt, sr_star) == pytest.approx(expected, rel=1e-12)


def test_expected_max_matches_reference_implementation():
    # ml4t-diagnostic 0.1.9 compute_expected_max_sharpe(37, 0.01)
    assert st.expected_max_sharpe(37, 0.01) == pytest.approx(0.2158501863737529, rel=1e-12)
    assert st.expected_max_sharpe(1, 0.01) == 0.0


def test_psr_normal_returns_closed_form():
    # con retornos normales (g3=0, g4=3): z = SR*sqrt(n-1)/sqrt(1 + SR^2/2)
    sr, n = 0.08, 500
    z = sr * math.sqrt(n - 1) / math.sqrt(1 + sr**2 / 2)
    assert st.probabilistic_sharpe_ratio(sr, n) == pytest.approx(sps.norm.cdf(z))


def test_min_trl_known_value_and_consistency_with_psr():
    sr = 0.1
    expected = 1 + (1 + 0.5 * sr**2) * (sps.norm.ppf(0.95) / sr) ** 2  # ~272.9
    mtrl = st.min_track_record_length(sr, alpha=0.05)
    assert mtrl == pytest.approx(expected)
    assert mtrl == pytest.approx(272.9, abs=0.1)
    # con n = MinTRL el PSR vale exactamente 1 - alpha
    assert st.probabilistic_sharpe_ratio(sr, mtrl) == pytest.approx(0.95)
    assert st.min_track_record_length(-0.1) == math.inf


def test_dsr_from_returns_uses_trials_and_effective_k():
    rng = np.random.default_rng(3)
    r = rng.normal(0.002, 0.02, 730)
    trials = rng.normal(0.5, 0.6, 50)
    out = st.dsr_from_returns(r, trials)
    assert out["n_trials_used"] == 50
    assert out["dsr"] < out["psr"]
    # 50 intentos identicos (correlacion 1) -> K efectivo 1 -> sin penalizacion
    out1 = st.dsr_from_returns(r, trials, corr=np.ones((50, 50)))
    assert out1["n_trials_used"] == pytest.approx(1.0)
    assert out1["dsr"] == pytest.approx(out1["psr"])


def test_effective_n_trials():
    assert st.effective_n_trials(np.eye(10)) == pytest.approx(10.0)
    assert st.effective_n_trials(np.ones((5, 5))) == pytest.approx(1.0)
    c = np.full((4, 4), 0.5)
    np.fill_diagonal(c, 1.0)
    assert st.effective_n_trials(c, "avg_corr") == pytest.approx(0.5 + 0.5 * 4)
    assert 1.0 < st.effective_n_trials(c) < 4.0


# ---------------------------------------------------------------- Sharpe e IC

def test_sharpe_annualized_365():
    r = np.array([0.01, -0.005, 0.02, 0.0, 0.003])
    assert st.sharpe_ratio(r) == pytest.approx(r.mean() / r.std(ddof=1) * math.sqrt(365))
    assert math.isnan(st.sharpe_ratio([0.01, 0.01, 0.01]))


def test_stationary_bootstrap_indices_structure():
    rng = np.random.default_rng(0)
    idx = st.stationary_bootstrap_indices(500, 10.0, 200, rng)
    assert idx.shape == (200, 500) and idx.min() >= 0 and idx.max() < 500
    # largo medio de bloque ~ 10: proporcion de continuaciones ~ 0.9
    cont = ((idx[:, 1:] - idx[:, :-1]) % 500 == 1).mean()
    assert cont == pytest.approx(0.9, abs=0.02)


def test_sharpe_ci_contains_estimate_and_is_reproducible():
    rng = np.random.default_rng(1)
    r = rng.normal(0.001, 0.02, 400)
    a = st.sharpe_ci(r, n_boot=500, seed=42, holding_days=5)
    b = st.sharpe_ci(r, n_boot=500, seed=42, holding_days=5)
    assert a == b
    assert a["lo"] < a["sharpe"] < a["hi"]
    assert a["block_len"] >= 5
    # IC con ancho razonable: el error estandar del Sharpe anual con n=400 es ~ sqrt(365/400) ~ 0.96
    assert 2.0 < a["hi"] - a["lo"] < 6.0


# ---------------------------------------------------------------- otras

def test_max_drawdown():
    r = [0.1, -0.05, -0.05, 0.2]
    # equity no compuesta: 1.1, 1.05, 1.0, 1.2 -> dd = 0.1/1.1
    assert st.max_drawdown(r) == pytest.approx(0.1 / 1.1)
    eq = np.cumprod(1 + np.array(r))
    assert st.max_drawdown(r, compounding=True) == pytest.approx((eq[0] - eq[2]) / eq[0])


def test_wilson_and_hit_rate():
    lo, hi = st.wilson_interval(8, 10)
    # valores conocidos del intervalo de Wilson 95% para 8/10: (0.4902, 0.9433)
    assert lo == pytest.approx(0.4902, abs=1e-4)
    assert hi == pytest.approx(0.9433, abs=1e-4)
    h = st.hit_rate([1, -1, 2, 0, 3])
    assert h["n"] == 5 and h["wins"] == 3 and h["hit_rate"] == 0.6


def test_profit_factor():
    assert st.profit_factor([3, -1, 2, -2]) == pytest.approx(5 / 3)
    assert st.profit_factor([1, 2]) == math.inf


def test_cost_curve_and_breakeven():
    gross = pd.Series([0.002, 0.001, 0.003, -0.001, 0.002])
    turnover = pd.Series([2.0, 1.0, 2.0, 0.0, 1.0])
    be = st.breakeven_cost_bps(gross, turnover)
    assert be == pytest.approx(0.007 / 6.0 * 1e4)
    curve = st.cost_curve(gross, turnover, bps_grid=[0, 5, be])
    assert list(curve["cost_bps"])[:2] == [0.0, 5.0]
    assert curve["sharpe"].iloc[0] > curve["sharpe"].iloc[1]
    assert curve["ann_return"].iloc[2] == pytest.approx(0.0, abs=1e-12)


def test_cost_curve_from_sim_result_matches_resimulation():
    t0 = pd.Timestamp("2026-01-01", tz="UTC")
    rng = np.random.default_rng(5)
    n = 60 * 24 * 20
    idx = pd.date_range(t0, periods=n, freq="1min")
    c = 100 * np.exp(np.cumsum(rng.normal(0, 0.0005, n)))
    o = np.r_[100.0, c[:-1]]
    df = pd.DataFrame({"open": o, "high": np.maximum(o, c), "low": np.minimum(o, c), "close": c}, index=idx)
    sig = pd.Series(rng.choice([-1.0, 0.0, 1.0], 20 * 6), index=pd.date_range(t0, periods=120, freq="4h"))
    res0 = simulate({"X": df}, {"X": sig}, None, Costs(fee_bps=0, default_slippage_bps=0))
    res7 = simulate({"X": df}, {"X": sig}, None, Costs(fee_bps=5, default_slippage_bps=2))
    curve = st.cost_curve(res0, bps_grid=st.DEFAULT_BPS_GRID)
    assert len(curve) == 10
    row = curve.loc[curve["cost_bps"] == 7.0].iloc[0]
    assert row["sharpe"] == pytest.approx(st.sharpe_ratio(res7.daily_returns), rel=1e-9)
