"""
Panel interno de operacion (no es la API publica de Fase 4).

Corre dentro del proceso de ingesta (`ingestion/run.py`) porque en modo memory la ventana de eventos solo
existe en esa RAM. Escucha en 127.0.0.1: se abre con un tunel SSH, nunca se expone a internet
(decisions.md, "Datos de mercado solo en memoria": los datos no se ven en otro lado).

    GET /              pagina HTML (panel.html, sin dependencias externas)
    GET /api/status    workers, ventana en memoria, ultimo ciclo de analisis, estrategias e hipotesis
    GET /api/features  FeatureSet calculado AHORA desde la ventana (lo mismo que veria el grafo)
    GET /health        healthcheck

Los endpoints son `async def` a proposito: corren en el hilo del event loop, el mismo que usa la ingesta,
asi las conexiones SQLite del registry (no thread-safe) y las deques de la ventana no se tocan desde otro hilo.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse

from ..evaluation.pending_store import PendingHypothesisStore
from ..evaluation.strategy_registry import StrategyRegistry
from ..ingestion.health import HealthRegistry
from ..pipeline.analysis import AnalysisLoop
from ..storage.memory import MemoryWindowSink

_HTML_PATH = Path(__file__).with_name("panel.html")
MAX_HYPOTHESES = 50


@dataclass
class PanelContext:
    """Referencias vivas del proceso de ingesta que el panel lee (nunca escribe)."""

    health: HealthRegistry
    memory: MemoryWindowSink | None = None
    analysis: AnalysisLoop | None = None
    registry: StrategyRegistry | None = None
    store: PendingHypothesisStore | None = None
    config: dict[str, Any] = field(default_factory=dict)
    started_at: float = field(default_factory=time.time)


def _cycle_view(analysis: AnalysisLoop | None) -> dict[str, Any] | None:
    if analysis is None or analysis.last_result is None:
        return None
    r = analysis.last_result
    return {
        "run_id": r.run_id,
        "status": r.status,
        "n_hypotheses": r.n_hypotheses,
        "detail": r.detail,
        "features": {s: fs.model_dump(mode="json") for s, fs in r.features.items()},
    }


def _hypotheses_view(ctx: PanelContext) -> dict[str, Any]:
    if ctx.registry is None or ctx.store is None:
        return {"available": False, "strategies": [], "hypotheses": [], "pending_count": None}
    strategies = [
        {
            "strategy_id": str(s.strategy_id),
            "description": s.description,
            "status": s.status.value,
            "registered_at": s.registered_at.isoformat(),
            "author": s.author,
            "horizon_s": s.exit_conditions.get("horizon_s"),
        }
        for s in ctx.registry.get_all()
    ]
    records = []
    for sid in ctx.store.strategy_ids():
        records.extend(ctx.store.list_by_strategy(sid))
    records.sort(key=lambda r: r.hypothesis.created_at, reverse=True)
    hypotheses = []
    for rec in records[:MAX_HYPOTHESES]:
        h, res = rec.hypothesis, rec.resolution
        hypotheses.append({
            "hypothesis_id": str(h.hypothesis_id),
            "strategy_id": str(h.strategy_id),
            "symbol": h.symbol,
            "created_at": h.created_at.isoformat(),
            "direction": h.direction.value,
            "confidence": h.confidence_score,
            "entry": str(h.entry_reference_price),
            "horizon_s": h.horizon_s,
            "rationale": h.rationale,
            "status": rec.status.value,
            "exit": None if res is None or res.exit_price is None else str(res.exit_price),
            "pnl_pct": None if res is None else res.pnl_pct,
            "win": None if res is None else res.win,
        })
    return {
        "available": True,
        "strategies": strategies,
        "hypotheses": hypotheses,
        "total_hypotheses": len(records),
        "pending_count": ctx.store.pending_count(),
    }


def create_panel_app(ctx: PanelContext) -> FastAPI:
    app = FastAPI(title="HeatSignal panel interno", docs_url=None, redoc_url=None, openapi_url=None)

    @app.get("/", response_class=HTMLResponse)
    async def index() -> str:
        return _HTML_PATH.read_text(encoding="utf-8")

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/api/status")
    async def status() -> dict[str, Any]:
        ctx.health.sample()
        memory = None
        if ctx.memory is not None:
            memory = {
                "started_at_ms": ctx.memory.started_at_ms,
                "stats": ctx.memory.stats.as_dict(),
                "symbols": ctx.memory.summary(),
            }
        return {
            "now_ms": int(time.time() * 1000),
            "uptime_s": round(time.time() - ctx.started_at, 1),
            "config": ctx.config,
            "workers": ctx.health.snapshot(),
            "memory": memory,
            "analysis": {
                "enabled": ctx.analysis is not None,
                "graph_enabled": ctx.analysis is not None and ctx.analysis.graph_enabled,
                "last_cycle": _cycle_view(ctx.analysis),
            },
            "evaluation": _hypotheses_view(ctx),
        }

    @app.get("/api/features")
    async def features() -> dict[str, Any]:
        if ctx.analysis is None:
            raise HTTPException(status_code=503, detail="ciclo de analisis apagado: no hay features en vivo")
        now_ms = int(time.time() * 1000)
        fs = await ctx.analysis.build_features(now_ms)
        return {"ts_ms": now_ms, "features": {s: f.model_dump(mode="json") for s, f in fs.items()}}

    return app
