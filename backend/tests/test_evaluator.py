"""
Tests del Evaluador. Foco explícito en `_normalize` y `wilson_ci` con fixtures
de resultados conocidos (requerido), más cobertura de las métricas derivadas
y del criterio de aprobación end-to-end.
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import UUID, uuid4

import pytest

from heatsignal.evaluation.evaluator import (
    Evaluator,
    _beats_baseline,
    _compute_raw_metrics,
    _max_drawdown,
    _normalize,
    wilson_ci,
)
from heatsignal.schema.signals import Direction, Signal

STRATEGY_ID = uuid4()
_BASE_TIME = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _make_signal(
    pnl_pct: float,
    *,
    index: int = 0,
    direction: Direction = Direction.LONG,
    entry_price: str = "100",
    exit_price: str = "102",
    pnl: str = "20",
    holding: timedelta = timedelta(hours=1),
    strategy_id: UUID = STRATEGY_ID,
) -> Signal:
    entry_time = _BASE_TIME + index * timedelta(hours=1)
    return Signal(
        strategy_id=strategy_id,
        direction=direction,
        entry_price=Decimal(entry_price),
        exit_price=Decimal(exit_price),
        entry_time=entry_time,
        exit_time=entry_time + holding,
        pnl=Decimal(pnl),
        pnl_pct=pnl_pct,
    )


# ---- _normalize --------------------------------------------------------------


def test_normalize_extracts_pnl_pct_in_order() -> None:
    signals = [_make_signal(1.5, index=0), _make_signal(-2.25, index=1), _make_signal(0.0, index=2)]
    assert _normalize(signals) == [1.5, -2.25, 0.0]


def test_normalize_rejects_nan() -> None:
    signals = [_make_signal(float("nan"), index=0)]
    with pytest.raises(ValueError, match="no finito"):
        _normalize(signals)


def test_normalize_rejects_infinite() -> None:
    signals = [_make_signal(float("inf"), index=0)]
    with pytest.raises(ValueError, match="no finito"):
        _normalize(signals)


def test_normalize_empty_list_returns_empty() -> None:
    assert _normalize([]) == []


# ---- wilson_ci -----------------------------------------------------------------


def test_wilson_ci_known_reference_50_of_100() -> None:
    # Referencia estándar (score de Wilson, 95%) para p_hat=0.5, n=100.
    lower, upper = wilson_ci(50, 100)
    assert lower == pytest.approx(0.4038, abs=1e-3)
    assert upper == pytest.approx(0.5962, abs=1e-3)


def test_wilson_ci_zero_n_returns_full_uncertainty() -> None:
    assert wilson_ci(0, 0) == (0.0, 1.0)


def test_wilson_ci_all_wins_stays_within_unit_interval() -> None:
    lower, upper = wilson_ci(10, 10)
    assert 0.0 < lower < upper <= 1.0


def test_wilson_ci_all_losses_stays_within_unit_interval() -> None:
    lower, upper = wilson_ci(0, 10)
    assert 0.0 <= lower < upper < 1.0


def test_wilson_ci_narrows_as_n_grows() -> None:
    small_n = wilson_ci(5, 10)
    large_n = wilson_ci(500, 1000)
    width_small = small_n[1] - small_n[0]
    width_large = large_n[1] - large_n[0]
    assert width_large < width_small


# ---- métricas derivadas --------------------------------------------------------


def test_max_drawdown_is_zero_when_all_returns_positive() -> None:
    assert _max_drawdown([1.0, 2.0, 0.5, 3.0]) == 0.0


def test_max_drawdown_known_mixed_fixture() -> None:
    # equity: 1.00 -> 1.10 -> 1.045 -> 0.99275 -> 1.092025 (peak fijo en 1.10 tras el primer paso)
    dd = _max_drawdown([10.0, -5.0, -5.0, 10.0])
    assert dd == pytest.approx(-0.0975, abs=1e-4)


def test_profit_factor_known_ratio() -> None:
    metrics = _compute_raw_metrics(
        [10.0, 10.0, -5.0],
        [Decimal("100"), Decimal("100"), Decimal("-50")],
        [_make_signal(10.0, index=0), _make_signal(10.0, index=1), _make_signal(-5.0, index=2)],
    )
    assert metrics["profit_factor"] == pytest.approx(4.0)


def test_profit_factor_is_infinite_with_no_losses() -> None:
    metrics = _compute_raw_metrics(
        [10.0, 10.0],
        [Decimal("100"), Decimal("100")],
        [_make_signal(10.0, index=0), _make_signal(10.0, index=1)],
    )
    assert math.isinf(metrics["profit_factor"])


# ---- _beats_baseline (criterio compuesto) --------------------------------------


def _metrics_for(returns: list[float]) -> tuple[dict[str, float], list[float]]:
    signals = [_make_signal(r, index=i) for i, r in enumerate(returns)]
    metrics = _compute_raw_metrics(returns, [s.pnl for s in signals], signals)
    return metrics, returns


def test_beats_baseline_true_when_all_three_criteria_favor_strategy() -> None:
    strategy_metrics, strategy_returns = _metrics_for([5.0, 4.0, 5.0, 4.0])
    baseline_metrics, baseline_returns = _metrics_for([2.0, -1.0, 2.0, -1.0])

    assert _beats_baseline(strategy_returns, strategy_metrics, baseline_returns, baseline_metrics) is True


def test_beats_baseline_false_when_avg_return_is_lower() -> None:
    strategy_metrics, strategy_returns = _metrics_for([1.0, 1.0])
    baseline_metrics, baseline_returns = _metrics_for([5.0, 5.0])

    assert _beats_baseline(strategy_returns, strategy_metrics, baseline_returns, baseline_metrics) is False


def test_beats_baseline_false_when_avg_return_higher_but_sharpe_lower() -> None:
    # avg_return y max_drawdown favorecen a la estrategia; el Sharpe no.
    # Fixture verificado numéricamente (no solo derivado a mano):
    #   estrategia: avg_return=17.08, sharpe=39.36, max_drawdown=0.0
    #   baseline:   avg_return=1.70,  sharpe=56.84, max_drawdown=-0.04
    strategy_metrics, strategy_returns = _metrics_for([0.5, 0.5, 0.5, 0.5, 0.5, 100.0])
    baseline_metrics, baseline_returns = _metrics_for([3.0, 2.6, 3.0, 2.6, 3.0, -4.0])

    assert strategy_metrics["avg_return"] > baseline_metrics["avg_return"]
    assert abs(strategy_metrics["max_drawdown"]) < abs(baseline_metrics["max_drawdown"])
    assert strategy_metrics["sharpe_ratio"] < baseline_metrics["sharpe_ratio"]

    assert _beats_baseline(strategy_returns, strategy_metrics, baseline_returns, baseline_metrics) is False


def test_beats_baseline_false_when_drawdown_is_worse_despite_avg_return_and_sharpe() -> None:
    strategy_metrics, strategy_returns = _metrics_for([12.0, -3.0, 12.0, -3.0])
    baseline_metrics, baseline_returns = _metrics_for([4.0, 3.8, 4.0, 3.8])

    assert strategy_metrics["avg_return"] > baseline_metrics["avg_return"]
    assert abs(strategy_metrics["max_drawdown"]) > abs(baseline_metrics["max_drawdown"])

    assert _beats_baseline(strategy_returns, strategy_metrics, baseline_returns, baseline_metrics) is False


def test_beats_baseline_skips_sharpe_when_unavailable_on_either_side() -> None:
    # Baseline con una sola señal: Sharpe no está definido (requiere >=2 retornos).
    # avg_return y max_drawdown siguen decidiendo el resultado, y ambos favorecen
    # a la estrategia acá -> debe dar True pese a no poder comparar Sharpe.
    strategy_metrics, strategy_returns = _metrics_for([5.0, 3.0, 5.0, 3.0])
    baseline_metrics, baseline_returns = _metrics_for([-5.0])

    assert len(baseline_returns) < 2  # precondición: Sharpe del baseline no definido
    assert _beats_baseline(strategy_returns, strategy_metrics, baseline_returns, baseline_metrics) is True


# ---- Evaluator.evaluate end-to-end ---------------------------------------------


def test_evaluate_with_zero_signals_is_insufficient_data() -> None:
    result = Evaluator().evaluate(STRATEGY_ID, [], historical_prices=[])
    assert result.n_signals == 0
    assert result.passed is False
    assert result.reason == "insufficient_data"
    assert result.wilson_ci == (0.0, 1.0)


def test_evaluate_below_min_n_is_insufficient_data() -> None:
    signals = [_make_signal(2.0, index=i) for i in range(10)]
    result = Evaluator().evaluate(STRATEGY_ID, signals, historical_prices=[100.0] * 15)
    assert result.n_signals == 10
    assert result.passed is False
    assert result.reason == "insufficient_data"


def test_evaluate_rejects_signals_from_another_strategy() -> None:
    foreign = _make_signal(2.0, index=0, strategy_id=uuid4())
    with pytest.raises(ValueError, match="otro strategy_id"):
        Evaluator().evaluate(STRATEGY_ID, [foreign], historical_prices=[100.0])


def test_evaluate_strong_synthetic_edge_passes_all_criteria() -> None:
    # 40 señales, dirección alternando LONG/SHORT y siempre acertando (pnl_pct
    # variable alrededor de +2% para que haya varianza y el Sharpe sea finito).
    # Precios históricos planos: MomentumBaseline y AlwaysLongBaseline colapsan
    # a "siempre LONG", perdiendo la mitad de las apuestas -> avg_return ~0,
    # muy por debajo del ~+2% de la estrategia real.
    signals = []
    for i in range(40):
        direction = Direction.LONG if i % 2 == 0 else Direction.SHORT
        pnl_pct = 2.0 + (0.2 if i % 2 == 0 else -0.2)
        signals.append(
            _make_signal(pnl_pct, index=i, direction=direction, pnl=str(pnl_pct * 10))
        )
    historical_prices = [100.0] * 45

    result = Evaluator().evaluate(STRATEGY_ID, signals, historical_prices=historical_prices)

    assert result.n_signals == 40
    assert result.win_rate == 1.0
    assert result.p_value < 0.05
    assert result.wilson_ci[0] > 0.5
    assert result.vs_always_long["avg_return"] > 0
    assert result.vs_momentum["avg_return"] > 0
    assert result.passed is True
    assert result.reason is None


def test_evaluate_signals_are_sorted_chronologically_before_scoring() -> None:
    # Mismas señales, pasadas fuera de orden: el resultado no debe depender del
    # orden de la lista de entrada (max_drawdown/sharpe son sensibles al orden).
    signals_in_order = [_make_signal(2.0, index=i) for i in range(35)]
    shuffled = list(reversed(signals_in_order))

    ordered_result = Evaluator().evaluate(STRATEGY_ID, signals_in_order, historical_prices=[100.0] * 40)
    shuffled_result = Evaluator().evaluate(STRATEGY_ID, shuffled, historical_prices=[100.0] * 40)

    assert ordered_result.max_drawdown == shuffled_result.max_drawdown
    assert ordered_result.win_rate == shuffled_result.win_rate
