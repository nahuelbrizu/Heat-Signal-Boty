"""
Signal: una señal de trading ya resuelta (entrada + salida), producida por el
grafo de agentes LangGraph a partir de datos normalizados de BinanceWorker.

Es el input crudo del Evaluador (`evaluation/evaluator.py`) — el PnL ya viene
calculado por quien produce la señal; el Evaluador nunca lo recalcula, solo
lo agrega estadísticamente. `pnl_pct` se expresa en puntos porcentuales
(2.5 significa +2.5%), no como fracción.

Precios en `Decimal`, nunca `float` — ver regla de diseño en CLAUDE.md.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import Enum
from uuid import UUID, uuid4

from pydantic import BaseModel, Field


class Direction(str, Enum):
    LONG = "LONG"
    SHORT = "SHORT"


class Signal(BaseModel):
    """Una señal ya resuelta: se conoce tanto la entrada como la salida."""

    signal_id: UUID = Field(default_factory=uuid4)
    strategy_id: UUID
    direction: Direction
    entry_price: Decimal
    exit_price: Decimal
    entry_time: datetime
    exit_time: datetime
    pnl: Decimal
    pnl_pct: float
