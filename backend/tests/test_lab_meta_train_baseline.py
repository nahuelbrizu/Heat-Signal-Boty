from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from heatsignal.lab.meta import train_baseline as tb
from heatsignal.lab.meta import validation as v


def test_solo_desarrollo() -> None:
    t = pd.to_datetime(["2026-07-15 23:59", "2026-07-16 00:00", "2026-07-20 00:00"], utc=True)
    ev = pd.DataFrame({"t_decision": t, "x": [1, 2, 3]})
    out, dropped = tb.development_only(ev)
    assert dropped == 2 and len(out) == 1
    assert (out["t_decision"] < pd.Timestamp("2026-07-16", tz="UTC")).all()
    assert tb.DEV_END == pd.Timestamp("2026-07-16", tz="UTC")


def test_load_events_descarta_posteriores(monkeypatch: pytest.MonkeyPatch) -> None:
    t = pd.to_datetime(["2026-07-01 00:00", "2026-07-16 00:00", "2026-07-17 00:00"], utc=True)
    fake = pd.DataFrame({"t_decision": t, "primaria": ["a", "b", "b"], "tipo": ["apertura"] * 3})
    monkeypatch.setattr(tb.F, "load_all", lambda *a, **k: fake)
    ev, dropped = tb.load_events()
    assert dropped == 2 and len(ev) == 1 and ev["t_decision"].max() < tb.DEV_END
    assert ev["primaria_nombre"].tolist() == ["a"]


def test_solo_las_cuatro_preregistradas() -> None:
    assert set(tb.CONFIGS) == {"L1", "L2", "L3", "L4"}
    assert tb.parse_configs("l1, L3") == ["L1", "L3"]
    for bad in ("L5", "L1,L2b", "", "lightgbm"):
        with pytest.raises(ValueError):
            tb.parse_configs(bad)
    c = tb.CONFIGS
    assert [c[n].hiper["C"] for n in ("L1", "L2", "L3", "L4")] == [1.0, 0.1, 0.1, 1.0]
    assert len(tb.NUCLEO) == 12 and len(tb.TODAS) == 78
    assert not any(f.startswith("pm_") for cfg in c.values() for f in cfg.features)
    assert set(c["L1"].features) == {"primaria", *tb.NUCLEO} and set(c["L4"].features) == {"primaria", *tb.TODAS}
    assert len({cfg.hash for cfg in c.values()}) == 4
    with pytest.raises(SystemExit):
        tb.main(["--configs", "L1,L9"])


def test_preproceso_dentro_del_fold() -> None:
    rng = np.random.default_rng(0)
    n = 400
    X = np.c_[rng.normal(size=n), rng.normal(size=n), rng.integers(0, 3, n).astype(float)]
    X[:40, 0] = np.nan  # 10 % -> indicador
    X[:5, 1] = np.nan  # 1,25 % -> solo mediana
    y = (X[:, 2] == 1).astype(int)
    m = tb.MetaLogit(C=1.0, cat_idx=(2,), n_features=3).fit(X, y, sample_weight=np.ones(n))
    imp = m.pipe_.named_steps["pre"].named_transformers_["num"].named_steps["imp"]
    assert list(imp.indicator_cols_) == [0]
    p = m.predict_proba(X)[:, 1]
    assert np.isfinite(p).all() and p[y == 1].mean() > p[y == 0].mean()
    cal = v.FoldCalibrated(tb.MetaLogit(C=1.0, cat_idx=(2,), n_features=3)).fit(X, y, sample_weight=np.ones(n))
    assert np.isfinite(cal.predict_proba(X)).all()
