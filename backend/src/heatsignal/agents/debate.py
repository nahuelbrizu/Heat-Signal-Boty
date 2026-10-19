"""
Capa de debate (dialectica anti-sesgo): Estratega-Alcista vs Estratega-Bajista.

Ambos argumentan sobre LAS MISMAS salidas de los tres analistas. Cada turno es
UN call batcheado para todos los simbolos. Una ronda = un turno alcista + un
turno bajista; el loop se controla con una arista condicional en `graph.py`
(`max_debate_rounds`). El transcript completo queda en el estado
(`debate_transcript`, reducer de concatenacion) y cada turno ve los anteriores.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Callable, Literal

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, Field

from ..schema.features import FeatureSet
from ._common import HISTORICAL_LANGUAGE_RULE, format_data_gaps, htf_digest_line, new_real_gaps

if TYPE_CHECKING:
    from .state import GraphState

logger = logging.getLogger(__name__)

DebateSide = Literal["bull", "bear"]

_SIDE_PROMPTS: dict[str, str] = {
    "bull": (
        "Sos el Estratega-Alcista de HeatSignal. Construi, por simbolo, el mejor argumento "
        "historico a favor de un escenario alcista (LONG) usando SOLO la evidencia de los analistas. "
        "Si hubo turnos previos del Estratega-Bajista, respondele punto por punto. Si la evidencia "
        "alcista es debil, decilo: un argumento honesto y debil vale mas que uno inflado. "
    ),
    "bear": (
        "Sos el Estratega-Bajista de HeatSignal. Construi, por simbolo, el mejor argumento "
        "historico a favor de un escenario bajista (SHORT) usando SOLO la evidencia de los analistas. "
        "Si hubo turnos previos del Estratega-Alcista, respondele punto por punto. Si la evidencia "
        "bajista es debil, decilo: un argumento honesto y debil vale mas que uno inflado. "
    ),
}


class SymbolArgument(BaseModel):
    symbol: str
    argument: str = Field(description="Argumento en lenguaje historico, no predictivo")
    key_evidence: list[str] = Field(default_factory=list, description="Evidencia de los analistas citada")
    strength: float = Field(ge=0.0, le=1.0, description="Fuerza honesta del argumento propio, 0-1")
    data_gaps_acknowledged: list[str] = Field(default_factory=list)


class DebateArgumentBatch(BaseModel):
    arguments: list[SymbolArgument]


class DebateTurn(BaseModel):
    """Un turno del debate guardado en el transcript del estado."""

    round: int = Field(ge=1)
    side: DebateSide
    arguments: list[SymbolArgument]


def render_analyst_digest(state: "GraphState") -> str:
    """Resumen por simbolo de las salidas de los analistas + huecos declarados."""
    features: dict[str, FeatureSet] = state.get("features_by_symbol") or {}
    liq = {a.symbol: a for a in state.get("liquidation_analyses") or []}
    flow = {a.symbol: a for a in state.get("order_flow_analyses") or []}
    ctx = {a.symbol: a for a in state.get("market_context_analyses") or []}
    news = {a.symbol: a for a in state.get("news_analyses") or []}
    blocks: list[str] = []
    sess = state.get("session") or {}
    if sess.get("name"):
        blocks.append(f"# Sesion de esta corrida: {sess.get('label') or sess['name']} (dia UTC {sess.get('day')})")
    if state.get("day_journal"):
        blocks.append("# Diario del dia (analisis de sesiones previas de HOY, sumar a esto)\n" + str(state["day_journal"]))
    for symbol, fs in features.items():
        lines = [f"## {symbol}", f"- {format_data_gaps(fs)}", htf_digest_line(fs)]
        if fs.session is not None:
            lines.append(f"- Ultima sesion (codigo, {fs.session.session}): {fs.session.summary}")
        if (a := liq.get(symbol)) is not None:
            lines.append(
                f"- Liquidaciones (conf {a.confidence:.2f}): {a.cluster_summary} "
                f"[notional ${a.notional_usd_total:,.0f}, n={a.n_liquidations}, "
                f"cluster mas cercano {a.nearest_cluster_distance_atr} ATR, "
                f"huecos: {new_real_gaps(a.data_gaps, fs)}]"
            )
        else:
            lines.append("- Liquidaciones: SIN SALIDA del analista")
        if (b := flow.get(symbol)) is not None:
            lines.append(
                f"- Order flow (conf {b.confidence:.2f}): {b.flow_summary} "
                f"[agresor {b.dominant_aggressor}, imbalance {b.imbalance:+.3f}, CVD ${b.cvd_usd:+,.0f}, "
                f"huecos: {new_real_gaps(b.data_gaps, fs)}]"
            )
        else:
            lines.append("- Order flow: SIN SALIDA del analista")
        if (c := ctx.get(symbol)) is not None:
            lines.append(
                f"- Contexto (conf {c.confidence:.2f}, regimen {c.regime}): {c.context_summary} "
                f"| {c.regime_commentary} [huecos: {new_real_gaps(c.data_gaps, fs)}]"
            )
        else:
            lines.append("- Contexto: SIN SALIDA del analista")
        if (d := news.get(symbol)) is not None:
            lines.append(
                f"- Noticias (conf {d.confidence:.2f}, tono {d.tone}, {d.n_items} items, {d.n_primary} de fuente "
                f"primaria, negativa material primaria: {'SI' if d.material_primary_negative else 'no'}): "
                f"{d.news_summary} [ids relevantes: {', '.join(d.relevant_item_ids) or 'ninguno'}; "
                f"huecos: {new_real_gaps(d.data_gaps, fs)}]"
            )
        else:
            lines.append("- Noticias: SIN SALIDA del analista")
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


def render_transcript(turns: list[DebateTurn]) -> str:
    if not turns:
        return "(sin turnos previos)"
    out: list[str] = []
    for t in turns:
        label = "ALCISTA" if t.side == "bull" else "BAJISTA"
        for arg in t.arguments:
            out.append(f"[ronda {t.round} {label} {arg.symbol} fuerza {arg.strength:.2f}] {arg.argument}")
    return "\n".join(out)


def make_debate_node(llm: BaseChatModel, side: DebateSide) -> Callable[["GraphState"], Any]:
    """Nodo de un lado del debate. El bajista cierra la ronda (incrementa `debate_round`)."""
    structured_llm = llm.with_structured_output(DebateArgumentBatch)
    system = _SIDE_PROMPTS[side] + HISTORICAL_LANGUAGE_RULE

    async def _node(state: "GraphState") -> dict[str, Any]:
        features: dict[str, FeatureSet] = state.get("features_by_symbol") or {}
        current_round = (state.get("debate_round") or 0) + 1
        update: dict[str, Any] = {}
        if side == "bear":
            update["debate_round"] = current_round
        if not features:
            return update

        prompt = (
            f"Ronda de debate {current_round}.\n\n# Salidas de los analistas\n"
            f"{render_analyst_digest(state)}\n\n# Transcript previo\n"
            f"{render_transcript(state.get('debate_transcript') or [])}"
        )
        result: DebateArgumentBatch = await structured_llm.ainvoke(
            [SystemMessage(content=system), HumanMessage(content=prompt)]
        )
        arguments = [a for a in result.arguments if a.symbol in features]
        dropped = len(result.arguments) - len(arguments)
        if dropped:
            logger.warning("debate %s: %d argumento(s) sobre simbolos no solicitados descartados", side, dropped)
        missing = set(features) - {a.symbol for a in arguments}
        if missing:
            logger.warning("debate %s ronda %d: sin argumento para %s", side, current_round, sorted(missing))
        update["debate_transcript"] = [DebateTurn(round=current_round, side=side, arguments=arguments)]
        return update

    return _node


def make_debate_router(max_debate_rounds: int) -> Callable[["GraphState"], str]:
    """Arista condicional despues del bajista: otra ronda o pasar al sintetizador."""

    # Sin anotaciones: LangGraph evalua los type hints del router y `GraphState`
    # solo existe bajo TYPE_CHECKING (evita import circular).
    def _route(state):  # type: ignore[no-untyped-def]
        return "bull_strategist" if (state.get("debate_round") or 0) < max_debate_rounds else "synthesizer"

    return _route
