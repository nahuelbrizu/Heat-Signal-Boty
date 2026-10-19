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
from .sessions import SessionSymbolAnalysis


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


CoverageState = Literal["ok", "partial", "stale", "missing", "na"]  # na: el exchange no lo publica


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


HTF_MODEL_ID = "htf-trend-v1"
# Niveles para dias laterales (rango de Asia, extremos de ayer, VWAP, ADX, mechas): version propia para no tocar la
# regla de tendencia `htf-trend-v1`, con la que estan pre-registradas las 5 estrategias de tendencia diaria.
LEVELS_MODEL_ID = "htf-levels-v1"

DailyTrend = Literal["alcista", "bajista", "lateral", "sin_datos"]
TrendAlignment = Literal["a_favor", "en_contra", "lateral", "sin_datos"]
CandleSource = Literal["klines", "bars_1m", "rest"]


class Candle1h(BaseModel):
    """Vela de 1h CERRADA (open_ms + 1h <= momento de calculo), alineada a UTC."""

    open_ms: int
    open: float
    high: float
    low: float
    close: float
    source: CandleSource = Field(description="klines 1m de Binance (base), bars_1m propios o REST 1h de Binance")
    minutes: int = Field(default=60, description="Minutos de 1m con precio usados (60 = completa; REST = 60)")
    vwap_pv: float | None = Field(default=None, description="Suma de precio tipico (H+L+C)/3 x volumen base de las "
                                                            "velas 1m de la hora (None si no hay 1m: REST)")
    vwap_vol: float | None = Field(default=None, description="Suma del volumen base de las velas 1m de la hora")


class HtfSetups(BaseModel):
    """Banderas de setup calculadas por CODIGO (las estrategias 1h las exigen en `entry_conditions`)."""

    pullback_ema20_1h_a_favor: bool = Field(
        default=False, description="Tendencia alcista/bajista y la ultima vela de 1h retrocedio a la EMA20 de 1h")
    extremo_rango_diario: Literal["maximo", "minimo"] | None = Field(
        default=None, description="Ultimo cierre en el 15% superior/inferior de un rango diario >= 2 ATR(1h)")
    continuacion_a_favor: bool = Field(
        default=False, description="Tendencia alcista/bajista, cierre del lado de la tendencia de la EMA20 1h, "
                                   "a <= 1.5 ATR de ella y fuera del 15% extremo del rango diario")


LateralSignal = Literal["LONG", "SHORT"]


class HtfLevels(BaseModel):
    """Niveles del dia UTC y banderas de dias laterales (`htf-levels-v1`, ver features/htf.py). Todo por CODIGO.

    Un dato que falta queda en None y su motivo en `sin_datos` (nunca un valor inventado).
    """

    levels_model_id: str = LEVELS_MODEL_ID
    asia_high: float | None = Field(default=None, description="Maximo de las velas 1h de 00:00-08:00 UTC del dia")
    asia_low: float | None = None
    asia_hours: int = Field(default=0, description="Velas de 1h de Asia (00-07 UTC) disponibles (8 = completo)")
    prev_day_high: float | None = Field(default=None, description="Maximo del dia UTC anterior (24 velas 1h)")
    prev_day_low: float | None = None
    vwap: float | None = Field(default=None, description="VWAP del dia UTC (precio tipico de velas 1m, desde 00:00)")
    vwap_minutes: int = Field(default=0, description="Minutos de 1m con precio que entraron al VWAP")
    dist_vwap_atr: float | None = Field(default=None, description="(cierre - VWAP) / ATR14 1h")
    adx14_1h: float | None = Field(default=None, description="ADX14 de Wilder en 1h")
    last_range: float | None = Field(default=None, description="high - low de la ultima vela 1h cerrada")
    last_upper_wick: float | None = Field(default=None, description="high - max(open, close)")
    last_lower_wick: float | None = Field(default=None, description="min(open, close) - low")
    upper_wick_frac: float | None = Field(default=None, description="mecha superior / rango (None si rango 0)")
    lower_wick_frac: float | None = None
    falsa_ruptura_asia: LateralSignal | None = Field(
        default=None, description="Dia lateral: ruptura > 0.5 ATR del rango de Asia que cierra adentro (direccion)")
    rechazo_dia_anterior: LateralSignal | None = Field(
        default=None, description="Dia lateral: toca el extremo de ayer (0.25 ATR), mecha >= 50% y cierra adentro")
    extremo_vwap: LateralSignal | None = Field(
        default=None, description="Dia lateral: |cierre - VWAP| >= 2 ATR y ADX14 1h < 20, hacia el VWAP")
    sin_datos: list[str] = Field(default_factory=list, description="Datos que faltan, con motivo")

    def brief(self) -> dict:
        return self.model_dump(exclude={"sin_datos"}) | {"n_sin_datos": len(self.sin_datos)}


class HtfContext(BaseModel):
    """Contexto multi-temporalidad (1h + dia UTC) desde nuestra base historica; ver features/htf.py."""

    htf_model_id: str = HTF_MODEL_ID
    symbol: str
    ts_ms: int
    status: Literal["ok", "partial", "missing"]
    candles_1h: list[Candle1h] = Field(default_factory=list, description="Ultimas <= 48 velas de 1h cerradas")
    last_closed_open_ms: int | None = None
    day_start_ms: int
    day_hours_closed: int = 0
    day_open: float | None = None
    day_high: float | None = None
    day_low: float | None = None
    last_close: float | None = None
    change_from_open_pct: float | None = None
    change_from_open_atr: float | None = None
    pos_in_day_range: float | None = Field(default=None, description="(cierre - min) / (max - min) del dia, 0..1")
    day_range_atr: float | None = None
    prev_day: dict[str, float] | None = Field(default=None, description="open/high/low/close del dia UTC anterior")
    ema20_1h: float | None = None
    sma50_1h: float | None = None
    dist_ema20_1h_atr: float | None = Field(default=None, description="(cierre - EMA20 1h) / ATR(1h)")
    ema20_1h_slope_atr: float | None = Field(default=None, description="(EMA20 ahora - EMA20 hace 4 velas) / ATR(1h)")
    ema10_1d: float | None = None
    sma20_1d: float | None = None
    close_vs_ema10_1d_pct: float | None = None
    daily_closes: int = Field(default=0, description="Dias UTC completos usados para las medias diarias")
    atr14_1h: float | None = None
    atr14_1h_pct: float | None = None
    daily_trend: DailyTrend = "sin_datos"
    trend_reason: str = ""
    setups: HtfSetups = Field(default_factory=HtfSetups)
    levels: HtfLevels | None = Field(default=None, description="Rango de Asia, extremos de ayer, VWAP, ADX, mechas")
    sources: dict[str, int] = Field(default_factory=dict, description="Velas por fuente (klines/bars_1m/rest)")
    data_gaps: list[str] = Field(default_factory=list)

    def snapshot(self) -> dict:
        """Resumen chico que se guarda con cada hipotesis (auditoria del corte por tendencia)."""
        return {
            "htf_model_id": self.htf_model_id, "status": self.status, "last_closed_open_ms": self.last_closed_open_ms,
            "day_open": self.day_open, "last_close": self.last_close, "change_from_open_pct": self.change_from_open_pct,
            "change_from_open_atr": self.change_from_open_atr, "pos_in_day_range": self.pos_in_day_range,
            "dist_ema20_1h_atr": self.dist_ema20_1h_atr, "ema20_1h_slope_atr": self.ema20_1h_slope_atr,
            "close_vs_ema10_1d_pct": self.close_vs_ema10_1d_pct, "atr14_1h": self.atr14_1h,
            "setups": self.setups.model_dump(),
            "levels": None if self.levels is None else self.levels.brief(),
        }


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
    htf: HtfContext | None = Field(default=None, description="Velas de 1h cerradas + tendencia del dia UTC")
    session: SessionSymbolAnalysis | None = Field(
        default=None, description="Ultimo analisis de sesion del dia para este simbolo (schema/sessions.py)")
