"""
Panel interno de operacion (no es la API publica de Fase 4).

Corre dentro del proceso de ingesta (`ingestion/run.py`) porque en modo memory la ventana de eventos solo
existe en esa RAM. Escucha en 127.0.0.1: se abre con un tunel SSH, nunca se expone a internet
(decisions.md, "Datos de mercado solo en memoria": los datos no se ven en otro lado).

    GET /              pagina HTML (panel.html, sin dependencias externas)
    GET /api/status    workers, ventana en memoria, ultimo ciclo de analisis, estrategias e hipotesis
    GET /api/features  FeatureSet calculado AHORA desde la ventana (lo mismo que veria el grafo)
    GET /api/klines    velas de Binance pedidas en el momento y no guardadas (`api/klines.py`)
    GET /static/...    lightweight-charts (TradingView, Apache 2.0) servido local, sin CDN
    GET /health        healthcheck

Los endpoints son `async def` a proposito: corren en el hilo del event loop, el mismo que usa la ingesta,
asi las conexiones SQLite del registry (no thread-safe) y las deques de la ventana no se tocan desde otro hilo.
"""

from __future__ import annotations

import hashlib
from datetime import timedelta
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, HTMLResponse

from ..evaluation.pending_store import PendingHypothesisStore
from ..evaluation.price_source import HttpGet, to_binance_symbol
from ..evaluation.strategy_registry import StrategyRegistry
from ..ingestion.health import HealthRegistry
from ..pipeline.analysis import AnalysisLoop
from ..storage.memory import MemoryWindowSink
from .klines import INTERVALS, MAX_LIMIT, KlinesError, fetch_klines

_HTML_PATH = Path(__file__).with_name("panel.html")
_CHARTS_JS = Path(__file__).with_name("static") / "lightweight-charts.standalone.production.js"
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
    http_get: HttpGet | None = None  # inyectable en tests (sin red)
    news: Any = None  # news.window.NewsWindow
    history: Any = None  # history.recorder.MinuteRecorder
    cycle_log: Any = None  # pipeline.cycle_log.CycleLog
    lab_summary_path: Path | None = None  # resumen del laboratorio publicado desde la PC (lab/publish.py)


def _cycle_view(analysis: AnalysisLoop | None, *, ran: bool = False) -> dict[str, Any] | None:
    if analysis is None:
        return None
    r = analysis.last_ran if ran else analysis.last_result
    if r is None:
        return None
    return {
        "run_id": r.run_id,
        "status": r.status,
        "n_hypotheses": r.n_hypotheses,
        "detail": r.detail,
        "decisions": r.decisions,
        "report": r.report,
        # el ultimo ciclo con LLM no repite los features (ya van en last_cycle y en /api/features)
        "features": {} if ran else {s: fs.model_dump(mode="json") for s, fs in r.features.items()},
    }


_HISTORY_CACHE: dict[int, tuple[float, dict[str, Any]]] = {}
HISTORY_SUMMARY_TTL_S = 30.0


def _history_view(ctx: PanelContext) -> dict[str, Any] | None:
    """Que se esta guardando. El resumen de la base (COUNTs) se cachea 30 s: la tabla crece mucho."""
    if ctx.history is None:
        return None
    cached = _HISTORY_CACHE.get(id(ctx.history))
    if cached is None or time.time() - cached[0] > HISTORY_SUMMARY_TTL_S:
        summary = ctx.history.store.summary()
        exports = sorted((ctx.history.store.path.parent / "export").glob("*/*/*/bars_1m.csv.gz"))
        summary["last_export_day"] = "-".join(exports[-1].parts[-4:-1]) if exports else None
        summary["export_days"] = len(exports)
        cached = (time.time(), summary)
        _HISTORY_CACHE[id(ctx.history)] = cached
    return {"recorder": ctx.history.stats.as_dict(), "store": cached[1], "summary_age_s": round(time.time() - cached[0], 1)}


_LAB_CACHE: dict[str, tuple[float, Any]] = {}


def _lab_view(ctx: PanelContext) -> Any:
    """Resumen del laboratorio (se relee solo si el archivo cambio)."""
    path = ctx.lab_summary_path
    if path is None or not path.exists():
        return None
    mtime = path.stat().st_mtime
    cached = _LAB_CACHE.get(str(path))
    if cached is None or cached[0] != mtime:
        import json

        try:
            cached = (mtime, json.loads(path.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            return {"error": "lab_summary.json ilegible"}
        _LAB_CACHE[str(path)] = cached
    return cached[1]


_PER_SYMBOL_CACHE: dict[int, tuple[float, dict[str, Any]]] = {}


def _per_symbol_view(ctx: PanelContext) -> dict[str, Any]:
    """Ultimo analisis de cada simbolo, desde el historial persistente (cache 30 s)."""
    if ctx.cycle_log is None:
        return {}
    cached = _PER_SYMBOL_CACHE.get(id(ctx.cycle_log))
    if cached is None or time.time() - cached[0] > HISTORY_SUMMARY_TTL_S:
        cached = (time.time(), ctx.cycle_log.latest_per_symbol())
        _PER_SYMBOL_CACHE[id(ctx.cycle_log)] = cached
    return cached[1]


def wilson(k: int, n: int, z: float = 1.959964) -> list[float] | None:
    """IC 95% de Wilson para una proporcion (sin dependencias: el panel corre en la VM sin pandas)."""
    if n <= 0:
        return None
    p = k / n
    den = 1 + z * z / n
    c = (p + z * z / (2 * n)) / den
    h = z * ((p * (1 - p) / n + z * z / (4 * n * n)) ** 0.5) / den
    return [max(0.0, c - h), min(1.0, c + h)]


def _resolved_summary(records: list) -> dict[str, Any]:
    """Track record global: cuantas resueltas, cuantas acerto (IC de Wilson), retorno neto y alpha medios."""
    res = [r for r in records if r.resolution is not None and r.resolution.win is not None]
    n = len(res)
    wins = sum(int(r.resolution.win) for r in res)
    alphas = [r.metrics.alpha_pct for r in res if r.metrics is not None]
    return {
        "n": n, "wins": wins, "rate": wins / n if n else None, "ci95": wilson(wins, n),
        "mean_pnl_pct": sum(r.resolution.pnl_pct for r in res) / n if n else None,
        "mean_alpha_pct": sum(alphas) / len(alphas) if alphas else None,
        "expired_no_data": sum(1 for r in records if r.status.value == "expired_no_data"),
    }


def _hypotheses_view(ctx: PanelContext) -> dict[str, Any]:
    if ctx.registry is None or ctx.store is None:
        return {"available": False, "strategies": [], "hypotheses": [], "pending_count": None}
    records = []
    for sid in ctx.store.strategy_ids():
        records.extend(ctx.store.list_by_strategy(sid))
    records.sort(key=lambda r: r.hypothesis.created_at, reverse=True)
    counts: dict[str, dict[str, int]] = {}
    for r in records:
        c = counts.setdefault(str(r.hypothesis.strategy_id), {"total": 0, "pending": 0, "wins": 0})
        c["total"] += 1
        c["pending"] += int(r.status.value == "pending")
        c["wins"] += int(r.resolution is not None and bool(r.resolution.win))
    per_strategy: dict[str, dict[str, Any]] = {}
    for rec in records:
        if rec.resolution is None or rec.resolution.win is None:
            continue
        st = per_strategy.setdefault(str(rec.hypothesis.strategy_id), {"resolved": 0, "wins": 0})
        st["resolved"] += 1
        st["wins"] += int(rec.resolution.win)
    for st in per_strategy.values():
        st["rate"], st["ci95"] = st["wins"] / st["resolved"], wilson(st["wins"], st["resolved"])
    strategies = [
        {
            "strategy_id": str(s.strategy_id),
            "description": s.description,
            "status": s.status.value,
            "registered_at": s.registered_at.isoformat(),
            "author": s.author,
            "symbol": s.entry_conditions.get("symbol"),
            "horizon_s": s.exit_conditions.get("horizon_s"),
            "hypotheses": counts.get(str(s.strategy_id), {"total": 0, "pending": 0, "wins": 0}),
            "track_record": per_strategy.get(str(s.strategy_id)),
        }
        for s in ctx.registry.get_all()
    ]
    hypotheses = []
    for rec in records[:MAX_HYPOTHESES]:
        h, res, met = rec.hypothesis, rec.resolution, rec.metrics
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
            "due_at": (h.created_at + timedelta(seconds=h.horizon_s)).isoformat(),
            "resolved_at": None if res is None else res.resolved_at.isoformat(),
            # comparacion con la realidad: cuanto se movio el precio, que dio mantener la moneda y la diferencia
            "real_move_pct": None if res is None or res.exit_price is None
            else float((res.exit_price / h.entry_reference_price - 1) * 100),
            "gross_pnl_pct": None if met is None else met.gross_pnl_pct,
            "benchmark_pnl_pct": None if met is None else met.benchmark_pnl_pct,
            "alpha_pct": None if met is None else met.alpha_pct,
        })
    return {
        "available": True,
        "strategies": strategies,
        "hypotheses": hypotheses,
        "total_hypotheses": len(records),
        "resolved": _resolved_summary(records),
        "pending_count": ctx.store.pending_count(),
    }


def create_panel_app(ctx: PanelContext) -> FastAPI:
    app = FastAPI(title="HeatSignal panel interno", docs_url=None, redoc_url=None, openapi_url=None)

    # Cambia con cada reinicio del proceso (deploy) o cambio del HTML: la pagina abierta lo compara en cada
    # poll y se recarga sola, asi nunca queda mostrando una version vieja del panel.
    build_id = hashlib.sha256(_HTML_PATH.read_bytes() + str(ctx.started_at).encode()).hexdigest()[:12]

    @app.get("/", response_class=HTMLResponse)
    async def index() -> HTMLResponse:
        return HTMLResponse(_HTML_PATH.read_text(encoding="utf-8"), headers={"Cache-Control": "no-store"})

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
            "build": build_id,
            "now_ms": int(time.time() * 1000),
            "uptime_s": round(time.time() - ctx.started_at, 1),
            "config": ctx.config,
            "workers": ctx.health.snapshot(),
            "memory": memory,
            "analysis": {
                "enabled": ctx.analysis is not None,
                "graph_enabled": ctx.analysis is not None and ctx.analysis.graph_enabled,
                "last_cycle": _cycle_view(ctx.analysis),
                "last_ran": _cycle_view(ctx.analysis, ran=True),
                "history": [] if ctx.cycle_log is None else ctx.cycle_log.latest(30),
                "history_counts": {} if ctx.cycle_log is None else ctx.cycle_log.counts(),
                "per_symbol": _per_symbol_view(ctx),
            },
            "evaluation": _hypotheses_view(ctx),
            "news": None if ctx.news is None else ctx.news.summary(),
            "history": _history_view(ctx),
            "lab": _lab_view(ctx),
        }

    @app.get("/api/features")
    async def features() -> dict[str, Any]:
        if ctx.analysis is None:
            raise HTTPException(status_code=503, detail="ciclo de analisis apagado: no hay features en vivo")
        now_ms = int(time.time() * 1000)
        fs = await ctx.analysis.build_features(now_ms)
        return {"ts_ms": now_ms, "features": {s: f.model_dump(mode="json") for s, f in fs.items()}}

    @app.get("/static/lightweight-charts.js")
    async def charts_js() -> FileResponse:
        return FileResponse(_CHARTS_JS, media_type="application/javascript")

    @app.get("/api/klines")
    async def klines(
        symbol: str,
        interval: str = Query("1h"),
        limit: int = Query(500, ge=1, le=MAX_LIMIT),
    ) -> dict[str, Any]:
        allowed = {to_binance_symbol(s) for s in ctx.config.get("symbols", [])}
        if to_binance_symbol(symbol) not in allowed:
            raise HTTPException(status_code=400, detail=f"simbolo no configurado: {symbol!r}")
        if interval not in INTERVALS:
            raise HTTPException(status_code=400, detail=f"interval debe ser uno de {list(INTERVALS)}")
        try:
            candles = await fetch_klines(symbol, interval, limit, http_get=ctx.http_get)
        except KlinesError as exc:
            raise HTTPException(status_code=502, detail=f"Binance: {exc}") from exc
        return {"symbol": symbol, "interval": interval, "source": "binance-futures-last", "candles": candles}

    return app
