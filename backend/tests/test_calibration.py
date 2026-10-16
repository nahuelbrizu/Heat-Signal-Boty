"""Tests de calibracion: Brier, buckets, Wilson por bucket, ECE/MCE y casos borde (a mano)."""

from __future__ import annotations

import math

import pytest

from heatsignal.evaluation.calibration import bucket_index, brier_score, calibration_report
from heatsignal.evaluation.evaluator import wilson_ci


def test_brier_tiny_known_example() -> None:
    # (0.9-1)^2 + (0.2-0)^2 + (0.6-0)^2 = 0.01 + 0.04 + 0.36 = 0.41 -> /3
    assert brier_score([0.9, 0.2, 0.6], [True, False, False]) == pytest.approx(0.41 / 3)


def test_brier_extremes() -> None:
    assert brier_score([1.0, 0.0], [True, False]) == 0.0  # perfecto
    assert brier_score([0.0, 1.0], [True, False]) == 1.0  # peor posible
    assert brier_score([0.5] * 4, [True, False, True, False]) == pytest.approx(0.25)


def test_brier_empty_is_none() -> None:
    assert brier_score([], []) is None


@pytest.mark.parametrize("bad", [-0.01, 1.01, math.nan, math.inf])
def test_invalid_confidence_raises(bad: float) -> None:
    with pytest.raises(ValueError):
        calibration_report([bad], [True])


def test_length_mismatch_raises() -> None:
    with pytest.raises(ValueError):
        calibration_report([0.5, 0.5], [True])


def test_n_bins_must_be_positive() -> None:
    with pytest.raises(ValueError):
        calibration_report([0.5], [True], n_bins=0)


@pytest.mark.parametrize(
    "p, n_bins, expected",
    [
        (0.0, 10, 0),
        (0.0999, 10, 0),
        (0.1, 10, 1),
        (0.7, 10, 7),
        (0.99, 10, 9),
        (1.0, 10, 9),  # 1.0 cae en el ultimo bucket
        (0.57, 100, 57),  # 0.57*100 = 56.99999999999999 en float
        (0.5, 2, 1),
        (0.49, 2, 0),
        (1.0, 1, 0),
    ],
)
def test_bucket_index_edges(p: float, n_bins: int, expected: int) -> None:
    assert bucket_index(p, n_bins) == expected


def test_bucket_math_ece_mce_hand_computed() -> None:
    confidences = [0.15, 0.18, 0.72, 0.74, 0.76, 0.78, 1.0]
    outcomes = [False, True, True, True, True, False, True]
    rep = calibration_report(confidences, outcomes, n_bins=10)

    assert rep.n == 7 and rep.n_bins == 10 and len(rep.buckets) == 10
    b1, b7, b9 = rep.buckets[1], rep.buckets[7], rep.buckets[9]

    # bucket [0.1, 0.2): n=2, conf media 0.165, acierto 0.5, gap 0.335
    assert (b1.lower, b1.upper, b1.n) == (pytest.approx(0.1), pytest.approx(0.2), 2)
    assert b1.mean_confidence == pytest.approx(0.165)
    assert b1.hit_rate == 0.5
    assert b1.gap == pytest.approx(0.335)
    assert b1.wilson_ci == wilson_ci(1, 2)

    # bucket [0.7, 0.8): n=4, conf media 0.75, acierto 3/4 -> gap 0
    assert b7.n == 4
    assert b7.mean_confidence == pytest.approx(0.75)
    assert b7.hit_rate == 0.75
    assert b7.gap == pytest.approx(0.0)
    # Wilson 95% para 3/4, valor de referencia ~ (0.3006, 0.9544)
    assert b7.wilson_ci[0] == pytest.approx(0.3006, abs=1e-4)
    assert b7.wilson_ci[1] == pytest.approx(0.9544, abs=1e-4)

    # bucket [0.9, 1.0]: incluye 1.0
    assert b9.n == 1 and b9.hit_rate == 1.0 and b9.gap == pytest.approx(0.0)

    # ECE = (2/7)*0.335 + (4/7)*0 + (1/7)*0
    assert rep.ece == pytest.approx(2 / 7 * 0.335)
    assert rep.mce == pytest.approx(0.335)

    # Brier = (0.15^2 + 0.82^2 + 0.28^2 + 0.26^2 + 0.24^2 + 0.78^2 + 0^2) / 7
    expected_brier = (0.0225 + 0.6724 + 0.0784 + 0.0676 + 0.0576 + 0.6084 + 0.0) / 7
    assert rep.brier == pytest.approx(expected_brier)
    # Tasa base 5/7 -> Brier de referencia 5/7 * 2/7 = 10/49
    assert rep.base_rate == pytest.approx(5 / 7)
    assert rep.brier_reference == pytest.approx(10 / 49)
    assert rep.brier_skill == pytest.approx(1 - expected_brier / (10 / 49))


def test_empty_buckets_are_explicit() -> None:
    rep = calibration_report([0.72, 0.74], [True, False], n_bins=10)
    empty = [b for i, b in enumerate(rep.buckets) if i != 7]
    assert all(b.n == 0 for b in empty)
    assert all(b.mean_confidence is None and b.hit_rate is None and b.gap is None for b in empty)
    assert all(b.wilson_ci == (0.0, 1.0) for b in empty)
    # ECE solo pondera buckets con datos: |0.5 - 0.73| = 0.23
    assert rep.ece == pytest.approx(0.23)


def test_n_zero_report_is_none_not_zero() -> None:
    rep = calibration_report([], [], n_bins=5)
    assert rep.n == 0
    assert rep.brier is None and rep.ece is None and rep.mce is None
    assert rep.base_rate is None and rep.brier_reference is None and rep.brier_skill is None
    assert len(rep.buckets) == 5 and all(b.n == 0 for b in rep.buckets)


def test_brier_skill_undefined_when_all_same_outcome() -> None:
    rep = calibration_report([0.6, 0.8], [True, True], n_bins=10)
    assert rep.brier_reference == 0.0
    assert rep.brier_skill is None
    assert rep.brier == pytest.approx((0.16 + 0.04) / 2)


def test_perfectly_calibrated_has_zero_ece() -> None:
    # 10 senales a 0.3 con 3 aciertos y 10 a 0.8 con 8 aciertos
    confidences = [0.3] * 10 + [0.8] * 10
    outcomes = [True] * 3 + [False] * 7 + [True] * 8 + [False] * 2
    rep = calibration_report(confidences, outcomes)
    assert rep.ece == pytest.approx(0.0)
    assert rep.mce == pytest.approx(0.0)
    # Brier = (3*0.49 + 7*0.09 + 8*0.04 + 2*0.64) / 20 = (1.47+0.63+0.32+1.28)/20 = 0.185
    assert rep.brier == pytest.approx(0.185)


def test_overconfident_detected() -> None:
    # Dice 0.9 pero acierta 50%: gap 0.4 en todos -> ECE 0.4
    rep = calibration_report([0.9] * 10, [True, False] * 5)
    assert rep.ece == pytest.approx(0.4)
    assert rep.buckets[9].hit_rate == 0.5


def test_report_is_serializable() -> None:
    rep = calibration_report([0.5], [True])
    dumped = rep.model_dump(mode="json")
    assert dumped["buckets"][5]["n"] == 1
    assert rep.model_validate_json(rep.model_dump_json()) == rep
