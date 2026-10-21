"""
Redactor: reporte por corrida en lenguaje historico-no-predictivo.

Flujo con guardrail en CODIGO (`guardrails.find_predictive_phrases`):
  1. Borrador estructurado del LLM.
  2. Si contiene frases predictivas -> UN reintento con feedback explicito
     (las frases detectadas).
  3. Si el reintento sigue fallando -> el reporte queda con
     `status="guardrail_failed"` y las violaciones listadas. No se descarta ni se
     "limpia" en silencio: el consumidor (dashboard, Fase 4) NO debe publicarlo.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Callable, Literal

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from pydantic import BaseModel, Field

from ..schema.features import FeatureSet
from ._common import HISTORICAL_LANGUAGE_RULE
from .debate import render_analyst_digest, render_transcript
from .guardrails import find_predictive_phrases

if TYPE_CHECKING:
    from .state import GraphState

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT = (
    "Sos el Redactor de HeatSignal. Escribi el reporte de esta corrida para un humano: que "
    "observaron los analistas, que discutio el debate y que hipotesis pendientes se registraron "
    "(o por que no se registro ninguna, incluidos los rechazos). Las hipotesis son PENDIENTES de "
    "resolucion: no las presentes como senales confirmadas ni des win-rates. Menciona todos los "
    "huecos de datos declarados; las notas de cobertura son conocidas: no las llames severas ni huecos. PROHIBIDO lenguaje predictivo o de certeza ('va a subir', "
    "'subira', 'garantiza', 'seguro que', 'will rise'). "
    "Si la corrida es de una sesion de mercado (Asia, Europa, EE.UU. o cierre del dia UTC) y hay diario del dia, el "
    "resumen es el analisis de ESA sesion: sumalo a lo que ya dijeron las sesiones previas de hoy (que se confirmo, "
    "que cambio), con la vela de apertura y la tendencia del dia calculadas por codigo. "
    "Menciona por simbolo el veredicto del CIO (postura que gano el debate, su peso y si tomo un candidato o no opero) "
    "y, si los hubo, los rechazos por codigo y los vetos del CRO con su motivo. " + HISTORICAL_LANGUAGE_RULE
)

ReportStatus = Literal["ok", "ok_after_retry", "guardrail_failed"]


class SymbolReportSection(BaseModel):
    symbol: str
    text: str


class ReportDraft(BaseModel):
    """Salida estructurada del LLM."""

    title: str
    summary: str
    sections: list[SymbolReportSection] = Field(default_factory=list)


class RunReport(BaseModel):
    """Reporte final guardado en el estado."""

    run_id: str | None
    status: ReportStatus
    attempts: int = Field(ge=0)
    violations: list[str] = Field(default_factory=list, description="Frases predictivas del ultimo intento")
    draft: ReportDraft | None


def draft_text(draft: ReportDraft) -> str:
    return "\n".join([draft.title, draft.summary, *(f"{s.symbol}: {s.text}" for s in draft.sections)])


def _render_decisions(state: "GraphState") -> str:
    decisions = state.get("synthesis_decisions") or []
    if not decisions:
        return "(sin decisiones)"
    hyps = {h.hypothesis_id: h for h in state.get("pending_hypotheses") or []}
    lines = []
    for d in decisions:
        line = f"- {d.symbol}: {d.outcome} ({d.reason})"
        h = hyps.get(d.hypothesis_id) if d.hypothesis_id else None
        if h is not None:
            line += (
                f" -> strategy_id={h.strategy_id}, {h.direction.value}, confianza {h.confidence_score:.2f}, "
                f"precio de referencia {h.entry_reference_price}, horizonte {h.horizon_s}s"
            )
        lines.append(line)
    return "\n".join(lines)


def _render_cio(state: "GraphState") -> str:
    cio = state.get("cio_decisions") or []
    if not cio:
        return "(sin veredicto del CIO)"
    reviews = {r.symbol: r for r in state.get("cro_reviews") or []}
    lines = []
    for d in cio:
        peso = "sin peso" if d.peso is None else f"peso {d.peso:.2f}"
        take = f"tomar {d.strategy_id} {d.direction}" if d.takes else "no operar"
        line = f"- {d.symbol}: postura {d.postura} ({peso}); {take}. Razones: {' '.join(d.razones) or '-'}"
        if d.rejection:
            line += f" | rechazo por codigo: {d.rejection}"
        r = reviews.get(d.symbol)
        if r is not None and r.veto:
            line += f" | VETO del CRO: {r.veto}"
        lines.append(line)
    return "\n".join(lines)


def make_redactor_node(llm: BaseChatModel) -> Callable[["GraphState"], Any]:
    structured_llm = llm.with_structured_output(ReportDraft)

    async def _node(state: "GraphState") -> dict[str, Any]:
        run_id = state.get("run_id")
        features: dict[str, FeatureSet] = state.get("features_by_symbol") or {}
        if not features:
            return {"report": RunReport(run_id=run_id, status="ok", attempts=0, draft=None)}

        prompt = (
            f"Corrida {run_id}.\n\n# Analistas\n{render_analyst_digest(state)}\n\n"
            f"# Debate\n{render_transcript(state.get('debate_transcript') or [])}\n\n"
            f"# Veredicto del CIO y control del CRO\n{_render_cio(state)}\n\n"
            f"# Decisiones registradas\n{_render_decisions(state)}"
        )
        messages: list = [SystemMessage(content=_SYSTEM_PROMPT), HumanMessage(content=prompt)]
        draft: ReportDraft = await structured_llm.ainvoke(messages)
        violations = find_predictive_phrases(draft_text(draft))
        if not violations:
            return {"report": RunReport(run_id=run_id, status="ok", attempts=1, draft=draft)}

        logger.warning("redactor: lenguaje predictivo detectado %s; reintentando una vez", violations)
        retry_messages = [
            *messages,
            AIMessage(content=draft_text(draft)),
            HumanMessage(
                content=(
                    "Tu borrador fue RECHAZADO por el guardrail de lenguaje: contiene frases "
                    f"predictivas o de certeza: {violations}. Reescribi el reporte completo en "
                    "lenguaje estrictamente historico y descriptivo, sin esas frases ni equivalentes."
                )
            ),
        ]
        draft = await structured_llm.ainvoke(retry_messages)
        violations = find_predictive_phrases(draft_text(draft))
        if not violations:
            return {"report": RunReport(run_id=run_id, status="ok_after_retry", attempts=2, draft=draft)}

        logger.error("redactor: guardrail FALLIDO tras reintento; reporte marcado guardrail_failed: %s", violations)
        return {
            "report": RunReport(
                run_id=run_id, status="guardrail_failed", attempts=2, violations=violations, draft=draft
            )
        }

    return _node
