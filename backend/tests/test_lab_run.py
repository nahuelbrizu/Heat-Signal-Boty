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
