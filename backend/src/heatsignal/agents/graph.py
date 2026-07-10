"""
Esqueleto del grafo LangGraph (ver `agents.md`).

Hoy: un solo nodo, el Analista de Liquidaciones — sin la capa de debate ni el
resto de los analistas paralelos (Order Flow, Contexto de Mercado) todavía.
Los próximos nodos se agregan a ESTE grafo, no arrancan uno nuevo:

    [Ingesta: WS + REST]
            |
            v
    ANALISTAS (paralelo)          <- hoy solo "liquidation_analyst"
            |
            v
    DEBATE (bull vs bear)         <- sin implementar
            |
            v
    Estratega/Sintetizador        <- sin implementar
            |
            v
    Evaluador/Juez                <- ya existe standalone en evaluation/,
    Redactor                         falta conectarlo a este grafo
"""

from __future__ import annotations

from langchain_core.language_models import BaseChatModel
from langgraph.graph import END
from langgraph.graph.state import CompiledStateGraph, StateGraph

from .liquidation_analyst import make_liquidation_analyst_node
from .state import GraphState


def build_graph(llm: BaseChatModel) -> CompiledStateGraph:
    """Compila el grafo. `llm` se inyecta para no atar el grafo a un proveedor."""
    graph = StateGraph(GraphState)
    graph.add_node("liquidation_analyst", make_liquidation_analyst_node(llm))
    graph.set_entry_point("liquidation_analyst")
    graph.add_edge("liquidation_analyst", END)
    return graph.compile()
