"""Tests de ThrottledSink y FanoutSink (sin red)."""

from __future__ import annotations

import asyncio

import pytest

from heatsignal.schema.events import (
    Coverage,
    Exchange,
    LiquidationEvent,
    Side,
    TickerEvent,
    TradeEvent,
)
from heatsignal.schema.sink import BufferedSink
from heatsignal.storage.fanout import FanoutSink
from heatsignal.storage.throttle import ThrottledSink


def _ticker(price: float, *, symbol: str = "BTC-USDT", source: Exchange = Exchange.BYBIT) -> TickerEvent:
    return TickerEvent(source=source, symbol=symbol, mark_price=price, ts_event_ms=1, ts_ingest_ms=1)


def _trade(i: int) -> TradeEvent:
    return TradeEvent(
        source=Exchange.BYBIT, symbol="BTC-USDT", side=Side.BUY, price=float(i), qty=1.0,
        ts_event_ms=i, ts_ingest_ms=i,
    )


def _liq() -> LiquidationEvent:
    return LiquidationEvent(
        source=Exchange.BINANCE, symbol="BTC-USDT", side=Side.SELL, price=1.0, qty=1.0,
        notional_usd=1.0, ts_event_ms=1, ts_ingest_ms=1, coverage=Coverage.PARTIAL,
    )


class FakeClock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t


# ---- ThrottledSink --------------------------------------------------------------------------------


async def test_throttle_first_ticker_passes_immediately() -> None:
    out = BufferedSink()
    sink = ThrottledSink(out, {"ticker": 10.0})
    await sink.write(_ticker(1.0))
    assert [e.mark_price for e in out.buffer] == [1.0]


async def test_throttle_last_value_wins_and_is_flushed_at_interval_end() -> None:
    out = BufferedSink()
    sink = ThrottledSink(out, {"ticker": 0.05})
    for p in (1.0, 2.0, 3.0, 4.0):
        await sink.write(_ticker(p))
    assert [e.mark_price for e in out.buffer] == [1.0]
    await asyncio.sleep(0.12)
    # el ultimo valor (4.0) se emite al cerrar la ventana; 2.0 y 3.0 se submuestrean
    assert [e.mark_price for e in out.buffer] == [1.0, 4.0]
    assert sink.coalesced == 2


async def test_throttle_is_per_source_and_symbol() -> None:
    out = BufferedSink()
    sink = ThrottledSink(out, {"ticker": 10.0})
    await sink.write(_ticker(1.0, symbol="BTC-USDT"))
    await sink.write(_ticker(2.0, symbol="SOL-USDT"))
    await sink.write(_ticker(3.0, symbol="BTC-USDT", source=Exchange.BINANCE))
    assert len(out.buffer) == 3
    await sink.close()


async def test_throttle_passes_liquidations_and_trades_untouched() -> None:
    out = BufferedSink()
    sink = ThrottledSink(out, {"ticker": 10.0})
    events = [_liq(), _trade(1), _trade(2), _liq(), _trade(3)]
    for e in events:
        await sink.write(e)
    assert out.buffer == events


async def test_throttle_close_flushes_pending_final_value() -> None:
    out = BufferedSink()
    sink = ThrottledSink(out, {"ticker": 60.0})
    await sink.write(_ticker(1.0))
    await sink.write(_ticker(2.0))
    assert len(out.buffer) == 1
    await sink.close()
    assert [e.mark_price for e in out.buffer] == [1.0, 2.0]


async def test_throttle_new_window_after_interval_passes_immediately() -> None:
    clock = FakeClock()
    out = BufferedSink()
    sink = ThrottledSink(out, {"ticker": 1.0}, clock=clock)
    await sink.write(_ticker(1.0))
    clock.t = 1.5
    await sink.write(_ticker(2.0))
    assert [e.mark_price for e in out.buffer] == [1.0, 2.0]


async def test_throttle_inner_failure_does_not_raise() -> None:
    class Boom:
        async def write(self, event):  # noqa: ANN001
            raise RuntimeError("boom")

    sink = ThrottledSink(Boom(), {"ticker": 1.0})
    await sink.write(_ticker(1.0))  # no lanza
    await sink.write(_trade(1))


# ---- FanoutSink -----------------------------------------------------------------------------------


class FailingSink:
    def __init__(self) -> None:
        self.closed = False

    async def write(self, event) -> None:  # noqa: ANN001
        raise ConnectionError("caido")

    async def close(self) -> None:
        self.closed = True


async def test_fanout_writes_to_all_sinks() -> None:
    a, b = BufferedSink(), BufferedSink()
    sink = FanoutSink(a, b)
    await sink.write(_liq())
    assert len(a.buffer) == len(b.buffer) == 1


async def test_fanout_isolates_failures_and_counts_them() -> None:
    bad, good = FailingSink(), BufferedSink()
    sink = FanoutSink(bad, good)
    await sink.write(_liq())
    await sink.write(_trade(1))
    assert len(good.buffer) == 2
    assert sink.error_counts() == {"0:FailingSink": 2, "1:BufferedSink": 0}


async def test_fanout_single_sink_failure_is_isolated() -> None:
    sink = FanoutSink(FailingSink())
    await sink.write(_liq())
    assert sink.errors[0] == 1


async def test_fanout_close_closes_sinks_with_close_and_isolates_errors() -> None:
    class BadClose:
        async def write(self, event) -> None:  # noqa: ANN001
            pass

        async def close(self) -> None:
            raise RuntimeError("no cierra")

    failing = FailingSink()
    sink = FanoutSink(BadClose(), failing, BufferedSink())
    await sink.close()
    assert failing.closed


def test_fanout_requires_sinks() -> None:
    with pytest.raises(ValueError):
        FanoutSink()
