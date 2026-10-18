"""
Analisis de sesiones de mercado (Asia / Europa / EE.UU. + cierre del dia UTC) y diario del dia.

Contrato entre `features/sessions.py` (codigo, sin LLM), `pipeline/session_journal.py` (persistencia append-only),
el grafo (recibe el diario del dia como contexto) y el panel. Todo numero lo pone el codigo.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

SESSION_MODEL_ID = "session-v1"
BreakKind = Literal["arriba", "abajo", "ambos", "no"]


class OpeningCandle(BaseModel):
    """Primera vela de 1h de la sesion, armada desde 1m: [open_ms, open_ms + 1h). En el cierre: 23:00-00:00 UTC."""

    open_ms: int
    open: float
    high: float
    low: float
    close: float
    minutes: int = Field(description="Minutos de 1m con precio (60 = completa)")
    sources: dict[str, int] = Field(default_factory=dict, description="Minutos por fuente (klines/bars_1m/rest)")
    quote_volume: float | None = Field(default=None, description="Volumen en USDT de la vela (Binance)")
    range: float
    body: float = Field(description="close - open (con signo)")
    upper_wick: float
    lower_wick: float
    body_frac: float = Field(description="|cuerpo| / rango (0 si rango 0)")
    range_atr: float | None = Field(default=None, description="rango / ATR14 de 1h")
    vol_avg_20d: float | None = Field(default=None, description="Volumen medio de la MISMA hora local, ultimos 20 dias")
    vol_days: int = Field(default=0, description="Dias usados en el promedio")
    vol_ratio: float | None = None


class DayOwnData(BaseModel):
    """Datos propios del dia UTC hasta el disparo (bars_1m de todos los exchanges)."""

    minutes: int = 0
    exchanges: list[str] = Field(default_factory=list)
    buy_usd: float = 0.0
    sell_usd: float = 0.0
    cvd_usd: float = 0.0
    liq_long_usd: float = 0.0
    liq_short_usd: float = 0.0
    n_liqs: int = 0
    funding_by_exchange: dict[str, float] = Field(default_factory=dict, description="Ultimo funding (8 h) del dia")
    funding_8h_mean: float | None = None
    oi_usd: float | None = Field(default=None, description="Suma del ultimo OI informado por exchange")
    oi_change_pct: float | None = Field(default=None, description="Cambio de la suma de OI desde el primer dato del dia")


class SessionFlags(BaseModel):
    """Banderas por codigo que exigen las estrategias pre-registradas por sesion."""

    apertura_continuacion_a_favor: bool = Field(
        default=False, description="Tendencia alcista/bajista y cuerpo de la vela de apertura >= 50% del rango "
                                   "en la direccion de la tendencia")
    apertura_reversion_en_contra: bool = Field(
        default=False, description="Tendencia alcista (bajista) y la vela de apertura rompio el maximo (minimo) "
                                   "de la sesion anterior pero cerro de nuevo adentro")


class SessionSymbolAnalysis(BaseModel):
    symbol: str
    session: str
    day: str = Field(description="Dia UTC del diario (YYYY-MM-DD)")
    role: Literal["open", "close"]
    open_ms: int
    trigger_ms: int
    status: Literal["ok", "partial", "missing"]
    opening: OpeningCandle | None = None
    opening_body_dir: Literal["alcista", "bajista"] | None = None
    day_open: float | None = None
    day_high: float | None = None
    day_low: float | None = None
    change_from_day_open_pct: float | None = None
    daily_trend: str = "sin_datos"
    trend_reason: str = ""
    prev_session: str | None = None
    prev_range_high: float | None = None
    prev_range_low: float | None = None
    broke_prev_range: BreakKind | None = None
    own: DayOwnData | None = None
    flags: SessionFlags = Field(default_factory=SessionFlags)
    data_gaps: list[str] = Field(default_factory=list)
    summary: str = Field(default="", description="Resumen por codigo (lenguaje historico)")


class SessionAnalysis(BaseModel):
    """Lo que se guarda en el diario (fila kind='code') para una sesion de un dia."""

    model_id: str = SESSION_MODEL_ID
    day: str
    session: str
    label: str
    role: Literal["open", "close"]
    open_ms: int
    trigger_ms: int
    symbols: dict[str, SessionSymbolAnalysis]
