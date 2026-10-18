"""Diagnosticos anti-sobreajuste del laboratorio: PBO por CSCV y robustez por parametros vecinos.

Logica estadistica (no negociable): PBO ~ 0,5 con ruido puro, bajo con ventaja real, C(S, S/2) particiones exactas.
Reglas anti p-hacking: la robustez no usa el holdout, no suma K, no cambia parametros congelados; vecinos en rango.
"""

from __future__ import annotations

import copy
import json
import math
import shutil
import sqlite3
import subprocess
import sys
from math import comb
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from heatsignal.lab import diagnostics as D
from heatsignal.lab.publish import program, summarize, write_all
from heatsignal.lab.strategies import MarketData, Spec
from heatsignal.lab.trials import TrialRegistry

sys.path.insert(0, str(Path(__file__).parent))
from test_lab_run import T0, _md  # noqa: E402

VIEW = Path(__file__).parents[1] / "src/heatsignal/api/static/app/views/laboratorio.js"
HARNESS = Path(__file__).parent / "js" / "render_view.mjs"


# ---------------------------------------------------------------------------- PBO


def test_partitions_are_exactly_c_s_half_and_complementary() -> None:
    for s in (2, 6, 16):
        p = D.cscv_partitions(s)
        assert p.shape == (comb(s, s // 2), s)
        assert (p.sum(axis=1) == s // 2).all()
        rows = {tuple(r) for r in p}
        assert len(rows) == len(p)                              # sin repetidas
        assert all(tuple(~np.array(r)) in rows for r in rows)   # cada mitad IS aparece tambien como OOS
    assert D.pbo_cscv(np.random.default_rng(0).normal(0, 0.01, (400, 4)), 16)["n_partitions"] == 12870
    with pytest.raises(ValueError):
        D.cscv_partitions(5)


def test_pbo_is_about_one_half_for_pure_noise() -> None:
    # N par: con ruido el rango OOS del mejor IS es uniforme -> P(logit <= 0) = 1/2
    vals = [D.pbo_cscv(np.random.default_rng(seed).normal(0, 0.01, (960, 10)), 12)["pbo"] for seed in range(40)]
    assert 0.4 < float(np.mean(vals)) < 0.6


def test_pbo_low_when_one_combination_has_a_real_edge() -> None:
    rng = np.random.default_rng(3)
    m = rng.normal(0, 0.01, (1600, 8))
    m[:, 5] += 0.002  # Sharpe ~3,8 anual contra ruido
    r = D.pbo_cscv(m, 16)
    assert r["pbo"] < 0.05
    assert r["prob_oos_loss"] < 0.05
    assert r["best_is_counts"].get("5", 0) > 0.9 * r["n_partitions"]


def test_pbo_matches_sharpe_ratio_of_stats_on_each_half() -> None:
    from heatsignal.lab import stats

    m = np.random.default_rng(5).normal(0.001, 0.01, (40, 3))
    r = D.pbo_cscv(m, 2)  # 2 particiones: [bloque 0 | bloque 1] y al reves
    is0 = [stats.sharpe_ratio(m[:20, j]) for j in range(3)]
    oos0 = [stats.sharpe_ratio(m[20:, j]) for j in range(3)]
    best = int(np.argmax(is0))
    assert r["n_partitions"] == 2 and r["days"] == 40
    rank = sorted(oos0).index(oos0[best]) + 1
    w = rank / 4
    expected_first = math.log(w / (1 - w)) <= 0
    best1 = int(np.argmax(oos0))
    rank1 = sorted(is0).index(is0[best1]) + 1
    expected_second = math.log((rank1 / 4) / (1 - rank1 / 4)) <= 0
    assert r["pbo"] == pytest.approx((expected_first + expected_second) / 2)


def test_pbo_trims_oldest_rows_to_equal_blocks_and_needs_two_combos() -> None:
    r = D.pbo_cscv(np.random.default_rng(1).normal(0, 0.01, (103, 2)), 4)
    assert r["days_trimmed"] == 3 and r["days"] == 100 and r["block_days"] == 25
    with pytest.raises(ValueError):
        D.pbo_cscv(np.zeros((100, 1)), 4)


# ---------------------------------------------------------------------------- estrategia de juguete


def _toy_rule(data, p):
    """Momentum horario: signo del retorno de `lookback` horas si |ret| > `thr`; tope `max_leverage`."""
    out = {}
    for s, md in data.items():
        c = md.klines["close"].resample("1h", label="right", closed="left").last().dropna()
        r = c / c.shift(p["lookback"]) - 1
        sig = np.sign(r).where(r.abs() > p["thr"], 0.0).fillna(0.0) * p["max_leverage"]
        out[s] = sig.astype(float)
    return out


TOY = Spec("toy-diag-v1", "test", "juguete", "1h", "both", "h",
           {"lookback": 24, "thr": 0.002, "max_leverage": 1.0, "imb_pct": 0.95, "flag": True, "tf": "1h"},
           grid={"lookback": [12, 24]})


def _result(holdout_start: str, chosen=({"lookback": 24}, {"lookback": 24}, {"lookback": 12})) -> dict:
    return {"strategy_id": TOY.strategy_id, "spec_hash": TOY.spec_hash(), "symbols": ["AAAUSDT", "BBBUSDT"],
            "data": {"holdout_start": holdout_start, "holdout": "SELLADO"},
            "walk_forward": {"chosen": [{"params": c} for c in chosen]}}


@pytest.fixture(scope="module")
def market():
    return {"AAAUSDT": _md(60, 11), "BBBUSDT": _md(60, 12)}


HOLDOUT = str(T0 + pd.Timedelta(days=45))


def test_pbo_for_toy_grid_and_skip_without_grid(market) -> None:
    dev = D.development(market, pd.Timestamp(HOLDOUT))
    r = D.pbo_for(TOY, _toy_rule, dev, n_blocks=4)
    assert r["n_partitions"] == comb(4, 2) and r["n_combinations"] == 2 and 0 <= r["pbo"] <= 1
    assert r["adds_trials"] is False and set(r["combination_sharpe"]) == {"lookback=12", "lookback=24"}
    nogrid = Spec("x-v1", "t", "d", "1h", "both", "h", {"lookback": 24, "thr": 0.0, "max_leverage": 1.0})
    assert "skipped" in D.pbo_for(nogrid, _toy_rule, dev)


# ---------------------------------------------------------------------------- vecinos


def test_neighbors_respect_integers_and_ranges() -> None:
    ints = D.neighbor_values("lookback", 24)
    assert [n["value"] for n in ints] == [14, 19, 29, 34] and all(isinstance(n["value"], int) for n in ints)
    one = D.neighbor_values("skip_hours", 1)
    assert all(n["status"] == "omitido" for n in one)                 # 0,6 / 0,8 / 1,2 / 1,4 -> 1 = original
    small = D.neighbor_values("n_side", 2)
    assert [n["value"] for n in small if n["status"] != "omitido"] == [1, 3]
    pct = D.neighbor_values("imb_pct", 0.95)
    assert all(0.01 <= n["value"] <= 0.995 for n in pct)
    assert [n["status"] for n in pct] == ["ok", "ok", "recortado", "omitido"]
    win = D.neighbor_values("pct_window_hours", 720)                   # entero con "pct" en el nombre: no es percentil
    assert [n["value"] for n in win] == [432, 576, 864, 1008]
    lev = D.neighbor_values("max_leverage", 1.0)
    assert [n["status"] for n in lev] == ["ok", "ok", "omitido", "omitido"] and max(n["value"] for n in lev) == 1.0
    nbs, skipped = D.neighbors({"rsi_lo": 30.0, "rsi_hi": 70.0, "n_side": 3, "symbols": ["A"], "flag": True}, 7)
    assert set(skipped) == {"symbols", "flag"}
    bad = [n for n in nbs if n["param"] == "rsi_hi" and n["step"] == -0.4]
    assert bad[0]["status"] == "omitido" and "rsi_lo < 50 < rsi_hi" in bad[0]["reason"]
    assert [n["status"] for n in nbs if n["param"] == "n_side"][-1] == "omitido"   # 2*4 > 7 simbolos
    base = {"rsi_lo": 30.0, "rsi_hi": 70.0, "n_side": 3}
    for n in nbs:  # de a uno por vez: el resto queda fijo
        diff = [k for k in base if n["params"][k] != base[k]]
        assert diff in ([], [n["param"]])


def test_base_params_use_most_chosen_grid_value() -> None:
    assert D.base_params(TOY, _result(HOLDOUT))["lookback"] == 24
    assert D.base_params(TOY, _result(HOLDOUT, ({"lookback": 12}, {"lookback": 24})))["lookback"] == 24  # empate: ultimo
    assert D.base_params(TOY, None)["lookback"] == 24


def test_robustness_never_uses_holdout_and_never_changes_frozen_params(market, tmp_path) -> None:
    params_before, hash_before = copy.deepcopy(TOY.params), TOY.spec_hash()
    res = _result(HOLDOUT)
    clean = {s: MarketData(md.klines[md.klines.index < pd.Timestamp(HOLDOUT)], md.funding[md.funding.index < pd.Timestamp(HOLDOUT)])
             for s, md in market.items()}
    # holdout envenenado: si algo lo leyera, los Sharpe cambiarian
    poison = {}
    for s, md in market.items():
        k = md.klines.copy()
        k.loc[k.index >= pd.Timestamp(HOLDOUT), ["open", "high", "low", "close"]] *= np.linspace(1, 50, int((k.index >= pd.Timestamp(HOLDOUT)).sum()))[:, None]
        f = md.funding.copy()
        f[f.index >= pd.Timestamp(HOLDOUT)] *= 100
        poison[s] = MarketData(k, f)
    a = D.diagnose("ambos", TOY, _toy_rule, res, clean, n_blocks=4)
    b = D.diagnose("ambos", TOY, _toy_rule, res, poison, n_blocks=4)
    assert a["robustez"]["neighbors"] == b["robustez"]["neighbors"] and a["robustez"]["base"] == b["robustez"]["base"]
    assert a["pbo"]["pbo"] == b["pbo"]["pbo"] and a["pbo"]["combination_sharpe"] == b["pbo"]["combination_sharpe"]
    assert pd.Timestamp(a["pbo"]["window"][1]) <= pd.Timestamp(HOLDOUT)
    assert a["holdout"] == "NO USADO" and a["development"][1] == str(pd.Timestamp(HOLDOUT))
    with pytest.raises(D.HoldoutLeakError):
        D.assert_no_holdout(market, pd.Timestamp(HOLDOUT))
    # diagnostico, no seleccion: el spec congelado no cambia y no hay "mejor vecino"
    assert TOY.params == params_before and TOY.spec_hash() == hash_before
    rob = a["robustez"]
    assert rob["changes_frozen_params"] is False and rob["adds_trials"] is False
    assert not any("best" in k or "mejor" in k for k in rob)
    assert rob["n_neighbors"] > 0 and {"meseta", "max_drop", "base"} <= set(rob)
    ev = [n for n in rob["neighbors"] if n["status"] != "omitido"]
    assert {n["param"] for n in ev} == {"lookback", "thr", "max_leverage", "imb_pct"}
    assert all(n["sharpe_x3"] is None or n["sharpe"] is None or n["sharpe_x3"] <= n["sharpe"] + 1e-12 for n in ev)


def test_meseta_and_max_drop_definitions(market, monkeypatch) -> None:
    dev = D.development(market, pd.Timestamp(HOLDOUT))
    fake = iter([1.0, 0.9, 0.2, 0.6, -1.0])

    class _Daily:
        def __init__(self):
            self.daily_returns = pd.Series([0.0, 1.0])
            self.daily_gross_returns = pd.Series([0.0, 1.0])

    monkeypatch.setattr(D, "simulate_combo", lambda *a, **k: (None, _Daily()))
    monkeypatch.setattr(D, "_sharpes", lambda d, m: (next(fake), 0.1))
    spec = Spec("m-v1", "t", "d", "1h", "both", "h", {"lookback": 10})
    r = D.robustness(spec, _toy_rule, dev, {"lookback": 10})
    assert r["base"]["sharpe"] == 1.0 and r["n_neighbors"] == 4
    assert r["meseta"] == pytest.approx(2 / 4)           # 0,9 y 0,6 >= 0,5
    assert r["max_drop"] == pytest.approx(2.0) and r["max_drop_rel"] == pytest.approx(2.0)
    assert r["worst"]["step"] == 0.4


# ---------------------------------------------------------------------------- registro: no suma K


def test_diagnostics_go_to_separate_append_only_table_and_do_not_add_k(market, tmp_path) -> None:
    db = tmp_path / "trials.sqlite"
    reg = TrialRegistry(db)
    for lb in (12, 24):
        reg.register(TOY.strategy_id, TOY.spec_hash(), {"lookback": lb}, "train", ["AAAUSDT"], {"sharpe": 0.3})
    summary = {"strategies": [{"strategy_id": TOY.strategy_id, "sharpe": 0.5, "oos_days": 300},
                              {"strategy_id": "y", "sharpe": -0.2, "oos_days": 300}]}
    before, count_before = program(db, copy.deepcopy(summary)), reg.count()
    log = D.DiagnosticsLog(db)
    D.diagnose("ambos", TOY, _toy_rule, _result(HOLDOUT), market, n_blocks=4, log_db=log)
    assert log.count(TOY.strategy_id, "pbo") == 1 and log.count(TOY.strategy_id, "robustez") > 4
    after = program(db, copy.deepcopy(summary))
    assert after["k"] == before["k"] == 2 and after["sr0_annual"] == before["sr0_annual"]
    assert reg.count() == count_before
    con = sqlite3.connect(db)
    with pytest.raises(sqlite3.DatabaseError):
        con.execute("UPDATE lab_diagnostics SET kind = 'pbo'")
    with pytest.raises(sqlite3.DatabaseError):
        con.execute("DELETE FROM lab_diagnostics")
    with pytest.raises(sqlite3.IntegrityError):  # y lab_trials sigue sin aceptar 'robustez'
        con.execute("INSERT INTO lab_trials (strategy_id, spec_hash, params, split, symbols, metrics, created_at) "
                    "VALUES ('x', 'h', '{}', 'robustez', '[]', '{}', 'now')")
    con.close()
    log.close()
    reg.close()


def test_spec_change_is_refused(market) -> None:
    res = _result(HOLDOUT)
    res["spec_hash"] = "otro"
    with pytest.raises(ValueError, match="cambio"):
        D.diagnose("robustez", TOY, _toy_rule, res, market)


# ---------------------------------------------------------------------------- publish + panel


def _diag_files(tmp_path: Path) -> Path:
    d = tmp_path / "results" / "s1"
    d.mkdir(parents=True)
    (d / "20260101T000000.json").write_text(json.dumps({"strategy_id": "s1", "oos": {"sharpe": 0.4}, "data": {"holdout": "SELLADO"}}))
    (d / "20260102T000000.json").write_text(json.dumps({"strategy_id": "s1", "oos": {"sharpe": 0.9}, "data": {"holdout": "SELLADO"}}))
    (d / "diagnostics_20260103T000000.json").write_text(json.dumps({
        "strategy_id": "s1", "run": "20260102T000000", "generated_at": "2026-01-03", "development": ["2021-01-01", "2026-07-01"],
        "holdout": "NO USADO",
        "pbo": {"pbo": 0.62, "n_partitions": 12870, "n_blocks": 16, "block_days": 100, "n_combinations": 4,
                "degradation": {"slope": -0.4, "is_sharpe_mean": 1.1, "oos_sharpe_mean": 0.1}, "prob_oos_loss": 0.4}}))
    (d / "diagnostics_20260104T000000.json").write_text(json.dumps({
        "strategy_id": "s1", "run": "20260101T000000", "generated_at": "2026-01-04", "development": ["2021-01-01", "2026-07-01"],
        "holdout": "NO USADO",
        "robustez": {"base": {"sharpe": 1.0, "sharpe_x3": 0.4}, "base_params": {"lookback": 24}, "meseta": 0.75, "meseta_x3": 0.5,
                     "max_drop": 0.8, "max_drop_rel": 0.8, "n_neighbors": 4, "cost_mult": 3, "cost_bps_per_side": 7, "threshold": 0.5,
                     "worst": {"param": "lookback", "step": 0.4, "value": 34}, "params_not_numeric": ["tf"],
                     "neighbors": [{"param": "lookback", "step": s, "value": v, "status": "ok", "sharpe": sh, "sharpe_x3": sh - 0.5,
                                    "en_meseta": sh >= 0.5} for s, v, sh in ((-0.4, 14, 0.7), (-0.2, 19, 0.9), (0.2, 29, 0.6), (0.4, 34, 0.2))]
                     + [{"param": "skip", "step": 0.2, "value": 1, "status": "omitido", "reason": "redondea al valor original"}]}}))
    return d


def test_publish_adds_diagnostics_block_and_ignores_diag_files_as_runs(tmp_path) -> None:
    _diag_files(tmp_path)
    s = summarize(tmp_path / "results")["strategies"]
    assert len(s) == 1 and s[0]["run"] == "20260102T000000" and s[0]["runs"] == 2  # los diagnostics_ no son corridas
    dg = s[0]["diagnostics"]
    assert dg["pbo"] == 0.62 and dg["meseta"] == 0.75 and dg["sharpe_dev_x3"] == 0.4 and dg["max_drop"] == 0.8
    assert dg["stale"] is True  # la robustez es del resultado anterior
    files = write_all(tmp_path)
    det = json.loads((tmp_path / "lab_detail_s1.json").read_text())
    assert det["diagnostics"]["robustez_detail"]["neighbors"][0]["value"] == 14
    assert det["diagnostics"]["pbo_detail"]["n_partitions"] == 12870 and len(files) == 2


@pytest.mark.skipif(shutil.which("node") is None, reason="sin node")
def test_lab_view_renders_pbo_column_and_neighbors_chart(tmp_path) -> None:
    _diag_files(tmp_path)
    write_all(tmp_path)
    summary = json.loads((tmp_path / "lab_summary.json").read_text())
    det = json.loads((tmp_path / "lab_detail_s1.json").read_text())
    summary["strategies"][0]["strategy_id"] = "<img src=x onerror=alert(1)>"
    f = tmp_path / "data.json"
    f.write_text(json.dumps({"overview": summary, "detail": det}))
    out = subprocess.run(["node", str(HARNESS), str(VIEW), str(f)], capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    res = json.loads(out.stdout)
    assert "PBO" in res["overview"] and "0,62" in res["overview"] and "meseta 75 %" in res["overview"]
    assert "desactualizado" in res["overview"]
    assert "<img src=x onerror=alert(1)>" in res["overview"] and "img" not in res["tags"]
    d = res["detail"]
    assert "Diagnósticos anti-sobreajuste" in d and "Cómo leerlo" in d and "diagnóstico, no selección" in d
    assert "Robustez por parámetros vecinos" in d and "Meseta" in d and "75 %" in d and "lookback" in d
    assert "original 1,00" in d and "omitido" in d and "circle" in res["tags"]
    # sin diagnosticos: la tarjeta explica como correrlos
    det.pop("diagnostics")
    f.write_text(json.dumps({"overview": summary, "detail": det}))
    out = subprocess.run(["node", str(HARNESS), str(VIEW), str(f)], capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    assert "heatsignal.lab.diagnostics" in json.loads(out.stdout)["detail"]
