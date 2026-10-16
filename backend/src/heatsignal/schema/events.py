"""
Esquema común normalizado para eventos de ingesta de HeatSignal.

Cada exchange emite formatos distintos; los adaptadores de cada worker traducen
a estos modelos ANTES de escribir al sink. Todo timestamp en UTC (epoch ms).

Principio del proyecto: sin fallos silenciosos. Cada evento lleva su `source` y,
para liquidaciones, la `coverage` (full vs partial), porque la cobertura NO es
uniforme entre exchanges (Bybit allLiquidation da todas; Binance y OKX dan <=1/seg por simbolo).
"""

from __future__ import annotations

from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field


class Side(str, Enum):
    BUY = "buy"
    SELL = "sell"


class Coverage(str, Enum):
    FULL = "full"        # el stream empuja todas las liquidaciones (Bybit allLiquidation)
    PARTIAL = "partial"  # el stream submuestrea (Binance forceOrder, OKX, Bybit deprecado): <=1/seg/simbolo


class Exchange(str, Enum):
    BINANCE = "binance"
    BYBIT = "bybit"
    OKX = "okx"


class LiquidationEvent(BaseModel):
    """Una liquidacion forzada normalizada."""
    kind: Literal["liquidation"] = "liquidation"
    source: Exchange
    symbol: str                      # normalizado a formato interno, p.ej. "SOL-USDT"
    side: Side                       # lado de la orden de liquidacion
    price: float
    qty: float                       # cantidad en unidades del activo base
    notional_usd: float              # price * qty (o el valor que de el exchange)
    ts_event_ms: int = Field(..., description="timestamp del evento en el exchange, epoch ms UTC")
    ts_ingest_ms: int = Field(..., description="timestamp de recepcion local, epoch ms UTC")
    coverage: Coverage               # full/partial segun el stream de origen


class TradeEvent(BaseModel):
    """Un trade agregado (aggTrade) normalizado. Base para reconstruir OHLCV y CVD."""
    kind: Literal["trade"] = "trade"
    source: Exchange
    symbol: str
    side: Side                       # lado del taker (agresor)
    price: float
    qty: float
    ts_event_ms: int
    ts_ingest_ms: int


class TickerEvent(BaseModel):
    """Snapshot de ticker/mark price: funding, OI, mark price. Fuente de funding/OI del snapshot."""
    kind: Literal["ticker"] = "ticker"
    source: Exchange
    symbol: str
    mark_price: float | None = None
    last_price: float | None = None
    funding_rate: float | None = None
    open_interest: float | None = None       # en unidades del activo base, si el exchange lo da
    open_interest_usd: float | None = None
    ts_event_ms: int
    ts_ingest_ms: int


# Union discriminada por `kind` para que el sink sepa a que tabla/stream va cada evento.
IngestEvent = LiquidationEvent | TradeEvent | TickerEvent
