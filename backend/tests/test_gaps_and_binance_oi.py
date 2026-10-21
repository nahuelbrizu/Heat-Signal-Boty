"""Clasificacion de avisos de datos (features/gaps.py) y OI de Binance por REST (ingestion/binance_oi_poller.py).

Sin red: el poller usa un `http_get` falso y un sink en memoria.
"""

from __future__ import annotations

import asyncio
import json

import pytest
from _agent_fakes import ScriptedChatModel, full_script, make_feature_set, prompt_text
from features_helpers import SYM, liq, ticker, trade

from heatsignal.agents.graph import build_graph
from heatsignal.agents.redactor import ReportDraft
from heatsignal.agents.debate import DebateArgumentBatch
from heatsignal.agents.cio import _SYSTEM_PROMPT as SYNTH_PROMPT  # el CIO tomo la decision del Sintetizador
from heatsignal.features.builder import build_feature_set
from heatsignal.features.gaps import classify_gap, gap_classes, render_gaps, split_gaps
from heatsignal.ingestion.binance_oi_poller import BinanceOiPoller, oi_url, parse_open_interest
from heatsignal.schema.events import Exchange, TickerEvent
from heatsignal.storage.throttle import ThrottledSink

M = 60_000
NOW = 40 * M

# ---------------------------------------------------------------------------- clasificacion

STRUCTURAL = [
    "liquidaciones binance PARTIAL (<=1/seg/simbolo): subrepresentan el notional real",
    "liquidaciones binance/okx PARTIAL (<=1/seg/simbolo): subrepresentan el notional real",
    "liquidaciones: sin eventos de okx en la ventana (conectado y sano)",
    "liquidaciones: 0 eventos en la ventana (exchanges conectados y sanos)",
    "noticias: Ripple/XRPL no publican RSS; sin fuente primaria del emisor (solo SEC + medios)",
]
FALLBACK = [
    "htf: 2 de las ultimas 48 velas de 1h vinieron del REST de Binance (faltaban en la base propia)",
    "sesion: 12 minuto(s) vinieron del REST de Binance (faltaban en la base)",
    "htf: base historica no disponible (se usa REST de Binance)",
    "htf: no se pudo abrir la base historica (boom); se usa REST",
    "htf: fallo la lectura de la base historica (boom); se usa REST",
    "sesion: base historica no disponible (precio solo por REST, sin datos propios)",
]
REAL = [
    "liquidaciones: sin eventos de okx en la ventana",  # exchange no sano: no distinguible
    "liquidaciones: 0 eventos en la ventana (sin datos o sin liquidaciones: no distinguible)",
    "trades: sin datos de okx en la ventana",
    "ticker okx: stale, ultimo hace 200s (> 120s)",
    "open_interest: sin dato de binance (la suma es parcial)",
    "open_interest: fallo el REST de binance (ValueError: HTTP 500); sin dato desde el arranque",
    "htf: fallo el REST de Binance para completar velas de 1h (timeout)",
    "htf: faltan 3 de las ultimas 48 velas de 1h (ni base ni REST)",
    "htf: sin velas de 1h cerradas: tendencia diaria sin_datos",
    "htf-levels: VWAP: 2 hora(s) del dia sin velas 1m en la base (solo REST 1h)",
    "memoria: calentamiento tras reinicio, 10s acumulados de 1800s pedidos",
    "algo nuevo que nadie clasifico",
]


@pytest.mark.parametrize("text", STRUCTURAL)
def test_structural(text: str) -> None:
    assert classify_gap(text)[0] == "estructural"


@pytest.mark.parametrize("text", FALLBACK)
def test_fallback(text: str) -> None:
    assert classify_gap(text)[0] == "respaldo"


@pytest.mark.parametrize("text", REAL)
def test_real_and_unknown_default_to_real(text: str) -> None:
    assert classify_gap(text) == ("real", text)


def test_split_and_panel_classes_keep_everything() -> None:
    gaps = [STRUCTURAL[0], REAL[2], FALLBACK[0], STRUCTURAL[0]]
    real, notes = split_gaps(gaps)
    assert real == [REAL[2]]
    assert notes == ["liqs binance muestreadas (<=1/s)", "2 velas 1h por REST"]
    assert [g["cls"] for g in gap_classes(gaps)] == ["estructural", "real", "respaldo", "estructural"]


def test_render_separates_and_is_shorter() -> None:
    gaps = STRUCTURAL[:3] + FALLBACK[:1]
    text = render_gaps(gaps)
    assert text.startswith("huecos de datos: ninguno declarado | notas de cobertura (conocidas, no son fallas): ")
    assert "severo" not in text.lower()
    assert len(text) < len("huecos de datos DECLARADOS: " + "; ".join(gaps))
    assert render_gaps([REAL[2]] + gaps).startswith("huecos de datos: trades: sin datos de okx en la ventana | notas")


# ---------------------------------------------------------------------------- builder: sano vs caido

def _healthy_world():
    trades = [trade(100, 1, source=ex, ts=NOW - 10) for ex in (Exchange.BINANCE, Exchange.BYBIT, Exchange.OKX)]
    tickers = [ticker(ex, NOW - 5, mark=100.0, funding=0.0001, oi_usd=1e6)
               for ex in (Exchange.BYBIT, Exchange.OKX)] + [ticker(Exchange.BINANCE, NOW - 5, mark=100.0, funding=0.0)]
    return trades, tickers


EXP = [Exchange.BINANCE, Exchange.BYBIT, Exchange.OKX]


def test_zero_okx_liqs_with_okx_healthy_is_structural() -> None:
    trades, tickers = _healthy_world()
    fs = build_feature_set(SYM, [liq(100, 1000, ts=NOW - 60)], trades, tickers, NOW, 1800, expected_exchanges=EXP)
    g = [x for x in fs.data_gaps if "sin eventos de okx" in x]
    assert g == ["liquidaciones: sin eventos de okx en la ventana (conectado y sano)"]
    assert classify_gap(g[0])[0] == "estructural"


def test_zero_okx_liqs_with_okx_down_is_real() -> None:
    trades, tickers = _healthy_world()
    trades = [t for t in trades if t.source != Exchange.OKX]
    fs = build_feature_set(SYM, [liq(100, 1000, ts=NOW - 60)], trades, tickers, NOW, 1800, expected_exchanges=EXP)
    assert "liquidaciones: sin eventos de okx en la ventana" in fs.data_gaps
    assert classify_gap("liquidaciones: sin eventos de okx en la ventana")[0] == "real"


def test_zero_liqs_all_healthy_is_structural() -> None:
    trades, tickers = _healthy_world()
    fs = build_feature_set(SYM, [], trades, tickers, NOW, 1800, expected_exchanges=EXP)
    assert "liquidaciones: 0 eventos en la ventana (exchanges conectados y sanos)" in fs.data_gaps


# ---------------------------------------------------------------------------- contexto suma el OI REST

def _rest_oi(ts: int, oi: float, usd: float | None) -> TickerEvent:
    return TickerEvent(source=Exchange.BINANCE, symbol=SYM, open_interest=oi, open_interest_usd=usd,
                       ts_event_ms=ts, ts_ingest_ms=ts, coverage="rest_poll")


def test_context_sums_binance_rest_oi_and_coverage_is_rest() -> None:
    trades, tickers = _healthy_world()
    tickers += [_rest_oi(NOW - 10 * M, 1000, 100_000.0), _rest_oi(NOW - 5 * M, 1100, 110_000.0)]
    fs = build_feature_set(SYM, [liq(100, 1000, ts=NOW - 60)], trades, tickers, NOW, 1800, expected_exchanges=EXP)
    assert fs.context.open_interest_usd == pytest.approx(2e6 + 110_000)
    assert fs.context.oi_change_pct == pytest.approx(10.0)  # solo binance tiene 2 obs: 100k -> 110k
    assert fs.quality.coverage["binance"]["open_interest"] == "rest"
    assert fs.quality.oi_ts_ms["binance"] == NOW - 5 * M
    assert not any(g.startswith("open_interest") for g in fs.data_gaps)


def test_rest_oi_does_not_fake_a_live_ticker_and_stale_rest_is_real_gap() -> None:
    trades, tickers = _healthy_world()
    tickers = [t for t in tickers if t.source != Exchange.BINANCE] + [_rest_oi(NOW - 20 * M, 1000, 1e5)]
    fs = build_feature_set(SYM, [], trades, tickers, NOW, 1800, expected_exchanges=EXP)
    assert "ticker: sin datos de binance" in fs.data_gaps
    stale = [g for g in fs.data_gaps if "REST de binance sin actualizar" in g]
    assert stale == ["open_interest: REST de binance sin actualizar hace 20 min"]
    assert classify_gap(stale[0])[0] == "real"


# ---------------------------------------------------------------------------- poller con servidor falso

class _Sink:
    def __init__(self) -> None:
        self.events: list = []

    async def write(self, event) -> None:
        self.events.append(event)

    async def close(self) -> None:
        pass


class _FakeBinance:
    """Servidor falso de /fapi/v1/openInterest: responde por URL, registra las llamadas."""

    def __init__(self, fail: set[str] | None = None, status: int = 200) -> None:
        self.calls: list[str] = []
        self.fail = fail or set()
        self.status = status

    def __call__(self, url: str, timeout_s: float) -> tuple[int, bytes]:
        self.calls.append(url)
        sym = url.split("symbol=")[1]
        if sym in self.fail:
            return self.status, b'{"code":-1003,"msg":"Too many requests"}'
        return 200, json.dumps({"openInterest": "10659.509", "symbol": sym, "time": 1589437530011}).encode()


def test_parse_documented_response() -> None:
    body = b'{"openInterest": "10659.509", "symbol": "BTCUSDT", "time": 1589437530011}'
    assert parse_open_interest(body, "BTCUSDT") == (10659.509, 1589437530011)
    assert oi_url("BTC-USDT") == "https://fapi.binance.com/fapi/v1/openInterest?symbol=BTCUSDT"
    with pytest.raises(ValueError):
        parse_open_interest(b'{"code": -1121, "msg": "Invalid symbol."}', "BTCUSDT")
    with pytest.raises(ValueError):
        parse_open_interest(body, "ETHUSDT")


async def test_poller_emits_rest_poll_events_with_usd() -> None:
    sink, http = _Sink(), _FakeBinance()
    p = BinanceOiPoller(["BTC-USDT", "ETH-USDT"], sink, http_get=http, clock_ms=lambda: 1_000_000,
                        mark_lookup=lambda s: 2.0 if s == "BTC-USDT" else None)
    assert await p.poll_once() == 2
    btc, eth = sink.events
    assert (btc.source, btc.symbol, btc.coverage) == (Exchange.BINANCE, "BTC-USDT", "rest_poll")
    assert btc.open_interest == pytest.approx(10659.509) and btc.open_interest_usd == pytest.approx(21319.018)
    assert btc.ts_event_ms == 1589437530011 and btc.mark_price is None
    assert eth.open_interest_usd is None  # sin mark: se declara
    assert p.data_gaps("BTC-USDT", 1_000_000) == []
    assert p.data_gaps("ETH-USDT", 1_000_000) == [
        "open_interest: binance sin mark para pasar el OI a USD (no entra en la suma)"]


async def test_poller_error_is_real_gap_and_backoff() -> None:
    sink, http = _Sink(), _FakeBinance(fail={"ETHUSDT"}, status=500)
    p = BinanceOiPoller(["BTC-USDT", "ETH-USDT"], sink, http_get=http, clock_ms=lambda: 1_000_000, interval_s=300,
                        mark_lookup=lambda s: 1.0)
    assert await p.poll_once() == 1
    gaps = p.data_gaps("ETH-USDT", 1_000_000)
    assert len(gaps) == 1 and gaps[0].startswith("open_interest: fallo el REST de binance (ValueError: HTTP 500")
    assert gaps[0].endswith("sin dato desde el arranque")
    assert classify_gap(gaps[0])[0] == "real"
    assert p.next_delay_s() == 30  # reintento antes que el intervalo
    await p.poll_once()
    assert p.next_delay_s() == 60
    http.fail.clear()
    await p.poll_once()
    assert p.next_delay_s() == 300 and p.data_gaps("ETH-USDT", 1_000_000) == []


async def test_poller_rate_limit_waits_full_interval() -> None:
    p = BinanceOiPoller(["BTC-USDT"], _Sink(), http_get=_FakeBinance(fail={"BTCUSDT"}, status=429),
                        clock_ms=lambda: 0, interval_s=300)
    await p.poll_once()
    assert p.next_delay_s() == 300 and "HTTP 429" in p.last_error["BTC-USDT"]


async def test_poller_cadence() -> None:
    http = _FakeBinance()
    p = BinanceOiPoller(["BTC-USDT", "ETH-USDT"], _Sink(), http_get=http, interval_s=0.05)
    stop = asyncio.Event()
    task = asyncio.create_task(p.run_forever(stop))
    await asyncio.sleep(0.13)  # t=0, 0.05, 0.10 -> 3 ciclos
    stop.set()
    await task
    assert len(http.calls) in (6, 8)  # 2 simbolos por ciclo, 3 (o 4 por jitter) ciclos
    assert all(u.startswith("https://fapi.binance.com/fapi/v1/openInterest?symbol=") for u in http.calls)


async def test_throttle_does_not_coalesce_rest_oi_with_ws_mark() -> None:
    sink = _Sink()
    t = ThrottledSink(sink, {"ticker": 60.0})
    await t.write(ticker(Exchange.BINANCE, 1, mark=100.0))
    await t.write(_rest_oi(2, 1000, 1e5))
    await t.write(ticker(Exchange.BINANCE, 3, mark=101.0))  # este si se coalesce
    assert [e.coverage for e in sink.events] == ["stream", "rest_poll"]
    await t.close()


# ---------------------------------------------------------------------------- texto al LLM

async def test_llm_text_separates_structural_from_real_and_prompts_forbid_severo() -> None:
    gaps = STRUCTURAL[:3] + FALLBACK[:1] + ["trades: sin datos de okx en la ventana"]
    fake = ScriptedChatModel(full_script(["BTC-USDT"]))
    await build_graph(fake).ainvoke({"run_id": "r", "allowed_strategies": [],
                                     "features_by_symbol": {"BTC-USDT": make_feature_set(data_gaps=gaps)}})
    for schema in (DebateArgumentBatch, ReportDraft):
        text = prompt_text(fake.calls_for(schema)[0])
        assert "huecos de datos: trades: sin datos de okx en la ventana | notas de cobertura" in text
        assert "subrepresentan el notional real" not in text  # el texto largo estructural no llega
        assert "severo" not in text.replace("no las llames severas", "")
        # el digest no repite por analista lo que ya esta en la linea del simbolo
        assert text.count("trades: sin datos de okx en la ventana") == 1
    assert "no las llames severas" in prompt_text(fake.calls_for(ReportDraft)[0])
    assert "no son huecos ni fallas severas" in SYNTH_PROMPT
