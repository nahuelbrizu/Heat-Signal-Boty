"""
Semaforo de los criterios de parada pre-registrados (`parada_preregistrada.json`, generado una vez por
`python -m heatsignal.lab.parada`; ver su docstring y decisions.md).

Sin numpy ni pandas (corre en el proceso de ingesta). Para cada familia principal: drawdown maximo y duracion maxima
bajo el agua de la equity diaria (cierres 00:00 UTC, misma convencion que el lab: con capital fijo sin reinversion,
equity / capital = 1 + suma de retornos) desde el inicio, contra P95 / P99 del horizonte transcurrido interpolado.
  > P95 -> "alerta" (tamano a la mitad), > P99 -> "parar". En paper NO cambia operaciones: solo se muestra y registra.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

PATH = Path(__file__).with_name("parada_preregistrada.json")
ESTADOS = ("ok", "alerta", "parar")
LABEL = {"ok": "ok", "alerta": "alerta: tamaño a la mitad", "parar": "parar"}
NOTA_PAPER = ("En paper no cambia las operaciones: se muestra y se registra. En plata real es regla obligatoria "
              "(alerta = tamaño a la mitad, parar = cerrar).")


@lru_cache(maxsize=1)
def load(path: str | None = None) -> dict[str, Any]:
    return json.loads(Path(path or PATH).read_text())


def threshold(horizontes: dict[str, Any], metric: str, q: str, dias: float) -> float:
    """Umbral `q` ("p95"/"p99") de `metric` interpolado linealmente en `dias` (0 -> 0; despues del ultimo horizonte se
    usa el ultimo)."""
    pts = [(0.0, 0.0)] + sorted((float(h), float(v[metric][q])) for h, v in horizontes.items())
    if dias >= pts[-1][0]:
        return pts[-1][1]
    for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
        if dias <= x1:
            return y0 + (y1 - y0) * (dias - x0) / (x1 - x0)
    return pts[-1][1]


def dd_and_duration(equity: list[float]) -> tuple[float, int, int]:
    """(drawdown maximo como fraccion positiva, racha maxima bajo el agua en dias, racha actual) de una equity diaria
    que arranca en el capital inicial (equity[0])."""
    if not equity:
        return 0.0, 0, 0
    peak, mdd, run, best = equity[0], 0.0, 0, 0
    for v in equity[1:]:
        if v > peak:
            peak = v
        if peak > 0:
            mdd = max(mdd, (peak - v) / peak)
        run = run + 1 if v < peak - 1e-9 * abs(peak) else 0
        best = max(best, run)
    return mdd, best, run


def _nivel(x: float, p95: float, p99: float) -> str:
    return "parar" if x > p99 else ("alerta" if x > p95 else "ok")


def evaluate(strategy_id: str, daily_returns: list[float], doc: dict[str, Any] | None = None) -> dict[str, Any] | None:
    """Estado de parada de una familia principal a partir de sus retornos diarios desde el inicio (`view.daily_returns`,
    sobre el capital fijo). None si la estrategia no tiene criterio pre-registrado."""
    doc = doc if doc is not None else load()
    fam = (doc.get("familias") or {}).get(strategy_id)
    if not fam:
        return None
    eq, acc = [1.0], 1.0
    for r in daily_returns:  # retornos sobre la equity del dia anterior -> equity relativa al capital inicial
        acc *= 1.0 + r
        eq.append(acc)
    dias = len(daily_returns)
    mdd, dur, cur = dd_and_duration(eq)
    h = fam["horizontes"]
    u = {m: {q: threshold(h, m, q, dias) for q in ("p95", "p99")} for m in ("drawdown", "duracion_dias")}
    n_dd = _nivel(mdd, u["drawdown"]["p95"], u["drawdown"]["p99"]) if dias else "ok"
    n_du = _nivel(dur, u["duracion_dias"]["p95"], u["duracion_dias"]["p99"]) if dias else "ok"
    estado = max(n_dd, n_du, key=ESTADOS.index)
    return {
        "estado": estado, "label": LABEL[estado], "dias": dias,
        "drawdown": {"valor": mdd, "p95": u["drawdown"]["p95"], "p99": u["drawdown"]["p99"], "nivel": n_dd},
        "duracion": {"valor": dur, "actual": cur, "p95": u["duracion_dias"]["p95"], "p99": u["duracion_dias"]["p99"],
                     "nivel": n_du},
        "hash": doc.get("hash"), "fecha": doc.get("fecha"), "nota": NOTA_PAPER,
    }
