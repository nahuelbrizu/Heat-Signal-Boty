"""Tests del panel interno: estado, features en vivo y pagina, sin red ni LLM."""

from __future__ import annotations

import httpx

from heatsignal.api.panel import PanelContext, create_panel_app
from heatsignal.evaluation.pending_store import PendingHypothesisStore
from heatsignal.evaluation.strategy_registry import StrategyHypothesis, StrategyRegistry
from heatsignal.ingestion.health import CountingSink, HealthRegistry
from heatsignal.pipeline.analysis import AnalysisLoop
from heatsignal.schema.events import Exchange, Side
from heatsignal.storage.memory import MemoryWindowSink

from features_helpers import SYM, liq, ticker, trade


def _client(ctx: PanelContext) -> httpx.AsyncClient:
    # Mismo event loop que el test (como en produccion): el registry SQLite no es thread-safe.
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=create_panel_app(ctx)), base_url="http://panel")


async def _ctx(tmp_path, *, with_analysis: bool = True) -> PanelContext:
    import time

    now = int(time.time() * 1000)
    memory = MemoryWindowSink()
    health = HealthRegistry()
    sink = CountingSink(memory, health, "binance")
    await sink.write(trade(150.0, 2.0, side=Side.SELL, ts=now - 5_000))
    await sink.write(ticker(Exchange.BINANCE, now - 4_000, mark=150.1))
    await sink.write(ticker(Exchange.OKX, now - 3_000, mark=150.2))
    await sink.write(liq(149.0, 25_000.0, ts=now - 2_000))
    if not with_analysis:
        return PanelContext(health=health, memory=memory, config={"symbols": ["SOLUSDT"]})
    registry = StrategyRegistry(tmp_path / "eval.sqlite")
    registry.register(StrategyHypothesis(
        description="rebote", entry_conditions={}, exit_conditions={"horizon_s": 3600}, author="tests",
    ))
    store = PendingHypothesisStore(tmp_path / "eval.sqlite", registry)
    loop = AnalysisLoop(memory, [SYM], registry)
    return PanelContext(health=health, memory=memory, analysis=loop, registry=registry, store=store,
                        config={"symbols": ["SOLUSDT"]})


async def test_status_reports_workers_memory_and_strategies(tmp_path) -> None:
    client = _client(await _ctx(tmp_path))
    body = (await client.get("/api/status")).json()
    assert body["workers"][0]["worker"] == "binance"
    sym = body["memory"]["symbols"][SYM]
    assert sym["counts"] == {"liquidation": 1, "trade": 1, "ticker": 2}
    assert sym["mark_by_source"] == {"okx": 150.2, "binance": 150.1}
    assert sym["last_trade"]["side"] == "sell"
    assert sym["last_liquidation"]["notional_usd"] == 25_000.0
    assert body["analysis"] == {"enabled": True, "graph_enabled": False, "last_cycle": None, "last_ran": None,
                                "history": [], "history_counts": {}, "per_symbol": {}}
    ev = body["evaluation"]
    assert ev["available"] and len(ev["strategies"]) == 1 and ev["strategies"][0]["horizon_s"] == 3600
    assert ev["hypotheses"] == [] and ev["pending_count"] == 0
    assert ev["strategies"][0]["hypotheses"] == {"total": 0, "pending": 0, "wins": 0}


async def test_live_features(tmp_path) -> None:
    client = _client(await _ctx(tmp_path))
    body = (await client.get("/api/features")).json()
    fs = body["features"][SYM]
    assert fs["context"]["mark_price"] is not None
    assert fs["order_flow"]["n_trades"] == 1
    assert any("calentamiento" in g for g in fs["data_gaps"])


async def test_without_analysis_features_are_503_and_eval_unavailable(tmp_path) -> None:
    client = _client(await _ctx(tmp_path, with_analysis=False))
    assert (await client.get("/api/features")).status_code == 503
    assert (await client.get("/api/status")).json()["evaluation"]["available"] is False


async def test_index_serves_html(tmp_path) -> None:
    client = _client(await _ctx(tmp_path))
    r = (await client.get("/"))
    assert r.status_code == 200 and "HeatSignal" in r.text
    assert "http" not in r.text.split("<script>")[0].lower().replace("http-equiv", "")  # sin recursos externos


async def test_klines_passthrough_validates_and_reports_upstream_errors(tmp_path) -> None:
    calls: list[str] = []

    def fake_get(url: str, timeout_s: float) -> tuple[int, bytes]:
        calls.append(url)
        if "limit=2" in url:
            return 200, b'[[1792108800000,"1.3343","1.3528","1.2884","1.3109","474559415.8",0,"0",1,"0","0","0"]]'
        return 503, b"mantenimiento"

    ctx = await _ctx(tmp_path, with_analysis=False)
    ctx.http_get = fake_get
    client = _client(ctx)

    r = await client.get("/api/klines", params={"symbol": "SOL-USDT", "interval": "1d", "limit": 2})
    assert r.status_code == 200
    assert r.json()["candles"] == [{"t": 1792108800, "o": 1.3343, "h": 1.3528, "l": 1.2884, "c": 1.3109, "v": 474559415.8}]
    assert "symbol=SOLUSDT" in calls[0] and "interval=1d" in calls[0]

    assert (await client.get("/api/klines", params={"symbol": "ETHUSDT"})).status_code == 400  # no configurado
    assert (await client.get("/api/klines", params={"symbol": "SOLUSDT", "interval": "3m"})).status_code == 400
    assert (await client.get("/api/klines", params={"symbol": "SOLUSDT", "limit": 1501})).status_code == 422
    assert (await client.get("/api/klines", params={"symbol": "SOLUSDT", "limit": 5})).status_code == 502
    assert len(calls) == 2  # lo invalido no llega a Binance


async def test_charts_library_is_served_locally(tmp_path) -> None:
    client = _client(await _ctx(tmp_path, with_analysis=False))
    r = await client.get("/static/lightweight-charts.js")
    assert r.status_code == 200 and "Lightweight Charts" in r.text[:300]
    assert '<script src="/static/lightweight-charts.js">' in (await client.get("/")).text


async def test_page_is_never_cached_and_status_carries_build_id(tmp_path) -> None:
    ctx = await _ctx(tmp_path, with_analysis=False)
    client = _client(ctx)
    page = await client.get("/")
    assert page.headers["cache-control"] == "no-store"
    assert "location.reload()" in page.text
    build = (await client.get("/api/status")).json()["build"]
    assert len(build) == 12 and (await client.get("/api/status")).json()["build"] == build
    ctx.started_at += 1  # otro proceso (deploy/reinicio) -> otro build: la pagina abierta se recarga
    assert (await _client(ctx).get("/api/status")).json()["build"] != build
