"""Liston del programa (regla del 2026-10-19): tres metodos, estados por intento, publicacion y panel.

Logica estadistica (no negociable): el bootstrap de Sharpe maximo da p ~ uniforme con ruido, detecta una ventaja
sintetica y Romano-Wolf la identifica; la correlacion baja el K efectivo; la dispersion nula analitica coincide con la
simulacion; asimetria y curtosis mueven el DSR en la direccion correcta; los estados son append-only y un invalido
sale de la dispersion y de la matriz pero sigue en K; el holdout no se usa.
"""

from __future__ import annotations

import json
import math
import shutil
import sqlite3
import subprocess
from pathlib import Path

import numpy as np
import pytest

from heatsignal.lab import program as P
from heatsignal.lab import stats
from heatsignal.lab.publish import PSR_LABEL, summarize, write_all
from heatsignal.lab.run import dsr_label
from heatsignal.lab.trials import TrialRegistry, main as trials_main, read_statuses

VIEW = Path(__file__).parents[1] / "src/heatsignal/api/static/app/views/laboratorio.js"
HARNESS = Path(__file__).parent / "js" / "render_view.mjs"


# ---------------------------------------------------------------------------- metodo (iii): bootstrap


def test_bootstrap_max_sharpe_p_is_about_uniform_under_pure_noise() -> None:
    # 200 matrices de ruido puro (con colas y correlacion cruzada): la fraccion con p <= 0,05 ~ 0,05
    ps = []
    for seed in range(200):
        rng = np.random.default_rng(1000 + seed)
        common = rng.standard_t(5, (300, 1))
        x = 0.01 * (0.5 * common + rng.standard_t(5, (300, 3)))
        ps.append(P.max_sharpe_bootstrap(x, block_len=5, n_boot=199, seed=seed)["p_max"])
    ps = np.asarray(ps)
    frac = float(np.mean(ps <= 0.05))
    assert 0.02 <= frac <= 0.10, frac
    assert 0.35 < float(np.mean(ps)) < 0.65  # centrada (uniforme -> media 1/2)


def test_bootstrap_detects_synthetic_edge_and_romano_wolf_identifies_it() -> None:
    rng = np.random.default_rng(7)
    x = rng.normal(0, 0.01, (1500, 6))
    x[:, 4] += 0.0015  # Sharpe ~2,9 anual
    res = P.max_sharpe_bootstrap(x, block_len=20, n_boot=999, seed=1)
    assert res["argmax"] == 4 and res["p_max"] <= 0.01
    p_adj = np.asarray(res["p_adjusted"])
    assert p_adj[4] <= 0.05
    assert all(p_adj[j] > 0.05 for j in range(6) if j != 4)
    assert p_adj[4] == res["p_max"]  # el primer paso del escalonado es el test del maximo


def test_romano_wolf_is_monotone_and_never_below_single_step_order() -> None:
    rng = np.random.default_rng(3)
    boot = rng.normal(0, 1, (2000, 5))
    obs = np.array([3.0, 2.5, 0.1, -1.0, 2.6])
    p = P.romano_wolf_stepdown(obs, boot)
    order = np.argsort(-obs)
    assert all(p[order[i]] <= p[order[i + 1]] for i in range(4))  # monotono en el orden de los observados
    single = [(1 + np.sum(boot.max(axis=1) >= o)) / 2001 for o in obs]
    assert all(p[i] <= single[i] + 1e-12 for i in range(5))  # el escalonado nunca es peor que el de un paso


def test_bootstrap_is_reproducible_with_fixed_seed() -> None:
    x = np.random.default_rng(0).normal(0, 0.01, (200, 3))
    a = P.max_sharpe_bootstrap(x, n_boot=99, seed=5)
    b = P.max_sharpe_bootstrap(x, n_boot=99, seed=5)
    assert a["p_max"] == b["p_max"] and a["p_adjusted"] == b["p_adjusted"]
    assert P.N_BOOT == 5000 and P.BLOCK_LEN == 20  # parametros de la regla, sin cambios


# ---------------------------------------------------------------------------- metodo (ii): DSR corregido


def test_correlated_columns_reduce_effective_k() -> None:
    rng = np.random.default_rng(1)
    indep = rng.normal(0, 1, (2000, 6))
    base = rng.normal(0, 1, (2000, 1))
    corr_cols = base + 0.2 * rng.normal(0, 1, (2000, 6))
    k_ind = P.effective_k(P.corr_matrix(indep), [1] * 6, 0, 0)["k_eff"]
    k_cor = P.effective_k(P.corr_matrix(corr_cols), [1] * 6, 0, 0)["k_eff"]
    assert k_ind > 5.8 and k_cor < 2.0
    # Freqtrade y no validas suman como independientes; combinaciones extra (aproximacion conservadora) tambien
    kk = P.effective_k(P.corr_matrix(corr_cols), [1, 3, 1, 1, 1, 1], excluded_combos=2, n_benchmark=7)
    assert kk["k_eff"] == pytest.approx(kk["k_eff_estrategias"] + 2 + 2 + 7)
    assert kk["k_eff_multiplicativa"] <= kk["k_eff"]  # la aditiva es la conservadora


def test_analytic_null_dispersion_matches_simulation_for_normal_returns() -> None:
    t = 500
    rng = np.random.default_rng(11)
    x = rng.normal(0, 0.01, (4000, t))
    sr = x.mean(axis=1) / x.std(axis=1, ddof=1)
    assert float(sr.var()) == pytest.approx(P.null_sr_variance(t), rel=0.08)
    assert P.null_sr_variance(t, skew=-2.0, kurt=12.0) == pytest.approx(1 / t)  # con SR = 0 los momentos se anulan
    assert P.null_sr_variance(t, skew=-2.0, kurt=12.0, sr=0.1) > 1 / t


def test_skew_and_kurtosis_move_the_dsr_in_the_right_direction() -> None:
    rng = np.random.default_rng(5)
    n = 1500
    normal = rng.normal(0.0012, 0.01, n)
    # misma media y desvio aproximados, pero ganancias chicas frecuentes y perdidas grandes raras (asimetria < 0)
    neg = np.where(rng.random(n) < 0.03, -0.045, 0.0026) + rng.normal(0, 0.004, n)
    pos = -neg + 2 * neg.mean()
    d_neg, d_pos = P.dsr_corrected(neg, k_eff=1.0), P.dsr_corrected(pos, k_eff=1.0)
    assert d_neg["skew"] < -1 and d_pos["skew"] > 1 and d_neg["kurt"] > 4
    assert d_neg["dsr"] < P.dsr_corrected(neg, 1.0, use_moments=False)["dsr"]   # asimetria negativa baja el DSR
    assert d_pos["dsr"] > P.dsr_corrected(pos, 1.0, use_moments=False)["dsr"]   # positiva lo sube
    # curtosis sola (asimetria 0) con SR > SR0: colas gordas bajan el DSR
    sr = 0.05
    assert stats.deflated_sharpe_ratio(sr, n, 1.0, 1 / n, 0.0, 10.0) < stats.deflated_sharpe_ratio(sr, n, 1.0, 1 / n, 0.0, 3.0)
    # y la normal da casi lo mismo con o sin momentos
    assert P.dsr_corrected(normal, 5.0)["dsr"] == pytest.approx(P.dsr_corrected(normal, 5.0, use_moments=False)["dsr"], abs=0.02)


# ---------------------------------------------------------------------------- laboratorio falso


HOLDOUT = "2024-12-01T00:00:00+00:00"


def _curve(r: np.ndarray, start: str = "2024-01-01") -> list[list]:
    import datetime as dt

    d0 = dt.date.fromisoformat(start)
    return [[str(d0 + dt.timedelta(days=i)), round(float(v), 6)] for i, v in enumerate(np.cumsum(r))]


def _result(sid: str, r: np.ndarray, n_comb: int = 1, start: str = "2024-01-01", checks_ok: bool = True) -> dict:
    eq = _curve(r, start)
    checks = {"opera": True, "dsr": False, "breakeven": checks_ok, "le_gana_a_la_canasta": checks_ok,
              "le_gana_al_buy_and_hold_medio": checks_ok, "le_gana_al_azar": checks_ok}
    return {"strategy_id": sid, "spec_hash": "h-" + sid, "symbols": ["AAAUSDT"], "n_combinations": n_comb,
            "data": {"holdout_start": HOLDOUT, "holdout": "SELLADO"},
            "walk_forward": {"oos": [eq[0][0], eq[-1][0]], "windows": 10},
            "oos": {"sharpe": stats.sharpe_ratio(r), "days": len(r), "dsr": 0.5, "psr": 0.6, "equity_daily": eq, "trades": 50},
            "baselines": {"basket_sharpe": 0.1, "random_entries": {"p_value": 0.01}},
            "verdict": {"pasa": False, "checks": checks, "criterios": {"dsr_min": 0.95}}}


@pytest.fixture
def lab(tmp_path: Path) -> Path:
    rng = np.random.default_rng(42)
    t = 320
    common = rng.normal(0, 0.008, t)
    series = {
        "edge-v1": rng.normal(0.004, 0.01, t),                 # ventaja fuerte (Sharpe ~7)
        "ruido-a-v1": rng.normal(0, 0.01, t),
        "ruido-b-v1": common + rng.normal(0, 0.002, t),
        "ruido-c-v1": common + rng.normal(0, 0.002, t),        # muy correlacionada con b
        "rota-v1": rng.normal(-0.02, 0.05, t),                 # se marca invalida
    }
    combos = {"edge-v1": 1, "ruido-a-v1": 3, "ruido-b-v1": 1, "ruido-c-v1": 1, "rota-v1": 2}
    res = tmp_path / "results"
    reg = TrialRegistry(tmp_path / "trials.sqlite")
    for sid, r in series.items():
        d = res / sid
        d.mkdir(parents=True)
        (d / "20260101T000000.json").write_text(json.dumps(_result(sid, r, combos[sid])))
        for i in range(combos[sid]):
            reg.register(sid, "h-" + sid, {"n": i}, "test", ["AAAUSDT"], {"sharpe": 0.1}, "2024-01-01", "2024-02-01")
    # el holdout de edge esta "abierto" con basura: no tiene que entrar en ningun calculo
    (res / "edge-v1" / "holdout").mkdir()
    (res / "edge-v1" / "holdout" / "20260201T000000.json").write_text(json.dumps(
        {"window": [HOLDOUT, "2025-03-01"], "holdout": {"sharpe": 99.0, "equity_daily": [["2025-01-01", 50.0]]}}))
    reg.register("edge-v1", "h-edge-v1", {"n": 0}, "holdout", ["AAAUSDT"], {"sharpe": 99.0}, HOLDOUT, "2025-03-01")
    reg.close()
    bench = res / "_benchmark_ta"
    bench.mkdir()
    (bench / "summary_x.json").write_text(json.dumps({"start": "2024-01-01", "end": "2024-11-15", "results": {
        "FtA": {"ok": True, "sharpe_daily": 0.4, "days": 320}, "FtB": {"ok": True, "sharpe_daily": -0.2, "days": 320},
        "FtC": {"ok": False, "error": "x", "days": 0}}}))
    return tmp_path


def test_compute_three_methods_on_fake_lab(lab: Path) -> None:
    rep = P.compute(lab, n_boot=499)
    assert rep["k_real"] == 8 + 2 and rep["k_lab"] == 8  # combinaciones registradas (sin holdout) + Freqtrade OK
    assert rep["metodo_i"]["k"] == 10
    assert rep["metodo_iii"]["estrategia_max"] == "edge-v1" and rep["metodo_iii"]["p_max"] <= 0.01
    assert rep["metodo_iii"]["p_ajustado"]["edge-v1"] <= 0.05
    assert "edge-v1" in rep["pasan"]["iii"] and "edge-v1" in rep["pasan"]["ii"] and "edge-v1" in rep["pasan"]["programa"]
    m2 = rep["metodo_ii"]
    assert m2["k_eff"] <= rep["k_real"] and m2["k_eff_estrategias"] < 5
    assert m2["dispersiones"]["edge-v1"]["var_null_period"] == pytest.approx(1 / 320)
    assert set(rep["pasan"]) == {"i", "ii", "iii", "programa"} and rep["holdout"] == "NO USADO"


def test_trial_status_is_append_only_and_invalid_leaves_matrix_and_dispersion_but_not_k(lab: Path) -> None:
    db = lab / "trials.sqlite"
    before = P.compute(lab, n_boot=199)
    assert before["estados"]["rota-v1"]["status"] == "valido"  # por defecto, todo es valido
    assert "rota-v1" in before["alineacion"]["estrategias"]
    reg = TrialRegistry(db)
    with pytest.raises(ValueError, match="motivo"):
        reg.mark("rota-v1", "invalido", "  ")
    with pytest.raises(ValueError):
        reg.mark("no-existe-v1", "invalido", "typo")
    with pytest.raises(ValueError):
        reg.mark("rota-v1", "roto", "estado desconocido")
    eid = reg.mark("rota-v1", "invalido", "bug en el simulador")
    con = sqlite3.connect(db)
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        con.execute("UPDATE lab_trial_status SET status = 'valido' WHERE id = ?", (eid,))
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        con.execute("DELETE FROM lab_trial_status")
    con.close()
    reg.close()
    after = P.compute(lab, n_boot=199)
    assert after["estados"]["rota-v1"] == {"status": "invalido", "motivo": "bug en el simulador", "fuente": "registro",
                                           "n_combinations": 2, "run": "20260101T000000"}
    assert "rota-v1" not in after["alineacion"]["estrategias"]                    # fuera de la matriz
    assert "rota-v1" not in after["metodo_iii"]["p_ajustado"]
    assert "rota-v1" not in after["metodo_ii"]["dispersiones"]                    # fuera de la dispersion (ii)
    assert after["metodo_i"]["excluidas"] == ["rota-v1"]                          # y de la varianza del metodo (i)
    assert after["metodo_i"]["n_sharpes"] == before["metodo_i"]["n_sharpes"] - 1
    assert after["k_real"] == before["k_real"] == 10 and after["metodo_i"]["k"] == 10  # sigue en el conteo real
    assert after["metodo_ii"]["k_no_validas"] == 2                                 # y suma a K efectivo como independiente
    # volver a valido es OTRO evento; el ultimo manda
    reg = TrialRegistry(db)
    reg.mark("rota-v1", "valido", "era un falso positivo")
    assert [e.status for e in reg.status_events("rota-v1")] == ["invalido", "valido"]
    assert reg.statuses()["rota-v1"]["status"] == "valido"
    # por combinacion: una sola combinacion invalida alcanza para sacar a la estrategia de la matriz
    reg.mark("ruido-a-v1", "sin_resultado", "la corrida de esa combinacion se corto", params={"n": 1})
    st = reg.statuses()["ruido-a-v1"]
    assert st["status"] == "sin_resultado" and st["combinations"]['{"n": 0}']["status"] == "valido"
    reg.close()
    assert read_statuses(db)["ruido-a-v1"]["status"] == "sin_resultado"


def test_trials_cli_requires_reason(lab: Path, capsys) -> None:
    db = str(lab / "trials.sqlite")
    with pytest.raises(SystemExit):
        trials_main(["--registry", db, "marcar", "rota-v1", "--estado", "invalido"])
    with pytest.raises(SystemExit):
        trials_main(["--registry", db, "marcar", "rota-v1", "--estado", "invalido", "--motivo", ""])
    with pytest.raises(SystemExit):
        trials_main(["--registry", db, "marcar", "rota-v1", "--estado", "invalido", "--motivo", "   "])
    assert trials_main(["--registry", db, "marcar", "rota-v1", "--estado", "invalido", "--motivo", "datos rotos"]) == 0
    assert trials_main(["--registry", db, "estados"]) == 0
    out = capsys.readouterr().out
    assert "rota-v1" in out and "invalido" in out and "datos rotos" in out


def test_holdout_is_never_used(lab: Path) -> None:
    rep = P.compute(lab, n_boot=99)
    assert rep["alineacion"]["hasta"] < HOLDOUT[:10]
    assert rep["metodo_iii"]["sharpe_observado"]["edge-v1"] < 20  # el Sharpe 99 del holdout no entra
    # una curva que pisa el holdout hace fallar el calculo (no se publica un liston contaminado)
    r = np.random.default_rng(0).normal(0, 0.01, 400)  # 2024-01-01 + 400 dias > 2024-12-01
    (lab / "results" / "ruido-a-v1" / "20260301T000000.json").write_text(json.dumps(_result("ruido-a-v1", r, 3)))
    with pytest.raises(P.HoldoutLeakError):
        P.compute(lab, n_boot=99)


def test_alignment_intersection_vs_union() -> None:
    curves = {"a": {"dates": ["d1", "d2", "d3"], "returns": np.array([1.0, 2.0, 3.0])},
              "b": {"dates": ["d2", "d3", "d4"], "returns": np.array([5.0, 6.0, 7.0])}}
    d, x = P.align(curves, ["a", "b"])
    assert d == ["d2", "d3"] and x.tolist() == [[2.0, 5.0], [3.0, 6.0]]
    d, x = P.align(curves, ["a", "b"], how="union")
    assert d == ["d1", "d2", "d3", "d4"] and x[0].tolist() == [1.0, 0.0]
    dates, ret = P.returns_from_equity([["2024-01-01", 0.01], ["2024-01-02", 0.03], ["2024-01-03", 0.02]])
    assert np.allclose(ret, [0.01, 0.02, -0.01])


# ---------------------------------------------------------------------------- publicacion y panel


def _write_program(lab: Path) -> dict:
    rep = P.compute(lab, n_boot=299)
    out = lab / "results" / P.PROGRAM_DIR
    out.mkdir(exist_ok=True)
    (out / "program_20260102T000000.json").write_text(json.dumps(P._clean(rep)))
    return rep


def test_publish_carries_three_methods_and_veredicto_programa(lab: Path) -> None:
    s = summarize(lab / "results")
    assert {x["strategy_id"] for x in s["strategies"]} == {"edge-v1", "ruido-a-v1", "ruido-b-v1", "ruido-c-v1", "rota-v1"}
    assert s["program"]["metodos"] is None  # todavia no se corrio lab.program
    edge = next(x for x in s["strategies"] if x["strategy_id"] == "edge-v1")
    assert edge["veredicto_programa"]["pasa"] is None and edge["veredicto_programa"]["estado"] == "sin calcular"
    assert edge["dsr_label"] == PSR_LABEL == "PSR (sin corrección de selección)" == dsr_label(1)
    assert next(x for x in s["strategies"] if x["strategy_id"] == "ruido-a-v1")["dsr_label"] == "DSR"

    _write_program(lab)
    files = write_all(lab)
    summary = json.loads((lab / "lab_summary.json").read_text())
    m = summary["program"]["metodos"]
    assert m["k_real"] == 10 and set(m) >= {"i", "ii", "iii", "pasan", "criterio", "estados"} and m["stale"] is False
    assert m["iii"]["n_boot"] == 299 and m["ii"]["k_eff"] <= 10
    rows = {x["strategy_id"]: x for x in summary["strategies"]}
    e = rows["edge-v1"]
    assert e["p_boot_adj"] <= 0.05 and e["dsr_corr"] >= 0.95
    assert e["veredicto_programa"]["pasa"] is True
    assert "dsr" not in e["veredicto_programa"]["checks"] and e["veredicto_programa"]["checks"]["p_bootstrap_ajustado"]
    assert e["verdict"]["checks"]["dsr"] is False  # el veredicto viejo del JSON no se toca
    assert rows["ruido-a-v1"]["veredicto_programa"]["pasa"] is False
    # los JSON de resultados no se reescribieron
    raw = json.loads((lab / "results" / "edge-v1" / "20260101T000000.json").read_text())
    assert "veredicto_programa" not in raw and raw["oos"]["dsr"] == 0.5
    det = json.loads((lab / "lab_detail_edge-v1.json").read_text())
    assert det["programa"]["veredicto_programa"]["pasa"] is True and len(files) == 6
    # un resultado nuevo despues del liston lo deja desactualizado
    r = np.random.default_rng(1).normal(0, 0.01, 300)
    (lab / "results" / "ruido-b-v1" / "20260105T000000.json").write_text(json.dumps(_result("ruido-b-v1", r)))
    s = summarize(lab / "results")
    assert s["program"]["metodos"]["stale"] is True and s["program"]["metodos"]["stale_strategies"] == ["ruido-b-v1"]
    assert next(x for x in s["strategies"] if x["strategy_id"] == "ruido-b-v1")["veredicto_programa"]["estado"] == "desactualizado"


def test_publish_and_api_need_no_pandas_for_the_program_block() -> None:
    src = (Path(__file__).parents[1] / "src/heatsignal/lab/publish.py").read_text()
    head = src.split("def ")[0]
    assert "import pandas" not in head and "from .program" not in head
    for f in (Path(__file__).parents[1] / "src/heatsignal/api").glob("*.py"):
        assert "import pandas" not in f.read_text(), f


@pytest.mark.skipif(shutil.which("node") is None, reason="sin node")
def test_lab_view_renders_program_card_columns_and_chip(lab: Path, tmp_path: Path) -> None:
    _write_program(lab)
    write_all(lab)
    summary = json.loads((lab / "lab_summary.json").read_text())
    det = json.loads((lab / "lab_detail_edge-v1.json").read_text())
    f = tmp_path / "data.json"
    f.write_text(json.dumps({"overview": summary, "detail": det}))
    out = subprocess.run(["node", str(HARNESS), str(VIEW), str(f)], capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    res = json.loads(out.stdout)
    ov = res["overview"]
    assert "Listón del programa" in ov
    assert "(i) DSR del programa" in ov and "(ii) DSR corregido" in ov and "(iii) Bootstrap" in ov
    assert "p bootstrap (ajustado)" in ov and "DSR corregido" in ov
    assert "pasa el listón" in ov and "no pasa el listón" in ov
    assert "K real 10" in ov and "PSR (sin corrección de selección)" in ov
    assert "Listón del programa" in res["detail"] and "pasa el listón" in res["detail"]
    # sin liston calculado: la tarjeta explica como correrlo
    summary["program"]["metodos"] = None
    for s in summary["strategies"]:
        s["veredicto_programa"] = {"pasa": None, "estado": "sin calcular", "checks": {}}
    f.write_text(json.dumps({"overview": summary, "detail": det}))
    out = subprocess.run(["node", str(HARNESS), str(VIEW), str(f)], capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    assert "heatsignal.lab.program" in json.loads(out.stdout)["overview"]
