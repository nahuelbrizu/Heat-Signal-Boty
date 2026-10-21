"""
Estado compartido del grafo LangGraph (ver `agents.md`). Un unico estado que
crece con el grafo: cada nodo escribe solo sus propias claves.

Entrada (la arma quien invoca el grafo):
    run_id               identificador de la corrida (tambien va en thread_id)
    features_by_symbol   FeatureSet por simbolo (capa `features/`, sin LLM)
    allowed_strategies   estrategias PRE-REGISTRADAS que el Sintetizador puede instanciar

Salidas por nodo:
    liquidation_analyses / order_flow_analyses / market_context_analyses / news_analyses  (analistas)
    debate_transcript (reducer: concatenacion) + debate_round             (debate)
    cio_decisions + cio_ts_ms                                             (CIO, LLM fuerte)
    cro_reviews                                                           (CRO, codigo)
    pending_hypotheses + synthesis_decisions + cio_verdicts               (registro, codigo)
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
from .news_analyst import NewsAnalysis
from .order_flow_analyst import OrderFlowAnalysis
from .redactor import RunReport
from .cio import CioDecision
from .cro import CroReview
from .synthesizer import SymbolDecision


class GraphState(TypedDict, total=False):
    # --- entrada ---
    run_id: str
    features_by_symbol: dict[str, FeatureSet]
    allowed_strategies: list[StrategyHypothesis]
    session: dict          # {"name", "label", "day", "trigger_ms"}: sesion de la corrida o ultima disparada
    day_journal: str       # diario del dia (sesiones previas, codigo + LLM), ver pipeline/session_journal.py
    macro_context: str     # bloque "Macro y eventos" point-in-time (macro/context.py); solo el Analista de Contexto
    # --- analistas (paralelo; cada uno escribe su clave, sin conflicto de fan-in) ---
    liquidation_analyses: list[LiquidationAnalysis]
    order_flow_analyses: list[OrderFlowAnalysis]
    market_context_analyses: list[MarketContextAnalysis]
    news_analyses: list[NewsAnalysis]
    # --- debate ---
    debate_transcript: Annotated[list[DebateTurn], operator.add]
    debate_round: int
    # --- CIO (decide) y CRO (veta por riesgo) ---
    cio_decisions: list[CioDecision]
    cio_ts_ms: int
    cro_reviews: list[CroReview]
    # --- registro (codigo) ---
    pending_hypotheses: list[PendingHypothesis]
    synthesis_decisions: list[SymbolDecision]
    cio_verdicts: list[dict]   # filas de cio_verdicts del ciclo (tambien persistidas)
    # --- redactor ---
    report: RunReport
