"""
Tests del worker de OKX: simbolos, lado de liquidacion, conversion contratos -> activo base,
merge de los 4 canales de ticker y heartbeat. No abren conexiones reales. Los payloads siguen
los ejemplos de la doc oficial de OKX V5 (liquidation-orders, trades, tickers, mark-price,
funding-rate, open-interest) y muestras reales del WS (oct 2026).
"""

from __future__ import annotations

import asyncio
import json

import pytest

from heatsignal.ingestion.okx_worker import (
    OkxContractError,
    OkxWorker,
    _internal_symbol,
    _order_side,
    _to_inst_id,
    contract_values_from_instruments,
)
from heatsignal.schema.events import Coverage, Exchange, Side
from heatsignal.schema.sink import BufferedSink

# ctVal reales de GET /api/v5/public/instruments?instType=SWAP (oct 2026).
CT = {"BTC-USDT-SWAP": 0.01, "SOL-USDT-SWAP": 1.0, "IOST-USDT-SWAP": 10.0}


def _worker(**overrides) -> OkxWorker:
    defaults = dict(symbols=["BTC-USDT-SWAP"], sink=BufferedSink(), contract_values=CT)
    defaults.update(overrides)
    return OkxWorker(**defaults)


# ---- simbolos -------------------------------------------------------------------


@pytest.mark.parametrize("raw", ["BTC-USDT-SWAP", "btc-usdt-swap", "BTC-USDT", "BTCUSDT", "btcusdt"])
def test_symbol_forms_map_to_swap_inst_id(raw: str) -> None:
    assert _to_inst_id(raw) == "BTC-USDT-SWAP"


def test_internal_symbol_strips_swap_suffix() -> None:
    assert _internal_symbol("BTC-USDT-SWAP") == "BTC-USDT"


def test_unrecognized_symbol_raises() -> None:
    with pytest.raises(ValueError):
        _to_inst_id("BTC-USDT-250627-X")


def test_constructor_dedupes_symbol_forms() -> None:
    worker = _worker(symbols=["BTCUSDT", "BTC-USDT-SWAP", "sol-usdt"])
    assert worker._inst_ids == ["BTC-USDT-SWAP", "SOL-USDT-SWAP"]


# ---- suscripcion ----------------------------------------------------------------


def test_args_liquidations_per_inst_type_and_five_channels_per_symbol() -> None:
    args = _worker(symbols=["BTCUSDT", "SOLUSDT"])._args()
    assert args[0] == {"channel": "liquidation-orders", "instType": "SWAP"}
    assert args[1:6] == [
        {"channel": ch, "instId": "BTC-USDT-SWAP"}
        for ch in ("trades", "tickers", "mark-price", "funding-rate", "open-interest")
    ]
    assert len(args) == 11
    assert json.loads(_worker()._subscribe_message())["op"] == "subscribe"


# ---- lados ----------------------------------------------------------------------


def test_order_side_is_not_inverted() -> None:
    assert _order_side("buy") == Side.BUY
    assert _order_side("sell") == Side.SELL
    with pytest.raises(ValueError):
        _order_side("long")


# ---- contract value -------------------------------------------------------------


def _inst(inst_id: str, ct_val: str, ct_type: str = "linear", ccy: str | None = None) -> dict:
    return {
        "instId": inst_id, "instType": "SWAP", "ctType": ct_type, "ctMult": "1",
        "ctVal": ct_val, "ctValCcy": ccy or inst_id.split("-")[0],
    }


def test_contract_values_from_instruments() -> None:
    instruments = [_inst("BTC-USDT-SWAP", "0.01"), _inst("SOL-USDT-SWAP", "1"), _inst("ETH-USDT-SWAP", "0.1")]
    assert contract_values_from_instruments(instruments, ["BTC-USDT-SWAP", "SOL-USDT-SWAP"]) == {
        "BTC-USDT-SWAP": 0.01, "SOL-USDT-SWAP": 1.0,
    }


def test_contract_values_missing_symbol_fails_loudly() -> None:
    with pytest.raises(OkxContractError, match="FOO-USDT-SWAP"):
        contract_values_from_instruments([_inst("BTC-USDT-SWAP", "0.01")], ["BTC-USDT-SWAP", "FOO-USDT-SWAP"])


def test_contract_values_reject_inverse_and_bad_values() -> None:
    with pytest.raises(OkxContractError, match="inverse"):
        contract_values_from_instruments([_inst("BTC-USD-SWAP", "100", "inverse", "USD")], ["BTC-USD-SWAP"])
    with pytest.raises(OkxContractError):
        contract_values_from_instruments([_inst("BTC-USDT-SWAP", "")], ["BTC-USDT-SWAP"])


def test_injected_contract_values_must_cover_all_symbols() -> None:
    with pytest.raises(OkxContractError):
        OkxWorker(symbols=["BTCUSDT", "ETHUSDT"], sink=BufferedSink(), contract_values={"BTC-USDT-SWAP": 0.01})


async def test_load_contract_values_uses_injected_loader() -> None:
    async def loader() -> list[dict]:
        return [_inst("BTC-USDT-SWAP", "0.01")]

    worker = OkxWorker(symbols=["BTCUSDT"], sink=BufferedSink(), instruments_loader=loader)
    await worker._load_contract_values()
    assert worker._ct_val == {"BTC-USDT-SWAP": 0.01}


async def test_load_contract_values_keeps_cache_if_refresh_fails() -> None:
    calls = 0

    async def loader() -> list[dict]:
        nonlocal calls
        calls += 1
        if calls > 1:
            raise OSError("red caida")
        return [_inst("BTC-USDT-SWAP", "0.01")]

    worker = OkxWorker(symbols=["BTCUSDT"], sink=BufferedSink(), instruments_loader=loader)
    await worker._load_contract_values()
    await worker._load_contract_values()  # falla el refresh -> se queda con el cache
    assert worker._ct_val == {"BTC-USDT-SWAP": 0.01}


async def test_run_stops_on_unknown_contract() -> None:
    async def loader() -> list[dict]:
        return [_inst("BTC-USDT-SWAP", "0.01")]

    worker = OkxWorker(symbols=["BTCUSDT", "FOOUSDT"], sink=BufferedSink(), instruments_loader=loader)
    with pytest.raises(OkxContractError):
        await asyncio.wait_for(worker.run(), timeout=2)


def test_normalize_without_ct_val_raises_instead_of_guessing() -> None:
    worker = OkxWorker(symbols=["BTCUSDT"], sink=BufferedSink(), instruments_loader=None)
    with pytest.raises(OkxContractError):
        worker._normalize(
            {"arg": {"channel": "trades", "instId": "BTC-USDT-SWAP"},
             "data": [{"instId": "BTC-USDT-SWAP", "px": "1", "sz": "1", "side": "buy", "ts": "1"}]}
        )


# ---- liquidation-orders ---------------------------------------------------------

# Push de ejemplo de la doc oficial (liquidation-orders, instType SWAP).
DOC_LIQUIDATION = {
    "arg": {"channel": "liquidation-orders", "instType": "SWAP"},
    "data": [
        {
            "details": [
                {"bkLoss": "0", "bkPx": "0.007831", "ccy": "", "posSide": "short",
                 "side": "buy", "sz": "13", "ts": "1692266434010"}
            ],
            "instFamily": "IOST-USDT", "instId": "IOST-USDT-SWAP", "instType": "SWAP", "uly": "IOST-USDT",
        }
    ],
}


def test_liquidation_doc_payload_side_is_order_side() -> None:
    # Doc: `side` = "Order side"; posSide=short + side=buy -> se liquido un SHORT con una orden
    # de COMPRA. El esquema guarda el lado de la orden -> BUY (sin invertir, a diferencia de Bybit).
    [ev] = _worker(symbols=["IOST-USDT-SWAP"])._normalize(DOC_LIQUIDATION)
    assert ev.kind == "liquidation"
    assert ev.source == Exchange.OKX
    assert ev.symbol == "IOST-USDT"
    assert ev.side == Side.BUY
    assert ev.price == pytest.approx(0.007831)
    assert ev.qty == pytest.approx(130.0)  # 13 contratos x ctVal 10 IOST
    assert ev.notional_usd == pytest.approx(130.0 * 0.007831)
    assert ev.ts_event_ms == 1692266434010
    assert ev.coverage == Coverage.PARTIAL


def test_liquidation_live_sample_long_liquidated_is_sell() -> None:
    # Muestra real (oct 2026): posSide=long, side=sell -> long liquidado, orden de VENTA.
    msg = {
        "arg": {"channel": "liquidation-orders", "instType": "SWAP"},
        "data": [{"details": [{"bkLoss": "0", "bkPx": "81000.5", "ccy": "", "posSide": "long",
                               "side": "sell", "sz": "2.5", "ts": "1792177462117"}],
                  "instFamily": "BTC-USDT", "instId": "BTC-USDT-SWAP", "instType": "SWAP", "uly": "BTC-USDT"}],
    }
    [ev] = _worker()._normalize(msg)
    assert ev.side == Side.SELL
    assert ev.qty == pytest.approx(0.025)  # 2.5 contratos x 0.01 BTC
    assert ev.notional_usd == pytest.approx(0.025 * 81000.5)


def test_liquidation_net_mode_uses_side() -> None:
    msg = {"arg": {"channel": "liquidation-orders", "instType": "SWAP"},
           "data": [{"instId": "BTC-USDT-SWAP", "details": [
               {"bkPx": "1", "posSide": "net", "side": "sell", "sz": "1", "ts": "1"},
               {"bkPx": "1", "posSide": "net", "side": "buy", "sz": "1", "ts": "2"}]}]}
    assert [e.side for e in _worker()._normalize(msg)] == [Side.SELL, Side.BUY]


def test_liquidations_filtered_to_configured_symbols() -> None:
    # El canal es SWAP-wide: IOST no esta configurado en este worker -> se descarta.
    assert _worker()._normalize(DOC_LIQUIDATION) == []


# ---- trades ---------------------------------------------------------------------


def test_normalize_trades_converts_contracts_to_base() -> None:
    msg = {
        "arg": {"channel": "trades", "instId": "BTC-USDT-SWAP"},
        "data": [
            {"instId": "BTC-USDT-SWAP", "tradeId": "3001555328", "px": "81086", "sz": "100",
             "side": "sell", "ts": "1792177530874", "count": "9", "source": "0", "seqId": 346665408505},
            {"instId": "BTC-USDT-SWAP", "tradeId": "130639474", "px": "42219.9", "sz": "3",
             "side": "buy", "ts": "1630048897897", "count": "1", "source": "0", "seqId": 1234},
        ],
    }
    events = _worker()._normalize(msg)
    assert [e.kind for e in events] == ["trade", "trade"]
    assert events[0].symbol == "BTC-USDT"
    assert events[0].side == Side.SELL
    assert events[0].price == 81086.0
    assert events[0].qty == pytest.approx(1.0)  # 100 contratos x 0.01 BTC
    assert events[0].ts_event_ms == 1792177530874
    assert events[0].is_block_trade is False
    assert events[1].side == Side.BUY
    assert events[1].qty == pytest.approx(0.03)


# ---- tickers (merge de 4 canales) -----------------------------------------------


def _push(channel: str, **data) -> dict:
    return {"arg": {"channel": channel, "instId": "BTC-USDT-SWAP"},
            "data": [{"instId": "BTC-USDT-SWAP", "instType": "SWAP", **data}]}


def test_ticker_merges_channels() -> None:
    worker = _worker()
    [ev1] = worker._normalize(_push("mark-price", markPx="81066.3", ts="100"))
    assert ev1.kind == "ticker"
    assert ev1.symbol == "BTC-USDT"
    assert ev1.mark_price == 81066.3
    assert ev1.funding_rate is None and ev1.last_price is None

    worker._normalize(_push("tickers", last="81066.8", lastSz="0.03", askPx="81066.8", ts="200"))
    worker._normalize(_push("funding-rate", fundingRate="0.0001", nextFundingRate="", ts="300"))
    [ev] = worker._normalize(
        _push("open-interest", oi="3089425.71", oiCcy="30894.2571", oiUsd="2505289454.45", ts="400")
    )
    assert ev.mark_price == 81066.3
    assert ev.last_price == 81066.8
    assert ev.funding_rate == 0.0001
    assert ev.open_interest == 30894.2571  # oiCcy (activo base), no `oi` (contratos)
    assert ev.open_interest_usd == 2505289454.45
    assert ev.ts_event_ms == 400


def test_ticker_cache_is_per_symbol() -> None:
    worker = _worker(symbols=["BTCUSDT", "SOLUSDT"])
    worker._normalize(_push("mark-price", markPx="100", ts="1"))
    sol = {"arg": {"channel": "mark-price", "instId": "SOL-USDT-SWAP"},
           "data": [{"instId": "SOL-USDT-SWAP", "markPx": "150", "ts": "2"}]}
    [ev] = worker._normalize(sol)
    assert ev.symbol == "SOL-USDT"
    assert ev.mark_price == 150.0
    assert worker._tickers["BTC-USDT-SWAP"]["mark_price"] == 100.0


# ---- control / errores ----------------------------------------------------------


def test_event_messages_produce_no_events() -> None:
    worker = _worker()
    assert worker._normalize(
        {"event": "subscribe", "arg": {"channel": "trades", "instId": "BTC-USDT-SWAP"}, "connId": "x"}
    ) == []


def test_error_event_is_logged(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level("ERROR", logger="heatsignal.ingestion.okx"):
        out = _worker()._normalize(
            {"event": "error", "code": "60018",
             "msg": "Subscribe failed, wrong URL or channel:trades,instId:NOPE-USDT-SWAP doesn't exist.",
             "connId": "x"}
        )
    assert out == []
    assert "60018" in caplog.text


def test_malformed_payload_is_dropped_with_warning(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level("WARNING", logger="heatsignal.ingestion.okx"):
        out = _worker()._normalize(
            {"arg": {"channel": "trades", "instId": "BTC-USDT-SWAP"}, "data": [{"instId": "BTC-USDT-SWAP"}]}
        )
    assert out == []
    assert "no se pudo normalizar" in caplog.text


# ---- ciclo de vida ----------------------------------------------------------------


class FakeWS:
    def __init__(self, frames: list[str]) -> None:
        self.frames = frames
        self.sent: list[str] = []

    async def recv(self) -> str:
        if self.frames:
            return self.frames.pop(0)
        await asyncio.sleep(10)  # silencio -> heartbeat/watchdog
        return ""

    async def send(self, data: str) -> None:
        self.sent.append(data)


async def test_consume_writes_events_pings_when_idle_and_returns_on_timeout() -> None:
    sink = BufferedSink()
    worker = _worker(sink=sink, ping_interval_s=0.02, message_timeout_s=0.08)
    ws = FakeWS([
        json.dumps({"event": "subscribe", "arg": {"channel": "liquidation-orders", "instType": "SWAP"}}),
        "pong",
        json.dumps({"arg": {"channel": "liquidation-orders", "instType": "SWAP"},
                    "data": [{"instId": "BTC-USDT-SWAP", "details": [
                        {"bkPx": "10", "posSide": "long", "side": "sell", "sz": "100", "ts": "1"}]}]}),
        "not json",
    ])
    await worker._consume(ws)
    assert [e.kind for e in sink.buffer] == ["liquidation"]
    assert sink.buffer[0].qty == pytest.approx(1.0)
    assert ws.sent == ["ping"]  # un solo ping por periodo de silencio


async def test_consume_returns_on_notice() -> None:
    worker = _worker(ping_interval_s=5, message_timeout_s=10)
    ws = FakeWS([json.dumps({"event": "notice", "code": "64008",
                             "msg": "The connection will soon be closed for a service upgrade."})])
    await asyncio.wait_for(worker._consume(ws), timeout=1)
