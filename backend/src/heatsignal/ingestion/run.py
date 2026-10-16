"""
Entrypoint del servicio de ingesta: `python -m heatsignal.ingestion.run`.

Corre un worker WS por exchange en paralelo y los cablea a los sinks:

    worker -> CountingSink (health por worker)
           -> ThrottledSink (tickers a 1/seg por source+symbol)
           -> FanoutSink(RedisStreamSink, TimescaleSink)

Configuracion por variables de entorno (ver `.env.example` en la raiz del repo):
  HEATSIGNAL_SYMBOLS            "BTCUSDT,SOLUSDT"   simbolos en formato del exchange (sin guion)
  HEATSIGNAL_EXCHANGES          "binance,bybit,okx" exchanges a habilitar (okx solo si el modulo existe)
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
from typing import Any

from ..schema.sink import ConsoleSink, EventSink, RedisStreamSink
from ..storage.fanout import FanoutSink
from ..storage.throttle import ThrottledSink
from ..storage.timescale import TimescaleSink, _redact
from .binance_worker import BinanceWorker
from .bybit_worker import BybitWorker
from .health import CountingSink, HealthRegistry, ReconnectLogHandler

log = logging.getLogger("heatsignal.ingestion.run")

# ---- registro de workers --------------------------------------------------------------------------
# Todos comparten la API `Worker(symbols=[...], sink=...)` + `await run()` / `await stop()`.
WORKER_FACTORIES: dict[str, Callable[..., Any]] = {
    "binance": BinanceWorker,
    "bybit": BybitWorker,
}

# HOOK OKX: `ingestion/okx_worker.py` (misma API de constructor que BybitWorker) se esta escribiendo en
# paralelo. Se importa opcionalmente: cuando se mergee, "okx" queda habilitado solo, sin tocar este archivo.
try:
    from .okx_worker import OkxWorker  # type: ignore[import-not-found]
except ImportError:  # pragma: no cover - depende de si el modulo ya fue mergeado
    OkxWorker = None  # type: ignore[assignment,misc]
else:  # pragma: no cover
    WORKER_FACTORIES["okx"] = OkxWorker

KNOWN_EXCHANGES = ("binance", "bybit", "okx")
SHUTDOWN_TIMEOUT_S = 3.0


# ---- configuracion --------------------------------------------------------------------------------

@dataclass(frozen=True)
class RunConfig:
    symbols: tuple[str, ...]
    exchanges: tuple[str, ...]
    redis_url: str | None
    database_url: str | None
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

        return cls(
            symbols=symbols,
            exchanges=exchanges,
            redis_url=_opt("REDIS_URL"),
            database_url=_opt("DATABASE_URL"),
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

async def build_sinks(cfg: RunConfig) -> tuple[ThrottledSink, list[EventSink]]:
    """Arma Throttled -> Fanout(Redis, Timescale). Nunca lanza por falta de Redis/DB."""
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

    fanout = FanoutSink(*real)
    intervals = {"ticker": cfg.ticker_interval_s} if cfg.ticker_interval_s > 0 else {}
    return ThrottledSink(fanout, intervals), real


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

    try:
        if cfg.run_seconds:
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=cfg.run_seconds)
            log.info("HEATSIGNAL_RUN_SECONDS=%s cumplido - cerrando", cfg.run_seconds)
        else:
            await stop.wait()
            log.info("senal de parada recibida - cerrando")
    finally:
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
