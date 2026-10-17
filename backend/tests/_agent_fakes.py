"""
Fakes compartidos por los tests de agentes: un chat model guionado que respeta
el contrato `llm.with_structured_output(Schema).ainvoke(messages)` — sin red,
sin API key, deterministico — y builders de `FeatureSet`.
"""

from __future__ import annotations

from collections import deque
from typing import Any, Callable

from heatsignal.schema.events import Exchange, Side
from heatsignal.schema.features import (
    FeatureSet,
    HeatmapSnapshot,
    LiquidationCluster,
    MarketContext,
    OrderFlowWindow,
)

Response = Any  # instancia del schema | Exception | callable(messages) -> instancia/Exception


class ScriptExhaustedError(AssertionError):
    pass


class _ScriptedStructured:
    def __init__(self, model: "ScriptedChatModel", schema: type) -> None:
        self._model = model
        self._schema = schema

    async def ainvoke(self, messages: list, config: Any = None, **kwargs: Any) -> Any:
        return self._model._respond(self._schema, messages)


class ScriptedChatModel:
    """Sustituto de `BaseChatModel` guionado por schema.

    `script[Schema]` puede ser:
      - una lista: cada call consume el siguiente elemento (falla si se agota);
      - un callable(messages): se llama en cada call.
    Cada elemento/resultado puede ser una instancia del schema o una excepcion (se lanza).
    """

    def __init__(self, script: dict[type, list[Response] | Callable[[list], Response]] | None = None) -> None:
        self._script: dict[type, deque | Callable] = {
            k: (deque(v) if isinstance(v, list) else v) for k, v in (script or {}).items()
        }
        self.requested_schemas: list[type] = []
        self.calls: list[tuple[type, list]] = []

    def with_structured_output(self, schema: type, **kwargs: Any) -> _ScriptedStructured:
        self.requested_schemas.append(schema)
        return _ScriptedStructured(self, schema)

    def calls_for(self, schema: type) -> list[list]:
        return [msgs for s, msgs in self.calls if s is schema]

    def _respond(self, schema: type, messages: list) -> Any:
        self.calls.append((schema, messages))
        entry = self._script.get(schema)
        if entry is None:
            raise ScriptExhaustedError(f"sin guion para {schema.__name__}")
        if isinstance(entry, deque):
            if not entry:
                raise ScriptExhaustedError(f"guion agotado para {schema.__name__}")
            resp = entry.popleft()
        else:
            resp = entry
        if callable(resp) and not isinstance(resp, type):
            resp = resp(messages)
        if isinstance(resp, BaseException):
            raise resp
        return resp


def prompt_text(messages: list) -> str:
    return "\n".join(str(m.content) for m in messages)


def make_feature_set(
    symbol: str = "BTC-USDT",
    *,
    mark: float | None = 50_000.0,
    atr: float | None = 500.0,
    data_gaps: list[str] | None = None,
    clusters: list[LiquidationCluster] | None = None,
) -> FeatureSet:
    if clusters is None:
        clusters = [
            LiquidationCluster(
                price_low=49_000.0,
                price_high=49_200.0,
                price_mid=49_100.0,
                notional_usd=2_000_000.0,
                n_events=40,
                dominant_side=Side.SELL,
                sources=[Exchange.BINANCE, Exchange.BYBIT],
                partial_coverage_share=0.3,
                distance_atr=-1.8,
            ),
            LiquidationCluster(
                price_low=51_000.0,
                price_high=51_100.0,
                price_mid=51_050.0,
                notional_usd=500_000.0,
                n_events=10,
                dominant_side=Side.BUY,
                sources=[Exchange.BYBIT],
                partial_coverage_share=0.0,
                distance_atr=2.1,
            ),
        ]
    return FeatureSet(
        symbol=symbol,
        ts_ms=1_700_000_000_000,
        heatmap=HeatmapSnapshot(
            heatmap_model_id="liqcluster-v1",
            symbol=symbol,
            ts_ms=1_700_000_000_000,
            window_s=3600,
            mark_price=mark,
            atr=atr,
            clusters=clusters,
        ),
        order_flow=OrderFlowWindow(
            symbol=symbol,
            ts_ms=1_700_000_000_000,
            window_s=300,
            buy_volume_usd=3_000_000.0,
            sell_volume_usd=1_000_000.0,
            cvd_usd=2_000_000.0,
            imbalance=0.5,
            n_trades=1200,
            block_trade_volume_usd=0.0,
            sources=[Exchange.BINANCE],
        ),
        context=MarketContext(
            symbol=symbol,
            ts_ms=1_700_000_000_000,
            mark_price=mark,
            funding_rate_by_exchange={Exchange.BINANCE: 0.0001, Exchange.BYBIT: 0.00012},
            open_interest_usd=8_000_000_000.0,
            oi_change_pct=1.5,
            atr=atr,
            atr_pct=(atr / mark * 100) if (atr and mark) else None,
            regime="range",
        ),
        data_gaps=data_gaps or [],
    )


# ---- guion completo por defecto para el grafo ---------------------------------------


def full_script(symbols: list[str], *, strategy_id: str | None = None, rounds: int = 1) -> dict:
    """Guion que hace correr el grafo completo para `symbols`.

    Con `strategy_id`, el Sintetizador propone LONG con esa estrategia para cada simbolo;
    sin el, declara `no_hypothesis` para todos. Las respuestas son callables, asi el
    guion soporta cualquier cantidad de rondas de debate.
    """
    from heatsignal.agents.debate import DebateArgumentBatch, SymbolArgument
    from heatsignal.agents.liquidation_analyst import LiquidationAnalysis, LiquidationAnalysisBatch
    from heatsignal.agents.market_context_analyst import MarketContextAnalysis, MarketContextAnalysisBatch
    from heatsignal.agents.news_analyst import NewsAnalysis, NewsAnalysisBatch
    from heatsignal.agents.order_flow_analyst import OrderFlowAnalysis, OrderFlowAnalysisBatch
    from heatsignal.agents.redactor import ReportDraft, SymbolReportSection
    from heatsignal.agents.synthesizer import StrategyProposal, SynthesisBatch

    def liq(_):
        return LiquidationAnalysisBatch(
            analyses=[
                LiquidationAnalysis(symbol=s, cluster_summary=f"{s}: clusters historicos", confidence=0.5)
                for s in symbols
            ]
        )

    def flow(_):
        return OrderFlowAnalysisBatch(
            analyses=[
                OrderFlowAnalysis(symbol=s, flow_summary="comprador domino", dominant_aggressor="buyers", confidence=0.5)
                for s in symbols
            ]
        )

    def ctx(_):
        return MarketContextAnalysisBatch(
            analyses=[
                MarketContextAnalysis(
                    symbol=s, context_summary="funding neutro", regime_commentary="consistente", confidence=0.5
                )
                for s in symbols
            ]
        )

    def news(_):
        return NewsAnalysisBatch(
            analyses=[NewsAnalysis(symbol=s, news_summary="sin novedades relevantes", tone="neutral", confidence=0.3)
                      for s in symbols]
        )

    def debate(_):
        return DebateArgumentBatch(
            arguments=[SymbolArgument(symbol=s, argument="en casos similares...", strength=0.4) for s in symbols]
        )

    def synth(_):
        if strategy_id is None:
            return SynthesisBatch(
                proposals=[StrategyProposal(symbol=s, decision="no_hypothesis", rationale="evidencia debil") for s in symbols]
            )
        return SynthesisBatch(
            proposals=[
                StrategyProposal(
                    symbol=s,
                    decision="hypothesis",
                    strategy_id=strategy_id,
                    direction="LONG",
                    confidence=0.62,
                    atr_normalized_trigger=-1.8,
                    rationale="clusters similares reaccionaron en el 58% de los casos",
                )
                for s in symbols
            ]
        )

    def report(_):
        return ReportDraft(
            title="Reporte de corrida",
            summary="En ventanas similares el precio reacciono en el 58% de los casos.",
            sections=[SymbolReportSection(symbol=s, text="observacion historica") for s in symbols],
        )

    return {
        LiquidationAnalysisBatch: liq,
        OrderFlowAnalysisBatch: flow,
        MarketContextAnalysisBatch: ctx,
        NewsAnalysisBatch: news,
        DebateArgumentBatch: debate,
        SynthesisBatch: synth,
        ReportDraft: report,
    }
