"""Calidad de los trades (lab.stats.trade_quality): win-rate minimo, azar por barreras, rachas, GHPR, R² y estabilidad.

Investigacion: .claude/context/research/mql5-matematica-trading.md."""

from __future__ import annotations

import json
import math
import subprocess
from pathlib import Path

import numpy as np
import pytest
from scipy import stats as sps

from heatsignal.lab import stats as st
from heatsignal.lab.run import RunConfig, barrier_of, evaluate, open_holdout
from heatsignal.lab.trials import TrialRegistry

from test_lab_run import _md

VIEW = Path(__file__).parents[1] / "src/heatsignal/api/static/app/views/laboratorio.js"
HARNESS = Path(__file__).parent / "js" / "render_view.mjs"


# ---------------------------------------------------------------- formulas

def test_breakeven_hit_rate_is_one_over_one_plus_payoff() -> None:
    # ganancia media 2, perdida media 1 -> payoff 2 -> minimo 1/3 (articulo 19211: RRR 2 -> 33,3 %)
    be = st.breakeven_hit_rate([2.0, 2.0, -1.0, -1.0, -1.0])
    assert be["payoff"] == pytest.approx(2.0) and be["breakeven"] == pytest.approx(1 / 3)
    # pnl 0 cuenta como perdida (Vince): baja la perdida media
    assert st.breakeven_hit_rate([2.0, -1.0, 0.0])["avg_loss"] == pytest.approx(0.5)
    assert math.isnan(st.breakeven_hit_rate([1.0, 2.0])["breakeven"])  # sin perdidas no hay minimo
    assert math.isnan(st.breakeven_hit_rate([])["breakeven"])


def test_barrier_probability_matches_random_walk_simulation() -> None:
    assert st.barrier_hit_probability(2.0, 1.0) == pytest.approx(1 / 3)
    assert st.barrier_hit_probability(1.5, 1.5) == pytest.approx(0.5)
    assert math.isnan(st.barrier_hit_probability(0.0, 1.0))
    # paseo +-1 con objetivo a +4 y stop a -2: P(objetivo primero) = 2 / 6
    rng = np.random.default_rng(0)
    hits, n = 0, 4000
    for _ in range(n):
        x = 0
        while -2 < x < 4:
            x += 1 if rng.random() < 0.5 else -1
        hits += x == 4
    assert hits / n == pytest.approx(st.barrier_hit_probability(4, 2), abs=0.025)


def test_runs_test_known_sequences() -> None:
    alt = [1, -1] * 10  # alterna siempre: maximo de rachas -> Z muy positivo
    r = st.runs_test(alt)
    assert r["runs"] == 20 and r["expected_runs"] == pytest.approx(11.0) and r["z"] > 3 and r["p_value"] < 0.01
    blocks = [1] * 10 + [-1] * 10  # dos rachas -> Z muy negativo
    r = st.runs_test(blocks)
    assert r["runs"] == 2 and r["z"] < -3 and r["max_win_streak"] == 10 and r["max_loss_streak"] == 10
    # valor a mano: W=3, L=2, N=5, R=3 (+ + - - +): E = 2*6/5 + 1 = 3.4; Var = 12*7/(25*4) = 0.84
    r = st.runs_test([1, 1, -1, -1, 1])
    assert r["z"] == pytest.approx((3 - 3.4) / math.sqrt(0.84))
    # sin perdedores el test no existe; las rachas igual se cuentan
    r = st.runs_test([1, 2, 3])
    assert r["z"] is None and r["runs"] == 1 and r["max_win_streak"] == 3
    assert st.runs_test([])["runs"] is None


def test_runs_test_is_calibrated_under_independence() -> None:
    rng = np.random.default_rng(1)
    zs = [st.runs_test(rng.normal(size=200))["z"] for _ in range(400)]
    rej = np.mean([abs(z) > 1.96 for z in zs])
    assert np.mean(zs) == pytest.approx(0, abs=0.15) and 0.02 < rej < 0.09


def test_hpr_means_geometric_below_arithmetic_and_ruin() -> None:
    h = st.hpr_means([0.5, -0.4])  # +50 % y -40 %: AHPR 1,05 pero reinvirtiendo 1,5*0,6 = 0,9
    assert h["ahpr"] == pytest.approx(1.05) and h["ghpr"] == pytest.approx(math.sqrt(0.9))
    assert st.hpr_means([0.1, -1.0])["ghpr"] == 0.0  # perdio todo lo asignado
    assert math.isnan(st.hpr_means([])["ghpr"])


def test_equity_r2_sign_and_smoothness() -> None:
    assert st.equity_r2([0.01] * 50) == pytest.approx(1.0)
    assert st.equity_r2([-0.01] * 50) == pytest.approx(-1.0)
    assert math.isnan(st.equity_r2([0.0] * 10)) and math.isnan(st.equity_r2([0.01]))
    rng = np.random.default_rng(2)
    noisy = 0.001 + rng.normal(0, 0.02, 300)
    assert 0 < abs(st.equity_r2(noisy)) < 1
    # 2358: el R² de paseos al azar no se amontona cerca de 1 (la "LR correlation" de MT5 si)
    r2 = [abs(st.equity_r2(rng.normal(0, 0.01, 250))) for _ in range(300)]
    assert np.median(r2) < 0.6


def test_split_stability_matches_mann_whitney() -> None:
    rng = np.random.default_rng(3)
    same = rng.normal(size=100)
    s = st.split_stability(same)
    u = sps.mannwhitneyu(same[:70], same[70:], alternative="two-sided").statistic
    assert s["n_first"] == 70 and s["n_last"] == 30
    assert s["stability"] == pytest.approx(1 - abs(1 - 2 * u / (70 * 30)))
    decayed = np.r_[rng.normal(1, 0.5, 70), rng.normal(-1, 0.5, 30)]  # el edge se apago
    d = st.split_stability(decayed)
    assert d["stability"] < 0.1 and d["p_value"] < 1e-6 and d["mean_last"] < 0 < d["mean_first"]
    assert st.split_stability(rng.normal(size=20))["stability"] is None  # 6 trades en el ultimo tramo


def test_trade_quality_block_flags_enough_hit_rate() -> None:
    pnl = np.array([2.0] * 60 + [-1.0] * 40)  # 60 % con minimo 33 %: alcanza
    q = st.trade_quality(pnl, pnl / 100, pnl / 100, barrier=(3.0, 1.5))
    assert q["above_breakeven"] is True and q["barrier_chance"] == pytest.approx(1 / 3)
    assert q["n"] == 100 and q["runs"]["max_win_streak"] == 60
    q = st.trade_quality(np.array([1.0, -1.0] * 20), np.zeros(40), np.zeros(40))
    assert q["above_breakeven"] is False and q["barrier_chance"] is None


# ---------------------------------------------------------------- laboratorio + panel

def test_barrier_of_reads_exit_fiche() -> None:
    assert barrier_of("ema-cruce-v1") == (3.0, 1.5)  # stop 1,5 ATR, objetivo 3 ATR (R:R 1:2)
    assert barrier_of("absorcion-flujo-v1") is None  # sale por tiempo
    assert barrier_of("vwap-reversion-v1") is None  # objetivo en el VWAP, no en ATR
    assert barrier_of("no-existe") is None


def test_reports_carry_trade_quality(tmp_path) -> None:
    data = {"AAAUSDT": _md(80, 1), "BBBUSDT": _md(80, 2)}
    cfg = RunConfig("absorcion-flujo-v1", list(data), holdout_months=1, train_days=20, test_days=10, n_perm=0)
    rep = evaluate(cfg, TrialRegistry(tmp_path / "t.sqlite"), data=data)
    q = rep["oos"]["trade_quality"]
    assert q["n"] == rep["oos"]["trades"] == rep["oos"]["hit_rate"]["n"]
    assert q["hit_rate"] == pytest.approx(rep["oos"]["hit_rate"]["hit_rate"])
    assert q["barrier_chance"] is None and math.isfinite(q["equity_r2"])
    hold = open_holdout(cfg, TrialRegistry(tmp_path / "h.sqlite"), data=data)
    assert hold["holdout"]["trade_quality"]["n"] == hold["holdout"]["trades"]


def _render(tmp_path, detail) -> str:
    f = tmp_path / "data.json"
    f.write_text(json.dumps({"overview": {"strategies": []}, "detail": detail}))
    out = subprocess.run(["node", str(HARNESS), str(VIEW), str(f)], capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)["detail"]


def test_lab_detail_shows_trade_quality_card(tmp_path) -> None:
    pnl = np.r_[np.full(30, 2.0), np.full(30, -1.0)]
    q = json.loads(json.dumps(st.trade_quality(pnl, pnl / 100, pnl / 100, barrier=(3.0, 1.5))))
    base = {"strategy_id": "s1", "spec_hash": "h", "run": "r", "runs": ["r"], "symbols": ["AAAUSDT"],
            "oos": {"trades": 60, "trade_quality": q}, "costs": {}, "walk_forward": {}, "baselines": {}}
    d = _render(tmp_path, base)
    assert "Calidad de los trades (fuera de muestra)" in d and "Mínimo para no perder" in d and "33 %" in d
    assert "Win-rate del azar" in d and "las ganancias y pérdidas vienen agrupadas en rachas" in d
    assert "Racha máxima" in d and "30 / 30" in d and "acierto suficiente" in d
    # resultado viejo sin el bloque: lo explica; sin trades: lo dice
    base["oos"] = {"trades": 5}
    assert "próxima corrida del laboratorio" in _render(tmp_path, base)
    base["oos"] = {"trades": 0, "trade_quality": None}
    assert "Sin trades" in _render(tmp_path, base)
