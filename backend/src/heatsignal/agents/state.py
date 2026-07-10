"""
Estado compartido del grafo LangGraph (ver `agents.md`).

Hoy tiene un solo nodo (Analista de Liquidaciones). Los próximos agentes
(Order Flow, Contexto de Mercado, debate Alcista/Bajista, Estratega,
Evaluador, Redactor) suman sus propios campos acá — no reemplazan estos ni
arrancan un `GraphState` nuevo, es un único estado que crece con el grafo.
"""

from __future__ import annotations

from typing import TypedDict

from ..schema.events import LiquidationEvent
from .liquidation_analyst import LiquidationAnalysis


class GraphState(TypedDict, total=False):
    liquidations_by_symbol: dict[str, list[LiquidationEvent]]
    liquidation_analyses: list[LiquidationAnalysis]
