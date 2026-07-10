"""Tests de HypothesisLog: append-only real, log_result y las dos vistas de lectura."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from heatsignal.evaluation.evaluator import EvalResult
from heatsignal.evaluation.hypothesis_log import HypothesisLog
from heatsignal.evaluation.strategy_registry import StrategyHypothesis


def _make_hypothesis() -> StrategyHypothesis:
    return StrategyHypothesis(
        description="test hypothesis",
        entry_conditions={"x": 1},
        exit_conditions={"y": 2},
        author="tester",
    )


def _make_eval_result(strategy_id: UUID, *, passed: bool, n_signals: int = 5) -> EvalResult:
    return EvalResult(
        strategy_id=strategy_id,
        n_signals=n_signals,
        win_rate=0.6,
        avg_return=1.2,
        sharpe_ratio=0.5,
        max_drawdown=-0.1,
        profit_factor=1.8,
        wilson_ci=(0.4, 0.8),
        p_value=0.2,
        vs_random={"avg_return": 0.5},
        vs_always_long={"avg_return": 0.3},
        vs_momentum={"avg_return": 0.4},
        passed=passed,
        reason=None if passed else "insufficient_data",
    )


def test_log_result_and_get_history(tmp_path: Path) -> None:
    log = HypothesisLog(tmp_path / "log.db")
    hypothesis = _make_hypothesis()
    result = _make_eval_result(hypothesis.strategy_id, passed=True)

    log.log_result(hypothesis, result)
    history = log.get_history()

    assert len(history) == 1
    entry = history[0]
    assert entry.strategy_id == hypothesis.strategy_id
    assert entry.passed is True
    assert entry.n_signals == result.n_signals
    assert entry.p_value == result.p_value
    assert entry.sharpe == result.sharpe_ratio
    assert entry.result["win_rate"] == result.win_rate


def test_failed_hypotheses_are_logged_too(tmp_path: Path) -> None:
    log = HypothesisLog(tmp_path / "log.db")
    hypothesis = _make_hypothesis()
    failed_result = _make_eval_result(hypothesis.strategy_id, passed=False, n_signals=3)

    log.log_result(hypothesis, failed_result)

    history = log.get_history()
    assert len(history) == 1
    assert history[0].passed is False
    assert log.get_passed() == []


def test_get_passed_filters_only_passed(tmp_path: Path) -> None:
    log = HypothesisLog(tmp_path / "log.db")
    passing = _make_hypothesis()
    failing = _make_hypothesis()

    log.log_result(passing, _make_eval_result(passing.strategy_id, passed=True))
    log.log_result(failing, _make_eval_result(failing.strategy_id, passed=False))

    passed_entries = log.get_passed()
    assert len(passed_entries) == 1
    assert passed_entries[0].strategy_id == passing.strategy_id


def test_repeated_evaluation_appends_instead_of_overwriting(tmp_path: Path) -> None:
    log = HypothesisLog(tmp_path / "log.db")
    hypothesis = _make_hypothesis()

    log.log_result(hypothesis, _make_eval_result(hypothesis.strategy_id, passed=False, n_signals=10))
    log.log_result(hypothesis, _make_eval_result(hypothesis.strategy_id, passed=True, n_signals=40))

    history = log.get_history()
    assert len(history) == 2
    assert [e.n_signals for e in history] == [10, 40]


def test_hypothesis_results_table_rejects_update_at_db_level(tmp_path: Path) -> None:
    log = HypothesisLog(tmp_path / "log.db")
    hypothesis = _make_hypothesis()
    log.log_result(hypothesis, _make_eval_result(hypothesis.strategy_id, passed=True))

    with pytest.raises(sqlite3.IntegrityError):
        log._conn.execute("UPDATE hypothesis_results SET passed = 0")


def test_hypothesis_results_table_rejects_delete_at_db_level(tmp_path: Path) -> None:
    log = HypothesisLog(tmp_path / "log.db")
    hypothesis = _make_hypothesis()
    log.log_result(hypothesis, _make_eval_result(hypothesis.strategy_id, passed=True))

    with pytest.raises(sqlite3.IntegrityError):
        log._conn.execute("DELETE FROM hypothesis_results")
