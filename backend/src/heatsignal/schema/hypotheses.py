"""
Hipotesis pendientes: lo que emite el Estratega/Sintetizador del grafo y lo que la
Fase 3 resuelve con el retorno realizado.

Ciclo de vida (anti-p-hacking: nada se borra ni se pisa):
    PendingHypothesis (status=pending)
        -> Resolution (cuando vence `horizon_s` y hay precio de salida)
        -> se convierte en `schema.signals.Signal` para el Evaluador.
    Si al vencer no hay precio de salida confiable, se resuelve como `expired_no_data`
    (queda registrada, no se descarta en silencio).

Cada hipotesis referencia un `strategy_id` YA pre-registrado en
`evaluation.strategy_registry.StrategyRegistry`. El Estratega no inventa estrategias.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from uuid import UUID, uuid4

from pydantic import BaseModel, Field

from .signals import Direction


class HypothesisStatus(str, Enum):
    PENDING = "pending"
    RESOLVED = "resolved"
    EXPIRED_NO_DATA = "expired_no_data"


class PendingHypothesis(BaseModel):
    hypothesis_id: UUID = Field(default_factory=uuid4)
    strategy_id: UUID = Field(description="Estrategia pre-registrada que esta hipotesis instancia")
    symbol: str = Field(description="Formato interno, p.ej. 'BTC-USDT'")
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    direction: Direction
    confidence_score: float = Field(ge=0.0, le=1.0)
    entry_reference_price: Decimal = Field(description="Mark price al emitir la hipotesis")
    horizon_s: int = Field(gt=0, description="Ventana tras la cual se resuelve")
    atr_normalized_trigger: float | None = Field(
        default=None, description="Distancia del trigger en unidades de ATR"
    )
    rationale: str = Field(description="Justificacion en lenguaje historico, no predictivo")
    heatmap_model_id: str | None = None
    run_id: str | None = Field(default=None, description="Corrida del grafo que la emitio")


class Resolution(BaseModel):
    hypothesis_id: UUID
    status: HypothesisStatus
    resolved_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    exit_price: Decimal | None = None
    pnl_pct: float | None = Field(default=None, description="Puntos porcentuales (2.5 = +2.5%)")
    win: bool | None = None
