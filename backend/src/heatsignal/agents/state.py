"""
Estado compartido del grafo LangGraph (ver `agents.md`). Un unico estado que
crece con el grafo: cada nodo escribe solo sus propias claves.

Entrada (la arma quien invoca el grafo):
    run_id               identificador de la corrida (tambien va en thread_id)
    features_by_symbol   FeatureSet por simbolo (capa `features/`, sin LLM)
    allowed_strategies   estrategias PRE-REGISTRADAS que el Sintetizador puede instanciar

Salidas por nodo:
    liquidation_analyses / order_flow_analyses / market_context_analyses  (analistas, paralelo)
    debate_transcript (reducer: concatenacion) + debate_round             (debate)
    pending_hypotheses + synthesis_decisions                              (sintetizador)
    report                                                                (redactor)
"""

from __future__ import annotations

import operator
from typing import Annotated, TypedDict

from ..evaluation.strategy_registry import StrategyHypothesis
from ..schema.features import FeatureSet
from ..schema.hypotheses import PendingHypothesis
from .debate import DebateTurn
from .liquidation_analyst import LiquidationAnalysis
from .market_context_analyst import MarketContextAnalysis
from .order_flow_analyst import OrderFlowAnalysis
from .redactor import RunReport
from .synthesizer import SymbolDecision


class GraphState(TypedDict, total=False):
    # --- entrada ---
    run_id: str
    features_by_symbol: dict[str, FeatureSet]
    allowed_strategies: list[StrategyHypothesis]
    # --- analistas (paralelo; cada uno escribe su clave, sin conflicto de fan-in) ---
    liquidation_analyses: list[LiquidationAnalysis]
    order_flow_analyses: list[OrderFlowAnalysis]
    market_context_analyses: list[MarketContextAnalysis]
    # --- debate ---
    debate_transcript: Annotated[list[DebateTurn], operator.add]
    debate_round: int
    # --- sintetizador ---
    pending_hypotheses: list[PendingHypothesis]
    synthesis_decisions: list[SymbolDecision]
    # --- redactor ---
    report: RunReport
