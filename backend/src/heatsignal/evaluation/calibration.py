"""
Calibracion del confidence_score (Fase 3, CLAUDE.md "Honestidad estadistica").

Objetivo: que de las senales con confidence ~0.72 acierte ~72%. Sin esto el
confidence_score es decorativo.

Definiciones (p_i = confidence_score en [0, 1], o_i = 1 si acerto, 0 si no; N = n total):
    Brier          = (1/N) * sum (p_i - o_i)^2                  (0 = perfecto, menor es mejor)
    Brier ref      = base_rate * (1 - base_rate)                 (Brier de pronosticar siempre
                     la tasa base observada, "climatologia")
    Brier skill    = 1 - Brier / Brier_ref                       (>0 = mejor que la tasa base;
                     None si Brier_ref == 0, i.e. todos aciertos o todos fallos)
    Buckets        = n_bins intervalos de igual ancho [k/n, (k+1)/n); el ultimo incluye 1.0.
    ECE            = sum_b (n_b / N) * |hit_rate_b - mean_conf_b|  (solo buckets con n_b > 0)
    MCE            = max_b |hit_rate_b - mean_conf_b|              (idem)

Casos borde explicitos:
    - N == 0: brier, brier_reference, brier_skill, ece, mce y base_rate son None (no 0:
      "sin datos" no es "calibracion perfecta"). Los buckets se devuelven igual, vacios.
    - Bucket con n_b == 0: mean_confidence, hit_rate y gap son None; wilson_ci = (0, 1)
      (incertidumbre total, convencion de `wilson_ci`). No contribuye a ECE/MCE.
    - Una confianza fuera de [0, 1] o no finita es un error (ValueError), no se recorta.

Solo entran hipotesis `resolved`: las `expired_no_data` no tienen outcome. Se cuentan
aparte en el reporte (ver `report.py`).
"""

from __future__ import annotations

import math
from collections.abc import Sequence

from pydantic import BaseModel

from .evaluator import wilson_ci

DEFAULT_N_BINS = 10


class CalibrationBucket(BaseModel):
    lower: float
    upper: float
    n: int
    mean_confidence: float | None
    hit_rate: float | None
    wilson_ci: tuple[float, float]
    gap: float | None  # |hit_rate - mean_confidence|


class CalibrationReport(BaseModel):
    n: int
    n_bins: int
    base_rate: float | None
    brier: float | None
    brier_reference: float | None
    brier_skill: float | None
    ece: float | None
    mce: float | None
    buckets: list[CalibrationBucket]


def _validate(confidences: Sequence[float], outcomes: Sequence[bool]) -> None:
    if len(confidences) != len(outcomes):
        raise ValueError(
            f"confidences ({len(confidences)}) y outcomes ({len(outcomes)}) deben tener el mismo largo"
        )
    for p in confidences:
        if not math.isfinite(p) or p < 0.0 or p > 1.0:
            raise ValueError(f"confidence fuera de [0, 1] o no finita: {p}")


def brier_score(confidences: Sequence[float], outcomes: Sequence[bool]) -> float | None:
    """Brier score; None si no hay datos."""
    _validate(confidences, outcomes)
    if not confidences:
        return None
    return sum((p - float(o)) ** 2 for p, o in zip(confidences, outcomes)) / len(confidences)


def bucket_index(p: float, n_bins: int) -> int:
    """Indice del bucket de igual ancho para p en [0, 1]; 1.0 cae en el ultimo.

    Se redondea p*n_bins a 9 decimales antes del floor para que errores de punto
    flotante (0.57*100 = 56.99999999999999) no manden un valor de borde al bucket
    de abajo.
    """
    idx = math.floor(round(p * n_bins, 9))
    return min(max(idx, 0), n_bins - 1)


def calibration_report(
    confidences: Sequence[float],
    outcomes: Sequence[bool],
    n_bins: int = DEFAULT_N_BINS,
) -> CalibrationReport:
    if n_bins < 1:
        raise ValueError(f"n_bins debe ser >= 1, recibido {n_bins}")
    _validate(confidences, outcomes)

    grouped: list[list[tuple[float, bool]]] = [[] for _ in range(n_bins)]
    for p, o in zip(confidences, outcomes):
        grouped[bucket_index(p, n_bins)].append((p, bool(o)))

    total = len(confidences)
    buckets: list[CalibrationBucket] = []
    ece = 0.0
    mce = 0.0
    for k, members in enumerate(grouped):
        n_b = len(members)
        if n_b == 0:
            buckets.append(
                CalibrationBucket(
                    lower=k / n_bins,
                    upper=(k + 1) / n_bins,
                    n=0,
                    mean_confidence=None,
                    hit_rate=None,
                    wilson_ci=wilson_ci(0, 0),
                    gap=None,
                )
            )
            continue
        hits = sum(1 for _, o in members if o)
        mean_conf = sum(p for p, _ in members) / n_b
        hit_rate = hits / n_b
        gap = abs(hit_rate - mean_conf)
        ece += (n_b / total) * gap
        mce = max(mce, gap)
        buckets.append(
            CalibrationBucket(
                lower=k / n_bins,
                upper=(k + 1) / n_bins,
                n=n_b,
                mean_confidence=mean_conf,
                hit_rate=hit_rate,
                wilson_ci=wilson_ci(hits, n_b),
                gap=gap,
            )
        )

    if total == 0:
        return CalibrationReport(
            n=0,
            n_bins=n_bins,
            base_rate=None,
            brier=None,
            brier_reference=None,
            brier_skill=None,
            ece=None,
            mce=None,
            buckets=buckets,
        )

    brier = brier_score(confidences, outcomes)
    assert brier is not None
    base_rate = sum(1 for o in outcomes if o) / total
    brier_ref = base_rate * (1.0 - base_rate)
    brier_skill = None if brier_ref == 0 else 1.0 - brier / brier_ref
    return CalibrationReport(
        n=total,
        n_bins=n_bins,
        base_rate=base_rate,
        brier=brier,
        brier_reference=brier_ref,
        brier_skill=brier_skill,
        ece=ece,
        mce=mce,
        buckets=buckets,
    )
