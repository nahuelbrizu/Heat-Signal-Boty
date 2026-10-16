"""
Estratega/Sintetizador: convierte analistas + debate en hipotesis pendientes.

Reglas (anti-p-hacking, ver CLAUDE.md y `phase-2-agents.md`):
  - El LLM SOLO puede elegir entre `allowed_strategies` (pre-registradas en
    `StrategyRegistry`). La validacion se hace en CODIGO despues de que el LLM
    responde: un `strategy_id` desconocido se RECHAZA y se loguea, nunca se
    acepta en silencio ni se "corrige" al mas parecido.
  - `confidence_score` fuera de [0, 1] -> rechazo (no se recorta en silencio).
  - `entry_reference_price` lo pone el CODIGO desde `FeatureSet.context.mark_price`
    (el LLM no ve ni elige el precio de entrada). Sin mark -> rechazo explicito.
  - `horizon_s` sale de `exit_conditions["horizon_s"]` de la estrategia si esta;
    si no, `default_horizon_s` (logueado como warning).
  - Cada simbolo termina con una decision explicita: `hypothesis`,
    `no_hypothesis` (con motivo) o `rejected` (con motivo). Un simbolo que el LLM
    omite queda como `no_hypothesis` con motivo "omitido".
  - Las hipotesis aceptadas se persisten via `PendingHypothesisStore.add`.
"""

from __future__ import annotations

import json
import logging
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Callable, Literal
from uuid import UUID

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, Field

from ..evaluation.strategy_registry import StrategyHypothesis
from ..schema.features import FeatureSet
from ..schema.hypotheses import PendingHypothesis
from ..schema.signals import Direction
from ._common import HISTORICAL_LANGUAGE_RULE
from .debate import render_analyst_digest, render_transcript
from .hypothesis_store import PendingHypothesisStore

if TYPE_CHECKING:
    from .state import GraphState

logger = logging.getLogger(__name__)

DEFAULT_HORIZON_S = 4 * 3600

_SYSTEM_PROMPT = (
    "Sos el Estratega/Sintetizador de HeatSignal. Leiste las salidas de los analistas y el "
    "debate Alcista/Bajista. Para CADA simbolo decidi: o instanciar UNA de las estrategias "
    "pre-registradas listadas (copiando su strategy_id EXACTO, direccion LONG/SHORT, confianza "
    "en [0, 1] y, si aplica, la distancia del trigger en unidades de ATR), o declarar "
    "'no_hypothesis' explicando el motivo. NUNCA inventes una estrategia ni una variante: si "
    "ninguna estrategia listada encaja, la respuesta correcta es 'no_hypothesis'. No elijas el "
    "precio de entrada: lo fija el sistema. Los huecos de datos declarados deben bajar la "
    "confianza o justificar 'no_hypothesis'. " + HISTORICAL_LANGUAGE_RULE
)


class StrategyProposal(BaseModel):
    """Lo que devuelve el LLM por simbolo (sin restricciones duras: se validan en codigo)."""

    symbol: str
    decision: Literal["hypothesis", "no_hypothesis"]
    strategy_id: str | None = Field(default=None, description="strategy_id EXACTO de la lista permitida")
    direction: Literal["LONG", "SHORT"] | None = None
    confidence: float | None = Field(default=None, description="Confianza en [0, 1]")
    atr_normalized_trigger: float | None = None
    rationale: str = Field(description="Justificacion historica, o motivo de no_hypothesis")


class SynthesisBatch(BaseModel):
    proposals: list[StrategyProposal]


DecisionOutcome = Literal["hypothesis", "no_hypothesis", "rejected"]


class SymbolDecision(BaseModel):
    """Decision final por simbolo/propuesta, tras la validacion en codigo."""

    symbol: str
    outcome: DecisionOutcome
    reason: str
    strategy_id: str | None = None
    hypothesis_id: UUID | None = None


def render_allowed_strategies(strategies: list[StrategyHypothesis]) -> str:
    if not strategies:
        return "(ninguna estrategia pre-registrada habilitada)"
    return "\n".join(
        f"- strategy_id={s.strategy_id}: {s.description} | entrada={json.dumps(s.entry_conditions, sort_keys=True)} "
        f"| salida={json.dumps(s.exit_conditions, sort_keys=True)}"
        for s in strategies
    )


def _horizon_for(strategy: StrategyHypothesis, default_horizon_s: int) -> int:
    raw = strategy.exit_conditions.get("horizon_s")
    if isinstance(raw, (int, float)) and not isinstance(raw, bool) and raw > 0:
        return int(raw)
    logger.warning(
        "strategy_id %s no pre-registra exit_conditions.horizon_s valido (%r); se usa default %ds",
        strategy.strategy_id,
        raw,
        default_horizon_s,
    )
    return default_horizon_s


def _validate(
    p: StrategyProposal,
    fs: FeatureSet,
    allowed: dict[str, StrategyHypothesis],
    run_id: str | None,
    default_horizon_s: int,
) -> PendingHypothesis | str:
    """Devuelve la hipotesis valida o el motivo de rechazo (str)."""
    if p.strategy_id is None or p.strategy_id.strip().lower() not in allowed:
        return f"strategy_id desconocido o no habilitado: {p.strategy_id!r}"
    strategy = allowed[p.strategy_id.strip().lower()]
    if p.direction is None:
        return "falta direction"
    if p.confidence is None or not (0.0 <= p.confidence <= 1.0):
        return f"confidence fuera de [0, 1]: {p.confidence!r}"
    mark = fs.context.mark_price
    if mark is None or mark <= 0:
        return "sin mark_price en FeatureSet.context: no hay precio de entrada de referencia"
    return PendingHypothesis(
        strategy_id=strategy.strategy_id,
        symbol=fs.symbol,
        direction=Direction(p.direction),
        confidence_score=p.confidence,
        entry_reference_price=Decimal(str(mark)),
        horizon_s=_horizon_for(strategy, default_horizon_s),
        atr_normalized_trigger=p.atr_normalized_trigger,
        rationale=p.rationale,
        heatmap_model_id=fs.heatmap.heatmap_model_id,
        run_id=run_id,
    )


def apply_proposals(
    proposals: list[StrategyProposal],
    features: dict[str, FeatureSet],
    allowed_strategies: list[StrategyHypothesis],
    *,
    run_id: str | None,
    default_horizon_s: int = DEFAULT_HORIZON_S,
) -> tuple[list[PendingHypothesis], list[SymbolDecision]]:
    """Validacion pura (sin LLM ni I/O): propuestas -> hipotesis + decisiones por simbolo."""
    allowed = {str(s.strategy_id).lower(): s for s in allowed_strategies}
    hypotheses: list[PendingHypothesis] = []
    decisions: list[SymbolDecision] = []
    seen: set[str] = set()
    for p in proposals:
        fs = features.get(p.symbol)
        if fs is None:
            logger.warning("synthesizer: propuesta para simbolo no solicitado %r rechazada", p.symbol)
            decisions.append(
                SymbolDecision(symbol=p.symbol, outcome="rejected", reason="simbolo no solicitado", strategy_id=p.strategy_id)
            )
            continue
        seen.add(p.symbol)
        if p.decision == "no_hypothesis":
            decisions.append(SymbolDecision(symbol=p.symbol, outcome="no_hypothesis", reason=p.rationale))
            continue
        result = _validate(p, fs, allowed, run_id, default_horizon_s)
        if isinstance(result, str):
            logger.warning("synthesizer: propuesta RECHAZADA para %s: %s", p.symbol, result)
            decisions.append(
                SymbolDecision(symbol=p.symbol, outcome="rejected", reason=result, strategy_id=p.strategy_id)
            )
            continue
        hypotheses.append(result)
        decisions.append(
            SymbolDecision(
                symbol=p.symbol,
                outcome="hypothesis",
                reason=p.rationale,
                strategy_id=str(result.strategy_id),
                hypothesis_id=result.hypothesis_id,
            )
        )
    for symbol in features:
        if symbol not in seen:
            logger.warning("synthesizer: el LLM omitio el simbolo %r; queda como no_hypothesis", symbol)
            decisions.append(
                SymbolDecision(symbol=symbol, outcome="no_hypothesis", reason="omitido por el modelo (sin decision)")
            )
    return hypotheses, decisions


def make_synthesizer_node(
    llm: BaseChatModel,
    *,
    hypothesis_store: PendingHypothesisStore | None = None,
    default_horizon_s: int = DEFAULT_HORIZON_S,
) -> Callable[["GraphState"], Any]:
    structured_llm = llm.with_structured_output(SynthesisBatch)

    async def _node(state: "GraphState") -> dict[str, Any]:
        features: dict[str, FeatureSet] = state.get("features_by_symbol") or {}
        allowed: list[StrategyHypothesis] = list(state.get("allowed_strategies") or [])
        if not features:
            return {"pending_hypotheses": [], "synthesis_decisions": []}
        if not allowed:
            # Sin estrategias pre-registradas no hay nada legitimo que instanciar: no se llama al LLM.
            return {
                "pending_hypotheses": [],
                "synthesis_decisions": [
                    SymbolDecision(symbol=s, outcome="no_hypothesis", reason="sin estrategias pre-registradas habilitadas")
                    for s in features
                ],
            }

        prompt = (
            f"# Estrategias pre-registradas permitidas\n{render_allowed_strategies(allowed)}\n\n"
            f"# Salidas de los analistas\n{render_analyst_digest(state)}\n\n"
            f"# Debate\n{render_transcript(state.get('debate_transcript') or [])}"
        )
        result: SynthesisBatch = await structured_llm.ainvoke(
            [SystemMessage(content=_SYSTEM_PROMPT), HumanMessage(content=prompt)]
        )
        hypotheses, decisions = apply_proposals(
            result.proposals,
            features,
            allowed,
            run_id=state.get("run_id"),
            default_horizon_s=default_horizon_s,
        )
        if hypothesis_store is not None:
            for h in hypotheses:
                hypothesis_store.add(h)
        elif hypotheses:
            logger.warning("synthesizer: sin hypothesis_store; %d hipotesis quedan solo en el estado", len(hypotheses))
        return {"pending_hypotheses": hypotheses, "synthesis_decisions": decisions}

    return _node
