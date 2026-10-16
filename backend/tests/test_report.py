"""Tests del reporte por estrategia: gating de modo shadow, logging append-only, vista publica."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from heatsignal.evaluation.evaluator import EvalResult
from heatsignal.evaluation.hypothesis_log import HypothesisLog
from heatsignal.evaluation.job import JobConfig, main, run_once
from heatsignal.evaluation.pending_store import PendingHypothesisStore
from heatsignal.evaluation.price_source import StaticPriceSource
from heatsignal.evaluation.report import ReportLog, StrategyReporter
from heatsignal.evaluation.resolver import Resolver
from heatsignal.evaluation.strategy_registry import (
    StrategyHypothesis,
    StrategyRegistry,
    UnknownStrategyError,
)
from heatsignal.schema.hypotheses import PendingHypothesis
from heatsignal.schema.signals import Direction, Signal

T0 = datetime(2026, 9, 1, 0, 0, tzinfo=timezone.utc)
H = 3600


class FakeEvaluator:
    """Evaluador controlado: el gating no debe depender de poder fabricar edge real."""

    def __init__(self, passed: bool) -> None:
        self.passed = passed
        self.calls: list[tuple[UUID, list[Signal], list[float]]] = []

    def evaluate(self, strategy_id: UUID, signals: list[Signal], historical_prices: list[float]) -> EvalResult:
        self.calls.append((strategy_id, signals, historical_prices))
        return EvalResult(
            strategy_id=strategy_id,
            n_signals=len(signals),
            win_rate=0.7,
            avg_return=0.8,
            sharpe_ratio=1.5,
            max_drawdown=-0.05,
            profit_factor=2.0,
            wilson_ci=(0.55, 0.82),
            p_value=0.01,
            vs_random={"avg_return": 0.5},
            vs_always_long={"avg_return": 0.4},
            vs_momentum={"avg_return": 0.3},
            passed=self.passed,
            reason=None if self.passed else "not_significant",
        )


class Env:
    def __init__(self, tmp_path: Path) -> None:
        db = tmp_path / "eval.db"
        self.db = db
        self.registry = StrategyRegistry(db)
        self.strategy = StrategyHypothesis(description="s", entry_conditions={}, exit_conditions={}, author="t")
        self.registry.register(self.strategy)
        self.store = PendingHypothesisStore(db, self.registry)
        self.log = HypothesisLog(db)
        self.report_log = ReportLog(db)

    def add(self, *, created_at=T0, direction=Direction.LONG, conf=0.7, entry="100", symbol="BTC-USDT"):
        h = PendingHypothesis(
            strategy_id=self.strategy.strategy_id,
            symbol=symbol,
            created_at=created_at,
            direction=direction,
            confidence_score=conf,
            entry_reference_price=Decimal(entry),
            horizon_s=H,
            rationale="x",
        )
        self.store.add(h)
        return h

    def resolve_all(self, exit_price: str, now: datetime, symbol="BTC-USDT", cost_bps=10.0) -> None:
        # Precio constante, una observacion por minuto en los 2 primeros dias (cubre todas las salidas
        # de estos tests; hipotesis creadas mas tarde quedan sin dato -> expired si ya vencieron).
        obs = [(T0 + timedelta(minutes=m), Decimal(exit_price)) for m in range(0, 60 * 24 * 2)]
        Resolver(self.store, StaticPriceSource({symbol: obs}), cost_bps=cost_bps).resolve_due(now)

    def reporter(self, evaluator=None, **kw) -> StrategyReporter:
        return StrategyReporter(
            self.store, self.registry, self.log, evaluator=evaluator, report_log=self.report_log, **kw
        )


def _populate(env: Env, n: int = 5) -> None:
    for i in range(n):
        env.add(created_at=T0 + timedelta(hours=i))


def test_publishable_only_when_shadow_elapsed_and_passed(tmp_path: Path) -> None:
    env = Env(tmp_path)
    _populate(env)
    now = T0 + timedelta(days=15)
    env.resolve_all("102", now)
    report = env.reporter(FakeEvaluator(passed=True)).evaluate(env.strategy.strategy_id, now)
    assert report.publishable is True
    assert report.publish_blockers == []
    public = report.public_view()
    assert public["status"] == "published"
    assert public["win_rate"] == 0.7 and public["n"] == 5
    assert public["brier"] == report.calibration.brier


@pytest.mark.parametrize(
    "elapsed, expected",
    [
        (timedelta(days=14) - timedelta(seconds=1), False),
        (timedelta(days=14), True),  # borde: now >= primera + 14d
    ],
)
def test_shadow_boundary(tmp_path: Path, elapsed: timedelta, expected: bool) -> None:
    env = Env(tmp_path)
    _populate(env)
    now = T0 + elapsed
    env.resolve_all("102", now)
    report = env.reporter(FakeEvaluator(passed=True)).evaluate(env.strategy.strategy_id, now)
    assert report.shadow.elapsed is expected
    assert report.shadow.first_hypothesis_at == T0
    assert report.shadow.shadow_ends_at == T0 + timedelta(days=14)
    assert report.publishable is expected
    if not expected:
        assert report.publish_blockers == ["shadow_period_not_elapsed"]


def test_shadow_period_is_configurable(tmp_path: Path) -> None:
    env = Env(tmp_path)
    _populate(env)
    now = T0 + timedelta(days=15)
    env.resolve_all("102", now)
    report = env.reporter(FakeEvaluator(passed=True), shadow_period=timedelta(days=28)).evaluate(
        env.strategy.strategy_id, now
    )
    assert report.publishable is False
    assert "shadow_period_not_elapsed" in report.publish_blockers


def test_shadow_starts_at_first_hypothesis_of_that_strategy(tmp_path: Path) -> None:
    env = Env(tmp_path)
    env.add(created_at=T0 + timedelta(days=10))
    env.add(created_at=T0 + timedelta(days=2))  # la primera no es la primera agregada
    report = env.reporter(FakeEvaluator(passed=True)).evaluate(env.strategy.strategy_id, T0 + timedelta(days=16))
    assert report.shadow.first_hypothesis_at == T0 + timedelta(days=2)
    assert report.shadow.elapsed is True


def test_not_passed_is_never_publishable_and_hides_numbers(tmp_path: Path) -> None:
    env = Env(tmp_path)
    _populate(env)
    now = T0 + timedelta(days=30)
    env.resolve_all("102", now)
    report = env.reporter(FakeEvaluator(passed=False)).evaluate(env.strategy.strategy_id, now)
    assert report.publishable is False
    assert report.publish_blockers == ["evaluator_not_passed: not_significant"]
    public = report.public_view()
    assert public["status"] == "shadow"
    for hidden in ("win_rate", "wilson_ci", "p_value", "brier", "alpha_vs_buy_and_hold", "avg_return_pct"):
        assert hidden not in public
    assert "win_rate" not in json.dumps(public)


def test_expired_rate_blocks_publication(tmp_path: Path) -> None:
    env = Env(tmp_path)
    for i in range(4):
        env.add(created_at=T0 + timedelta(hours=i))
    env.add(created_at=T0, symbol="ETH-USDT")  # sin precio -> expired
    env.add(created_at=T0 + timedelta(hours=1), symbol="ETH-USDT")
    now = T0 + timedelta(days=15)
    env.resolve_all("102", now)
    report = env.reporter(FakeEvaluator(passed=True)).evaluate(env.strategy.strategy_id, now)
    assert report.counts.expired_no_data == 2 and report.counts.resolved == 4
    assert report.counts.expired_rate == pytest.approx(2 / 6)
    assert report.publishable is False
    assert report.publish_blockers == ["expired_no_data_rate_above_0.20: 0.333"]


def test_no_hypotheses_not_publishable(tmp_path: Path) -> None:
    env = Env(tmp_path)
    report = env.reporter(FakeEvaluator(passed=True)).evaluate(env.strategy.strategy_id, T0 + timedelta(days=100))
    assert report.publishable is False
    assert report.publish_blockers == ["no_hypotheses", "shadow_period_not_elapsed"]
    assert report.counts.total == 0 and report.calibration.n == 0 and report.calibration.brier is None


def test_counts_alpha_calibration_and_signals_passed_to_evaluator(tmp_path: Path) -> None:
    env = Env(tmp_path)
    env.add(created_at=T0, direction=Direction.LONG, conf=0.8)
    env.add(created_at=T0 + timedelta(hours=1), direction=Direction.SHORT, conf=0.6)
    env.add(created_at=T0 + timedelta(days=20), direction=Direction.LONG)  # todavia pendiente
    now = T0 + timedelta(days=15)
    env.resolve_all("102", now)  # LONG: neto +1.9 (alfa -0.1); SHORT: neto -2.1 (alfa -4.1)
    fake = FakeEvaluator(passed=False)
    report = env.reporter(fake).evaluate(env.strategy.strategy_id, now)

    assert report.counts.model_dump() == {
        "total": 3, "pending": 1, "resolved": 2, "expired_no_data": 0, "expired_rate": 0.0
    }
    alpha = report.alpha_vs_buy_and_hold
    assert alpha.n == 2
    assert alpha.mean_pnl_pct == pytest.approx((1.9 - 2.1) / 2)
    assert alpha.mean_benchmark_pct == pytest.approx(2.0)
    assert alpha.mean_alpha_pct == pytest.approx((-0.1 - 4.1) / 2)
    assert alpha.n_positive_alpha == 0
    # Brier: (0.8-1)^2 + (0.6-0)^2 = 0.04 + 0.36 -> /2 = 0.2
    assert report.calibration.brier == pytest.approx(0.2)

    [(sid, signals, prices)] = fake.calls
    assert sid == env.strategy.strategy_id
    assert [s.direction for s in signals] == [Direction.LONG, Direction.SHORT]  # cronologico
    assert prices == [100.0, 100.0]


def test_every_evaluation_is_logged_append_only(tmp_path: Path) -> None:
    env = Env(tmp_path)
    _populate(env, 3)
    reporter = env.reporter()  # Evaluador real: n=3 -> insufficient_data
    now = T0 + timedelta(days=15)
    env.resolve_all("102", now)
    r1 = reporter.evaluate(env.strategy.strategy_id, now)
    r2 = reporter.evaluate(env.strategy.strategy_id, now + timedelta(hours=1))
    assert r1.evaluation.reason == "insufficient_data" and r1.publishable is False
    assert r1.evaluation.n_signals == 3
    history = env.log.get_history()
    assert len(history) == 2  # fallidas tambien quedan
    assert all(not e.passed for e in history)
    assert env.report_log.count() == 2
    assert env.report_log.latest(env.strategy.strategy_id)["generated_at"] == r2.generated_at.isoformat().replace(
        "+00:00", "Z"
    )
    for sql in ("UPDATE strategy_reports SET publishable = 1", "DELETE FROM strategy_reports"):
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            env.report_log._conn.execute(sql)
        env.report_log._conn.rollback()


def test_unknown_strategy_rejected(tmp_path: Path) -> None:
    env = Env(tmp_path)
    with pytest.raises(UnknownStrategyError):
        env.reporter().evaluate(uuid4(), T0)


def test_report_serializable(tmp_path: Path) -> None:
    env = Env(tmp_path)
    _populate(env, 2)
    now = T0 + timedelta(days=1)
    env.resolve_all("98", now)
    report = env.reporter().evaluate(env.strategy.strategy_id, now)
    payload = json.loads(report.model_dump_json())
    assert payload["counts"]["resolved"] == 2
    assert payload["publishable"] is False
    json.dumps(report.public_view())


# --------------------------------------------------------------------------- job


def test_run_once_resolves_and_evaluates_all(tmp_path: Path) -> None:
    env = Env(tmp_path)
    empty = StrategyHypothesis(description="sin hipotesis", entry_conditions={}, exit_conditions={}, author="t")
    env.registry.register(empty)
    _populate(env, 3)
    exit_obs = [(T0 + timedelta(hours=i + 1), Decimal("101")) for i in range(3)]
    now = T0 + timedelta(days=2)
    out = run_once(
        registry=env.registry,
        store=env.store,
        hypothesis_log=env.log,
        report_log=env.report_log,
        price_source=StaticPriceSource({"BTC-USDT": exit_obs}),
        config=JobConfig(cost_bps=0),
        now=now,
    )
    assert out["resolution"]["resolved"] == 3
    assert out["pending_count"] == 0
    assert {r["strategy_id"] for r in out["reports"]} == {
        str(env.strategy.strategy_id),
        str(empty.strategy_id),
    }
    assert all(p["publishable"] is False for p in out["public"])
    assert len(env.log.get_history()) == 2
    json.dumps(out)


def test_job_main_cli(tmp_path: Path, monkeypatch, capsys) -> None:
    env = Env(tmp_path)
    env.add(created_at=T0)
    obs = [(T0 + timedelta(hours=1), Decimal("103"))]

    import heatsignal.evaluation.job as job

    monkeypatch.setattr(job, "BinanceKlinePriceSource", lambda **kw: StaticPriceSource({"BTC-USDT": obs}))
    code = main(["--db", str(env.db), "--cost-bps", "0", "--public-only"])
    assert code == 0
    printed = json.loads(capsys.readouterr().out)
    [public] = printed["public"]
    assert public["publishable"] is False and "win_rate" not in public
    assert public["counts"]["resolved"] == 1
