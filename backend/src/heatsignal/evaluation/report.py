"""
Reporte de evaluacion por estrategia (Fase 3): junta Evaluador + calibracion +
alfa vs buy-and-hold + conteos de `expired_no_data` + gating de modo shadow.

Modo shadow (obligatorio antes de publicar, ver phase-3-evaluation.md):
    publishable = True  SOLO SI se cumplen TODAS:
      1. Paso el periodo shadow: now >= primera hipotesis de la estrategia + shadow_period
         (default 14 dias). Sin hipotesis, nunca.
      2. `Evaluator.evaluate()` aprobo (passed=True: n>=30, p<0.05, Wilson lower>0.5,
         Sharpe>1, supera los 3 baselines). El criterio del Evaluador no se relaja aca.
      3. La tasa de `expired_no_data` sobre las vencidas (resolved + expired) no supera
         `max_expired_rate` (default 20%). Si faltan muchos outcomes por huecos de datos,
         el win-rate de lo que si se resolvio no es representativo (sin fallos silenciosos).
    Cualquier otro caso -> publishable=False con `publish_blockers` explicitos.
    `public_view()` es lo unico que deberia consumir una API publica: si no es
    publicable NO incluye win-rate, PnL, alfa ni calibracion — solo estado y conteos.

Toda evaluacion se loguea en `HypothesisLog` (append-only), pase o no pase; ademas el
reporte completo (con calibracion y shadow) se guarda en `ReportLog`, tambien append-only.
"""

from __future__ import annotations

import json
import sqlite3
import statistics
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field

from ..schema.hypotheses import HypothesisStatus
from .calibration import DEFAULT_N_BINS, CalibrationReport, calibration_report
from .evaluator import EvalResult, Evaluator
from .hypothesis_log import HypothesisLog
from .pending_store import HypothesisRecord, PendingHypothesisStore
from .resolver import record_to_signal
from .strategy_registry import StrategyRegistry, UnknownStrategyError

DEFAULT_SHADOW_PERIOD = timedelta(days=14)
DEFAULT_MAX_EXPIRED_RATE = 0.20


class HypothesisCounts(BaseModel):
    total: int
    pending: int
    resolved: int
    expired_no_data: int
    expired_rate: float | None = Field(
        description="expired_no_data / (resolved + expired_no_data); None si no hay vencidas"
    )


class AlphaSummary(BaseModel):
    """Alfa por resolucion vs buy-and-hold en la misma ventana (ver resolver.py)."""

    n: int
    mean_pnl_pct: float | None
    mean_benchmark_pct: float | None
    mean_alpha_pct: float | None
    n_positive_alpha: int


class ShadowStatus(BaseModel):
    first_hypothesis_at: datetime | None
    shadow_period_s: float
    shadow_ends_at: datetime | None
    elapsed: bool


class StrategyReport(BaseModel):
    strategy_id: UUID
    generated_at: datetime
    counts: HypothesisCounts
    evaluation: EvalResult
    calibration: CalibrationReport
    alpha_vs_buy_and_hold: AlphaSummary
    shadow: ShadowStatus
    publishable: bool
    publish_blockers: list[str]

    def public_view(self) -> dict[str, Any]:
        """Vista segura para API/dashboard publico: sin numeros de performance en shadow."""
        base: dict[str, Any] = {
            "strategy_id": str(self.strategy_id),
            "generated_at": self.generated_at.isoformat(),
            "publishable": self.publishable,
            "status": "published" if self.publishable else "shadow",
            "counts": self.counts.model_dump(mode="json"),
        }
        if not self.publishable:
            base["publish_blockers"] = list(self.publish_blockers)
            return base
        ev = self.evaluation
        base.update(
            {
                "n": ev.n_signals,
                "win_rate": ev.win_rate,
                "wilson_ci": list(ev.wilson_ci),
                "p_value": ev.p_value,
                "avg_return_pct": ev.avg_return,
                "sharpe_ratio": ev.sharpe_ratio,
                "max_drawdown": ev.max_drawdown,
                "vs_random": ev.vs_random,
                "vs_always_long": ev.vs_always_long,
                "vs_momentum": ev.vs_momentum,
                "alpha_vs_buy_and_hold": self.alpha_vs_buy_and_hold.model_dump(mode="json"),
                "brier": self.calibration.brier,
                "brier_skill": self.calibration.brier_skill,
                "ece": self.calibration.ece,
                "calibration_buckets": [b.model_dump(mode="json") for b in self.calibration.buckets],
            }
        )
        return base


def _counts(records: list[HypothesisRecord]) -> HypothesisCounts:
    pending = sum(1 for r in records if r.status == HypothesisStatus.PENDING)
    resolved = sum(1 for r in records if r.status == HypothesisStatus.RESOLVED)
    expired = sum(1 for r in records if r.status == HypothesisStatus.EXPIRED_NO_DATA)
    matured = resolved + expired
    return HypothesisCounts(
        total=len(records),
        pending=pending,
        resolved=resolved,
        expired_no_data=expired,
        expired_rate=None if matured == 0 else expired / matured,
    )


def _alpha_summary(resolved: list[HypothesisRecord]) -> AlphaSummary:
    with_metrics = [r for r in resolved if r.metrics is not None]
    if not with_metrics:
        return AlphaSummary(
            n=0, mean_pnl_pct=None, mean_benchmark_pct=None, mean_alpha_pct=None, n_positive_alpha=0
        )
    alphas = [r.metrics.alpha_pct for r in with_metrics]  # type: ignore[union-attr]
    return AlphaSummary(
        n=len(with_metrics),
        mean_pnl_pct=statistics.mean(r.resolution.pnl_pct for r in with_metrics),  # type: ignore[union-attr,misc]
        mean_benchmark_pct=statistics.mean(r.metrics.benchmark_pnl_pct for r in with_metrics),  # type: ignore[union-attr]
        mean_alpha_pct=statistics.mean(alphas),
        n_positive_alpha=sum(1 for a in alphas if a > 0),
    )


def shadow_status(
    records: list[HypothesisRecord], now: datetime, shadow_period: timedelta
) -> ShadowStatus:
    if not records:
        return ShadowStatus(
            first_hypothesis_at=None,
            shadow_period_s=shadow_period.total_seconds(),
            shadow_ends_at=None,
            elapsed=False,
        )
    first = min(r.hypothesis.created_at for r in records)
    ends = first + shadow_period
    return ShadowStatus(
        first_hypothesis_at=first,
        shadow_period_s=shadow_period.total_seconds(),
        shadow_ends_at=ends,
        elapsed=now >= ends,
    )


class StrategyReporter:
    """Evalua estrategias combinando Evaluador + calibracion + shadow, y loguea todo."""

    def __init__(
        self,
        store: PendingHypothesisStore,
        registry: StrategyRegistry,
        hypothesis_log: HypothesisLog,
        *,
        evaluator: Evaluator | None = None,
        report_log: ReportLog | None = None,
        shadow_period: timedelta = DEFAULT_SHADOW_PERIOD,
        max_expired_rate: float = DEFAULT_MAX_EXPIRED_RATE,
        n_bins: int = DEFAULT_N_BINS,
    ) -> None:
        self._store = store
        self._registry = registry
        self._log = hypothesis_log
        self._evaluator = evaluator or Evaluator()
        self._report_log = report_log
        self._shadow_period = shadow_period
        self._max_expired_rate = max_expired_rate
        self._n_bins = n_bins

    def evaluate(self, strategy_id: UUID, now: datetime | None = None) -> StrategyReport:
        now = now or datetime.now(timezone.utc)
        strategy = self._registry.get(strategy_id)
        if strategy is None:
            raise UnknownStrategyError(f"strategy_id {strategy_id} no esta pre-registrado")

        records = self._store.list_by_strategy(strategy_id)
        resolved = [r for r in records if r.status == HypothesisStatus.RESOLVED]
        signals = sorted((record_to_signal(r) for r in resolved), key=lambda s: s.entry_time)
        # Contrato del Evaluador: historical_prices[i] = precio conocido al entrar en la
        # senal i (cronologico). Usamos el precio de entrada de cada senal resuelta.
        historical_prices = [float(s.entry_price) for s in signals]
        eval_result = self._evaluator.evaluate(strategy_id, signals, historical_prices)

        calibration = calibration_report(
            [r.hypothesis.confidence_score for r in resolved],
            [bool(r.resolution.win) for r in resolved],  # type: ignore[union-attr]
            n_bins=self._n_bins,
        )
        counts = _counts(records)
        shadow = shadow_status(records, now, self._shadow_period)

        blockers: list[str] = []
        if not records:
            blockers.append("no_hypotheses")
        if not shadow.elapsed:
            blockers.append("shadow_period_not_elapsed")
        if not eval_result.passed:
            blockers.append(f"evaluator_not_passed: {eval_result.reason}")
        if counts.expired_rate is not None and counts.expired_rate > self._max_expired_rate:
            blockers.append(
                f"expired_no_data_rate_above_{self._max_expired_rate:.2f}: {counts.expired_rate:.3f}"
            )

        report = StrategyReport(
            strategy_id=strategy_id,
            generated_at=now,
            counts=counts,
            evaluation=eval_result,
            calibration=calibration,
            alpha_vs_buy_and_hold=_alpha_summary(resolved),
            shadow=shadow,
            publishable=not blockers,
            publish_blockers=blockers,
        )
        # Se loguea SIEMPRE, pase o no (anti-p-hacking).
        self._log.log_result(strategy, eval_result)
        if self._report_log is not None:
            self._report_log.append(report)
        return report

    def evaluate_all(self, now: datetime | None = None) -> list[StrategyReport]:
        """Evalua TODAS las estrategias pre-registradas, tengan o no hipotesis."""
        now = now or datetime.now(timezone.utc)
        return [self.evaluate(s.strategy_id, now) for s in self._registry.get_all()]


class ReportLog:
    """Historial append-only de reportes completos (incluye calibracion y shadow)."""

    def __init__(self, db_path: str | Path) -> None:
        self._conn = sqlite3.connect(str(db_path))
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS strategy_reports (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                strategy_id  TEXT NOT NULL,
                generated_at TEXT NOT NULL,
                publishable  INTEGER NOT NULL,
                report       TEXT NOT NULL
            );

            CREATE TRIGGER IF NOT EXISTS trg_strategy_reports_no_update
            BEFORE UPDATE ON strategy_reports
            BEGIN
                SELECT RAISE(ABORT, 'strategy_reports es append-only: UPDATE no permitido');
            END;

            CREATE TRIGGER IF NOT EXISTS trg_strategy_reports_no_delete
            BEFORE DELETE ON strategy_reports
            BEGIN
                SELECT RAISE(ABORT, 'strategy_reports es append-only: DELETE no permitido');
            END;
            """
        )
        self._conn.commit()

    def append(self, report: StrategyReport) -> None:
        self._conn.execute(
            "INSERT INTO strategy_reports (strategy_id, generated_at, publishable, report) VALUES (?, ?, ?, ?)",
            (
                str(report.strategy_id),
                report.generated_at.isoformat(),
                int(report.publishable),
                report.model_dump_json(),
            ),
        )
        self._conn.commit()

    def latest(self, strategy_id: UUID) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT report FROM strategy_reports WHERE strategy_id = ? ORDER BY id DESC LIMIT 1",
            (str(strategy_id),),
        ).fetchone()
        return None if row is None else json.loads(row[0])

    def count(self) -> int:
        return int(self._conn.execute("SELECT COUNT(*) FROM strategy_reports").fetchone()[0])

    def close(self) -> None:
        self._conn.close()
