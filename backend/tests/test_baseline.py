"""Tests de los baselines pasivos: reproducibilidad por seed y reglas de decisión."""

from __future__ import annotations

from heatsignal.evaluation.baseline import AlwaysLongBaseline, MomentumBaseline, RandomBaseline
from heatsignal.schema.signals import Direction


def test_random_baseline_is_reproducible_with_same_seed() -> None:
    a = RandomBaseline(seed=7)
    b = RandomBaseline(seed=7)
    seq_a = [a.signal([]) for _ in range(20)]
    seq_b = [b.signal([]) for _ in range(20)]
    assert seq_a == seq_b


def test_random_baseline_reset_replays_same_sequence() -> None:
    rb = RandomBaseline(seed=7)
    first_pass = [rb.signal([]) for _ in range(10)]
    rb.reset()
    second_pass = [rb.signal([]) for _ in range(10)]
    assert first_pass == second_pass


def test_random_baseline_different_seeds_can_diverge() -> None:
    a = RandomBaseline(seed=1)
    b = RandomBaseline(seed=2)
    seq_a = [a.signal([]) for _ in range(20)]
    seq_b = [b.signal([]) for _ in range(20)]
    assert seq_a != seq_b


def test_always_long_baseline_ignores_price_history() -> None:
    baseline = AlwaysLongBaseline()
    assert baseline.signal([]) == Direction.LONG
    assert baseline.signal([100.0, 90.0, 50.0]) == Direction.LONG


def test_momentum_baseline_green_candle_is_long() -> None:
    baseline = MomentumBaseline()
    assert baseline.signal([100.0, 105.0]) == Direction.LONG


def test_momentum_baseline_red_candle_is_short() -> None:
    baseline = MomentumBaseline()
    assert baseline.signal([105.0, 100.0]) == Direction.SHORT


def test_momentum_baseline_flat_candle_is_long() -> None:
    baseline = MomentumBaseline()
    assert baseline.signal([100.0, 100.0]) == Direction.LONG


def test_momentum_baseline_insufficient_history_defaults_to_long() -> None:
    baseline = MomentumBaseline()
    assert baseline.signal([]) == Direction.LONG
    assert baseline.signal([100.0]) == Direction.LONG
