"""Tests del worker de Hyperliquid con mensajes reales capturados el 2026-10-17 (sin red)."""

from __future__ import annotations

import pytest

from heatsignal.ingestion.hyperliquid_worker import FUNDING_TO_8H, HyperliquidWorker, coin_for
from heatsignal.schema.events import Exchange, Side, TickerEvent, TradeEvent
from heatsignal.schema.sink import BufferedSink

TRADES = {"channel": "trades", "data": [
    {"coin": "XRP", "side": "A", "px": "1.3048", "sz": "341.0", "time": 1792206697907,
     "hash": "0x44d7", "tid": 1056468234128643, "users": ["0xa", "0xb"]},
    {"coin": "XRP", "side": "B", "px": "1.3049", "sz": "12.0", "time": 1792206711543,
     "hash": "0xadb4", "tid": 895197983025766, "users": ["0xc", "0xd"]},
]}
CTX = {"channel": "activeAssetCtx", "data": {"coin": "XRP", "ctx": {
    "funding": "-0.0000025545", "openInterest": "177904022.0", "prevDayPx": "1.3385",
    "dayNtlVlm": "60047027.9230000004", "premium": "-0.0004912852", "oraclePx": "1.304741",
    "markPx": "1.304", "midPx": "1.30405", "impactPxs": ["1.304", "1.3041"], "dayBaseVlm": "45769470.0"}}}


def _worker() -> HyperliquidWorker:
    return HyperliquidWorker(symbols=["XRPUSDT", "BTC-USDT"], sink=BufferedSink())


def test_coin_mapping() -> None:
    assert coin_for("XRPUSDT") == coin_for("XRP-USDT") == "XRP"
    assert coin_for("BTCUSDC") == "BTC"


def test_subscriptions_two_per_coin() -> None:
    msgs = _worker()._subscribe_messages()
    assert len(msgs) == 4
    assert '{"method": "subscribe", "subscription": {"type": "trades", "coin": "XRP"}}' in msgs


def test_trades_use_aggressor_side_and_internal_symbol() -> None:
    events = _worker()._normalize(TRADES)
    assert all(isinstance(e, TradeEvent) for e in events)
    a, b = events
    assert (a.side, a.price, a.qty, a.ts_event_ms) == (Side.SELL, 1.3048, 341.0, 1792206697907)
    assert b.side is Side.BUY
    assert a.symbol == "XRP-USDT" and a.source is Exchange.HYPERLIQUID


def test_trades_dedup_across_resubscribe_snapshot() -> None:
    w = _worker()
    assert len(w._normalize(TRADES)) == 2
    assert w._normalize(TRADES) == []  # el lote inicial al reconectar repite trades ya vistos


def test_ticker_normalizes_funding_to_8h_and_oi_to_usd() -> None:
    (ev,) = _worker()._normalize(CTX)
    assert isinstance(ev, TickerEvent)
    assert ev.mark_price == 1.304 and ev.last_price == 1.30405
    assert ev.funding_rate == pytest.approx(-0.0000025545 * FUNDING_TO_8H)
    assert ev.open_interest == 177904022.0
    assert ev.open_interest_usd == pytest.approx(177904022.0 * 1.304)


def test_control_messages_unknown_coins_and_bad_payloads(caplog) -> None:
    w = _worker()
    assert w._normalize({"channel": "subscriptionResponse", "data": {}}) == []
    assert w._normalize({"channel": "pong"}) == []
    assert w._normalize({"channel": "error", "data": "Invalid subscription"}) == []
    assert "Invalid subscription" in caplog.text
    other = {"channel": "trades", "data": [{**TRADES["data"][0], "coin": "DOGE"}]}
    assert w._normalize(other) == []
    assert w._normalize({"channel": "trades", "data": [{"coin": "XRP", "side": "X", "px": "1", "sz": "1",
                                                        "time": 1, "tid": 2}]}) == []
