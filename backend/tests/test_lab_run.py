"""Orquestador del laboratorio con datos sinteticos: walk-forward, holdout sellado, registro de intentos."""

from __future__ import annotations

import numpy as np
import pandas as pd

from heatsignal.lab.run import RunConfig, evaluate, grid_combos, walk_forward_windows
from heatsignal.lab.strategies import CATALOG, MarketData
from heatsignal.lab.trials import TrialRegistry

T0 = pd.Timestamp("2026-01-01", tz="UTC")


def _md(n_days: int, seed: int) -> MarketData:
    n = n_days * 1440
    rng = np.random.default_rng(seed)
    c = 100 * np.exp(np.cumsum(rng.normal(0, 0.0008, n)))
    o = np.r_[100.0, c[:-1]]
    qv = rng.uniform(1e5, 2e5, n)
    k = pd.DataFrame({"open": o, "high": np.maximum(o, c) * 1.0002, "low": np.minimum(o, c) * 0.9998, "close": c,
                      "volume": qv / c, "quote_volume": qv, "count": 10, "taker_buy_volume": qv * 0.5 / c,
                      # regimenes de flujo por hora (para que la regla dispare)
                      "taker_buy_quote_volume": qv * np.repeat(rng.uniform(0.1, 0.9, n // 60 + 1), 60)[:n]},
                     index=pd.date_range(T0, periods=n, freq="1min"))
    f = pd.Series(0.0001, index=pd.date_range(T0, periods=n_days * 3, freq="8h"))
    return MarketData(k, f)


def test_windows_advance_without_overlap() -> None:
    w = list(walk_forward_windows(T0, T0 + pd.Timedelta(days=100), 30, 10))
    assert len(w) == 7 and w[0][1][0] == w[0][0][1] and w[1][1][0] == w[0][1][1]


def test_grid_combos_from_frozen_grid() -> None:
    spec, _ = CATALOG["reversion-flujo-extremo-15m-v1"]
    combos = grid_combos(spec)
    assert len(combos) == 9 and all(c["hold"] == "1h" for c in combos)


def test_evaluate_end_to_end_keeps_holdout_sealed(tmp_path) -> None:
    data = {"AAAUSDT": _md(80, 1), "BBBUSDT": _md(80, 2)}
    cfg = RunConfig("absorcion-flujo-v1", list(data), holdout_months=1, train_days=20, test_days=10, n_perm=5)
    reg = TrialRegistry(tmp_path / "trials.sqlite")
    rep = evaluate(cfg, reg, data=data)
    holdout = pd.Timestamp(rep["data"]["holdout_start"])
    oos_end = pd.Timestamp(rep["walk_forward"]["oos"][1], tz="UTC")
    assert oos_end <= holdout  # nada del holdout entra al resultado
    assert rep["walk_forward"]["windows"] >= 2 and rep["n_combinations"] == 1
    assert rep["oos"]["dsr_label"] == "PSR (sin corrección de selección)"  # K = 1: el DSR propio es el PSR
    assert set(rep["oos"]) >= {"sharpe", "dsr", "psr", "breakeven_bps_per_side", "cost_curve", "trades", "breakdown"}
    assert rep["baselines"]["random_entries"]["n"] == 5
    assert rep["oos"]["trades"] > 0
    assert set(rep["verdict"]["checks"]) == {"opera", "dsr", "breakeven", "le_gana_a_la_canasta",
                                             "le_gana_al_buy_and_hold_medio", "le_gana_al_azar"}
    # cada combinacion x ventana de train + cada ventana de test quedaron registradas
    windows = rep["walk_forward"]["windows"]
    assert reg.count("absorcion-flujo-v1", "train") == windows and reg.count("absorcion-flujo-v1", "test") == windows
    # el control (absorcion) no deberia "ganar" con datos sin senal
    assert rep["verdict"]["pasa"] is False


def test_rule_that_never_trades_is_reported_not_crashed(tmp_path) -> None:
    md = _md(80, 3)
    md.klines["taker_buy_quote_volume"] = md.klines["quote_volume"] * 0.5  # sin desbalance: nunca dispara
    cfg = RunConfig("absorcion-flujo-v1", ["AAAUSDT"], holdout_months=1, train_days=20, test_days=10, n_perm=0)
    rep = evaluate(cfg, TrialRegistry(tmp_path / "t.sqlite"), data={"AAAUSDT": md})
    assert rep["oos"]["trades"] == 0 and rep["oos"]["dsr"] is None
    assert rep["verdict"]["checks"]["opera"] is False and rep["verdict"]["pasa"] is False


def test_publish_summarizes_latest_run_per_strategy(tmp_path) -> None:
    import json

    from heatsignal.lab.publish import summarize

    d = tmp_path / "results" / "s1"
    d.mkdir(parents=True)
    (d / "20260101T000000.json").write_text(json.dumps({"strategy_id": "s1", "oos": {"sharpe": 0.1}}))
    (d / "20260102T000000.json").write_text(json.dumps({"strategy_id": "s1", "oos": {"sharpe": 0.9, "hit_rate": {"hit_rate": 0.55}},
                                                        "verdict": {"pasa": False}}))
    out = summarize(tmp_path / "results")["strategies"]
    assert len(out) == 1 and out[0]["sharpe"] == 0.9 and out[0]["runs"] == 2 and out[0]["hit_rate"] == 0.55


def test_publish_benchmark_is_not_a_strategy_and_program_bar_counts_all_trials(tmp_path) -> None:
    import json
    import sqlite3

    from heatsignal.lab.publish import summarize

    res = tmp_path / "results"
    for sid, sr in (("s1", 1.0), ("s2", -0.5)):
        (res / sid).mkdir(parents=True)
        (res / sid / "20260101T000000.json").write_text(json.dumps({"strategy_id": sid, "oos": {"sharpe": sr, "days": 1000}}))
    (res / "_benchmark_ta").mkdir()
    for name, days, sr in (("summary_20240108-20240115.json", 7, 9.0), ("summary_20210313-20260715.json", 1950, 0.4)):
        (res / "_benchmark_ta" / name).write_text(json.dumps({"start": name[8:16], "results": {
            "A": {"ok": True, "sharpe_daily": sr, "days": days}, "B": {"ok": False, "error": "x"}}}))
    (res / "_benchmark_ta" / "config_x.json").write_text("{}")
    con = sqlite3.connect(tmp_path / "trials.sqlite")
    con.execute("CREATE TABLE lab_trials (strategy_id TEXT, params TEXT, split TEXT)")
    con.executemany("INSERT INTO lab_trials VALUES (?, ?, ?)", [("s1", "a", "oos"), ("s1", "a", "oos"), ("s1", "b", "oos"),
                                                               ("s2", "a", "oos"), ("s2", "a", "holdout")])
    con.commit()
    con.close()
    out = summarize(res)
    assert [s["strategy_id"] for s in out["strategies"]] == ["s1", "s2"]  # _benchmark_ta no es una estrategia
    assert out["benchmark_ta"]["start"] == "20210313"  # la corrida mas larga, no la de prueba
    assert [r["strategy"] for r in out["benchmark_ta"]["results"]] == ["A", "B"]
    assert out["program"]["k"] == 4 and out["program"]["k_lab"] == 3 and out["program"]["k_benchmark"] == 1
    assert out["program"]["sr0_annual"] > 0
    assert 0 <= out["strategies"][1]["dsr_program"] < out["strategies"][0]["dsr_program"] <= 1


def test_report_carries_daily_equity_curves_without_changing_metrics(tmp_path) -> None:
    data = {"AAAUSDT": _md(80, 1), "BBBUSDT": _md(80, 2)}
    cfg = RunConfig("absorcion-flujo-v1", list(data), holdout_months=1, train_days=20, test_days=10, n_perm=0)
    rep = evaluate(cfg, TrialRegistry(tmp_path / "t.sqlite"), data=data)
    eq = rep["oos"]["equity_daily"]
    assert eq and all(len(p) == 2 and isinstance(p[0], str) for p in eq)
    assert abs(eq[-1][1] - rep["oos"]["total_return"]) < 1e-5  # misma convencion que total_return (suma)
    assert [p[0] for p in eq] == sorted(p[0] for p in eq)
    beq = rep["baselines"]["equity_daily"]
    assert set(beq["buy_and_hold"]) == set(data) and beq["basket"] and beq["buy_and_hold_mean"]
    assert eq[0][0] >= rep["walk_forward"]["oos"][0]


def test_publish_writes_detail_per_strategy(tmp_path) -> None:
    import json

    from heatsignal.lab.publish import cost_curve_rows, write_all

    d = tmp_path / "results" / "s1-v1"
    d.mkdir(parents=True)
    old = {"strategy_id": "s1-v1", "oos": {"sharpe": 0.1}}
    new = {"strategy_id": "s1-v1", "spec_hash": "h", "n_combinations": 3,
           "walk_forward": {"train_days": 180, "test_days": 30, "windows": 2, "oos": ["2021-01-01", "2021-03-01"],
                            "chosen": [{"train": ["a", "b"], "test": ["b", "c"], "params": {"k": 1}, "train_sharpe": 1.2}]},
           "oos": {"sharpe": float("nan"), "breakeven_bps_per_side": 21.0,
                   "cost_curve": {"cost_bps": {"0": 0.0, "1": 7.0}, "sharpe": {"0": 1.0, "1": 0.8},
                                  "ann_return": {"0": 0.1, "1": 0.08}},
                   "breakdown": {"precio": 1.0, "total": 0.9}, "equity_daily": [["2021-01-01", 0.01]]},
           "baselines": {"basket_sharpe": 0.5, "equity_daily": {"basket": [["2021-01-01", 0.0]],
                                                                "buy_and_hold_mean": [], "buy_and_hold": {}}},
           "verdict": {"pasa": False, "checks": {"dsr": False}}}
    (d / "20260101T000000.json").write_text(json.dumps(old))
    (d / "20260102T000000.json").write_text(json.dumps(new))
    (tmp_path / "results" / "viejo").mkdir()
    (tmp_path / "results" / "viejo" / "1.json").write_text(json.dumps({"strategy_id": "viejo", "oos": {}}))
    files = write_all(tmp_path)
    assert {f.name for f in files} == {"lab_summary.json", "lab_detail_s1-v1.json", "lab_detail_viejo.json"}
    det = json.loads((tmp_path / "lab_detail_s1-v1.json").read_text())  # JSON estricto: sin NaN
    assert det["oos"]["sharpe"] is None and det["runs"] == ["20260101T000000", "20260102T000000"]
    assert det["costs"]["cost_curve"] == [{"cost_bps": 0.0, "sharpe": 1.0, "ann_return": 0.1},
                                          {"cost_bps": 7.0, "sharpe": 0.8, "ann_return": 0.08}]
    assert det["costs"]["breakeven_vs_expected"] == 3.0 and det["walk_forward"]["chosen"][0]["params"] == {"k": 1}
    assert det["equity_daily"]["strategy"] == [["2021-01-01", 0.01]] and det["equity_daily"]["basket"]
    viejo = json.loads((tmp_path / "lab_detail_viejo.json").read_text())
    assert viejo["equity_daily"] is None and "volver a correr" in viejo["notes"][0]
    assert cost_curve_rows(None) is None and cost_curve_rows([{"cost_bps": 1}]) == [{"cost_bps": 1}]


def test_publish_upload_sends_every_file_to_the_vm_data_dir(tmp_path, monkeypatch) -> None:
    import json

    from heatsignal.lab import publish

    d = tmp_path / "data"
    (tmp_path / "results" / "s1-v1").mkdir(parents=True)
    (tmp_path / "results" / "s1-v1" / "1.json").write_text(json.dumps({"strategy_id": "s1-v1", "oos": {}}))
    monkeypatch.setenv("HEATSIGNAL_LAB_DIR", str(d))
    calls = []
    monkeypatch.setattr(publish.subprocess, "run", lambda cmd, **kw: calls.append(cmd))
    assert publish.main(["--upload"]) == 0
    cmd = calls[0]
    assert cmd[:3] == ["gcloud", "compute", "scp"] and cmd[-1] == f"{publish.VM_HOST}:~/heatsignal/data/"
    assert {p.rsplit("/", 1)[-1] for p in cmd if p.endswith(".json")} == {"lab_summary.json", "lab_detail_s1-v1.json"}


def test_holdout_opens_once_and_uses_only_holdout_window(tmp_path) -> None:
    import pytest

    from heatsignal.lab.run import open_holdout
    from heatsignal.lab.trials import HoldoutAlreadyOpenedError

    data = {"AAAUSDT": _md(80, 1), "BBBUSDT": _md(80, 2)}
    cfg = RunConfig("absorcion-flujo-v1", list(data), holdout_months=1, train_days=20, test_days=10, n_perm=3)
    reg = TrialRegistry(tmp_path / "trials.sqlite")
    rep = open_holdout(cfg, reg, data=data)
    start = pd.Timestamp(rep["window"][0])
    assert rep["split"] == "holdout" and rep["days"] <= 31
    assert all(pd.Timestamp(d).date() >= start.date() for d, _ in rep["holdout"]["equity_daily"])
    assert set(rep["verdict"]["checks"]) == {"retorno_neto_positivo", "le_gana_a_la_canasta", "le_gana_al_azar"}
    assert reg.count("absorcion-flujo-v1", "holdout") == 1
    with pytest.raises(HoldoutAlreadyOpenedError):
        open_holdout(cfg, reg, data=data)  # una sola vez
