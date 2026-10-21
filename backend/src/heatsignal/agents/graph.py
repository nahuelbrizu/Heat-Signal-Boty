"""
Grafo LangGraph de Fase 2 (ver `agents.md` / `phase-2-agents.md`).

    START
      |-----------------------------|------------------------------|-------------------|
      v                             v                              v                   v
    liquidation_analyst      order_flow_analyst       market_context_analyst    news_analyst  (cheap, paralelo)
      |-----------------------------|------------------------------|-------------------|
                                    v  (fan-in: espera a los cuatro)
                              bull_strategist  <-----------------+          (strong)
                                    v                            |
                              bear_strategist  -- round < N -----+          (strong)
                                    | round >= N
                                    v
                                   cio   decide por simbolo: postura, peso, tomar candidato verificado o no operar (strong)
                                    v
                                   cro   reglas fijas de riesgo, sin LLM: hueco real, macro, tope de sesion, correlacion
                                    v
                                registro  apply_proposals -> PendingHypothesisStore.add + cio_verdicts  (codigo)
                                    v
                                redactor (guardrail + 1 reintento)          (strong)
                                    v
                                   END

El Evaluador/Juez vive en `evaluation/` (Fase 3) y resuelve las hipotesis
pendientes fuera de este grafo.

Los LLM se inyectan (no se instancian aca): `llm_factory.make_llms()` arma el
par real; los tests usan modelos falsos.
"""

from __future__ import annotations

from typing import Any, Callable

from langchain_core.language_models import BaseChatModel
from langgraph.graph import END, START
from langgraph.graph.state import CompiledStateGraph, StateGraph

from .debate import make_debate_node, make_debate_router
from .hypothesis_store import PendingHypothesisStore
from .liquidation_analyst import make_liquidation_analyst_node
from .market_context_analyst import make_market_context_analyst_node
from .news_analyst import make_news_analyst_node
from .order_flow_analyst import make_order_flow_analyst_node
from .redactor import make_redactor_node
from .state import GraphState
from .cio import make_cio_node
from .cro import make_cro_node
from .synthesizer import DEFAULT_HORIZON_S, make_registrar_node

ANALYST_NODES = ("liquidation_analyst", "order_flow_analyst", "market_context_analyst", "news_analyst")


def build_graph(
    cheap_llm: BaseChatModel,
    strong_llm: BaseChatModel | None = None,
    *,
    max_debate_rounds: int = 1,
    checkpointer: Any | None = None,
    hypothesis_store: PendingHypothesisStore | None = None,
    default_horizon_s: int = DEFAULT_HORIZON_S,
    verdict_log: Any | None = None,
    macro_calendar: Callable[[], Any] | None = None,
    clock_ms: Callable[[], int] | None = None,
) -> CompiledStateGraph:
    """Compila el grafo completo.

    `strong_llm=None` reutiliza `cheap_llm` (compatibilidad con el esqueleto
    previo `build_graph(llm)`); en produccion pasar ambos via `make_llms()`.
    `verdict_log`: `evaluation.cio_verdicts.CioVerdictLog` (tabla `cio_verdicts`); `macro_calendar`: funcion que
    devuelve el calendario macro (`macro.calendar.load_calendar`) para el CRO; None = sin regla macro (tests).
    LLM por ciclo: 4 analistas + 2*N debate + 1 CIO + redactor (1-2), igual que graph-v1 (el CIO reemplaza la
    llamada del Sintetizador; CRO y registro son codigo).
    """
    if max_debate_rounds < 1:
        raise ValueError("max_debate_rounds debe ser >= 1")
    strong = strong_llm if strong_llm is not None else cheap_llm

    graph = StateGraph(GraphState)
    graph.add_node("liquidation_analyst", make_liquidation_analyst_node(cheap_llm))
    graph.add_node("order_flow_analyst", make_order_flow_analyst_node(cheap_llm))
    graph.add_node("market_context_analyst", make_market_context_analyst_node(cheap_llm))
    graph.add_node("news_analyst", make_news_analyst_node(cheap_llm))
    graph.add_node("bull_strategist", make_debate_node(strong, "bull"))
    graph.add_node("bear_strategist", make_debate_node(strong, "bear"))
    graph.add_node("cio", make_cio_node(strong, hypothesis_store=hypothesis_store, clock_ms=clock_ms))
    graph.add_node("cro", make_cro_node(hypothesis_store=hypothesis_store, calendar_fn=macro_calendar, clock_ms=clock_ms))
    graph.add_node(
        "registro",
        make_registrar_node(hypothesis_store=hypothesis_store, verdict_log=verdict_log,
                            default_horizon_s=default_horizon_s),
    )
    graph.add_node("redactor", make_redactor_node(strong))

    for node in ANALYST_NODES:
        graph.add_edge(START, node)
    graph.add_edge(list(ANALYST_NODES), "bull_strategist")
    graph.add_edge("bull_strategist", "bear_strategist")
    graph.add_conditional_edges(
        "bear_strategist",
        make_debate_router(max_debate_rounds),
        {"bull_strategist": "bull_strategist", "cio": "cio"},
    )
    graph.add_edge("cio", "cro")
    graph.add_edge("cro", "registro")
    graph.add_edge("registro", "redactor")
    graph.add_edge("redactor", END)
    return graph.compile(checkpointer=checkpointer)
