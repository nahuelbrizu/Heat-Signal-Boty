"""
Resolver: asigna outcome a las hipotesis vencidas con el retorno realizado (Fase 3).

Formulas (todo en puntos porcentuales, 2.5 = +2.5%; contratos lineales USDT-M,
notional fijado al precio de entrada):
    entry = PendingHypothesis.entry_reference_price (mark price al emitir)
    exit  = PriceSource.price_at(symbol, created_at + horizon_s)
    LONG : gross_pnl_pct = (exit - entry) / entry * 100
    SHORT: gross_pnl_pct = (entry - exit) / entry * 100
    pnl_pct (neto) = gross_pnl_pct - cost_bps / 100        (1 bp = 0.01 pp)
    win = pnl_pct > 0   (pnl_pct == 0 cuenta como NO acierto, igual que el Evaluador)

Costos: `cost_bps` es el costo ROUND-TRIP (entrada + salida) sobre el notional de
entrada. Default `DEFAULT_COST_BPS = 10` = 2 x 5 bps, la tarifa taker estandar
(VIP0, sin descuento BNB) de Binance USDS-M al momento de escribir esto. Supuestos
explicitos: NO incluye slippage, funding ni impacto de mercado (se modela como si la
orden se llenara al precio de referencia). Es configurable; `cost_bps=0` da el PnL bruto.

Benchmark pasivo por resolucion: buy-and-hold en la MISMA ventana y con los mismos
precios, sin costos (un holder pasivo amortiza su unico costo de entrada en el tiempo):
    benchmark_pnl_pct = (exit - entry) / entry * 100
    alpha_pct = pnl_pct - benchmark_pnl_pct
Notar que un LONG con costos tiene alpha = -cost_bps/100 por construccion: elegir LONG
no agrega edge sobre "estar long". El edge aparece en cuando NO estar long. La
comparacion contra los 3 baselines de `baseline.py` (random / always-long / momentum)
la hace el `Evaluator` a nivel de estrategia (ver `report.py`).

Hipotesis de 1h (`timeframe="1h"`, ver `timeframes.py`): `due_at` cae en un cierre de vela de 1h y el
precio de salida es el CIERRE de esa vela (`BinanceHourlyClosePriceSource`), si el job recibe esa fuente.

Sin datos: si la fuente no da un precio confiable (`None`) y ya paso el periodo de
gracia `no_data_grace` desde el vencimiento, se registra `expired_no_data` (nunca se
descarta). Si todavia no paso la gracia, queda pendiente. Un `PriceSourceError`
(fallo transitorio) deja la hipotesis pendiente y se reporta en el resumen.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import UUID

from pydantic import BaseModel, Field

from ..schema.hypotheses import HypothesisStatus, PendingHypothesis, Resolution
from ..schema.signals import Direction, Signal
from .pending_store import HypothesisRecord, PendingHypothesisStore, ResolutionMetrics, due_at
from .price_source import PriceSource, PriceSourceError

DEFAULT_COST_BPS = 10.0
DEFAULT_NO_DATA_GRACE = timedelta(minutes=10)

_HUNDRED = Decimal(100)


def gross_pnl_pct(direction: Direction, entry: Decimal, exit_price: Decimal) -> float:
    """Retorno bruto de la direccion elegida, en puntos porcentuales."""
    if entry <= 0 or exit_price <= 0:
        raise ValueError(f"precios deben ser > 0: entry={entry}, exit={exit_price}")
    move = (exit_price - entry) / entry * _HUNDRED
    return float(move if direction == Direction.LONG else -move)


def net_pnl_pct(direction: Direction, entry: Decimal, exit_price: Decimal, cost_bps: float) -> float:
    """Retorno neto de costos round-trip, en puntos porcentuales."""
    return gross_pnl_pct(direction, entry, exit_price) - cost_bps / 100.0


def buy_and_hold_pct(entry: Decimal, exit_price: Decimal) -> float:
    """Benchmark pasivo: long sin costos en la misma ventana."""
    return gross_pnl_pct(Direction.LONG, entry, exit_price)


class ResolutionError(BaseModel):
    hypothesis_id: UUID
    error: str


class ResolverRunSummary(BaseModel):
    """Resumen de una corrida. Nada se pierde en silencio: todo cae en algun contador."""

    ran_at: datetime
    due: int = 0
    resolved: int = 0
    expired_no_data: int = 0
    deferred_no_data: int = 0
    errors: list[ResolutionError] = Field(default_factory=list)


class Resolver:
    def __init__(
        self,
        store: PendingHypothesisStore,
        price_source: PriceSource,
        *,
        cost_bps: float = DEFAULT_COST_BPS,
        no_data_grace: timedelta = DEFAULT_NO_DATA_GRACE,
        hourly_price_source: PriceSource | None = None,
    ) -> None:
        if cost_bps < 0:
            raise ValueError(f"cost_bps no puede ser negativo: {cost_bps}")
        self._store = store
        self._prices = price_source
        self._cost_bps = cost_bps
        self._grace = no_data_grace
        self._hourly = hourly_price_source

    def resolve_due(self, now: datetime | None = None) -> ResolverRunSummary:
        now = now or datetime.now(timezone.utc)
        summary = ResolverRunSummary(ran_at=now)
        for h in self._store.due(now):
            summary.due += 1
            try:
                outcome = self.resolve_one(h, now)
            except PriceSourceError as exc:
                summary.errors.append(ResolutionError(hypothesis_id=h.hypothesis_id, error=str(exc)))
                continue
            if outcome is None:
                summary.deferred_no_data += 1
            elif outcome.status == HypothesisStatus.RESOLVED:
                summary.resolved += 1
            else:
                summary.expired_no_data += 1
        return summary

    def source_for(self, h: PendingHypothesis) -> PriceSource:
        """Hipotesis de 1h con vencimiento en cierre de vela -> vela de 1h; el resto, la fuente general."""
        if h.timeframe == "1h" and self._hourly is not None:
            due = due_at(h)
            if due.minute == 0 and due.second == 0 and due.microsecond == 0:
                return self._hourly
        return self._prices

    def resolve_one(self, h: PendingHypothesis, now: datetime) -> Resolution | None:
        """Resuelve y persiste una hipotesis vencida. None = sin datos aun, dentro de la gracia.

        Propaga `PriceSourceError` (fallo transitorio) sin persistir nada.
        """
        exit_ts = due_at(h)
        if now < exit_ts:
            raise ValueError(f"hypothesis {h.hypothesis_id} todavia no vencio (due_at={exit_ts})")
        exit_price = self.source_for(h).price_at(h.symbol, exit_ts)
        if exit_price is not None and (not exit_price.is_finite() or exit_price <= 0):
            exit_price = None  # precio no confiable == sin dato

        if exit_price is None:
            if now < exit_ts + self._grace:
                return None
            resolution = Resolution(
                hypothesis_id=h.hypothesis_id,
                status=HypothesisStatus.EXPIRED_NO_DATA,
                resolved_at=now,
            )
            self._store.resolve(resolution)
            return resolution

        entry = h.entry_reference_price
        gross = gross_pnl_pct(h.direction, entry, exit_price)
        net = gross - self._cost_bps / 100.0
        benchmark = buy_and_hold_pct(entry, exit_price)
        resolution = Resolution(
            hypothesis_id=h.hypothesis_id,
            status=HypothesisStatus.RESOLVED,
            resolved_at=now,
            exit_price=exit_price,
            pnl_pct=net,
            win=net > 0,
        )
        metrics = ResolutionMetrics(
            gross_pnl_pct=gross,
            cost_bps=self._cost_bps,
            benchmark_pnl_pct=benchmark,
            alpha_pct=net - benchmark,
            exit_price_ts=exit_ts,
        )
        self._store.resolve(resolution, metrics)
        return resolution


def record_to_signal(record: HypothesisRecord) -> Signal:
    """Convierte una hipotesis RESUELTA en `Signal` para el Evaluador.

    signal_id = hypothesis_id (trazabilidad 1:1). `pnl` es el PnL neto por 1 unidad
    del activo base: entry * pnl_pct / 100 (consistente con pnl_pct, contrato lineal).
    """
    res = record.resolution
    if res is None or res.status != HypothesisStatus.RESOLVED:
        raise ValueError(
            f"solo hipotesis resueltas se convierten en Signal (hypothesis {record.hypothesis.hypothesis_id})"
        )
    assert res.exit_price is not None and res.pnl_pct is not None  # garantizado por el store
    h = record.hypothesis
    entry = h.entry_reference_price
    return Signal(
        signal_id=h.hypothesis_id,
        strategy_id=h.strategy_id,
        direction=h.direction,
        entry_price=entry,
        exit_price=res.exit_price,
        entry_time=h.created_at,
        exit_time=due_at(h),
        pnl=entry * Decimal(repr(res.pnl_pct)) / _HUNDRED,
        pnl_pct=res.pnl_pct,
    )
