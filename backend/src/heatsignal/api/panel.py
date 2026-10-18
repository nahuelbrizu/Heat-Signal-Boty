"""
Panel interno de operacion (no es la API publica de Fase 4).

Corre dentro del proceso de ingesta (`ingestion/run.py`) porque en modo memory la ventana de eventos solo
existe en esa RAM. Escucha en 127.0.0.1: se abre con un tunel SSH, nunca se expone a internet
(decisions.md, "Datos de mercado solo en memoria": los datos no se ven en otro lado).

    GET /              single page app (panel.html: cascaron; la app vive en static/app/, modulos ES sin build
                       ni CDN; router por hash: #/mercado/SOL-USDT, #/ciclos/cycle-123, ...)
    GET /api/status    TODO junto (compatibilidad con panel.html): workers, ventana, ciclos, estrategias,
                       ultimas 50 hipotesis, noticias, historia, laboratorio. Pesado: preferir /api/summary
    GET /api/features  FeatureSet calculado AHORA desde la ventana (lo mismo que veria el grafo)
    GET /api/klines    velas de Binance pedidas en el momento y no guardadas (`api/klines.py`)
    GET /static/lightweight-charts.js   lightweight-charts 4.2.0 (TradingView, Apache 2.0) servido local, sin CDN
    GET /static/app/...                 JS/CSS propios de la SPA (solo .js/.css/.svg de static/app/)
    GET /health        healthcheck

API de solo lectura para la single page (`api/panel_api.py`; contrato y ejemplos en `api/API.md`):

    GET /api/summary                     estado liviano para el polling frecuente (workers, ventana resumida,
                                         ultimo ciclo resumido, conteos, build, frescura, LLM, grabacion)
    GET /api/hypotheses                  todas las hipotesis, filtradas y paginadas (strategy_id, symbol,
                                         status, win, since, until, timeframe, daily_trend, trend_alignment,
                                         order, limit, offset)
    GET /api/hypotheses/by-trend         tasa de acierto por tendencia del dia y a favor / en contra (n, IC Wilson)
    GET /api/htf/{symbol}                velas de 1h cerradas + tendencia del dia UTC (htf-trend-v1)
    GET /api/sessions?day=YYYY-MM-DD     diario del dia: sesiones Asia/Europa/EE.UU./cierre (DST), vela de apertura,
                                         analisis por codigo y del LLM (session_journal)
    GET /api/hypotheses/{id}             una hipotesis con su resolucion, su estrategia y el ciclo que la genero
    GET /api/strategies                  estrategias con track record (n, aciertos, IC Wilson, PnL, alpha, Brier)
    GET /api/strategies/{id}             spec completo, historial de estados, calibracion e hipotesis
    GET /api/cycles                      historial de ciclos del grafo (status, limit, offset), sin reportes
    GET /api/cycles/{run_id}             un ciclo con el razonamiento completo (cycle_log o checkpoint)
    GET /api/analysis/latest             ultimo analisis del grafo por simbolo
    GET /api/news                        noticias guardadas + ventana en memoria (symbol, source, primary, q,
                                         since, until, limit, offset)
    GET /api/history/summary             que hay guardado en la base historica
    GET /api/history/bars                barras de bars_1m agregadas a 1m/5m/15m/1h (symbol, exchange, tf,
                                         since, until, combine)
    GET /api/history/liquidations        liquidaciones guardadas (symbol, exchange, since, until, min_usd, ...)
    GET /api/lab                         resumen del laboratorio (lab_summary.json publicado desde la PC)
    GET /api/lab/{strategy_id}           detalle del laboratorio (lab_detail_<id>.json)
    GET /api/market/{symbol}             snapshot de mercado desde la ventana en memoria (vista por cripto)
    GET /api/paper                       paper trading: estado del dry-run de Freqtrade (base read-only + API REST),
                                         abiertas/cerradas con PnL, equity por operacion, resumen por par
    GET /api/paper/compare               ultima comparacion paper vs backtest (paper_summary.json) + historial
    GET /api/paperbot                    bot propio de paper trading del lote 3 (paperbot.sqlite, solo lectura):
                                         corridas, huecos, equity vs canasta y posiciones por estrategia
    GET /api/paperbot/{strategy_id}      detalle: equity horaria, ordenes, trades, funding, descomposicion
    GET /api/system                      operacion: uptime, version, tamanos de bases, export/backup, config,
                                         errores recientes

Acciones (lo unico que no es de solo lectura; exigen header `X-HeatSignal-Action: 1` y JSON <= 4 KB):

    POST /api/analyze                    {"symbol"}: ciclo del grafo fuera de turno para ese simbolo (202)
    GET  /api/analyze/{run_id}           estado de ese analisis (queued/running/estado del ciclo)
    POST /api/lab/requests               {"strategy_ids", "symbols", "note"}: encola un pedido al laboratorio
    GET  /api/lab/requests               cola (status, limit, offset)
    GET  /api/lab/requests/{id}          una solicitud con su historial de estados
    GET  /api/lab/requests/{id}/result   resultado subido por el worker de la PC (lab/worker.py)

Los endpoints son `async def` a proposito: corren en el hilo del event loop, el mismo que usa la ingesta,
asi las conexiones SQLite del registry (no thread-safe) y las deques de la ventana no se tocan desde otro hilo.
Las lecturas que pueden escanear mucho (base historica, checkpoints del grafo) van a un hilo aparte con una
conexion SQLite de solo lectura propia, abierta y cerrada en ese hilo.
"""

from __future__ import annotations

import asyncio
import hashlib
import re
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
_APP_DIR = Path(__file__).with_name("static") / "app"
_APP_TYPES = {".js": "application/javascript", ".css": "text/css", ".svg": "image/svg+xml"}
_APP_PATH_RE = re.compile(r"^[a-z0-9_-]+(/[a-z0-9_-]+)*\.(js|css|svg)$")
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
    checkpoint_db: Path | None = None  # checkpoints del grafo: respaldo de /api/cycles/{run_id}
    db_paths: dict[str, Path] = field(default_factory=dict)  # bases para /api/system (eval, history, ...)
    settings: dict[str, Any] = field(default_factory=dict)  # intervalos y retenciones para /api/system
    lab_requests: Any = None  # evaluation.lab_requests.LabRequestStore: cola "Mandar al laboratorio"
    paper: PaperSources | None = None  # paper trading (Freqtrade dry-run): base, API REST y comparacion publicada
    archive: Any = None  # history.cold.ColdReader: lectura fria de lo archivado en Drive (almacenamiento por niveles)
    paperbot_db: Path | None = None  # data/paperbot.sqlite del bot propio de paper trading (lote 3), solo lectura


@dataclass
class PaperSources:
    """De donde lee el panel el paper trading. Todo de solo lectura (ver paper/freqtrade_db.py)."""

    db_path: Path                       # base SQLite de Freqtrade (HEATSIGNAL_FREQTRADE_DB)
    secrets_path: Path | None = None    # config.secrets.json con api_server.username/password (HEATSIGNAL_FREQTRADE_SECRETS)
    api_url: str | None = None          # API REST de Freqtrade (HEATSIGNAL_FREQTRADE_API; "" = no consultarla)
    summary_path: Path | None = None    # paper_summary.json subido por lab/paper_compare.py --upload
    wallet: float = 10_000.0            # dry_run_wallet (si la API no responde)
    fetch: Any = None                   # inyectable en tests: (url, headers, timeout) -> (status, bytes)


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
_HISTORY_LOCKS: dict[int, asyncio.Lock] = {}
HISTORY_SUMMARY_TTL_S = 30.0


def _history_summary_sync(db_path: Path) -> dict[str, Any]:
    """COUNTs de la base historica con una conexion de solo lectura propia (corre en otro hilo)."""
    from ..history.store import open_readonly, summarize

    conn = open_readonly(db_path)
    try:
        summary = summarize(conn, db_path)
    finally:
        conn.close()
    exports = sorted((db_path.parent / "export").glob("*/*/*/bars_1m.csv.gz"))
    summary["last_export_day"] = "-".join(exports[-1].parts[-4:-1]) if exports else None
    summary["export_days"] = len(exports)
    return summary


async def _history_summary(ctx: PanelContext) -> tuple[float, dict[str, Any]] | None:
    """(momento del calculo, resumen). Cache 30 s; el COUNT de bars_1m/klines escanea tablas grandes, asi que
    se calcula en un hilo aparte y no frena la ingesta."""
    if ctx.history is None:
        return None
    key = id(ctx.history)
    cached = _HISTORY_CACHE.get(key)
    if cached is not None and time.time() - cached[0] <= HISTORY_SUMMARY_TTL_S:
        return cached
    lock = _HISTORY_LOCKS.setdefault(key, asyncio.Lock())
    async with lock:  # un solo recalculo a la vez aunque lleguen varios pedidos juntos
        cached = _HISTORY_CACHE.get(key)
        if cached is None or time.time() - cached[0] > HISTORY_SUMMARY_TTL_S:
            cached = (time.time(), await asyncio.to_thread(_history_summary_sync, Path(ctx.history.store.path)))
            _HISTORY_CACHE[key] = cached
    return cached


async def _history_view(ctx: PanelContext) -> dict[str, Any] | None:
    """Que se esta guardando: estadisticas del grabador + resumen de la base (cache 30 s)."""
    cached = await _history_summary(ctx)
    if cached is None:
        return None
    return {"recorder": ctx.history.stats.as_dict(), "store": cached[1], "summary_age_s": round(time.time() - cached[0], 1)}


_LAB_CACHE: dict[str, tuple[float, Any]] = {}


def json_safe(obj: Any) -> Any:
    """NaN/inf -> None, recursivo. La respuesta JSON se serializa con allow_nan=False: un NaN del
    laboratorio (p.ej. Sharpe de una regla que no opero) daria un 500 en vez de un null."""
    if isinstance(obj, float):
        return obj if obj == obj and obj not in (float("inf"), float("-inf")) else None
    if isinstance(obj, dict):
        return {k: json_safe(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [json_safe(v) for v in obj]
    return obj


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
            cached = (mtime, json_safe(json.loads(path.read_text(encoding="utf-8"))))
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
    # Incluye los archivos de la SPA (static/app/): cambiar un .js tambien recarga las paginas abiertas.
    digest = hashlib.sha256(_HTML_PATH.read_bytes())
    for f in sorted(_APP_DIR.rglob("*")) if _APP_DIR.is_dir() else []:
        if f.is_file():
            digest.update(f.relative_to(_APP_DIR).as_posix().encode() + b"\0" + f.read_bytes())
    digest.update(str(ctx.started_at).encode())
    build_id = digest.hexdigest()[:12]

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
                "llm_providers": [] if ctx.analysis is None else getattr(ctx.analysis, "llm_providers", []),
                "llm_cooldowns": _llm_cooldowns(),
                "last_cycle": _cycle_view(ctx.analysis),
                "last_ran": _cycle_view(ctx.analysis, ran=True),
                "history": [] if ctx.cycle_log is None else ctx.cycle_log.latest(30),
                "history_counts": {} if ctx.cycle_log is None else ctx.cycle_log.counts(),
                "per_symbol": _per_symbol_view(ctx),
            },
            "evaluation": _hypotheses_view(ctx),
            "news": None if ctx.news is None else ctx.news.summary(),
            "history": await _history_view(ctx),
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

    @app.get("/static/app/{path:path}")
    async def app_static(path: str) -> FileResponse:
        # Lista blanca de nombres (sin "..", sin mayusculas raras) y verificacion de que quede dentro de static/app.
        if not _APP_PATH_RE.match(path):
            raise HTTPException(status_code=404, detail="no existe")
        f = (_APP_DIR / path).resolve()
        if not f.is_file() or _APP_DIR.resolve() not in f.parents:
            raise HTTPException(status_code=404, detail="no existe")
        # no-cache: el navegador revalida (ETag) en cada carga, asi un deploy nunca deja modulos viejos mezclados
        return FileResponse(f, media_type=_APP_TYPES[f.suffix], headers={"Cache-Control": "no-cache"})

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

    from .panel_api import register_api

    register_api(app, ctx, build_id)
    return app


def _llm_cooldowns() -> dict:
    """Modelos LLM salteados ahora (cuota diaria agotada, sin credito, saturados) y por cuanto tiempo."""
    try:
        from ..agents.llm_factory import llm_status
    except Exception:  # noqa: BLE001 - sin dependencias del grafo: no hay modelos que mostrar
        return {}
    return llm_status()
