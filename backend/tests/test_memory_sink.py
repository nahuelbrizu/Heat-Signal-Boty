"""Tests de MemoryWindowSink: retencion por tipo, tope duro y huecos declarados."""

from __future__ import annotations

import logging

import pytest

from heatsignal.schema.events import Coverage, Exchange, LiquidationEvent, Side, TickerEvent, TradeEvent
from heatsignal.storage.memory import MemoryWindowSink


class Clock:
    def __init__(self, ms: int = 1_000_000) -> None:
        self.ms = ms

    def __call__(self) -> int:
        return self.ms


def _trade(ts: int, symbol: str = "BTC-USDT") -> TradeEvent:
    return TradeEvent(
        source=Exchange.BINANCE, symbol=symbol, side=Side.BUY, price=100.0, qty=1.0,
        ts_event_ms=ts, ts_ingest_ms=ts,
    )


def _liq(ts: int) -> LiquidationEvent:
    return LiquidationEvent(
        source=Exchange.BYBIT, symbol="BTC-USDT", side=Side.SELL, price=100.0, qty=1.0,
        notional_usd=100.0, ts_event_ms=ts, ts_ingest_ms=ts, coverage=Coverage.FULL,
    )


def _ticker(ts: int) -> TickerEvent:
    return TickerEvent(source=Exchange.OKX, symbol="BTC-USDT", ts_event_ms=ts, ts_ingest_ms=ts, mark_price=100.0)


async def test_events_are_split_by_kind_and_symbol() -> None:
    clock = Clock()
    sink = MemoryWindowSink(clock_ms=clock)
    await sink.write(_trade(clock.ms))
    await sink.write(_trade(clock.ms, symbol="SOL-USDT"))
    await sink.write(_liq(clock.ms))
    await sink.write(_ticker(clock.ms))
    assert len(sink.trades("BTC-USDT")) == 1
    assert len(sink.trades("SOL-USDT")) == 1
    assert len(sink.liquidations("BTC-USDT")) == 1
    assert len(sink.tickers("BTC-USDT")) == 1
    assert sink.trades("ETH-USDT") == []
    assert sink.stats.as_dict()["in_memory"] == {"liquidation": 1, "trade": 2, "ticker": 1}


async def test_retention_is_per_kind() -> None:
    clock = Clock()
    sink = MemoryWindowSink(retention_s={"trade": 60, "liquidation": 3600}, clock_ms=clock)
    await sink.write(_trade(clock.ms))
    await sink.write(_liq(clock.ms))
    clock.ms += 61_000
    assert sink.trades("BTC-USDT") == []  # vencio
    assert len(sink.liquidations("BTC-USDT")) == 1  # todavia dentro de su retencion
    assert sink.stats.expired == 1
    assert sink.stats.by_kind["trade"] == 0


async def test_cap_evicts_oldest_and_declares_gap(caplog: pytest.LogCaptureFixture) -> None:
    clock = Clock()
    sink = MemoryWindowSink(max_events={"trade": 3}, clock_ms=clock)
    clock.ms += 3_600_000  # pasado el calentamiento para aislar el hueco por tope
    with caplog.at_level(logging.WARNING, logger="heatsignal.storage.memory"):
        for i in range(5):
            await sink.write(_trade(clock.ms + i))
    clock.ms += 10
    assert [t.ts_event_ms for t in sink.trades("BTC-USDT")] == [clock.ms - 8, clock.ms - 7, clock.ms - 6]
    assert sink.stats.evicted_by_cap == 2
    assert "tope de 3 trade" in caplog.text
    gaps = sink.data_gaps("BTC-USDT", window_s=600)
    assert any("trade incompletos por tope" in g for g in gaps)
    assert sink.data_gaps("SOL-USDT", window_s=600) == []


async def test_warmup_gap_after_restart() -> None:
    clock = Clock()
    sink = MemoryWindowSink(clock_ms=clock)
    clock.ms += 120_000
    gaps = sink.data_gaps("BTC-USDT", window_s=900)
    assert gaps == ["memoria: calentamiento tras reinicio, 120s acumulados de 900s pedidos"]
    clock.ms += 900_000
    assert sink.data_gaps("BTC-USDT", window_s=900) == []


async def test_window_longer_than_retention_is_declared() -> None:
    clock = Clock()
    sink = MemoryWindowSink(retention_s={"trade": 60}, clock_ms=clock)
    clock.ms += 10_000_000
    assert sink.data_gaps("BTC-USDT", window_s=300) == [
        "memoria: trade se retienen 60s (< ventana 300s)"
    ]


async def test_close_discards_everything() -> None:
    clock = Clock()
    sink = MemoryWindowSink(clock_ms=clock)
    await sink.write(_trade(clock.ms))
    await sink.close()
    assert sink.trades("BTC-USDT") == []


def test_invalid_config_rejected() -> None:
    with pytest.raises(ValueError):
        MemoryWindowSink(retention_s={"trade": 0})


async def test_window_keeps_compact_read_only_rows_with_same_fields() -> None:
    """La ventana guarda filas compactas: mismos atributos y valores que el evento pydantic, solo lectura."""
    clock = Clock()
    sink = MemoryWindowSink(clock_ms=clock)
    events = [_trade(clock.ms), _liq(clock.ms), _ticker(clock.ms)]
    for ev in events:
        await sink.write(ev)
    rows = [sink.trades("BTC-USDT")[0], sink.liquidations("BTC-USDT")[0], sink.tickers("BTC-USDT")[0]]
    for ev, row in zip(events, rows):
        assert row.as_dict() == ev.model_dump()  # mismos campos, mismos valores (enums incluidos)
        assert row.kind == ev.kind and row.source is ev.source
        with pytest.raises(AttributeError):
            row.price = 1.0  # type: ignore[misc]


async def test_window_ram_per_event_is_bounded() -> None:
    """Regresion del 19/10: ~1.2 KB por trade pydantic llevaba la ventana a >600 MB y el servicio a swap."""
    import tracemalloc

    clock = Clock()
    sink = MemoryWindowSink(clock_ms=clock)
    n = 20_000
    tracemalloc.start()
    try:
        before = tracemalloc.get_traced_memory()[0]
        for i in range(n):
            await sink.write(TradeEvent(source=Exchange.BINANCE, symbol="BTC-" + "USDT", side=Side.BUY,
                                        price=60_000.0 + i, qty=0.1 + i, ts_event_ms=clock.ms + i,
                                        ts_ingest_ms=clock.ms))
        per_event = (tracemalloc.get_traced_memory()[0] - before) / n
    finally:
        tracemalloc.stop()
    assert per_event < 400, per_event
