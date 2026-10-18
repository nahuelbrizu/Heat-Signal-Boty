"""
Analista de Contexto de Mercado — nodo de la capa de analistas (paralelo).

Lee `features_by_symbol[*].context` (mark, funding por exchange, OI, ATR y el
regimen calculado por codigo). El regimen NO lo decide el LLM: se copia de
features (`regime`) para que el benchmark pasivo tenga una definicion estable;
el LLM puede marcar desacuerdo en `regime_commentary`, pero no lo pisa.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Callable

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, Field

from ..schema.features import FeatureSet, Regime
from ._common import (
    HISTORICAL_LANGUAGE_RULE,
    MISSING_ANALYSIS_TEXT,
    merge_gaps,
    reconcile_by_symbol,
    render_context,
)

if TYPE_CHECKING:
    from .state import GraphState

_SYSTEM_PROMPT = (
    "Sos el Analista de Contexto de Mercado de HeatSignal. Recibis, por simbolo, el snapshot de "
    "tickers: mark, funding por exchange, open interest y su cambio, ATR y el regimen "
    "(tendencia/rango) YA calculado por codigo. Describi el contexto (posicionamiento via "
    "funding y OI, volatilidad via ATR) y si los datos son consistentes con el regimen "
    "calculado. No reemplaces el regimen: si discrepas, explicalo en `regime_commentary`. "
    "Tambien recibis el contexto de velas de 1h CERRADAS y la tendencia del dia UTC (`daily_trend`, regla fija "
    "calculada por codigo): describi donde esta el precio respecto de la apertura del dia, la EMA20 de 1h y el "
    "rango diario, sin reemplazar la etiqueta. "
    + HISTORICAL_LANGUAGE_RULE
)


class MarketContextAnalysis(BaseModel):
    symbol: str
    context_summary: str = Field(description="Descripcion historica del contexto, no predictiva")
    regime_commentary: str = Field(description="Consistencia (o no) de los datos con el regimen calculado")
    regime: Regime = Field(default="unknown", description="Completado por codigo desde features")
    confidence: float = Field(ge=0.0, le=1.0)
    data_gaps: list[str] = Field(default_factory=list)


class MarketContextAnalysisBatch(BaseModel):
    analyses: list[MarketContextAnalysis]


def _fill(item: MarketContextAnalysis, fs: FeatureSet) -> MarketContextAnalysis:
    return item.model_copy(
        update={"regime": fs.context.regime, "data_gaps": merge_gaps(fs.data_gaps, item.data_gaps)}
    )


def _missing(fs: FeatureSet) -> MarketContextAnalysis:
    return _fill(
        MarketContextAnalysis(
            symbol=fs.symbol,
            context_summary=MISSING_ANALYSIS_TEXT,
            regime_commentary=MISSING_ANALYSIS_TEXT,
            confidence=0.0,
        ),
        fs,
    )


def make_market_context_analyst_node(llm: BaseChatModel) -> Callable[["GraphState"], Any]:
    structured_llm = llm.with_structured_output(MarketContextAnalysisBatch)

    async def _node(state: "GraphState") -> dict[str, Any]:
        features: dict[str, FeatureSet] = state.get("features_by_symbol") or {}
        if not features:
            return {"market_context_analyses": []}
        messages = [
            SystemMessage(content=_SYSTEM_PROMPT),
            HumanMessage(content="\n".join(
                render_context(fs) + (f"\n  ultima sesion (codigo): {fs.session.summary}" if fs.session else "")
                for fs in features.values())),
        ]
        result: MarketContextAnalysisBatch = await structured_llm.ainvoke(messages)
        return {
            "market_context_analyses": reconcile_by_symbol(
                result.analyses, features, node_name="market_context_analyst", fill=_fill, make_missing=_missing
            )
        }

    return _node
