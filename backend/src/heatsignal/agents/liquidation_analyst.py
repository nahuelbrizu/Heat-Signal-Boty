"""
Analista de Liquidaciones — nodo de la capa de analistas (paralelo) del grafo.

Contrato de este nodo:
  - Lee `features_by_symbol[*].heatmap` (`schema/features.py`): clusters del
    heatmap propio YA normalizados por ATR (`distance_atr`) por la capa de
    features. Este nodo no lee eventos crudos ni calcula el heatmap.
  - Hace UN call batcheado con salida estructurada Pydantic para todos los
    simbolos (regla de `phase-2-agents.md`: no 1 call por agente por par).
  - Los campos numericos (`notional_usd_total`, `n_liquidations`,
    `nearest_cluster_distance_atr`) los completa el CODIGO desde el heatmap,
    no el LLM: el LLM interpreta, no transcribe numeros.
  - `data_gaps` del FeatureSet se unen por codigo a la salida (sin fallos
    silenciosos), ademas de pasarse en el prompt.

El LLM se inyecta (no se instancia aca): el nodo queda agnostico de proveedor
y testeable con un modelo falso, sin red ni API key.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Callable

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, Field

from ..schema.events import Side
from ..schema.features import FeatureSet
from ._common import (
    HISTORICAL_LANGUAGE_RULE,
    MISSING_ANALYSIS_TEXT,
    merge_gaps,
    reconcile_by_symbol,
    render_heatmap,
)

if TYPE_CHECKING:
    from .state import GraphState

_SYSTEM_PROMPT = (
    "Sos el Analista de Liquidaciones de HeatSignal. Recibis, por simbolo, los clusters del "
    "heatmap propio de liquidaciones: banda de precio, notional, lado de la orden dominante "
    "(SELL = longs liquidados), distancia al mark en unidades de ATR y fraccion de cobertura "
    "PARCIAL. Interpreta los clusters SIEMPRE en unidades de ATR (nunca con umbrales planos "
    "en porcentaje) y considera que una cobertura PARCIAL alta subestima el notional real. "
    + HISTORICAL_LANGUAGE_RULE
)


class LiquidationAnalysis(BaseModel):
    """Analisis de un simbolo para el heatmap de liquidaciones recibido."""

    symbol: str
    cluster_summary: str = Field(description="Resumen historico del/los cluster(s), no predictivo")
    dominant_side: Side | None = Field(default=None, description="Lado de orden dominante, si lo hay")
    notional_usd_total: float = Field(default=0.0, description="Completado por codigo desde el heatmap")
    n_liquidations: int = Field(default=0, description="Completado por codigo desde el heatmap")
    nearest_cluster_distance_atr: float | None = Field(
        default=None, description="Completado por codigo: cluster mas cercano al mark, en ATR"
    )
    confidence: float = Field(ge=0.0, le=1.0, description="Confianza del analista en el resumen, 0-1")
    data_gaps: list[str] = Field(default_factory=list, description="Huecos de datos que afectan el analisis")


class LiquidationAnalysisBatch(BaseModel):
    """Salida estructurada de un call batcheado: un analisis por simbolo."""

    analyses: list[LiquidationAnalysis]


def _summarize_heatmap(fs: FeatureSet) -> str:
    """Texto (no interpretado) que se le pasa al LLM para un simbolo.

    Un heatmap sin clusters se reporta como tal, no se omite: el LLM debe
    saber que no hubo actividad, no adivinar.
    """
    return render_heatmap(fs)


def _fill_from_heatmap(item: LiquidationAnalysis, fs: FeatureSet) -> LiquidationAnalysis:
    clusters = fs.heatmap.clusters
    distances = [c.distance_atr for c in clusters if c.distance_atr is not None]
    return item.model_copy(
        update={
            "notional_usd_total": sum(c.notional_usd for c in clusters),
            "n_liquidations": sum(c.n_events for c in clusters),
            "nearest_cluster_distance_atr": min(distances, key=abs) if distances else None,
            "data_gaps": merge_gaps(fs.data_gaps, item.data_gaps),
        }
    )


def _missing(fs: FeatureSet) -> LiquidationAnalysis:
    return _fill_from_heatmap(
        LiquidationAnalysis(symbol=fs.symbol, cluster_summary=MISSING_ANALYSIS_TEXT, confidence=0.0),
        fs,
    )


def make_liquidation_analyst_node(
    llm: BaseChatModel,
) -> Callable[["GraphState"], Any]:
    """Devuelve el nodo del grafo, con el LLM ya cerrado sobre el closure.

    `llm` debe soportar `.with_structured_output(Schema)`.
    """
    structured_llm = llm.with_structured_output(LiquidationAnalysisBatch)

    async def _node(state: "GraphState") -> dict[str, Any]:
        features: dict[str, FeatureSet] = state.get("features_by_symbol") or {}
        if not features:
            return {"liquidation_analyses": []}

        messages = [
            SystemMessage(content=_SYSTEM_PROMPT),
            HumanMessage(content="\n".join(_summarize_heatmap(fs) for fs in features.values())),
        ]
        result: LiquidationAnalysisBatch = await structured_llm.ainvoke(messages)
        analyses = reconcile_by_symbol(
            result.analyses,
            features,
            node_name="liquidation_analyst",
            fill=_fill_from_heatmap,
            make_missing=_missing,
        )
        return {"liquidation_analyses": analyses}

    return _node
