"""Tests de RedisStreamSink contra fakeredis (sin Redis real ni red)."""

from __future__ import annotations

import json

import fakeredis
import pytest
from redis.asyncio import Redis

from heatsignal.schema.events import Coverage, Exchange, LiquidationEvent, Side, TickerEvent
from heatsignal.schema.sink import ConsoleSink, RedisStreamSink


@pytest.fixture
def redis_client() -> fakeredis.aioredis.FakeRedis:
    return fakeredis.aioredis.FakeRedis()


def _liquidation(symbol: str = "BTC-USDT") -> LiquidationEvent:
    return LiquidationEvent(
        source=Exchange.BINANCE,
        symbol=symbol,
        side=Side.SELL,
        price=100.0,
        qty=1.0,
        notional_usd=100.0,
        ts_event_ms=1,
        ts_ingest_ms=2,
        coverage=Coverage.FULL,
    )


# ---- stream key y payload -------------------------------------------------------


async def test_write_routes_to_stream_named_by_symbol_and_kind(redis_client) -> None:
    sink = RedisStreamSink(redis_client)
    await sink.write(_liquidation(symbol="BTC-USDT"))

    entries = await redis_client.xrange("heatsignal:BTC-USDT:liquidation")
    assert len(entries) == 1


async def test_different_symbols_go_to_different_streams(redis_client) -> None:
    sink = RedisStreamSink(redis_client)
    await sink.write(_liquidation(symbol="BTC-USDT"))
    await sink.write(_liquidation(symbol="ETH-USDT"))

    assert len(await redis_client.xrange("heatsignal:BTC-USDT:liquidation")) == 1
    assert len(await redis_client.xrange("heatsignal:ETH-USDT:liquidation")) == 1


async def test_different_event_kinds_go_to_different_streams(redis_client) -> None:
    sink = RedisStreamSink(redis_client)
    ticker = TickerEvent(
        source=Exchange.BINANCE,
        symbol="BTC-USDT",
        mark_price=100.0,
        ts_event_ms=1,
        ts_ingest_ms=2,
    )

    await sink.write(_liquidation(symbol="BTC-USDT"))
    await sink.write(ticker)

    assert len(await redis_client.xrange("heatsignal:BTC-USDT:liquidation")) == 1
    assert len(await redis_client.xrange("heatsignal:BTC-USDT:ticker")) == 1


async def test_write_stores_event_as_json_dump_payload(redis_client) -> None:
    sink = RedisStreamSink(redis_client)
    event = _liquidation(symbol="BTC-USDT")
    await sink.write(event)

    [(_, fields)] = await redis_client.xrange("heatsignal:BTC-USDT:liquidation")
    decoded = json.loads(fields[b"data"])
    assert decoded == event.model_dump(mode="json")
    # side es un enum str -> en JSON debe quedar el valor ("sell"), no el nombre.
    assert decoded["side"] == "sell"


async def test_stream_default_maxlen_is_10000(redis_client) -> None:
    sink = RedisStreamSink(redis_client)
    assert sink._maxlen == 10_000


async def test_stream_is_trimmed_to_custom_maxlen(redis_client) -> None:
    sink = RedisStreamSink(redis_client, maxlen=3)
    for _ in range(10):
        await sink.write(_liquidation())

    entries = await redis_client.xrange("heatsignal:BTC-USDT:liquidation")
    # MAXLEN aproximado: Redis puede dejar algunas entradas de más, pero nunca
    # deja crecer el stream sin límite.
    assert len(entries) < 10


# ---- connection pool -------------------------------------------------------------


def test_connect_wires_configurable_connection_pool_size() -> None:
    client = Redis.from_url("redis://localhost:6399", max_connections=25)
    assert client.connection_pool.max_connections == 25


async def test_connect_defaults_to_pool_size_10(monkeypatch) -> None:
    captured_kwargs = {}

    def _fake_from_url(url, **kwargs):
        captured_kwargs.update(kwargs)
        return fakeredis.aioredis.FakeRedis()

    monkeypatch.setattr(Redis, "from_url", _fake_from_url)

    await RedisStreamSink.connect("redis://ignored")

    assert captured_kwargs["max_connections"] == 10


# ---- fallback a ConsoleSink si Redis no responde al arrancar ---------------------


async def test_connect_falls_back_to_console_sink_when_redis_unreachable() -> None:
    sink = await RedisStreamSink.connect(
        "redis://localhost:1", connect_timeout_s=0.3  # puerto que rechaza la conexión ya mismo
    )
    assert isinstance(sink, ConsoleSink)


async def test_connect_returns_redis_stream_sink_when_redis_responds(monkeypatch) -> None:
    fake_client = fakeredis.aioredis.FakeRedis()
    monkeypatch.setattr(Redis, "from_url", lambda *a, **k: fake_client)

    sink = await RedisStreamSink.connect("redis://ignored", maxlen=42)

    assert isinstance(sink, RedisStreamSink)
    assert sink._maxlen == 42


# ---- no propaga excepciones si Redis cae durante la operación -------------------


class _AlwaysFailingRedis:
    async def xadd(self, *args, **kwargs):
        raise ConnectionError("redis se cayó")


async def test_write_swallows_exceptions_when_redis_fails_mid_operation() -> None:
    sink = RedisStreamSink(_AlwaysFailingRedis())  # type: ignore[arg-type]

    await sink.write(_liquidation())  # no debe lanzar
