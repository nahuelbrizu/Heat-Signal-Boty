"""
Entrypoint del servicio de ingesta: `python -m heatsignal.ingestion.run`.

Corre un worker WS por exchange en paralelo y los cablea a los sinks:

    worker -> CountingSink (health por worker)
           -> ThrottledSink (tickers a 1/seg por source+symbol)
           -> FanoutSink(MemoryWindowSink)                 HEATSIGNAL_STORAGE=memory (default)
           -> FanoutSink(RedisStreamSink, TimescaleSink)   HEATSIGNAL_STORAGE=persistent

En modo `memory` los datos de mercado solo pasan: viven en una ventana en RAM y se descartan
(decisions.md, "Datos de mercado solo en memoria"). REDIS_URL / DATABASE_URL se ignoran con aviso.

Configuracion por variables de entorno (ver `.env.example` en la raiz del repo):
  HEATSIGNAL_STORAGE            "memory" | "persistent"
  HEATSIGNAL_MEM_TRADES_S       1800   retencion en RAM por tipo (modo memory)
  HEATSIGNAL_MEM_TICKERS_S      7200
  HEATSIGNAL_MEM_LIQUIDATIONS_S 86400
  HEATSIGNAL_ANALYSIS_INTERVAL_S 900   ciclo features -> grafo -> hipotesis (modo memory; 0 lo apaga)
  HEATSIGNAL_ANALYSIS_WINDOW_S  1800   ventana de features (<= retencion de trades)
  HEATSIGNAL_GRAPH_ROTATION     2      simbolos sin estrategia que el grafo analiza por turnos en cada ciclo (0 = ninguno)
  HEATSIGNAL_NEWS_INTERVAL_S    300    poll de noticias RSS (news/feeds.py), ventana en memoria de 48 h (0 lo apaga)
  HEATSIGNAL_RECORD             1      guardar agregados por minuto + liquidaciones + noticias (history/), 0 lo apaga
  HEATSIGNAL_HISTORY_DB         data/heatsignal_history.sqlite   base historica privada (respaldo diario a Drive)
  HEATSIGNAL_EVAL_DB            data/heatsignal_eval.sqlite   estrategias + hipotesis (lo unico persistido)
  HEATSIGNAL_CHECKPOINT_DB      data/heatsignal_checkpoints.sqlite   checkpoints del grafo
  ANTHROPIC_API_KEY             sin key el ciclo solo calcula features (avisa en cada ciclo)
  HEATSIGNAL_PANEL_PORT         8000   panel interno en 127.0.0.1 (0 lo apaga); abrir con tunel SSH
  HEATSIGNAL_PANEL_HOST         127.0.0.1
  HEATSIGNAL_SYMBOLS            "BTCUSDT,SOLUSDT"   simbolos en formato del exchange (sin guion)
  HEATSIGNAL_EXCHANGES          "binance,bybit,okx,hyperliquid" exchanges a habilitar
  REDIS_URL                     p.ej. redis://localhost:6379/0     (opcional)
  DATABASE_URL                  p.ej. postgresql://u:p@host/db     (opcional)
  HEATSIGNAL_TICKER_INTERVAL_S  1.0   (0 desactiva el throttle de tickers)
  HEATSIGNAL_DB_BATCH_SIZE      500
  HEATSIGNAL_DB_FLUSH_S         1.0
  HEATSIGNAL_HEALTH_LOG_S       60    cada cuanto loguear el resumen de salud por worker
  HEATSIGNAL_STALE_AFTER_S      30    sin datos por mas de esto -> worker marcado CAIDO
  HEATSIGNAL_RUN_SECONDS        (vacio) corre N segundos y sale limpio - util para smoke tests
  HEATSIGNAL_LOG_LEVEL          INFO

Si falta REDIS_URL / DATABASE_URL o no responden, NO crashea: avisa fuerte y sigue con lo que haya; si no
queda ningun sink real, cae a `ConsoleSink`. Shutdown limpio con SIGINT/SIGTERM (flush de pendientes).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import signal
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..schema.sink import ConsoleSink, EventSink, RedisStreamSink
from ..storage.fanout import FanoutSink
from ..storage.memory import MemoryWindowSink
from ..storage.throttle import ThrottledSink
from ..storage.timescale import TimescaleSink, _redact
from .binance_worker import BinanceWorker, _norm_symbol
from .bybit_worker import BybitWorker
from .hyperliquid_worker import HyperliquidWorker
from .health import CountingSink, HealthRegistry, ReconnectLogHandler

log = logging.getLogger("heatsignal.ingestion.run")

# ---- registro de workers --------------------------------------------------------------------------
# Todos comparten la API `Worker(symbols=[...], sink=...)` + `await run()` / `await stop()`.
WORKER_FACTORIES: dict[str, Callable[..., Any]] = {
    "binance": BinanceWorker,
    "bybit": BybitWorker,
    "hyperliquid": HyperliquidWorker,
}

# HOOK OKX: `ingestion/okx_worker.py` (misma API de constructor que BybitWorker) se esta escribiendo en
# paralelo. Se importa opcionalmente: cuando se mergee, "okx" queda habilitado solo, sin tocar este archivo.
try:
    from .okx_worker import OkxWorker  # type: ignore[import-not-found]
except ImportError:  # pragma: no cover - depende de si el modulo ya fue mergeado
    OkxWorker = None  # type: ignore[assignment,misc]
else:  # pragma: no cover
    WORKER_FACTORIES["okx"] = OkxWorker

KNOWN_EXCHANGES = ("binance", "bybit", "okx", "hyperliquid")
STORAGE_MODES = ("memory", "persistent")
SHUTDOWN_TIMEOUT_S = 3.0


# ---- configuracion --------------------------------------------------------------------------------

@dataclass(frozen=True)
class RunConfig:
    symbols: tuple[str, ...]
    exchanges: tuple[str, ...]
    redis_url: str | None
    database_url: str | None
    storage: str = "memory"
    mem_trades_s: float = 1800.0
    mem_tickers_s: float = 7200.0
    mem_liquidations_s: float = 86400.0
    analysis_interval_s: float = 900.0
    analysis_window_s: int = 1800
    news_interval_s: float = 300.0
    graph_rotation: int = 2
    record: bool = True
    history_db: str = "data/heatsignal_history.sqlite"
    eval_db: str = "data/heatsignal_eval.sqlite"
    checkpoint_db: str = "data/heatsignal_checkpoints.sqlite"
    panel_port: int = 8000
    panel_host: str = "127.0.0.1"
    ticker_interval_s: float = 1.0
    db_batch_size: int = 500
    db_flush_s: float = 1.0
    health_log_s: float = 60.0
    stale_after_s: float = 30.0
    run_seconds: float | None = None
    trades_retention_days: int | None = 30
    tickers_retention_days: int | None = 90
    compress_after_days: int | None = 7
    log_level: str = "INFO"

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> RunConfig:
        env = os.environ if env is None else env

        def _opt(name: str) -> str | None:
            value = env.get(name, "").strip()
            return value or None

        def _float(name: str, default: float) -> float:
            raw = _opt(name)
            if raw is None:
                return default
            try:
                return float(raw)
            except ValueError as exc:
                raise ValueError(f"{name} invalido: {raw!r}") from exc

        symbols = tuple(
            s.strip().upper().replace("-", "")
            for s in env.get("HEATSIGNAL_SYMBOLS", "BTCUSDT,SOLUSDT").split(",")
            if s.strip()
        )
        if not symbols:
            raise ValueError("HEATSIGNAL_SYMBOLS vacio")

        exchanges = tuple(
            dict.fromkeys(
                e.strip().lower()
                for e in env.get("HEATSIGNAL_EXCHANGES", ",".join(KNOWN_EXCHANGES)).split(",")
                if e.strip()
            )
        )
        unknown = [e for e in exchanges if e not in KNOWN_EXCHANGES]
        if unknown:
            raise ValueError(f"exchanges desconocidos en HEATSIGNAL_EXCHANGES: {unknown}")
        if not exchanges:
            raise ValueError("HEATSIGNAL_EXCHANGES vacio")

        run_seconds = _float("HEATSIGNAL_RUN_SECONDS", 0.0)

        def _days(name: str, default: int) -> int | None:
            # 0 = sin politica (sin retencion / sin compresion).
            value = int(_float(name, default))
            return value if value > 0 else None

        storage = (_opt("HEATSIGNAL_STORAGE") or "memory").lower()
        if storage not in STORAGE_MODES:
            raise ValueError(f"HEATSIGNAL_STORAGE invalido: {storage!r} (opciones: {STORAGE_MODES})")

        return cls(
            symbols=symbols,
            exchanges=exchanges,
            redis_url=_opt("REDIS_URL"),
            database_url=_opt("DATABASE_URL"),
            storage=storage,
            mem_trades_s=_float("HEATSIGNAL_MEM_TRADES_S", 1800.0),
            mem_tickers_s=_float("HEATSIGNAL_MEM_TICKERS_S", 7200.0),
            mem_liquidations_s=_float("HEATSIGNAL_MEM_LIQUIDATIONS_S", 86400.0),
            analysis_interval_s=_float("HEATSIGNAL_ANALYSIS_INTERVAL_S", 900.0),
            analysis_window_s=int(_float("HEATSIGNAL_ANALYSIS_WINDOW_S", 1800)),
            news_interval_s=_float("HEATSIGNAL_NEWS_INTERVAL_S", 300.0),
            graph_rotation=int(_float("HEATSIGNAL_GRAPH_ROTATION", 2)),
            record=(_opt("HEATSIGNAL_RECORD") or "1").strip().lower() not in ("0", "false", "no"),
            history_db=_opt("HEATSIGNAL_HISTORY_DB") or "data/heatsignal_history.sqlite",
            eval_db=_opt("HEATSIGNAL_EVAL_DB") or "data/heatsignal_eval.sqlite",
            checkpoint_db=_opt("HEATSIGNAL_CHECKPOINT_DB") or "data/heatsignal_checkpoints.sqlite",
            panel_port=int(_float("HEATSIGNAL_PANEL_PORT", 8000)),
            panel_host=_opt("HEATSIGNAL_PANEL_HOST") or "127.0.0.1",
            ticker_interval_s=_float("HEATSIGNAL_TICKER_INTERVAL_S", 1.0),
            db_batch_size=int(_float("HEATSIGNAL_DB_BATCH_SIZE", 500)),
            db_flush_s=_float("HEATSIGNAL_DB_FLUSH_S", 1.0),
            health_log_s=_float("HEATSIGNAL_HEALTH_LOG_S", 60.0),
            stale_after_s=_float("HEATSIGNAL_STALE_AFTER_S", 30.0),
            run_seconds=run_seconds if run_seconds > 0 else None,
            trades_retention_days=_days("HEATSIGNAL_TRADES_RETENTION_DAYS", 30),
            tickers_retention_days=_days("HEATSIGNAL_TICKERS_RETENTION_DAYS", 90),
            compress_after_days=_days("HEATSIGNAL_COMPRESS_AFTER_DAYS", 7),
            log_level=(_opt("HEATSIGNAL_LOG_LEVEL") or "INFO").upper(),
        )

    def enabled_exchanges(self) -> list[str]:
        """Exchanges pedidos que tienen worker disponible (avisa por los que no)."""
        out = []
        for name in self.exchanges:
            if name in WORKER_FACTORIES:
                out.append(name)
            else:
                log.warning("exchange %r pedido pero su worker no esta disponible todavia - se omite", name)
        return out


# ---- sinks ----------------------------------------------------------------------------------------

def _throttled(cfg: RunConfig, sinks: list[EventSink]) -> ThrottledSink:
    intervals = {"ticker": cfg.ticker_interval_s} if cfg.ticker_interval_s > 0 else {}
    return ThrottledSink(FanoutSink(*sinks), intervals)


async def build_sinks(cfg: RunConfig) -> tuple[ThrottledSink, list[EventSink]]:
    """Arma Throttled -> Fanout(...) segun `cfg.storage`. Nunca lanza por falta de Redis/DB."""
    if cfg.storage == "memory":
        for name, value in (("REDIS_URL", cfg.redis_url), ("DATABASE_URL", cfg.database_url)):
            if value:
                log.warning("HEATSIGNAL_STORAGE=memory: %s configurado pero se IGNORA (no se persiste)", name)
        memory = MemoryWindowSink(retention_s={
            "trade": cfg.mem_trades_s,
            "ticker": cfg.mem_tickers_s,
            "liquidation": cfg.mem_liquidations_s,
        })
        log.info(
            "modo memory: ventana de analisis en RAM (trades %.0fs, tickers %.0fs, liquidaciones %.0fs)",
            cfg.mem_trades_s, cfg.mem_tickers_s, cfg.mem_liquidations_s,
        )
        sinks: list[EventSink] = [memory]
        if cfg.record:
            from ..history.recorder import MinuteRecorder
            from ..history.store import HistoryStore

            try:
                sinks.append(MinuteRecorder(HistoryStore(cfg.history_db)))
                log.info("grabando agregados por minuto, liquidaciones y noticias en %s", cfg.history_db)
            except Exception:  # noqa: BLE001 - sin disco la ingesta y el analisis siguen igual
                log.exception("!!! no se pudo abrir %s - NO se guarda historia !!!", cfg.history_db)
        else:
            log.warning("HEATSIGNAL_RECORD=0: no se guarda historia (solo ventana en RAM)")
        return _throttled(cfg, sinks), sinks

    real: list[EventSink] = []

    if cfg.redis_url:
        redis_sink = await RedisStreamSink.connect(cfg.redis_url)
        if isinstance(redis_sink, ConsoleSink):
            log.warning("!!! REDIS_URL configurado pero Redis no responde - SIN buffers en Redis !!!")
        else:
            real.append(redis_sink)
    else:
        log.warning("!!! REDIS_URL no configurado - SIN buffers en Redis !!!")

    if cfg.database_url:
        ts_sink = await TimescaleSink.connect(
            cfg.database_url,
            batch_size=cfg.db_batch_size,
            flush_interval_s=cfg.db_flush_s,
            retention_days={
                "liquidations": None,  # nucleo del producto: nunca se borran
                "trades": cfg.trades_retention_days,
                "tickers": cfg.tickers_retention_days,
            },
            compress_after_days=cfg.compress_after_days,
        )
        if ts_sink is None:
            log.warning(
                "!!! DATABASE_URL configurado pero PostgreSQL no responde (%s) - SIN persistencia !!!",
                _redact(cfg.database_url),
            )
        else:
            real.append(ts_sink)
    else:
        log.warning("!!! DATABASE_URL no configurado - SIN persistencia !!!")

    if not real:
        log.warning("!!! ningun sink real disponible - usando ConsoleSink (los datos NO se guardan) !!!")
        real.append(ConsoleSink())

    return _throttled(cfg, real), real


# ---- analisis (features -> grafo -> hipotesis) -----------------------------------------------------

@dataclass
class AnalysisHandles:
    task: asyncio.Task
    loop: Any  # pipeline.analysis.AnalysisLoop
    registry: Any  # evaluation.strategy_registry.StrategyRegistry
    store: Any  # evaluation.pending_store.PendingHypothesisStore
    news: Any = None  # news.window.NewsWindow
    news_task: asyncio.Task | None = None
    cycle_log: Any = None  # pipeline.cycle_log.CycleLog


def _memory_sink(sinks: list[EventSink]) -> MemoryWindowSink | None:
    return next((s for s in sinks if isinstance(s, MemoryWindowSink)), None)


def _recorder(sinks: list[EventSink]) -> Any:
    from ..history.recorder import MinuteRecorder

    return next((s for s in sinks if isinstance(s, MinuteRecorder)), None)


async def start_analysis(
    cfg: RunConfig, sinks: list[EventSink], stop: asyncio.Event, stack: contextlib.AsyncExitStack
) -> AnalysisHandles | None:
    """Arranca el ciclo de analisis si corresponde. Todo lo que abre queda en `stack`."""
    if cfg.analysis_interval_s <= 0:
        log.info("analisis apagado (HEATSIGNAL_ANALYSIS_INTERVAL_S=0)")
        return None
    memory = _memory_sink(sinks)
    if memory is None:
        log.warning("analisis en proceso solo disponible con HEATSIGNAL_STORAGE=memory - no se arranca")
        return None
    if cfg.analysis_window_s > cfg.mem_trades_s:
        log.warning(
            "HEATSIGNAL_ANALYSIS_WINDOW_S=%d > retencion de trades %.0fs: el order flow quedara corto "
            "(se declara en data_gaps)", cfg.analysis_window_s, cfg.mem_trades_s,
        )

    from ..agents.checkpoint import make_run_config, sqlite_checkpointer
    from ..agents.graph import build_graph
    from ..agents.llm_factory import MissingApiKeyError, make_llms
    from ..evaluation.pending_store import PendingHypothesisStore
    from ..evaluation.strategy_registry import StrategyRegistry
    from ..pipeline.analysis import AnalysisLoop

    Path(cfg.eval_db).parent.mkdir(parents=True, exist_ok=True)
    registry = StrategyRegistry(cfg.eval_db)
    stack.callback(registry.close)
    store = PendingHypothesisStore(cfg.eval_db, registry)
    stack.callback(store.close)

    symbols = [_norm_symbol(s) for s in cfg.symbols]
    runner = None
    try:
        llms = make_llms()
    except MissingApiKeyError:
        log.warning("!!! falta ANTHROPIC_API_KEY - el ciclo de analisis solo calcula features !!!")
    else:
        Path(cfg.checkpoint_db).parent.mkdir(parents=True, exist_ok=True)
        checkpointer = await stack.enter_async_context(sqlite_checkpointer(cfg.checkpoint_db))
        graph = build_graph(llms.cheap, llms.strong, checkpointer=checkpointer, hypothesis_store=store)

        async def runner(state: dict[str, Any], run_id: str) -> dict[str, Any]:
            return await graph.ainvoke(state, make_run_config(symbols, run_id))

    news = news_task = None
    if cfg.news_interval_s > 0:
        from ..news.window import NewsWindow

        recorder = _recorder(sinks)
        news = NewsWindow(interval_s=cfg.news_interval_s, on_new=recorder.record_news if recorder else None)
        news_task = asyncio.create_task(news.run_forever(stop), name="news")
    else:
        log.info("noticias apagadas (HEATSIGNAL_NEWS_INTERVAL_S=0)")

    from ..pipeline.cycle_log import CycleLog

    cycle_log = CycleLog(cfg.eval_db)
    stack.callback(cycle_log.close)
    loop = AnalysisLoop(
        memory, symbols, registry, graph_runner=runner,
        interval_s=cfg.analysis_interval_s, window_s=cfg.analysis_window_s, news=news, cycle_log=cycle_log,
        rotation_per_cycle=cfg.graph_rotation,
    )
    task = asyncio.create_task(loop.run_forever(stop), name="analysis")
    return AnalysisHandles(task=task, loop=loop, registry=registry, store=store, news=news, news_task=news_task,
                           cycle_log=cycle_log)


# ---- panel interno --------------------------------------------------------------------------------

async def start_panel(
    cfg: RunConfig, sinks: list[EventSink], health: HealthRegistry, analysis: AnalysisHandles | None
) -> tuple[Any, asyncio.Task] | None:
    """Panel interno (api/panel.py) en el mismo event loop. Solo lectura; escucha en `panel_host`."""
    if cfg.panel_port <= 0:
        log.info("panel interno apagado (HEATSIGNAL_PANEL_PORT=0)")
        return None
    import uvicorn

    from ..api.panel import PanelContext, create_panel_app

    if cfg.panel_host not in ("127.0.0.1", "localhost", "::1"):
        log.warning("!!! panel interno escuchando en %s: queda expuesto fuera de la maquina !!!", cfg.panel_host)
    ctx = PanelContext(
        health=health,
        memory=_memory_sink(sinks),
        history=_recorder(sinks),
        cycle_log=analysis.cycle_log if analysis else None,
        lab_summary_path=Path(cfg.eval_db).parent / "lab_summary.json",
        analysis=analysis.loop if analysis else None,
        registry=analysis.registry if analysis else None,
        store=analysis.store if analysis else None,
        news=analysis.news if analysis else None,
        config={
            "symbols": list(cfg.symbols), "exchanges": list(cfg.exchanges), "storage": cfg.storage,
            "analysis_interval_s": cfg.analysis_interval_s, "analysis_window_s": cfg.analysis_window_s,
        },
    )

    class _Server(uvicorn.Server):
        # Las senales las maneja run(); uvicorn no debe pisar esos handlers.
        def capture_signals(self):  # type: ignore[override]
            return contextlib.nullcontext()

    server = _Server(uvicorn.Config(
        create_panel_app(ctx), host=cfg.panel_host, port=cfg.panel_port, log_level="warning", access_log=False,
    ))
    task = asyncio.create_task(server.serve(), name="panel")
    log.info("panel interno en http://%s:%d", cfg.panel_host, cfg.panel_port)
    return server, task


# ---- ciclo de vida --------------------------------------------------------------------------------

async def _supervise(name: str, worker: Any) -> None:
    """`worker.run()` ya reconecta solo; si igual termina con excepcion, se loguea fuerte."""
    try:
        await worker.run()
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001
        log.exception("worker %s termino con error - queda CAIDO hasta reiniciar el servicio", name)


def _log_final_stats(throttled: ThrottledSink, sinks: list[EventSink], health: HealthRegistry) -> None:
    health.sample()
    health.log_summary()
    log.info("throttle: emitidos=%d submuestreados=%d", throttled.passed, throttled.coalesced)
    fanout = throttled.inner
    if isinstance(fanout, FanoutSink):
        log.info("fanout errores por sink: %s", fanout.error_counts())
    for s in sinks:
        stats = getattr(s, "stats", None)
        if stats is not None:
            log.info("%s stats: %s", type(s).__name__, stats.as_dict())


async def run(cfg: RunConfig, *, stop_event: asyncio.Event | None = None) -> None:
    stop = stop_event or asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError, RuntimeError):
            loop.add_signal_handler(sig, stop.set)

    names = cfg.enabled_exchanges()
    if not names:
        log.error("ningun exchange habilitado - nada que correr")
        return

    throttled, sinks = await build_sinks(cfg)
    health = HealthRegistry(stale_after_s=cfg.stale_after_s)
    reconnect_handler = ReconnectLogHandler(health, {n: n for n in names})
    ingestion_logger = logging.getLogger("heatsignal.ingestion")
    ingestion_logger.addHandler(reconnect_handler)

    workers = {
        n: WORKER_FACTORIES[n](symbols=list(cfg.symbols), sink=CountingSink(throttled, health, n))
        for n in names
    }
    log.info("ingesta arrancando: exchanges=%s simbolos=%s", names, list(cfg.symbols))
    tasks = {n: asyncio.create_task(_supervise(n, w), name=f"worker-{n}") for n, w in workers.items()}
    sampler = asyncio.create_task(
        health.run_sampler(sample_s=min(5.0, cfg.health_log_s), log_every_s=cfg.health_log_s)
    )
    analysis_stack = contextlib.AsyncExitStack()
    analysis: AnalysisHandles | None = None
    panel: tuple[Any, asyncio.Task] | None = None

    try:
        try:
            analysis = await start_analysis(cfg, sinks, stop, analysis_stack)
        except Exception:  # noqa: BLE001 - la ingesta sigue aunque el analisis no arranque
            log.exception("no se pudo arrancar el ciclo de analisis - la ingesta sigue sin el")
        try:
            panel = await start_panel(cfg, sinks, health, analysis)
        except Exception:  # noqa: BLE001 - la ingesta sigue aunque el panel no arranque
            log.exception("no se pudo arrancar el panel interno - la ingesta sigue sin el")
        if cfg.run_seconds:
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=cfg.run_seconds)
            log.info("HEATSIGNAL_RUN_SECONDS=%s cumplido - cerrando", cfg.run_seconds)
        else:
            await stop.wait()
            log.info("senal de parada recibida - cerrando")
    finally:
        stop.set()  # tambien corta el ciclo de analisis cuando termina por HEATSIGNAL_RUN_SECONDS
        # stop() de todos en paralelo y acotado: el cierre WS de algunos exchanges espera el close
        # handshake del server (Bybit puede tardar ~10s); si no llega, se cancela la tarea del worker.
        async def _stop(n: str, w: Any) -> None:
            try:
                await asyncio.wait_for(w.stop(), timeout=SHUTDOWN_TIMEOUT_S)
            except asyncio.TimeoutError:
                log.warning("worker %s: stop() no termino en %.0fs", n, SHUTDOWN_TIMEOUT_S)
            except Exception:  # noqa: BLE001
                log.warning("error deteniendo worker %s", n, exc_info=True)

        await asyncio.gather(*(_stop(n, w) for n, w in workers.items()))
        _, still = await asyncio.wait(tasks.values(), timeout=SHUTDOWN_TIMEOUT_S)
        for t in still:
            log.warning("%s no termino a tiempo - cancelando", t.get_name())
            t.cancel()
        await asyncio.gather(*tasks.values(), return_exceptions=True)
        sampler.cancel()
        await asyncio.gather(sampler, return_exceptions=True)
        if panel is not None:
            server, panel_task = panel
            server.should_exit = True
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(asyncio.shield(panel_task), timeout=SHUTDOWN_TIMEOUT_S)
            panel_task.cancel()
            await asyncio.gather(panel_task, return_exceptions=True)
        if analysis is not None:
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(asyncio.shield(analysis.task), timeout=SHUTDOWN_TIMEOUT_S)
            analysis.task.cancel()
            await asyncio.gather(analysis.task, return_exceptions=True)
            if analysis.news_task is not None:
                analysis.news_task.cancel()  # puede estar en medio de un GET: no se espera
                await asyncio.gather(analysis.news_task, return_exceptions=True)
        await analysis_stack.aclose()
        await throttled.close()  # flush de tickers pendientes + cierre de fanout/sinks
        _log_final_stats(throttled, sinks, health)
        ingestion_logger.removeHandler(reconnect_handler)
        for sig in (signal.SIGINT, signal.SIGTERM):
            with contextlib.suppress(NotImplementedError, RuntimeError):
                loop.remove_signal_handler(sig)


def main() -> None:
    cfg = RunConfig.from_env()
    logging.basicConfig(
        level=getattr(logging, cfg.log_level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    asyncio.run(run(cfg))


if __name__ == "__main__":
    main()
