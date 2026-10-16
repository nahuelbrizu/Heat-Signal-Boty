"""Tests de PendingHypothesisStore: append-only real, pre-registro obligatorio, una sola resolucion."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest

from heatsignal.evaluation.pending_store import (
    DuplicateHypothesisError,
    HypothesisAlreadyResolvedError,
    HypothesisSink,
    PendingHypothesisStore,
    ResolutionMetrics,
    UnknownHypothesisError,
    due_at,
)
from heatsignal.evaluation.strategy_registry import (
    StrategyHypothesis,
    StrategyRegistry,
    UnknownStrategyError,
)
from heatsignal.schema.hypotheses import HypothesisStatus, PendingHypothesis, Resolution
from heatsignal.schema.signals import Direction

T0 = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)


def _setup(tmp_path: Path) -> tuple[StrategyRegistry, PendingHypothesisStore, StrategyHypothesis]:
    registry = StrategyRegistry(tmp_path / "eval.db")
    strategy = StrategyHypothesis(description="s", entry_conditions={}, exit_conditions={}, author="t")
    registry.register(strategy)
    return registry, PendingHypothesisStore(tmp_path / "eval.db", registry), strategy


def _hyp(strategy_id, *, created_at=T0, horizon_s=3600, **kw) -> PendingHypothesis:
    base = dict(
        strategy_id=strategy_id,
        symbol="BTC-USDT",
        created_at=created_at,
        direction=Direction.LONG,
        confidence_score=0.7,
        entry_reference_price=Decimal("65000.10"),
        horizon_s=horizon_s,
        rationale="clusters similares reaccionaron en 60% de los casos",
    )
    base.update(kw)
    return PendingHypothesis(**base)


def _resolved(h: PendingHypothesis, *, pnl_pct=1.5, at=None) -> Resolution:
    return Resolution(
        hypothesis_id=h.hypothesis_id,
        status=HypothesisStatus.RESOLVED,
        resolved_at=at or due_at(h),
        exit_price=Decimal("66000"),
        pnl_pct=pnl_pct,
        win=pnl_pct > 0,
    )


def test_store_satisfies_sink_protocol(tmp_path: Path) -> None:
    _, store, _ = _setup(tmp_path)
    sink: HypothesisSink = store  # protocolo que consume el grafo
    assert callable(sink.add)


def test_add_and_list_roundtrip_exact(tmp_path: Path) -> None:
    _, store, s = _setup(tmp_path)
    h = _hyp(s.strategy_id, atr_normalized_trigger=1.25, heatmap_model_id="hm-v1", run_id="r1")
    store.add(h)
    records = store.list_by_strategy(s.strategy_id)
    assert len(records) == 1
    assert records[0].hypothesis == h
    assert records[0].hypothesis.entry_reference_price == Decimal("65000.10")  # Decimal exacto
    assert records[0].status == HypothesisStatus.PENDING
    assert records[0].resolution is None


def test_add_rejects_unregistered_strategy(tmp_path: Path) -> None:
    _, store, _ = _setup(tmp_path)
    with pytest.raises(UnknownStrategyError):
        store.add(_hyp(uuid4()))
    assert store.pending_count() == 0


def test_add_rejects_duplicate_id(tmp_path: Path) -> None:
    _, store, s = _setup(tmp_path)
    h = _hyp(s.strategy_id)
    store.add(h)
    with pytest.raises(DuplicateHypothesisError):
        store.add(h)


def test_add_rejects_naive_datetime(tmp_path: Path) -> None:
    _, store, s = _setup(tmp_path)
    with pytest.raises(ValueError, match="timezone-aware"):
        store.add(_hyp(s.strategy_id, created_at=datetime(2026, 10, 1, 12, 0)))


@pytest.mark.parametrize("price", [Decimal("0"), Decimal("-1"), Decimal("NaN")])
def test_add_rejects_non_positive_entry_price(tmp_path: Path, price: Decimal) -> None:
    _, store, s = _setup(tmp_path)
    with pytest.raises(ValueError, match="entry_reference_price"):
        store.add(_hyp(s.strategy_id, entry_reference_price=price))


def test_due_respects_horizon_and_resolution(tmp_path: Path) -> None:
    _, store, s = _setup(tmp_path)
    short = _hyp(s.strategy_id, horizon_s=600)
    long = _hyp(s.strategy_id, horizon_s=7200)
    store.add(long)
    store.add(short)

    assert store.due(T0 + timedelta(seconds=599)) == []
    assert [h.hypothesis_id for h in store.due(T0 + timedelta(seconds=600))] == [short.hypothesis_id]
    # ordenadas por due_at
    assert [h.hypothesis_id for h in store.due(T0 + timedelta(hours=3))] == [
        short.hypothesis_id,
        long.hypothesis_id,
    ]
    store.resolve(_resolved(short))
    assert [h.hypothesis_id for h in store.due(T0 + timedelta(hours=3))] == [long.hypothesis_id]


def test_due_accepts_other_timezones(tmp_path: Path) -> None:
    _, store, s = _setup(tmp_path)
    h = _hyp(s.strategy_id, horizon_s=3600)
    store.add(h)
    plus7 = timezone(timedelta(hours=7))
    assert store.due(datetime(2026, 10, 1, 19, 59, tzinfo=plus7)) == []
    assert len(store.due(datetime(2026, 10, 1, 20, 0, tzinfo=plus7))) == 1


def test_pending_count(tmp_path: Path) -> None:
    _, store, s = _setup(tmp_path)
    a, b = _hyp(s.strategy_id), _hyp(s.strategy_id)
    store.add(a)
    store.add(b)
    assert store.pending_count() == 2
    store.resolve(_resolved(a))
    assert store.pending_count() == 1
    store.resolve(
        Resolution(hypothesis_id=b.hypothesis_id, status=HypothesisStatus.EXPIRED_NO_DATA, resolved_at=due_at(b))
    )
    assert store.pending_count() == 0


def test_double_resolution_rejected(tmp_path: Path) -> None:
    _, store, s = _setup(tmp_path)
    h = _hyp(s.strategy_id)
    store.add(h)
    store.resolve(_resolved(h, pnl_pct=-0.5))
    with pytest.raises(HypothesisAlreadyResolvedError):
        store.resolve(_resolved(h, pnl_pct=3.0))  # "precio mas conveniente": prohibido
    with pytest.raises(HypothesisAlreadyResolvedError):
        store.resolve(
            Resolution(hypothesis_id=h.hypothesis_id, status=HypothesisStatus.EXPIRED_NO_DATA, resolved_at=due_at(h))
        )
    [record] = store.list_by_strategy(s.strategy_id)
    assert record.resolution is not None and record.resolution.pnl_pct == -0.5


def test_resolve_unknown_hypothesis(tmp_path: Path) -> None:
    _, store, _ = _setup(tmp_path)
    ghost = _hyp(uuid4())
    with pytest.raises(UnknownHypothesisError):
        store.resolve(_resolved(ghost))


def test_resolve_before_due_rejected(tmp_path: Path) -> None:
    _, store, s = _setup(tmp_path)
    h = _hyp(s.strategy_id, horizon_s=3600)
    store.add(h)
    with pytest.raises(ValueError, match="todavia no vencio"):
        store.resolve(_resolved(h, at=T0 + timedelta(seconds=3599)))


def test_resolve_rejects_incoherent_resolutions(tmp_path: Path) -> None:
    _, store, s = _setup(tmp_path)
    h = _hyp(s.strategy_id)
    store.add(h)
    when = due_at(h)
    bad = [
        Resolution(hypothesis_id=h.hypothesis_id, status=HypothesisStatus.PENDING, resolved_at=when),
        Resolution(hypothesis_id=h.hypothesis_id, status=HypothesisStatus.RESOLVED, resolved_at=when),
        Resolution(
            hypothesis_id=h.hypothesis_id,
            status=HypothesisStatus.RESOLVED,
            resolved_at=when,
            exit_price=Decimal("1"),
            pnl_pct=-1.0,
            win=True,  # win incoherente con pnl
        ),
        Resolution(
            hypothesis_id=h.hypothesis_id,
            status=HypothesisStatus.RESOLVED,
            resolved_at=when,
            exit_price=Decimal("1"),
            pnl_pct=0.0,
            win=True,  # pnl == 0 no es acierto
        ),
        Resolution(
            hypothesis_id=h.hypothesis_id,
            status=HypothesisStatus.EXPIRED_NO_DATA,
            resolved_at=when,
            exit_price=Decimal("1"),
        ),
    ]
    for resolution in bad:
        with pytest.raises(ValueError):
            store.resolve(resolution)
    with pytest.raises(ValueError):
        store.resolve(
            Resolution(hypothesis_id=h.hypothesis_id, status=HypothesisStatus.EXPIRED_NO_DATA, resolved_at=when),
            ResolutionMetrics(gross_pnl_pct=0, cost_bps=0, benchmark_pnl_pct=0, alpha_pct=0),
        )
    assert store.pending_count() == 1


def test_metrics_roundtrip(tmp_path: Path) -> None:
    _, store, s = _setup(tmp_path)
    h = _hyp(s.strategy_id)
    store.add(h)
    metrics = ResolutionMetrics(
        gross_pnl_pct=1.6, cost_bps=10.0, benchmark_pnl_pct=1.6, alpha_pct=-0.1, exit_price_ts=due_at(h)
    )
    store.resolve(_resolved(h), metrics)
    [record] = store.list_by_strategy(s.strategy_id)
    assert record.metrics == metrics
    assert record.status == HypothesisStatus.RESOLVED


@pytest.mark.parametrize(
    "sql",
    [
        "UPDATE pending_hypotheses SET symbol = 'ETH-USDT'",
        "DELETE FROM pending_hypotheses",
        "UPDATE hypothesis_resolutions SET pnl_pct = 99, win = 1",
        "DELETE FROM hypothesis_resolutions",
    ],
)
def test_append_only_triggers(tmp_path: Path, sql: str) -> None:
    _, store, s = _setup(tmp_path)
    h = _hyp(s.strategy_id)
    store.add(h)
    store.resolve(_resolved(h, pnl_pct=-1.0))
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        store._conn.execute(sql)
    store._conn.rollback()  # libera el lock de la transaccion implicita abortada
    # Tambien desde una conexion cruda independiente: es el motor el que bloquea.
    raw = sqlite3.connect(str(tmp_path / "eval.db"))
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        raw.execute(sql)
    raw.close()


def test_resolution_check_constraint_at_engine_level(tmp_path: Path) -> None:
    _, store, s = _setup(tmp_path)
    h = _hyp(s.strategy_id)
    store.add(h)
    with pytest.raises(sqlite3.IntegrityError):
        store._conn.execute(
            "INSERT INTO hypothesis_resolutions (hypothesis_id, status, resolved_at) VALUES (?, 'resolved', 'x')",
            (str(h.hypothesis_id),),
        )


def test_list_by_strategy_isolated_and_persistent(tmp_path: Path) -> None:
    registry, store, s = _setup(tmp_path)
    other = StrategyHypothesis(description="o", entry_conditions={}, exit_conditions={}, author="t")
    registry.register(other)
    later = _hyp(s.strategy_id, created_at=T0 + timedelta(minutes=5))
    first = _hyp(s.strategy_id)
    store.add(later)
    store.add(first)
    store.add(_hyp(other.strategy_id, created_at=T0 + timedelta(hours=1)))
    store.close()

    reopened = PendingHypothesisStore(tmp_path / "eval.db", registry)
    ids = [r.hypothesis.hypothesis_id for r in reopened.list_by_strategy(s.strategy_id)]
    assert ids == [first.hypothesis_id, later.hypothesis_id]
    assert reopened.strategy_ids() == [s.strategy_id, other.strategy_id]
