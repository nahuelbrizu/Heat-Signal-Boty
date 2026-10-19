"""El pre-registro del meta-modelo (plan-entrenamiento.md, etapa 0) queda congelado: cambiarlo es una version
nueva con decision en decisions.md, nunca un ajuste mirando resultados."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from heatsignal.lab.strategies import CATALOG

PRE = Path(__file__).resolve().parents[1] / "src/heatsignal/lab/meta/preregistro.json"
PRE_HASH = "36feb567ccef3b41"


def test_preregistro_frozen() -> None:
    txt = PRE.read_text().rstrip("\n")
    assert hashlib.sha256(txt.encode()).hexdigest()[:16] == PRE_HASH


def test_preregistro_primarias_match_frozen_specs() -> None:
    pre = json.loads(PRE.read_text())
    assert {s: CATALOG[s][0].spec_hash() for s in pre["primarias"]} == pre["primarias"]
    assert pre["periodos"]["examen_final_hacia_adelante"]["desde"] == "2026-10-20T00:00:00Z"
    assert pre["presupuesto_intentos"]["max_configuraciones"] == 24
