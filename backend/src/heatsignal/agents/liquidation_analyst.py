"""
Analista de Liquidaciones — primer nodo del grafo LangGraph (ver `agents.md`).

Primer agente implementado. Sin la capa de debate todavía (agents.md la ubica
recién después de los 3 analistas en paralelo) ni los otros dos analistas
(Order Flow, Contexto de Mercado) — esos son el próximo paso, no este.

Contrato de este nodo:
  - Recibe liquidaciones YA normalizadas (`LiquidationEvent`, ver
    `schema/events.py`) agrupadas por símbolo — típicamente una ventana
    reciente leída del Redis Stream `heatsignal:{symbol}:liquidation` (ver
    `schema/sink.py`), aunque este módulo no lee Redis directamente: el
    estado del grafo ya llega armado. Leer del stream es responsabilidad de
    quien invoca el grafo, no de este nodo.
  - Hace UN call batcheado con salida estructurada Pydantic para todos los
    símbolos de la ventana en un solo turno (regla de `phase-2-agents.md`:
    "no 1 call por agente por par" — acá "por símbolo").
  - No calcula el heatmap propio todavía (`heatmap_model_id`, ver
    `phase-1-ingestion.md`) — eso es un paso posterior. Hoy arma un resumen
    crudo (conteo, notional, sesgo de lado, rango de precio) por símbolo y
    deja que el LLM lo interprete en lenguaje histórico, no predictivo.

El LLM se inyecta (no se instancia acá): mantiene el nodo agnóstico de
proveedor mientras esa decisión siga sin cerrarse en `decisions.md`, y lo hace
testeable con un modelo falso, sin llamadas de red ni API key.
"""

from __future__ import annotations

from collections import Counter
from typing import TYPE_CHECKING, Any, Callable

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, Field

from ..schema.events import LiquidationEvent, Side

if TYPE_CHECKING:
    from .state import GraphState

_SYSTEM_PROMPT = (
    "Sos el Analista de Liquidaciones de HeatSignal. Recibís, por símbolo, un resumen "
    "crudo de liquidaciones forzadas recientes (conteo, notional total en USD, sesgo de "
    "lado compra/venta, rango de precio). Para cada símbolo del input, devolvé un análisis "
    "del cluster en lenguaje histórico ('los clusters similares reaccionaron...'), nunca "
    "predictivo ('va a reaccionar...'). No inventes símbolos que no estén en el input, y no "
    "omitas ninguno de los que sí están."
)


class LiquidationAnalysis(BaseModel):
    """Análisis de un símbolo para la ventana de liquidaciones recibida."""

    symbol: str
    cluster_summary: str = Field(description="Resumen histórico del cluster, no predictivo")
    dominant_side: Side | None = Field(default=None, description="Lado dominante del cluster, si lo hay")
    notional_usd_total: float
    n_liquidations: int
    confidence: float = Field(ge=0.0, le=1.0, description="Confianza del analista en el resumen, 0-1")


class LiquidationAnalysisBatch(BaseModel):
    """Salida estructurada de un call batcheado: un análisis por símbolo de la ventana."""

    analyses: list[LiquidationAnalysis]


def _summarize_window(symbol: str, events: list[LiquidationEvent]) -> str:
    """Arma el resumen crudo (no interpretado) que se le pasa al LLM para un símbolo.

    Sin fallos silenciosos: una ventana vacía para un símbolo se reporta como
    tal en el prompt, no se omite — el LLM debe saber que no hubo actividad,
    no adivinar.
    """
    if not events:
        return f"{symbol}: sin liquidaciones en la ventana."
    sides = Counter(e.side for e in events)
    notional_total = sum(e.notional_usd for e in events)
    prices = [e.price for e in events]
    return (
        f"{symbol}: {len(events)} liquidaciones, notional total ${notional_total:,.0f}, "
        f"lado: {sides.get(Side.BUY, 0)} buy / {sides.get(Side.SELL, 0)} sell, "
        f"precio min/max ${min(prices):,.2f}/${max(prices):,.2f}."
    )


def make_liquidation_analyst_node(
    llm: BaseChatModel,
) -> Callable[["GraphState"], Any]:
    """Devuelve el nodo del grafo, con el LLM ya cerrado sobre el closure.

    `llm` debe soportar `.with_structured_output(Schema)` (contrato estándar
    de `langchain_core.language_models.BaseChatModel`) — el proveedor
    concreto (Anthropic, OpenAI, lo que sea) lo decide quien arma el grafo.
    """
    structured_llm = llm.with_structured_output(LiquidationAnalysisBatch)

    async def _node(state: "GraphState") -> dict[str, Any]:
        liquidations_by_symbol: dict[str, list[LiquidationEvent]] = state.get(
            "liquidations_by_symbol", {}
        )
        if not liquidations_by_symbol:
            return {"liquidation_analyses": []}

        window_summary = "\n".join(
            _summarize_window(symbol, events) for symbol, events in liquidations_by_symbol.items()
        )
        messages = [
            SystemMessage(content=_SYSTEM_PROMPT),
            HumanMessage(content=window_summary),
        ]
        result: LiquidationAnalysisBatch = await structured_llm.ainvoke(messages)
        return {"liquidation_analyses": result.analyses}

    return _node
