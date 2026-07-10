"""Tests de StrategyRegistry: pre-registro inmutable y append-only real (triggers SQLite)."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from uuid import uuid4

import pytest

from heatsignal.evaluation.strategy_registry import (
    StrategyAlreadyRegisteredError,
    StrategyHypothesis,
    StrategyRegistry,
    StrategyStatus,
    UnknownStrategyError,
)


def _make_hypothesis(**overrides: object) -> StrategyHypothesis:
    defaults: dict = dict(
        description="liquidation cluster reversal",
        entry_conditions={"atr_multiple": 1.5, "side": "long"},
        exit_conditions={"take_profit_pct": 2.0, "stop_loss_pct": 1.0},
        author="estratega-alcista",
    )
    defaults.update(overrides)
    return StrategyHypothesis(**defaults)


def test_register_and_get_roundtrip(tmp_path: Path) -> None:
    registry = StrategyRegistry(tmp_path / "registry.db")
    hypothesis = _make_hypothesis()

    registry.register(hypothesis)
    fetched = registry.get(hypothesis.strategy_id)

    assert fetched is not None
    assert fetched.strategy_id == hypothesis.strategy_id
    assert fetched.description == hypothesis.description
    assert fetched.entry_conditions == hypothesis.entry_conditions
    assert fetched.exit_conditions == hypothesis.exit_conditions
    assert fetched.author == hypothesis.author
    assert fetched.status == StrategyStatus.PENDING


def test_get_unknown_strategy_returns_none(tmp_path: Path) -> None:
    registry = StrategyRegistry(tmp_path / "registry.db")
    assert registry.get(uuid4()) is None


def test_register_duplicate_strategy_id_raises(tmp_path: Path) -> None:
    registry = StrategyRegistry(tmp_path / "registry.db")
    hypothesis = _make_hypothesis()
    registry.register(hypothesis)

    with pytest.raises(StrategyAlreadyRegisteredError):
        registry.register(hypothesis)


def test_set_status_appends_event_and_get_reflects_latest(tmp_path: Path) -> None:
    registry = StrategyRegistry(tmp_path / "registry.db")
    hypothesis = _make_hypothesis()
    registry.register(hypothesis)

    registry.set_status(hypothesis.strategy_id, StrategyStatus.RUNNING)
    assert registry.get(hypothesis.strategy_id).status == StrategyStatus.RUNNING

    registry.set_status(hypothesis.strategy_id, StrategyStatus.EVALUATED)
    assert registry.get(hypothesis.strategy_id).status == StrategyStatus.EVALUATED


def test_set_status_unknown_strategy_raises(tmp_path: Path) -> None:
    registry = StrategyRegistry(tmp_path / "registry.db")
    with pytest.raises(UnknownStrategyError):
        registry.set_status(uuid4(), StrategyStatus.RUNNING)


def test_get_all_ordered_by_registered_at(tmp_path: Path) -> None:
    registry = StrategyRegistry(tmp_path / "registry.db")
    first = _make_hypothesis(description="first")
    registry.register(first)
    second = _make_hypothesis(description="second")
    registry.register(second)

    all_hypotheses = registry.get_all()
    assert [h.description for h in all_hypotheses] == ["first", "second"]


def test_strategy_hypotheses_table_rejects_update_at_db_level(tmp_path: Path) -> None:
    registry = StrategyRegistry(tmp_path / "registry.db")
    hypothesis = _make_hypothesis()
    registry.register(hypothesis)

    with pytest.raises(sqlite3.IntegrityError):
        registry._conn.execute(
            "UPDATE strategy_hypotheses SET description = 'tampered' WHERE strategy_id = ?",
            (str(hypothesis.strategy_id),),
        )


def test_strategy_hypotheses_table_rejects_delete_at_db_level(tmp_path: Path) -> None:
    registry = StrategyRegistry(tmp_path / "registry.db")
    hypothesis = _make_hypothesis()
    registry.register(hypothesis)

    with pytest.raises(sqlite3.IntegrityError):
        registry._conn.execute(
            "DELETE FROM strategy_hypotheses WHERE strategy_id = ?",
            (str(hypothesis.strategy_id),),
        )


def test_strategy_status_events_table_rejects_update_at_db_level(tmp_path: Path) -> None:
    registry = StrategyRegistry(tmp_path / "registry.db")
    hypothesis = _make_hypothesis()
    registry.register(hypothesis)

    with pytest.raises(sqlite3.IntegrityError):
        registry._conn.execute(
            "UPDATE strategy_status_events SET status = 'evaluated' WHERE strategy_id = ?",
            (str(hypothesis.strategy_id),),
        )
