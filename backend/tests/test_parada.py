"""Criterios de parada pre-registrados: bootstrap (lab/parada.py), semaforo (paperbot/parada.py) y JSON congelado."""

from __future__ import annotations

import json

import numpy as np
import pytest

from heatsignal.lab import parada as lp
from heatsignal.paperbot import parada as pp

FROZEN_HASH = "ba74b6de38914bb0"  # paperbot/parada_preregistrada.json (2026-10-21): NO cambiar; es pre-registro


def test_path_stats_known_path():
    r = np.array([[0.1, -0.05, -0.05, 0.02, 0.2, -0.01]])
    mdd, dur = lp.path_stats(r)
    # equity 1, 1.1, 1.05, 1.0, 1.02, 1.22, 1.21 -> DD max 0.1/1.1; bajo el agua 3 dias seguidos
    assert mdd[0] == pytest.approx(0.1 / 1.1)
    assert dur[0] == 3


def test_bootstrap_constant_positive_has_no_drawdown():
    out = lp.bootstrap(np.full(500, 0.001), horizons=(30, 90), n_boot=200, seed=1)
    for h in ("30", "90"):
        assert out[h]["drawdown"] == {"p50": 0.0, "p95": 0.0, "p99": 0.0}
        assert out[h]["duracion_dias"]["p99"] == 0.0


def test_bootstrap_random_walk_reasonable():
    rng = np.random.default_rng(7)
    r = rng.normal(0.0, 0.01, 2000)  # sin deriva, 1 % diario
    out = lp.bootstrap(r, horizons=(30, 90, 180, 365), n_boot=2000, seed=3)
    prev = 0.0
    for h in (30, 90, 180, 365):
        d, u = out[str(h)]["drawdown"], out[str(h)]["duracion_dias"]
        assert d["p50"] < d["p95"] < d["p99"]
        assert d["p50"] > prev  # crece con el horizonte
        prev = d["p50"]
        assert u["p99"] <= h
        # orden de magnitud: el DD de un paseo al azar escala con sigma * sqrt(h)
        assert 0.3 * 0.01 * h ** 0.5 < d["p50"] < 2.5 * 0.01 * h ** 0.5
        assert d["p99"] < 5 * 0.01 * h ** 0.5
    # determinista con la semilla fija
    assert lp.bootstrap(r, horizons=(30,), n_boot=500, seed=3) == lp.bootstrap(r, horizons=(30,), n_boot=500, seed=3)


def test_stationary_indices_shape_and_range():
    idx = lp.stationary_indices(50, 365, 20.0, 100, np.random.default_rng(0))
    assert idx.shape == (100, 365) and idx.min() >= 0 and idx.max() < 50
    # bloques de largo medio ~20: la mayoria de los pasos son el indice siguiente
    cont = np.mean((idx[:, 1:] - idx[:, :-1]) % 50 == 1)
    assert 0.9 < cont < 0.99


DOC = {"hash": "x", "fecha": "2026-10-21", "familias": {"fam": {"horizontes": {
    "30": {"drawdown": {"p50": 0.05, "p95": 0.10, "p99": 0.15}, "duracion_dias": {"p50": 10, "p95": 20, "p99": 25}},
    "90": {"drawdown": {"p50": 0.08, "p95": 0.16, "p99": 0.22}, "duracion_dias": {"p50": 40, "p95": 70, "p99": 80}},
}}}}


def test_threshold_interpolation():
    h = DOC["familias"]["fam"]["horizontes"]
    assert pp.threshold(h, "drawdown", "p95", 0) == 0.0
    assert pp.threshold(h, "drawdown", "p95", 15) == pytest.approx(0.05)
    assert pp.threshold(h, "drawdown", "p95", 30) == pytest.approx(0.10)
    assert pp.threshold(h, "drawdown", "p99", 60) == pytest.approx(0.185)
    assert pp.threshold(h, "duracion_dias", "p95", 400) == 70  # despues del ultimo horizonte, el ultimo


@pytest.mark.parametrize("dd,estado", [(0.099, "ok"), (0.101, "alerta"), (0.149, "alerta"), (0.151, "parar")])
def test_semaforo_drawdown_thresholds(dd, estado):
    # a los 30 dias: P95 10 %, P99 15 %. Duracion bajo el agua 29 dias > P99 25 -> aislar el DD con una recuperacion
    rs = [0.01, -dd, dd / (1 - dd) + 0.001] + [0.001] * 27
    out = pp.evaluate("fam", rs, DOC)
    assert out["dias"] == 30
    assert out["drawdown"]["valor"] == pytest.approx(dd)
    assert out["duracion"]["nivel"] == "ok"
    assert out["drawdown"]["nivel"] == estado and out["estado"] == estado


@pytest.mark.parametrize("bajo,estado", [(20, "ok"), (21, "alerta"), (25, "alerta"), (26, "parar")])
def test_semaforo_duracion_thresholds(bajo, estado):
    # 30 dias: P95 20, P99 25 dias bajo el agua; DD chico (0,1 %) para que no cuente
    rs = [0.01, -0.001] + [0.0] * (bajo - 1) + [0.01] * (29 - bajo)
    out = pp.evaluate("fam", rs, DOC)
    assert out["duracion"]["valor"] == bajo
    assert out["drawdown"]["nivel"] == "ok"
    assert out["duracion"]["nivel"] == estado and out["estado"] == estado


def test_semaforo_sin_dias_y_familia_desconocida():
    assert pp.evaluate("fam", [], DOC)["estado"] == "ok"
    assert pp.evaluate("otra", [0.01], DOC) is None


def test_json_congelado():
    doc = json.loads(pp.PATH.read_text())
    assert doc["hash"] == FROZEN_HASH
    assert lp.content_hash(doc) == FROZEN_HASH  # el contenido no se toco despues de generarlo
    assert set(doc["familias"]) == {"l6-familia-ruptura-long-v1", "l6-familia-ruptura-short-v1", "l6-familia-ruptura-neta-v1"}
    m = doc["metodo"]
    assert (m["bloque_medio_dias"], m["B"], m["semilla"], m["horizontes_dias"]) == (20.0, 5000, 20261020, [30, 90, 180, 365])
    for f in doc["familias"].values():
        assert set(f["horizontes"]) == {"30", "90", "180", "365"}
        for v in f["horizontes"].values():
            for k in ("drawdown", "duracion_dias"):
                assert v[k]["p50"] <= v[k]["p95"] <= v[k]["p99"]
