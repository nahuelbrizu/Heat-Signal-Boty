"""
Registro (antes Estratega/Sintetizador): convierte la decision del CIO, ya revisada por el CRO, en hipotesis pendientes.

Desde graph-v2-cio NO llama al LLM: la decision que tomaba el LLM del Sintetizador la toma el CIO (`cio.py`) y el CRO
(`cro.py`) puede vetarla. Este modulo conserva la validacion en codigo y el registro:
  - Solo se instancian `allowed_strategies` (pre-registradas en `StrategyRegistry`); un `strategy_id` desconocido se
    RECHAZA y se loguea, nunca se "corrige" al mas parecido.
  - `confidence_score` (= `peso` del CIO) fuera de [0, 1] -> rechazo (no se recorta en silencio).
  - `entry_reference_price` lo pone el CODIGO desde `FeatureSet.context.mark_price`. Sin mark -> rechazo explicito.
  - `horizon_s` sale de `exit_conditions["horizon_s"]` de la estrategia si esta; si no, `default_horizon_s`
    (logueado como warning). Las de 1h usan `timeframes.hourly_horizon` y verifican la entrada por codigo.
  - Cada simbolo termina con una decision explicita: `hypothesis`, `no_hypothesis` (con motivo: no operar del CIO,
    veto del CRO, solapamiento...) o `rejected` (con motivo).
  - Una hipotesis abierta por (estrategia, simbolo) (`store.open_positions()`).
  - Tope por familia, moneda y sesion (`entry_conditions.max_per_symbol_session`): lo veta el CRO y se vuelve a
    verificar aca contra la base (`store.session_hypotheses`) como defensa.
  - Las hipotesis aceptadas se persisten via `PendingHypothesisStore.add` con `graph_version`, y cada simbolo deja su
    fila en `cio_verdicts` (`evaluation/cio_verdicts.py`), se opere o no.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Callable, Literal
from uuid import UUID

from pydantic import BaseModel, Field

from ..evaluation.strategy_registry import StrategyHypothesis
from ..evaluation.timeframes import check_htf_entry, hourly_horizon, is_hourly
from ..features.htf import trend_alignment
from ..schema.features import FeatureSet
from ..schema.hypotheses import PendingHypothesis
from ..schema.signals import Direction
from .hypothesis_store import PendingHypothesisStore

if TYPE_CHECKING:
    from .cio import CioDecision
    from .cro import CroReview
    from .state import GraphState

logger = logging.getLogger(__name__)

DEFAULT_HORIZON_S = 4 * 3600

class StrategyProposal(BaseModel):
    """Propuesta por simbolo (desde graph-v2-cio la arma el codigo a partir del CIO + CRO; se valida en codigo)."""

    symbol: str
    decision: Literal["hypothesis", "no_hypothesis"]
    strategy_id: str | None = Field(default=None, description="strategy_id EXACTO de la lista permitida")
    direction: Literal["LONG", "SHORT"] | None = None
    confidence: float | None = Field(default=None, description="Confianza en [0, 1]")
    atr_normalized_trigger: float | None = None
    rationale: str = Field(description="Justificacion historica, o motivo de no_hypothesis")


class SynthesisBatch(BaseModel):
    """Esquema del Sintetizador LLM de graph-v1 (ya no se llama). Se conserva para leer corridas viejas."""

    proposals: list[StrategyProposal]


DecisionOutcome = Literal["hypothesis", "no_hypothesis", "rejected"]


class SymbolDecision(BaseModel):
    """Decision final por simbolo/propuesta, tras la validacion en codigo."""

    symbol: str
    outcome: DecisionOutcome
    reason: str
    strategy_id: str | None = None
    hypothesis_id: UUID | None = None


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


def _proposals_from_cio(decisions: list["CioDecision"], reviews: dict[str, "CroReview"]
                        ) -> tuple[list[StrategyProposal], dict[str, SymbolDecision]]:
    """CIO + CRO -> propuestas para `apply_proposals` (solo las aprobadas) y decisiones ya cerradas (el resto)."""
    proposals: list[StrategyProposal] = []
    closed: dict[str, SymbolDecision] = {}
    for d in decisions:
        r = reviews.get(d.symbol)
        why = " ".join(d.razones) or "sin razones"
        if d.rejection and d.strategy_id is not None:
            closed[d.symbol] = SymbolDecision(symbol=d.symbol, outcome="rejected", strategy_id=d.strategy_id,
                                              reason=f"decision del CIO rechazada por codigo: {d.rejection}")
        elif not d.takes:
            closed[d.symbol] = SymbolDecision(symbol=d.symbol, outcome="no_hypothesis",
                                              reason=f"CIO: no operar ({d.postura}). {why}")
        elif r is None or not r.approved:
            closed[d.symbol] = SymbolDecision(symbol=d.symbol, outcome="no_hypothesis", strategy_id=d.strategy_id,
                                              reason=f"veto del CRO: {r.veto if r else 'sin revision'}")
        else:
            proposals.append(StrategyProposal(symbol=d.symbol, decision="hypothesis", strategy_id=d.strategy_id,
                                              direction=d.direction, confidence=d.peso, rationale=f"CIO: {why}"))
    return proposals, closed


def make_registrar_node(
    *,
    hypothesis_store: PendingHypothesisStore | None = None,
    verdict_log: Any = None,
    default_horizon_s: int = DEFAULT_HORIZON_S,
) -> Callable[["GraphState"], Any]:
    """Registro (codigo, sin LLM) despues del CRO: valida con `apply_proposals`, persiste via
    `PendingHypothesisStore.add` (con `graph_version`) y escribe una fila de `cio_verdicts` por simbolo."""
    from .cio import GRAPH_VERSION

    async def _node(state: "GraphState") -> dict[str, Any]:
        features: dict[str, FeatureSet] = state.get("features_by_symbol") or {}
        allowed: list[StrategyHypothesis] = list(state.get("allowed_strategies") or [])
        cio: list = list(state.get("cio_decisions") or [])
        if not features:
            return {"pending_hypotheses": [], "synthesis_decisions": []}
        reviews = {r.symbol: r for r in state.get("cro_reviews") or []}
        proposals, closed = _proposals_from_cio(cio, reviews)
        sess = state.get("session") or {}
        open_fn = getattr(hypothesis_store, "open_positions", None)
        open_positions: set[tuple[str, str]] = open_fn() if callable(open_fn) else set()
        taken_fn = getattr(hypothesis_store, "session_hypotheses", None)
        session_taken: set[tuple[str, str]] = set()
        if callable(taken_fn) and sess.get("day") and sess.get("name") and any(session_cap(s) for s in allowed):
            session_taken = taken_fn(sess["day"], sess["name"])
        ts_ms = state.get("cio_ts_ms")
        now = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc) if ts_ms else None
        accepted, decided = apply_proposals(
            proposals, {p.symbol: features[p.symbol] for p in proposals if p.symbol in features}, allowed,
            run_id=state.get("run_id"), default_horizon_s=default_horizon_s, open_positions=open_positions,
            now=now, session=state.get("session"), session_taken=session_taken)
        hypotheses = [h.model_copy(update={"graph_version": GRAPH_VERSION}) for h in accepted]
        ids = {h.symbol: h.hypothesis_id for h in hypotheses}
        by_symbol = {**closed, **{d.symbol: d for d in decided}}
        decisions = []
        for sym in features:
            d = by_symbol.get(sym) or SymbolDecision(symbol=sym, outcome="no_hypothesis",
                                                     reason="sin veredicto del CIO")
            if d.hypothesis_id is not None:
                d = d.model_copy(update={"hypothesis_id": ids.get(sym, d.hypothesis_id)})
            decisions.append(d)
        if hypothesis_store is not None:
            for h in hypotheses:
                hypothesis_store.add(h)
        elif hypotheses:
            logger.warning("registro: sin hypothesis_store; %d hipotesis quedan solo en el estado", len(hypotheses))
        rows = verdict_rows(state, cio, reviews, {d.symbol: d for d in decisions}, GRAPH_VERSION)
        if verdict_log is not None:
            verdict_log.append(rows)
        return {"pending_hypotheses": hypotheses, "synthesis_decisions": decisions, "cio_verdicts": rows}

    return _node


def verdict_rows(state: "GraphState", cio: list, reviews: dict[str, Any], decisions: dict[str, SymbolDecision],
                 graph_version: str) -> list[dict[str, Any]]:
    """Filas de `cio_verdicts` (una por simbolo del ciclo, se opere o no)."""
    sess = state.get("session") or {}
    ts_ms = int(state.get("cio_ts_ms") or datetime.now(timezone.utc).timestamp() * 1000)
    by_sym = {d.symbol: d for d in cio}
    rows = []
    for sym in state.get("features_by_symbol") or {}:
        c = by_sym.get(sym)
        r = reviews.get(sym)
        fin = decisions.get(sym)
        rows.append({
            "run_id": str(state.get("run_id") or ""), "ts_ms": ts_ms, "symbol": sym,
            "postura": c.postura if c else "empate", "peso": c.peso if c else None,
            "decision": "tomar" if c is not None and c.takes else "no_operar",
            "strategy_id": c.strategy_id if c else None, "direction": c.direction if c else None,
            "razones": list(c.razones) if c else ["sin veredicto del CIO"],
            "candidates": [x.model_dump() for x in c.candidates] if c else [],
            "cio_rejection": c.rejection if c else None, "cro_veto": r.veto if r else None,
            "outcome": fin.outcome if fin else None, "outcome_reason": fin.reason if fin else None,
            "hypothesis_id": str(fin.hypothesis_id) if fin and fin.hypothesis_id else None,
            "session": sess.get("name"), "session_day": sess.get("day"), "graph_version": graph_version,
        })
    return rows
