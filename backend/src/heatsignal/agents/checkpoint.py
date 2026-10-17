"""
Checkpoint resume por corrida (ver `agents.md`, mecanismo 3).

- `thread_id = f"{symbol-set}:{run_id}"`: el set de simbolos ordenado y unido por
  coma, mas el run_id. Una corrida interrumpida se reanuda invocando el grafo con
  `None` como input y el mismo `config`: LangGraph retoma desde el ultimo nodo
  exitoso (los nodos ya completados no se re-ejecutan).
- `sqlite_checkpointer(path)`: `AsyncSqliteSaver` de `langgraph-checkpoint-sqlite`
  (los nodos son async). Usar como context manager async.

El serializador permite explicitamente los modulos de schema de HeatSignal: el
estado lleva modelos Pydantic (FeatureSet, PendingHypothesis, ...) y LangGraph
restringe la deserializacion msgpack a tipos permitidos.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator, Iterable

from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer


def make_thread_id(symbols: Iterable[str], run_id: str) -> str:
    symbol_set = ",".join(sorted(set(symbols)))
    if not symbol_set:
        raise ValueError("make_thread_id: el set de simbolos no puede ser vacio")
    if not run_id:
        raise ValueError("make_thread_id: run_id vacio")
    return f"{symbol_set}:{run_id}"


def make_run_config(symbols: Iterable[str], run_id: str) -> dict[str, Any]:
    return {"configurable": {"thread_id": make_thread_id(symbols, run_id)}}


def _state_types() -> list[type]:
    """Tipos que viajan en `GraphState` (allowlist explicita, no "permitir todo")."""
    from ..evaluation.strategy_registry import StrategyHypothesis, StrategyStatus
    from ..schema.events import Exchange, Side
    from ..schema.features import (
        FeatureSet,
        HeatmapSnapshot,
        LiquidationCluster,
        MarketContext,
        OrderFlowWindow,
    )
    from ..schema.hypotheses import PendingHypothesis
    from ..schema.news import NewsItem
    from ..schema.signals import Direction
    from .debate import DebateTurn, SymbolArgument
    from .liquidation_analyst import LiquidationAnalysis
    from .market_context_analyst import MarketContextAnalysis
    from .news_analyst import NewsAnalysis
    from .order_flow_analyst import OrderFlowAnalysis
    from .redactor import ReportDraft, RunReport, SymbolReportSection
    from .synthesizer import SymbolDecision

    return [
        StrategyHypothesis, StrategyStatus, Exchange, Side, Direction,
        FeatureSet, HeatmapSnapshot, LiquidationCluster, MarketContext, OrderFlowWindow,
        PendingHypothesis, DebateTurn, SymbolArgument, LiquidationAnalysis,
        MarketContextAnalysis, OrderFlowAnalysis, ReportDraft, RunReport,
        SymbolReportSection, SymbolDecision, NewsItem, NewsAnalysis,
    ]


def make_serializer() -> JsonPlusSerializer:
    return JsonPlusSerializer(allowed_msgpack_modules=_state_types())


@asynccontextmanager
async def sqlite_checkpointer(path: str | Path) -> AsyncIterator[Any]:
    """Checkpointer SQLite async. Un archivo puede alojar muchos thread_id (pares/corridas)."""
    import aiosqlite
    from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

    async with aiosqlite.connect(str(path)) as conn:
        yield AsyncSqliteSaver(conn, serde=make_serializer())
