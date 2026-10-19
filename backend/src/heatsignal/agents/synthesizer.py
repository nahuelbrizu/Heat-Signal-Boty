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
  - Una hipotesis abierta por (estrategia, simbolo): si el store informa una sin resolver, la
    propuesta nueva queda como `no_hypothesis` (no se solapan ventanas sobre el mismo movimiento,
    que inflarian n). El store lo expone con `open_positions()`; sin ese metodo no hay candado.
  - Tope por familia, moneda y sesion: las estrategias con `entry_conditions.max_per_symbol_session = N` (hoy las 3
    de lateralidad, family "1h-lateralidad", N = 1) emiten como mucho N hipotesis por (familia, simbolo, sesion del
    dia) ENTRE TODAS las de la familia. Cuenta lo ya registrado en la base (`store.session_hypotheses`) mas lo
    aceptado en la misma corrida. Sin sesion de corrida no se puede contar -> rechazo explicito.
  - Las hipotesis aceptadas se persisten via `PendingHypothesisStore.add`.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Callable, Literal
from uuid import UUID

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, Field

from ..evaluation.strategy_registry import StrategyHypothesis
from ..evaluation.timeframes import check_htf_entry, hourly_horizon, is_hourly
from ..features.htf import trend_alignment
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
    "confianza o justificar 'no_hypothesis'. "
    "Estrategias de temporalidad 1h (salida con timeframe='1h'): formula la hipotesis sobre velas de 1h CERRADAS y "
    "en relacion con la tendencia del dia UTC calculada por codigo (daily_trend), p.ej. 'a favor de la tendencia "
    "diaria tras retroceso a la EMA de 1h' o 'contra la tendencia en el extremo del rango diario'. Solo propone una "
    "si el setup que la estrategia exige figura como presente en 'setups (codigo)'; la direccion debe quedar a favor "
    "o en contra segun lo pre-registrado. El horizonte lo fija el sistema (cierre de vela de 1h). "
    "Familia 1h-lateralidad (solo daily_trend=lateral): falsa ruptura del rango de Asia, rechazo del max/min de ayer "
    "con mecha, extremo vs VWAP con ADX bajo; solo si su bandera figura en 'lateral (codigo)' y en esa direccion; "
    "como mucho una por simbolo y sesion entre las tres. "
    + HISTORICAL_LANGUAGE_RULE
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
    """Horizonte de estrategias NO 1h (las de 1h usan `timeframes.hourly_horizon`)."""
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
    now: datetime | None = None,
    session: dict | None = None,
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
    created = (now or datetime.now(timezone.utc)).replace(microsecond=0)
    timeframe = strategy.exit_conditions.get("timeframe")
    bars = None
    if is_hourly(strategy.exit_conditions):
        entry_problem = check_htf_entry(strategy.entry_conditions, fs.htf, p.direction, fs.session,
                                        int(created.timestamp() * 1000), (session or {}).get("name"))
        if entry_problem:
            return f"no cumple la entrada pre-registrada de 1h: {entry_problem}"
        hz = hourly_horizon(strategy.exit_conditions, created)
        if isinstance(hz, str):
            return hz
        horizon_s, bars = hz
    else:
        horizon_s = _horizon_for(strategy, default_horizon_s)
    htf = fs.htf
    return PendingHypothesis(
        strategy_id=strategy.strategy_id,
        symbol=fs.symbol,
        created_at=created,
        direction=Direction(p.direction),
        confidence_score=p.confidence,
        entry_reference_price=Decimal(str(mark)),
        horizon_s=horizon_s,
        atr_normalized_trigger=p.atr_normalized_trigger,
        rationale=p.rationale,
        heatmap_model_id=fs.heatmap.heatmap_model_id,
        run_id=run_id,
        timeframe=timeframe if isinstance(timeframe, str) else None,
        horizon_bars=bars,
        daily_trend=None if htf is None else htf.daily_trend,
        trend_alignment=None if htf is None else trend_alignment(p.direction, htf.daily_trend),
        htf_model_id=None if htf is None else htf.htf_model_id,
        htf_snapshot=None if htf is None else htf.snapshot(),
        session=(session or {}).get("name"),
        session_day=(session or {}).get("day"),
    )


def session_cap(strategy: StrategyHypothesis) -> tuple[str, int] | None:
    """(familia, N) si la estrategia tiene tope por moneda y sesion (`max_per_symbol_session`)."""
    raw = strategy.entry_conditions.get("max_per_symbol_session")
    if not isinstance(raw, int) or isinstance(raw, bool) or raw < 1:
        return None
    return str(strategy.entry_conditions.get("family") or strategy.strategy_id), raw


def apply_proposals(
    proposals: list[StrategyProposal],
    features: dict[str, FeatureSet],
    allowed_strategies: list[StrategyHypothesis],
    *,
    run_id: str | None,
    default_horizon_s: int = DEFAULT_HORIZON_S,
    open_positions: set[tuple[str, str]] | None = None,
    now: datetime | None = None,
    session: dict | None = None,
    session_taken: set[tuple[str, str]] | None = None,
) -> tuple[list[PendingHypothesis], list[SymbolDecision]]:
    """Validacion pura (sin LLM ni I/O): propuestas -> hipotesis + decisiones por simbolo.

    `open_positions`: pares (strategy_id en minusculas, symbol) con hipotesis sin resolver.
    `session_taken`: pares (strategy_id en minusculas, symbol) ya emitidos en la sesion de la corrida (base).
    """
    allowed = {str(s.strategy_id).lower(): s for s in allowed_strategies}
    cap_count: dict[tuple[str, str], int] = {}
    for sid, sym in session_taken or ():
        cap = session_cap(allowed[sid]) if sid in allowed else None
        if cap:
            cap_count[(cap[0], sym)] = cap_count.get((cap[0], sym), 0) + 1
    open_keys = set(open_positions or ())
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
        result = _validate(p, fs, allowed, run_id, default_horizon_s, now, session)
        if isinstance(result, str):
            logger.warning("synthesizer: propuesta RECHAZADA para %s: %s", p.symbol, result)
            decisions.append(
                SymbolDecision(symbol=p.symbol, outcome="rejected", reason=result, strategy_id=p.strategy_id)
            )
            continue
        key = (str(result.strategy_id).lower(), result.symbol)
        if key in open_keys:
            reason = "ya hay una hipotesis abierta de esta estrategia en este simbolo (sin solapamiento)"
            logger.info("synthesizer: %s %s: %s", p.symbol, result.strategy_id, reason)
            decisions.append(
                SymbolDecision(symbol=p.symbol, outcome="no_hypothesis", reason=reason, strategy_id=str(result.strategy_id))
            )
            continue
        cap = session_cap(allowed[key[0]])
        if cap is not None:
            if not result.session or not result.session_day:
                reason = "sin sesion de la corrida: no se puede aplicar el tope de una por moneda y sesion"
                decisions.append(SymbolDecision(symbol=p.symbol, outcome="rejected", reason=reason,
                                                strategy_id=str(result.strategy_id)))
                continue
            if cap_count.get((cap[0], result.symbol), 0) >= cap[1]:
                reason = (f"ya hay {cap[1]} hipotesis de la familia {cap[0]} en {result.symbol} en la sesion "
                          f"{result.session} {result.session_day} (tope por moneda y sesion)")
                logger.info("synthesizer: %s %s: %s", p.symbol, result.strategy_id, reason)
                decisions.append(SymbolDecision(symbol=p.symbol, outcome="no_hypothesis", reason=reason,
                                                strategy_id=str(result.strategy_id)))
                continue
            cap_count[(cap[0], result.symbol)] = cap_count.get((cap[0], result.symbol), 0) + 1
        open_keys.add(key)
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

        open_fn = getattr(hypothesis_store, "open_positions", None)
        open_positions: set[tuple[str, str]] = open_fn() if callable(open_fn) else set()
        open_lines = "\n".join(f"- strategy_id={sid} symbol={sym}" for sid, sym in sorted(open_positions))
        sess = state.get("session") or {}
        taken_fn = getattr(hypothesis_store, "session_hypotheses", None)
        session_taken: set[tuple[str, str]] = set()
        if callable(taken_fn) and sess.get("day") and sess.get("name") and any(session_cap(s) for s in allowed):
            session_taken = taken_fn(sess["day"], sess["name"])
        prompt = (
            f"# Estrategias pre-registradas permitidas\n{render_allowed_strategies(allowed)}\n\n"
            "# Hipotesis abiertas (no se puede abrir otra de la misma estrategia en el mismo simbolo)\n"
            f"{open_lines or '(ninguna)'}\n\n"
            + (f"# Ya emitidas en esta sesion (tope por simbolo y sesion)\n"
               + "\n".join(f"- strategy_id={sid} symbol={sym}" for sid, sym in sorted(session_taken)) + "\n\n"
               if session_taken else "") +
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
            open_positions=open_positions,
            session=state.get("session"),
            session_taken=session_taken,
        )
        if hypothesis_store is not None:
            for h in hypotheses:
                hypothesis_store.add(h)
        elif hypotheses:
            logger.warning("synthesizer: sin hypothesis_store; %d hipotesis quedan solo en el estado", len(hypotheses))
        return {"pending_hypotheses": hypotheses, "synthesis_decisions": decisions}

    return _node
