"""Tests del entrypoint de ingesta: config por env, armado de sinks y metrica de salud."""

from __future__ import annotations

import asyncio
import logging

import pytest

from heatsignal.ingestion import run as run_mod
from heatsignal.ingestion.health import CountingSink, HealthRegistry, ReconnectLogHandler
from heatsignal.ingestion.run import RunConfig, build_sinks
from heatsignal.schema.events import Exchange, TickerEvent
from heatsignal.schema.sink import BufferedSink, ConsoleSink
from heatsignal.storage.fanout import FanoutSink
from heatsignal.storage.memory import MemoryWindowSink
from heatsignal.storage.throttle import ThrottledSink

# ---- config ---------------------------------------------------------------------------------------


def test_config_defaults() -> None:
    cfg = RunConfig.from_env({})
    assert cfg.symbols == ("BTCUSDT", "SOLUSDT")
    assert cfg.exchanges == ("binance", "bybit", "okx")
    assert cfg.redis_url is None and cfg.database_url is None
    assert cfg.ticker_interval_s == 1.0
    assert cfg.run_seconds is None
    assert cfg.storage == "memory"


def test_config_parses_env() -> None:
    cfg = RunConfig.from_env({
        "HEATSIGNAL_SYMBOLS": " btcusdt, ETH-USDT ,,",
        "HEATSIGNAL_EXCHANGES": "Bybit,bybit, binance",
        "REDIS_URL": "redis://r:6379/0",
        "DATABASE_URL": "  ",
        "HEATSIGNAL_TICKER_INTERVAL_S": "2.5",
        "HEATSIGNAL_RUN_SECONDS": "30",
        "HEATSIGNAL_LOG_LEVEL": "debug",
    })
    assert cfg.symbols == ("BTCUSDT", "ETHUSDT")
    assert cfg.exchanges == ("bybit", "binance")  # sin duplicados, orden preservado
    assert cfg.redis_url == "redis://r:6379/0"
    assert cfg.database_url is None  # vacio = no configurado
    assert cfg.ticker_interval_s == 2.5
    assert cfg.run_seconds == 30.0
    assert cfg.log_level == "DEBUG"


@pytest.mark.parametrize("env", [
    {"HEATSIGNAL_EXCHANGES": "binance,kraken"},
    {"HEATSIGNAL_SYMBOLS": " , "},
    {"HEATSIGNAL_TICKER_INTERVAL_S": "rapido"},
    {"HEATSIGNAL_STORAGE": "disco"},
])
def test_config_rejects_invalid(env: dict[str, str]) -> None:
    with pytest.raises(ValueError):
        RunConfig.from_env(env)


def test_enabled_exchanges_skips_unavailable_okx(monkeypatch, caplog) -> None:
    monkeypatch.setattr(run_mod, "WORKER_FACTORIES", {"binance": object, "bybit": object})
    cfg = RunConfig.from_env({})
    with caplog.at_level(logging.WARNING):
        assert cfg.enabled_exchanges() == ["binance", "bybit"]
    assert "okx" in caplog.text


# ---- sinks ----------------------------------------------------------------------------------------


async def test_build_sinks_memory_mode_ignores_redis_and_db(caplog) -> None:
    cfg = RunConfig.from_env({
        "REDIS_URL": "redis://r:6379/0",
        "DATABASE_URL": "postgresql://x:y@h/db",
        "HEATSIGNAL_MEM_TRADES_S": "600",
    })
    with caplog.at_level(logging.WARNING):
        throttled, sinks = await build_sinks(cfg)
    assert len(sinks) == 1 and isinstance(sinks[0], MemoryWindowSink)
    assert sinks[0]._retention_ms["trade"] == 600_000
    assert "REDIS_URL configurado pero se IGNORA" in caplog.text
    assert "DATABASE_URL configurado pero se IGNORA" in caplog.text
    await throttled.close()


async def test_build_sinks_falls_back_to_console_without_redis_or_db(caplog) -> None:
    cfg = RunConfig.from_env({"HEATSIGNAL_STORAGE": "persistent"})
    with caplog.at_level(logging.WARNING):
        throttled, sinks = await build_sinks(cfg)
    assert isinstance(throttled, ThrottledSink)
    assert isinstance(throttled.inner, FanoutSink)
    assert len(sinks) == 1 and isinstance(sinks[0], ConsoleSink)
    assert "REDIS_URL no configurado" in caplog.text
    assert "DATABASE_URL no configurado" in caplog.text


async def test_build_sinks_unreachable_backends_do_not_crash() -> None:
    cfg = RunConfig.from_env({
        "HEATSIGNAL_STORAGE": "persistent",
        "REDIS_URL": "redis://127.0.0.1:1/0",
        "DATABASE_URL": "postgresql://x:y@127.0.0.1:1/db",
    })
    throttled, sinks = await build_sinks(cfg)
    assert len(sinks) == 1 and isinstance(sinks[0], ConsoleSink)
    await throttled.close()


async def test_run_wires_workers_and_shuts_down_cleanly(monkeypatch) -> None:
    created: list[FakeWorker] = []

    class FakeWorker:
        def __init__(self, symbols, sink) -> None:  # noqa: ANN001
            self.symbols, self.sink = symbols, sink
            self._stop = asyncio.Event()
            self.stopped = False
            created.append(self)

        async def run(self) -> None:
            logging.getLogger("heatsignal.ingestion.fake").info("conectando worker Fake (intento 1)")
            await self.sink.write(TickerEvent(
                source=Exchange.BYBIT, symbol="BTC-USDT", mark_price=1.0, ts_event_ms=1, ts_ingest_ms=1,
            ))
            await self._stop.wait()

        async def stop(self) -> None:
            self.stopped = True
            self._stop.set()

    monkeypatch.setattr(run_mod, "WORKER_FACTORIES", {"bybit": FakeWorker})
    cfg = RunConfig.from_env({"HEATSIGNAL_EXCHANGES": "bybit", "HEATSIGNAL_RUN_SECONDS": "0.2"})
    await run_mod.run(cfg)
    assert len(created) == 1 and created[0].stopped
    assert created[0].symbols == ["BTCUSDT", "SOLUSDT"]
    assert isinstance(created[0].sink, CountingSink)


# ---- health ---------------------------------------------------------------------------------------


class Clock:
    def __init__(self) -> None:
        self.t = 100.0

    def __call__(self) -> float:
        return self.t


async def test_health_tracks_events_staleness_and_uptime() -> None:
    clock = Clock()
    reg = HealthRegistry(stale_after_s=10.0, clock=clock)
    out = BufferedSink()
    sink = CountingSink(out, reg, "bybit")
    reg.sample()
    assert reg.workers["bybit"].connected is False

    await sink.write(TickerEvent(
        source=Exchange.BYBIT, symbol="BTC-USDT", ts_event_ms=1, ts_ingest_ms=1,
    ))
    clock.t += 5
    reg.sample()
    h = reg.workers["bybit"]
    assert h.connected and h.events_by_kind["ticker"] == 1 and len(out.buffer) == 1

    clock.t += 5
    reg.sample()  # 5s conectado de 10s muestreados
    clock.t += 20
    reg.sample()  # sin datos hace 25s -> caido
    assert h.connected is False
    assert h.uptime_ratio == pytest.approx(5 / 30)
    snap = reg.snapshot()[0]
    assert snap["worker"] == "bybit" and snap["events"] == {"ticker": 1}


def test_reconnect_handler_counts_connect_attempts() -> None:
    reg = HealthRegistry()
    reg.register("bybit")
    handler = ReconnectLogHandler(reg, {"bybit": "bybit"})
    logger = logging.getLogger("heatsignal.ingestion.test_reconnect")
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    try:
        for i in range(3):
            logger.info("conectando worker Bybit (intento %d)", i + 1)
        logger.info("conectando worker Binance (intento 1)")  # no registrado: se ignora
    finally:
        logger.removeHandler(handler)
    assert reg.workers["bybit"].reconnects == 2
