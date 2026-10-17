"""
Features deterministas que la capa de features calcula a partir de eventos normalizados
(`schema/events.py`) y que el grafo de agentes LLM consume.

Es el contrato entre `features/` (codigo puro, sin LLM) y `agents/` (LLM). Los agentes
nunca leen eventos crudos de a miles: leen un `FeatureSet` por simbolo.

Principio "sin fallos silenciosos": todo hueco o sesgo de datos conocido se declara
explicitamente (`data_gaps`, `partial_coverage_share`), nunca se omite.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from .events import Exchange, Side
from .news import NewsItem


class LiquidationCluster(BaseModel):
    """Un nivel/banda de precio donde se concentraron liquidaciones en la ventana."""

    price_low: float
    price_high: float
    price_mid: float
    notional_usd: float
    n_events: int
    dominant_side: Side | None = Field(
        default=None, description="Lado de la ORDEN de liquidacion dominante (SELL = longs liquidados)"
    )
    sources: list[Exchange]
    partial_coverage_share: float = Field(
        ge=0.0, le=1.0, description="Fraccion del notional que viene de streams con coverage PARTIAL"
    )
    distance_atr: float | None = Field(
        default=None, description="(price_mid - mark_price) / ATR. None si no hay ATR o mark."
    )


class HeatmapSnapshot(BaseModel):
    """Heatmap propio de liquidaciones para un simbolo (version del modelo en `heatmap_model_id`)."""

    heatmap_model_id: str = Field(description="Version del algoritmo, p.ej. 'liqcluster-v1'")
    symbol: str
    ts_ms: int
    window_s: int
    mark_price: float | None
    atr: float | None
    clusters: list[LiquidationCluster]


class OrderFlowWindow(BaseModel):
    """Agregado de trades del taker en una ventana."""

    symbol: str
    ts_ms: int
    window_s: int
    buy_volume_usd: float
    sell_volume_usd: float
    cvd_usd: float = Field(description="buy - sell (taker), en USD")
    imbalance: float = Field(ge=-1.0, le=1.0, description="(buy - sell) / (buy + sell); 0 si no hubo volumen")
    n_trades: int
    block_trade_volume_usd: float = Field(description="Volumen de block trades, EXCLUIDO de buy/sell/cvd")
    sources: list[Exchange]


Regime = Literal["trend_up", "trend_down", "range", "unknown"]


class MarketContext(BaseModel):
    """Regimen de mercado desde tickers (mark, funding, OI) y una serie de precios."""

    symbol: str
    ts_ms: int
    mark_price: float | None
    funding_rate_by_exchange: dict[Exchange, float]
    open_interest_usd: float | None = Field(description="Suma entre exchanges que lo informan")
    oi_change_pct: float | None = Field(description="Cambio % de OI en la ventana")
    atr: float | None
    atr_pct: float | None = Field(description="ATR / mark_price * 100")
    regime: Regime


CoverageState = Literal["ok", "partial", "stale", "missing"]


class DataQuality(BaseModel):
    """Lo mismo que `data_gaps` pero en numeros, para graficar en el panel (el LLM lee `data_gaps`)."""

    bars_closed: int = Field(description="Barras cerradas de la serie usada para ATR/regimen")
    bar_s: int
    atr_bars_needed: int
    regime_bars_needed: int
    window_s: int
    warmup_have_s: float | None = Field(default=None, description="Segundos acumulados en memoria (None: sin dato)")
    coverage: dict[str, dict[str, CoverageState]] = Field(
        default_factory=dict, description="exchange -> {trades, liquidations, ticker, funding, open_interest}"
    )


class FeatureSet(BaseModel):
    """Todo lo que el grafo de agentes recibe para un simbolo en una corrida."""

    symbol: str
    ts_ms: int
    heatmap: HeatmapSnapshot
    order_flow: OrderFlowWindow
    context: MarketContext
    data_gaps: list[str] = Field(
        default_factory=list,
        description="Huecos/sesgos declarados, p.ej. 'okx: stream caido 120s', 'binance liq PARTIAL'",
    )
    news: list[NewsItem] = Field(
        default_factory=list, description="Noticias recientes del simbolo y del mercado (ver schema/news.py)"
    )
    news_model_id: str | None = Field(default=None, description="Version del lector/etiquetado de noticias")
    quality: DataQuality | None = Field(default=None, description="Calidad de datos estructurada (panel)")
