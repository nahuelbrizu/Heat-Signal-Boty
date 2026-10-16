"""
Analista de Order Flow — nodo de la capa de analistas (paralelo).

Lee `features_by_symbol[*].order_flow` (agregado de trades taker por ventana,
calculado por la capa de features) y describe desequilibrios comprador/vendedor
en lenguaje historico. Un call batcheado para todos los simbolos. Los numeros
(`imbalance`, `cvd_usd`) los copia el CODIGO; el LLM solo interpreta.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Callable, Literal

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, Field

from ..schema.features import FeatureSet
from ._common import (
    HISTORICAL_LANGUAGE_RULE,
    MISSING_ANALYSIS_TEXT,
    merge_gaps,
    reconcile_by_symbol,
    render_order_flow,
)

if TYPE_CHECKING:
    from .state import GraphState

_SYSTEM_PROMPT = (
    "Sos el Analista de Order Flow de HeatSignal. Recibis, por simbolo, el agregado de trades "
    "del taker en una ventana: volumen comprador y vendedor en USD, CVD, imbalance en [-1, 1], "
    "cantidad de trades y volumen de block trades (excluido del CVD). Describi el desequilibrio "
    "comprador/vendedor observado y si es material o ruido para el volumen de la ventana. "
    + HISTORICAL_LANGUAGE_RULE
)

Aggressor = Literal["buyers", "sellers", "balanced"]


class OrderFlowAnalysis(BaseModel):
    symbol: str
    flow_summary: str = Field(description="Descripcion historica del desequilibrio, no predictiva")
    dominant_aggressor: Aggressor = Field(description="Quien domino la agresion taker en la ventana")
    imbalance: float = Field(default=0.0, description="Completado por codigo desde features")
    cvd_usd: float = Field(default=0.0, description="Completado por codigo desde features")
    confidence: float = Field(ge=0.0, le=1.0)
    data_gaps: list[str] = Field(default_factory=list)


class OrderFlowAnalysisBatch(BaseModel):
    analyses: list[OrderFlowAnalysis]


def _fill(item: OrderFlowAnalysis, fs: FeatureSet) -> OrderFlowAnalysis:
    return item.model_copy(
        update={
            "imbalance": fs.order_flow.imbalance,
            "cvd_usd": fs.order_flow.cvd_usd,
            "data_gaps": merge_gaps(fs.data_gaps, item.data_gaps),
        }
    )


def _missing(fs: FeatureSet) -> OrderFlowAnalysis:
    return _fill(
        OrderFlowAnalysis(
            symbol=fs.symbol, flow_summary=MISSING_ANALYSIS_TEXT, dominant_aggressor="balanced", confidence=0.0
        ),
        fs,
    )


def make_order_flow_analyst_node(llm: BaseChatModel) -> Callable[["GraphState"], Any]:
    structured_llm = llm.with_structured_output(OrderFlowAnalysisBatch)

    async def _node(state: "GraphState") -> dict[str, Any]:
        features: dict[str, FeatureSet] = state.get("features_by_symbol") or {}
        if not features:
            return {"order_flow_analyses": []}
        messages = [
            SystemMessage(content=_SYSTEM_PROMPT),
            HumanMessage(content="\n".join(render_order_flow(fs) for fs in features.values())),
        ]
        result: OrderFlowAnalysisBatch = await structured_llm.ainvoke(messages)
        return {
            "order_flow_analyses": reconcile_by_symbol(
                result.analyses, features, node_name="order_flow_analyst", fill=_fill, make_missing=_missing
            )
        }

    return _node
