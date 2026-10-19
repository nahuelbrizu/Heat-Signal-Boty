"""Marco de validacion del meta-modelo (etapa 3, subtarea A). Todo con datos sinteticos: nada del dataset real."""

from __future__ import annotations

import sqlite3

import numpy as np
import pandas as pd
import pytest
from sklearn.linear_model import LogisticRegression

from heatsignal.lab.meta import validation as v
from heatsignal.lab.meta.dataset import uniqueness_weights

PRIMS = ("prim-a", "prim-b", "prim-c")
SYMS = ("BTCUSDT", "ETHUSDT")


def synth(n_days: int = 900, signal: float = 0.0, seed: int = 1) -> pd.DataFrame:
    """Un evento por primaria y moneda cada ~2 dias, vida de 1 a 8 dias. pnl = signal * x + ruido."""
    rng = np.random.default_rng(seed)
    rows = []
    start = pd.Timestamp("2022-01-01", tz="UTC")
    for prim in PRIMS:
        for sym in SYMS:
            t = start + pd.Timedelta(hours=int(rng.integers(0, 48)))
            while t < start + pd.Timedelta(days=n_days):
                x = rng.normal()
                pnl = signal * 0.02 * x + rng.normal(0, 0.02)
                rows.append({"primaria": prim, "symbol": sym, "t_decision": t,
                             "t_salida": t + pd.Timedelta(hours=int(rng.integers(24, 192))), "tamaño": 1.0,
                             "x": x, "ruido": rng.normal(), "pnl_neto": pnl, "etiqueta": int(pnl > 0)})
                t += pd.Timedelta(hours=int(rng.integers(24, 72)))
    ev = pd.DataFrame(rows).sort_values("t_decision").reset_index(drop=True)
    ev["peso_unicidad"] = uniqueness_weights(ev["t_decision"], ev["t_salida"])
    return ev


def logit() -> LogisticRegression:
    return LogisticRegression(C=1.0)


# ---- purged k-fold ---------------------------------------------------------------------------------------


def test_prereg_values() -> None:
    assert v.K_FOLDS == 5 and v.EMBARGO == pd.Timedelta(days=5) and v.BUDGET == 24 and v.THRESHOLD == 0.5


def test_folds_contiguous_and_cover_everything() -> None:
    ev = synth(400)
    folds = v.purged_kfold(ev["t_decision"], ev["t_salida"])
    assert len(folds) == 5
    allt = np.concatenate([f.test for f in folds])
    assert sorted(allt.tolist()) == list(range(len(ev)))  # cubren todo, sin repetir
    t0 = ev["t_decision"].to_numpy()
    for a, b in zip(folds, folds[1:]):
        assert t0[a.test].max() < t0[b.test].min()  # contiguos y en orden temporal
    for f in folds:  # contiguo: nada de otro bloque cae entre su primer y ultimo t_decision
        lo, hi = t0[f.test].min(), t0[f.test].max()
        inside = np.flatnonzero((t0 >= lo) & (t0 <= hi))
        assert set(inside) == set(f.test)


def test_ties_stay_in_same_fold() -> None:
    t = pd.date_range("2023-01-01", periods=50, freq="D", tz="UTC").repeat(4)  # 4 monedas a la vez
    folds = v.purged_kfold(t, t + pd.Timedelta(days=1))
    for f in folds:
        days = set(t[f.test])
        others = np.setdiff1d(np.arange(len(t)), f.test)
        assert not days & set(t[others])


def test_no_train_overlaps_test_and_embargo() -> None:
    ev = synth(400)
    t0, t1 = ev["t_decision"].to_numpy(), ev["t_salida"].to_numpy()
    emb = np.timedelta64(5, "D")
    for f in v.purged_kfold(ev["t_decision"], ev["t_salida"]):
        lo, hi = t0[f.test].min(), t1[f.test].max()
        tr0, tr1 = t0[f.train], t1[f.train]
        assert not np.any((tr0 <= hi) & (tr1 >= lo))  # ninguna vida de train toca la ventana de test
        assert not np.any((tr0 > hi) & (tr0 <= hi + emb))  # embargo
        assert not np.intersect1d(f.train, f.test).size
        # y no se saca de mas: todo lo que no solapa ni esta embargado queda en train
        rest = np.setdiff1d(np.arange(len(ev)), f.test)
        keep = rest[~(((t0[rest] <= hi) & (t1[rest] >= lo)) | ((t0[rest] > hi) & (t0[rest] <= hi + emb)))]
        assert set(keep) == set(f.train)


def test_overlap_is_across_primarias_and_symbols() -> None:
    t = pd.Timestamp("2023-01-01", tz="UTC")
    d = pd.Timedelta(days=1)
    # evento largo de otra moneda/primaria que cruza todo el bloque del medio: tiene que salir del train
    t0 = pd.DatetimeIndex([t + i * 10 * d for i in range(10)] + [t + 35 * d])
    t1 = pd.DatetimeIndex([x + d for x in t0[:10]] + [t + 60 * d])
    folds = v.purged_kfold(t0, t1)
    long_ev = 10
    for f in folds:
        lo, hi = f.test_start, f.test_end
        if long_ev not in f.test and t0[long_ev] <= hi and t1[long_ev] >= lo:
            assert long_ev not in f.train


# ---- calibracion -----------------------------------------------------------------------------------------


def test_calibration_method_threshold() -> None:
    assert v.calibration_method(999) == "sigmoid"
    assert v.calibration_method(1000) == "isotonic"
    rng = np.random.default_rng(0)
    for n, want in ((600, "sigmoid"), (1200, "isotonic")):
        X = rng.normal(size=(n, 2))
        y = (X[:, 0] + rng.normal(size=n) > 0).astype(int)
        m = v.FoldCalibrated(logit()).fit(X, y)
        assert m.method_ == want
        p = m.predict_proba(X)[:, 1]
        assert p.shape == (n,) and np.all((p >= 0) & (p <= 1))


def test_oos_uses_method_by_fold_size() -> None:
    ev = synth(900)  # ~ 6 x 450 eventos -> trains de > 1000
    oos = v.oos_predictions(ev, ["x"], logit)
    assert set(oos["calibracion"]) == {"isotonic"}
    small = synth(200)
    oos2 = v.oos_predictions(small, ["x"], logit)
    assert set(oos2["calibracion"]) == {"sigmoid"}


# ---- metricas: ruido vs senal ----------------------------------------------------------------------------


def test_noise_gives_auc_half_and_no_significant_improvement() -> None:
    ev = synth(900, signal=0.0, seed=3)
    m = v.evaluate(ev, v.MetaConfig("logistica", ("x", "ruido")), logit, n_boot=500)
    st = m["estadisticas"]
    assert abs(st["auc"] - 0.5) < 0.05
    assert st["brier"] >= st["brier_base"] - 0.002  # no le gana a la tasa base
    assert m["economicas"]["_conjunto"]["p_mejora_filtrada"] > 0.05


def test_signal_is_detected_and_filter_improves_sharpe() -> None:
    ev = synth(900, signal=1.0, seed=4)
    m = v.evaluate(ev, v.MetaConfig("logistica", ("x",)), logit, n_boot=500)
    st, ec = m["estadisticas"], m["economicas"]
    assert st["auc"] > 0.6
    assert st["brier"] < st["brier_base"] and st["logloss"] < st["logloss_base"]
    assert len(st["calibracion_deciles"]) == 10
    c = ec["_conjunto"]
    assert c["filtrada"]["sharpe"] > c["sin_filtrar"]["sharpe"]
    assert c["p_mejora_filtrada"] < 0.05
    for prim in PRIMS:
        assert ec[prim]["filtrada"]["sharpe"] > ec[prim]["sin_filtrar"]["sharpe"]
        assert ec[prim]["filtrada"]["n_operaciones"] < ec[prim]["sin_filtrar"]["n_operaciones"]


def test_filtered_with_p_one_equals_unfiltered() -> None:
    ev = synth(300, signal=0.5, seed=5)
    ev["p"] = 1.0
    ec = v.economic_metrics(ev, n_boot=50)
    for key in (*PRIMS, "_conjunto"):
        f, u = ec[key]["filtrada"], ec[key]["sin_filtrar"]
        assert f["sharpe"] == pytest.approx(u["sharpe"])
        assert f["retorno"] == pytest.approx(u["retorno"]) and f["max_dd"] == pytest.approx(u["max_dd"])
        assert f["n_operaciones"] == u["n_operaciones"]
        assert ec[key]["tamano_prob"]["sharpe"] == pytest.approx(u["sharpe"])  # clip(2*(1-0.5)) = 1


def test_daily_returns_booked_on_exit_day() -> None:
    ev = pd.DataFrame({"primaria": ["a", "a"], "symbol": ["X", "Y"],
                       "t_decision": pd.to_datetime(["2024-01-01 10:00", "2024-01-02 00:00"], utc=True),
                       "t_salida": pd.to_datetime(["2024-01-04 03:00", "2024-01-04 23:00"], utc=True),
                       "tamaño": [1.0, 0.5], "pnl_neto": [0.10, -0.04]})
    r = v.daily_returns(ev, [1.0, 1.0])
    assert len(r) == 4 and r.iloc[:3].eq(0).all()
    assert r.iloc[3] == pytest.approx((0.10 * 1.0 - 0.04 * 0.5) / 2)  # 2 monedas: capital a medias
    assert v.size_by_probability([0.4, 0.5, 0.75, 1.0]).tolist() == [0.0, 0.0, 0.5, 1.0]
    assert v.size_filtered([0.49, 0.5]).tolist() == [0.0, 1.0]


# ---- registro y presupuesto -----------------------------------------------------------------------------


def _fake_metrics(seed: int) -> dict:
    rng = np.random.default_rng(seed)
    d = pd.Series(rng.normal(0, 0.01, 64), index=pd.date_range("2024-01-01", periods=64, freq="D", tz="UTC"))
    return {"economicas": {"_daily": {"filtrada": d}}, "auc": 0.5}


def test_budget_rejects_25th_and_ignores_duplicates(tmp_path) -> None:
    reg = v.MetaTrialRegistry(tmp_path / "meta_trials.sqlite")
    cfgs = [v.MetaConfig("logistica", ("x",), {"C": float(i)}) for i in range(24)]
    for i, c in enumerate(cfgs):
        reg.run(c, lambda i=i: _fake_metrics(i))
    assert reg.k == 24
    reg.run(cfgs[3], lambda: _fake_metrics(99))  # repetida: no suma
    assert reg.k == 24
    # mismo hash con features en otro orden
    assert v.MetaConfig("logistica", ("b", "a")).hash == v.MetaConfig("logistica", ("a", "b")).hash
    called = []
    with pytest.raises(v.BudgetExceeded):
        reg.run(v.MetaConfig("logistica", ("x",), {"C": 100.0}), lambda: called.append(1) or {})
    assert not called and reg.k == 24
    # el primer resultado manda (la repeticion no reemplaza)
    first = reg.results()[cfgs[3].hash]["daily"]
    assert np.allclose(first.to_numpy(), _fake_metrics(3)["economicas"]["_daily"]["filtrada"].to_numpy())
    # DSR con el K del meta-modelo y PBO de las configuraciones
    dsr = v.meta_dsr(first, reg)
    assert dsr["n_trials_raw"] == 24 and 0.0 <= dsr["dsr"] <= 1.0
    pbo = v.meta_pbo(reg, n_blocks=8)
    assert pbo["n_combinations"] == 24 and 0.0 <= pbo["pbo"] <= 1.0
    reg.close()


def test_failed_run_still_counts(tmp_path) -> None:
    reg = v.MetaTrialRegistry(tmp_path / "m.sqlite")

    def boom() -> dict:
        raise RuntimeError("se cayo")
    with pytest.raises(RuntimeError):
        reg.run(v.MetaConfig("logistica", ("x",)), boom)
    assert reg.k == 1


def test_meta_trials_is_append_only(tmp_path) -> None:
    db = tmp_path / "m.sqlite"
    reg = v.MetaTrialRegistry(db)
    reg.run(v.MetaConfig("logistica", ("x",)), lambda: _fake_metrics(1))
    con = sqlite3.connect(db)
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        con.execute("UPDATE meta_trials SET familia = 'otra'")
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        con.execute("DELETE FROM meta_trials")
    assert con.execute("SELECT COUNT(*) FROM meta_trials").fetchone()[0] == 2
    con.close()
    reg.close()


def test_config_validation() -> None:
    with pytest.raises(ValueError):
        v.MetaConfig("random_forest", ("x",))
    with pytest.raises(ValueError):
        v.MetaConfig("lightgbm", ("x",), {"max_depth": 6})
    v.MetaConfig("lightgbm", ("x",), {"max_depth": 3})


def test_meta_k_separate_from_lab_trials() -> None:
    assert v.DEFAULT_DB.name == "meta_trials.sqlite"
