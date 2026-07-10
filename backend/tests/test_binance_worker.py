"""
Tests del worker de Binance: normalización de eventos y watchdog de socket
zombie. No abren conexiones reales — `BinanceWorker` se instancia con un sink
en memoria y nunca se llama a `run()` contra la red.
"""

from __future__ import annotations

import asyncio
import time

import pytest

from heatsignal.ingestion.binance_worker import BinanceWorker, _norm_side, _norm_symbol
from heatsignal.schema.events import Coverage, Side
from heatsignal.schema.sink import BufferedSink


def _worker(**overrides) -> BinanceWorker:
    defaults = dict(symbols=["BTCUSDT"], sink=BufferedSink())
    defaults.update(overrides)
    return BinanceWorker(**defaults)


# ---- _norm_symbol / _norm_side --------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("BTCUSDT", "BTC-USDT"),
        ("solusdt", "SOL-USDT"),
        ("ETHUSDC", "ETH-USDC"),
        ("XRPUSD", "XRP-USD"),
        ("WEIRDPAIR", "WEIRDPAIR"),  # sin quote conocido: heurística no rompe, no adivina
    ],
)
def test_norm_symbol(raw: str, expected: str) -> None:
    assert _norm_symbol(raw) == expected


def test_norm_side() -> None:
    assert _norm_side("BUY") == Side.BUY
    assert _norm_side("SELL") == Side.SELL
    assert _norm_side("sell") == Side.SELL


# ---- _normalize -------------------------------------------------------------------


def test_normalize_force_order_is_full_coverage() -> None:
    worker = _worker()
    event = worker._normalize(
        {
            "e": "forceOrder",
            "E": 1000,
            "o": {"s": "BTCUSDT", "S": "SELL", "ap": "50000", "p": "50000", "q": "0.5"},
        }
    )
    assert event.kind == "liquidation"
    assert event.symbol == "BTC-USDT"
    assert event.side == Side.SELL
    assert event.price == 50000.0
    assert event.notional_usd == 25000.0
    assert event.coverage == Coverage.FULL


def test_normalize_agg_trade_maker_flag_flips_taker_side() -> None:
    worker = _worker()
    # "m": True -> el comprador es el market maker -> el taker (agresor) vendió.
    event = worker._normalize({"e": "aggTrade", "E": 1000, "s": "BTCUSDT", "p": "100", "q": "1", "m": True})
    assert event.side == Side.SELL

    event = worker._normalize({"e": "aggTrade", "E": 1000, "s": "BTCUSDT", "p": "100", "q": "1", "m": False})
    assert event.side == Side.BUY


def test_normalize_mark_price_update() -> None:
    worker = _worker()
    event = worker._normalize({"e": "markPriceUpdate", "E": 1000, "s": "BTCUSDT", "p": "50000", "r": "0.0001"})
    assert event.kind == "ticker"
    assert event.mark_price == 50000.0
    assert event.funding_rate == 0.0001


def test_normalize_unknown_event_type_returns_none() -> None:
    worker = _worker()
    assert worker._normalize({"e": "somethingElse"}) is None


def test_normalize_malformed_payload_returns_none_and_does_not_raise() -> None:
    worker = _worker()
    assert worker._normalize({"e": "forceOrder", "o": {"s": "BTCUSDT"}}) is None  # faltan campos


def test_on_message_never_raises_on_garbage_input() -> None:
    worker = _worker()
    worker._loop = asyncio.new_event_loop()
    try:
        worker._on_message(None, "not json at all {{{")
    finally:
        worker._loop.close()


def test_on_message_updates_last_message_at_even_for_non_json() -> None:
    worker = _worker()
    worker._loop = None  # sin loop: no debe explotar igual
    before = worker._last_message_at
    worker._on_message(None, "garbage")
    assert worker._last_message_at > before


# ---- watchdog de socket zombie -----------------------------------------------------


def test_is_connection_stale_false_when_recent() -> None:
    worker = _worker(message_timeout_s=60.0)
    worker._last_message_at = time.monotonic()
    assert worker._is_connection_stale() is False


def test_is_connection_stale_true_when_old() -> None:
    worker = _worker(message_timeout_s=60.0)
    worker._last_message_at = time.monotonic() - 100.0
    assert worker._is_connection_stale() is True


async def test_watchdog_marks_connection_dead_after_timeout() -> None:
    worker = _worker(message_timeout_s=0.05, watchdog_poll_s=0.01)
    worker._last_message_at = time.monotonic()

    await asyncio.wait_for(worker._watchdog(), timeout=1.0)

    assert worker._connection_dead.is_set()


async def test_watchdog_does_not_fire_while_messages_keep_arriving() -> None:
    worker = _worker(message_timeout_s=0.05, watchdog_poll_s=0.01)
    worker._last_message_at = time.monotonic()

    async def _keep_alive() -> None:
        for _ in range(10):
            await asyncio.sleep(0.01)
            worker._last_message_at = time.monotonic()

    watchdog_task = asyncio.create_task(worker._watchdog())
    await _keep_alive()
    worker._stop.set()  # corta el watchdog si sigue corriendo
    await asyncio.wait_for(watchdog_task, timeout=1.0)

    assert not worker._connection_dead.is_set()
