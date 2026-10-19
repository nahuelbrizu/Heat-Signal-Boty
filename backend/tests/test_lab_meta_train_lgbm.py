"""Etapa 3, subtarea C (lab/meta/train_lgbm.py): solo G1-G4 pre-registradas y evaluacion 3c (sin datos reales)."""
from __future__ import annotations

import json

import numpy as np
import pandas as pd
import pytest

from heatsignal.lab.meta import train_baseline as tb
from heatsignal.lab.meta import train_lgbm as tl
from heatsignal.lab.meta import validation as v


def test_solo_las_cuatro_preregistradas() -> None:
    assert set(tl.CONFIGS) == {"G1", "G2", "G3", "G4"}
    assert tl.parse_configs("g2, G4") == ["G2", "G4"]
    for bad in ("G5", "L1", "", "G1,x"):
        with pytest.raises(ValueError):
            tl.parse_configs(bad)
    c = tl.CONFIGS
    assert c["G1"].hiper == {"max_depth": 3, "num_leaves": 7, "n_estimators": 200, "learning_rate": 0.03,
                             "min_child_samples": 100, "reg_lambda": 5.0, "subsample": 0.8, "subsample_freq": 1,
                             "colsample_bytree": 0.8, "random_state": 20261019}
    assert c["G2"].hiper["max_depth"] == 2 and c["G2"].hiper["num_leaves"] == 3
    assert c["G2"].hiper["n_estimators"] == 300 and c["G2"].hiper["min_child_samples"] == 200
    assert c["G2"].hiper["reg_lambda"] == 10.0
    assert c["G3"].hiper == c["G1"].hiper and c["G4"].hiper == c["G2"].hiper
    assert set(c["G1"].features) == {"primaria", *tb.NUCLEO} and set(c["G4"].features) == {"primaria", *tb.TODAS}
    assert all(cfg.familia == "lightgbm" for cfg in c.values())
    assert len({cfg.hash for cfg in c.values()}) == 4 and not {cfg.hash for cfg in c.values()} & tb.PREREG_HASHES


def _ev(n: int = 1200, seed: int = 0) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    t = pd.Timestamp("2022-01-01", tz="UTC") + pd.to_timedelta(np.sort(rng.integers(0, 900 * 24, n)), unit="h")
    x = rng.normal(size=n)
    pnl = 0.01 * x + rng.normal(0, 0.02, n)
    ev = pd.DataFrame({"primaria": rng.integers(0, 2, n).astype(float), "symbol": "X", "t_decision": t,
                       "t_salida": t + pd.Timedelta(days=2), "t_salida_regla": t + pd.Timedelta(days=3),
                       "x": x, "pnl_neto": pnl, "pnl_regla": pnl * 1.5, "etiqueta": (pnl > 0).astype(int)})
    ev["peso_unicidad"] = 1.0
    ev["primaria_nombre"] = ev["primaria"].map({0.0: "a", 1.0: "b"})
    return ev


def test_evaluacion_3c_y_registro(tmp_path) -> None:
    ev = _ev()
    cfg = v.MetaConfig("lightgbm", ("primaria", "x"), dict(tl.HP_G2))
    reg = v.MetaTrialRegistry(tmp_path / "m.sqlite")
    m = reg.run(cfg, lambda: tl._evaluate_named(ev, cfg, 50, 2))
    assert m["estadisticas"]["auc"] > 0.6
    assert set(m["estadisticas_por_primaria"]) == {"a", "b"}
    assert set(m["umbral_por_fold"]) == set(range(v.K_FOLDS))
    assert m["economicas"]["_conjunto"]["sin_filtrar"]["retorno"] == pytest.approx(
        1.5 * m["economicas_barreras"]["_conjunto"]["sin_filtrar"]["retorno"])
    assert reg.k == 1 and len(reg.results()[cfg.hash]["daily"])
    reg.close()
    m["economicas"], m["economicas_barreras"] = v._clean(m["economicas"]), v._clean(m["economicas_barreras"])
    json.dumps(v._clean(m))
    c = tl.criterio(m, 0.3)
    assert set(c) >= {"auc_mayor_0_55", "brier_mejor_que_base", "p_bootstrap_menor_igual_0_05", "cumple"}
    assert not tl.criterio(m, 0.6)["cumple"]
