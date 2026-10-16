"""
Tests del worker de Bybit: normalizacion de los tres topics y merge de deltas de
`tickers`. No abren conexiones reales. Los payloads siguen los ejemplos de la doc
oficial de Bybit V5 (allLiquidation, publicTrade, tickers linear).
"""

from __future__ import annotations

import pytest

from heatsignal.ingestion.bybit_worker import (
    BybitWorker,
    _liquidation_order_side,
    _taker_side,
)
from heatsignal.schema.events import Coverage, Exchange, Side
from heatsignal.schema.sink import BufferedSink


def _worker(**overrides) -> BybitWorker:
    defaults = dict(symbols=["BTCUSDT"], sink=BufferedSink())
    defaults.update(overrides)
    return BybitWorker(**defaults)


# ---- lados ----------------------------------------------------------------------


def test_liquidation_side_is_inverted_from_position_side() -> None:
    # Doc Bybit: "Buy" = se liquido un LONG -> la orden de liquidacion es una VENTA.
    assert _liquidation_order_side("Buy") == Side.SELL
    assert _liquidation_order_side("Sell") == Side.BUY


def test_liquidation_side_rejects_unknown() -> None:
    with pytest.raises(ValueError):
        _liquidation_order_side("Hold")


def test_taker_side_is_not_inverted() -> None:
    assert _taker_side("Buy") == Side.BUY
    assert _taker_side("Sell") == Side.SELL


# ---- suscripcion ----------------------------------------------------------------


def test_topics_cover_three_channels_per_symbol() -> None:
    worker = _worker(symbols=["btcusdt", "SOLUSDT"])
    assert worker._topics() == [
        "allLiquidation.BTCUSDT", "publicTrade.BTCUSDT", "tickers.BTCUSDT",
        "allLiquidation.SOLUSDT", "publicTrade.SOLUSDT", "tickers.SOLUSDT",
    ]


# ---- allLiquidation -------------------------------------------------------------


def test_normalize_all_liquidation() -> None:
    events = _worker()._normalize(
        {
            "topic": "allLiquidation.ROSEUSDT",
            "type": "snapshot",
            "ts": 1739502303204,
            "data": [{"T": 1739502302929, "s": "ROSEUSDT", "S": "Sell", "v": "20000", "p": "0.04499"}],
        }
    )
    assert len(events) == 1
    ev = events[0]
    assert ev.kind == "liquidation"
    assert ev.source == Exchange.BYBIT
    assert ev.symbol == "ROSE-USDT"
    assert ev.side == Side.BUY  # short liquidado -> orden de compra
    assert ev.price == pytest.approx(0.04499)
    assert ev.qty == 20000.0
    assert ev.notional_usd == pytest.approx(899.8)
    assert ev.ts_event_ms == 1739502302929
    assert ev.coverage == Coverage.FULL


def test_normalize_all_liquidation_multiple_entries() -> None:
    events = _worker()._normalize(
        {
            "topic": "allLiquidation.BTCUSDT",
            "type": "snapshot",
            "ts": 1,
            "data": [
                {"T": 1, "s": "BTCUSDT", "S": "Buy", "v": "0.1", "p": "60000"},
                {"T": 2, "s": "BTCUSDT", "S": "Sell", "v": "0.2", "p": "60010"},
            ],
        }
    )
    assert [e.side for e in events] == [Side.SELL, Side.BUY]


# ---- publicTrade ----------------------------------------------------------------


def test_normalize_public_trade() -> None:
    events = _worker()._normalize(
        {
            "topic": "publicTrade.BTCUSDT",
            "type": "snapshot",
            "ts": 1672304486868,
            "data": [
                {
                    "T": 1672304486865, "s": "BTCUSDT", "S": "Buy", "v": "0.001",
                    "p": "16578.50", "L": "PlusTick", "i": "20f43950", "BT": False,
                    "seq": 1783284617,
                },
                {
                    "T": 1672304486866, "s": "BTCUSDT", "S": "Sell", "v": "5",
                    "p": "16578.40", "L": "MinusTick", "i": "20f43951", "BT": True,
                    "seq": 1783284618,
                },
            ],
        }
    )
    assert [e.kind for e in events] == ["trade", "trade"]
    assert events[0].side == Side.BUY
    assert events[0].price == 16578.5
    assert events[0].qty == 0.001
    assert events[0].ts_event_ms == 1672304486865
    assert events[0].is_block_trade is False
    assert events[1].side == Side.SELL
    assert events[1].is_block_trade is True


# ---- tickers (snapshot + delta) -------------------------------------------------


def _ticker_msg(type_: str, ts: int, **data) -> dict:
    return {"topic": "tickers.BTCUSDT", "type": type_, "ts": ts, "data": {"symbol": "BTCUSDT", **data}}


def test_ticker_snapshot() -> None:
    [ev] = _worker()._normalize(
        _ticker_msg(
            "snapshot", 100,
            markPrice="66666.60", lastPrice="66666.70", fundingRate="-0.005",
            openInterest="492373.72", openInterestValue="32824881841.75",
        )
    )
    assert ev.kind == "ticker"
    assert ev.symbol == "BTC-USDT"
    assert ev.mark_price == 66666.6
    assert ev.last_price == 66666.7
    assert ev.funding_rate == -0.005
    assert ev.open_interest == 492373.72
    assert ev.open_interest_usd == 32824881841.75
    assert ev.ts_event_ms == 100


def test_ticker_delta_keeps_missing_fields_from_snapshot() -> None:
    worker = _worker()
    worker._normalize(_ticker_msg("snapshot", 100, markPrice="100", fundingRate="0.0001", openInterest="5"))
    [ev] = worker._normalize(_ticker_msg("delta", 200, markPrice="101"))
    assert ev.mark_price == 101.0          # cambiado por el delta
    assert ev.funding_rate == 0.0001       # ausente en el delta -> sin cambios
    assert ev.open_interest == 5.0
    assert ev.ts_event_ms == 200


def test_ticker_new_snapshot_replaces_state() -> None:
    worker = _worker()
    worker._normalize(_ticker_msg("snapshot", 1, markPrice="100", fundingRate="0.0001"))
    [ev] = worker._normalize(_ticker_msg("snapshot", 2, markPrice="200"))
    assert ev.mark_price == 200.0
    assert ev.funding_rate is None  # un snapshot no hereda estado viejo


def test_ticker_delta_without_snapshot_leaves_unknowns_as_none() -> None:
    [ev] = _worker()._normalize(_ticker_msg("delta", 1, markPrice="100"))
    assert ev.mark_price == 100.0
    assert ev.funding_rate is None


# ---- control / errores ----------------------------------------------------------


def test_control_messages_produce_no_events() -> None:
    worker = _worker()
    assert worker._normalize({"success": True, "ret_msg": "pong", "conn_id": "x", "op": "ping"}) == []
    assert worker._normalize({"success": True, "ret_msg": "", "conn_id": "x", "op": "subscribe"}) == []


def test_rejected_subscription_is_logged(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level("ERROR", logger="heatsignal.ingestion.bybit"):
        out = _worker()._normalize(
            {"success": False, "ret_msg": "error:handler not found,topic:foo.BTCUSDT", "op": "subscribe"}
        )
    assert out == []
    assert "rechazo la suscripcion" in caplog.text


def test_malformed_payload_is_dropped_with_warning(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level("WARNING", logger="heatsignal.ingestion.bybit"):
        out = _worker()._normalize(
            {"topic": "publicTrade.BTCUSDT", "type": "snapshot", "ts": 1, "data": [{"s": "BTCUSDT"}]}
        )
    assert out == []
    assert "no se pudo normalizar" in caplog.text


# ---- ciclo de vida ----------------------------------------------------------------


async def test_consume_writes_events_and_returns_on_timeout() -> None:
    import json

    sink = BufferedSink()
    worker = _worker(sink=sink, message_timeout_s=0.05)
    frames = [
        json.dumps({"success": True, "op": "subscribe"}),
        json.dumps(
            {"topic": "allLiquidation.BTCUSDT", "type": "snapshot", "ts": 1,
             "data": [{"T": 1, "s": "BTCUSDT", "S": "Buy", "v": "1", "p": "10"}]}
        ),
        "not json",
    ]

    class FakeWS:
        async def recv(self):
            if frames:
                return frames.pop(0)
            import asyncio

            await asyncio.sleep(10)  # silencio -> el timeout del watchdog corta

    await worker._consume(FakeWS())
    assert [e.kind for e in sink.buffer] == ["liquidation"]


async def test_run_does_not_log_error_when_stopped(monkeypatch, caplog: pytest.LogCaptureFixture) -> None:
    worker = _worker()

    async def closed_by_stop() -> None:
        await worker.stop()
        raise RuntimeError("ConnectionClosedOK simulado")

    monkeypatch.setattr(worker, "_run_once", closed_by_stop)
    with caplog.at_level("ERROR", logger="heatsignal.ingestion.bybit"):
        await worker.run()
    assert "error en el worker" not in caplog.text
