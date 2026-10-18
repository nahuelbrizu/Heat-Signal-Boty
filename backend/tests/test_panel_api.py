"""Tests de la API de solo lectura del panel (api/panel_api.py): datos en tmp_path, sin red ni LLM."""

from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import httpx
import pytest

from heatsignal.api import panel, panel_api
from heatsignal.api.panel import PanelContext, create_panel_app
from heatsignal.evaluation.pending_store import PendingHypothesisStore, ResolutionMetrics
from heatsignal.evaluation.strategy_registry import StrategyHypothesis, StrategyRegistry, StrategyStatus
from heatsignal.history.recorder import MinuteRecorder
from heatsignal.history.store import HistoryStore, MinuteBar
from heatsignal.ingestion.health import CountingSink, HealthRegistry
from heatsignal.news.window import NewsWindow
from heatsignal.pipeline.analysis import AnalysisLoop
from heatsignal.pipeline.cycle_log import CycleLog
from heatsignal.schema.events import Exchange, Side
from heatsignal.schema.hypotheses import HypothesisStatus, PendingHypothesis, Resolution
from heatsignal.schema.news import NewsItem
from heatsignal.schema.signals import Direction
from heatsignal.storage.memory import MemoryWindowSink

from features_helpers import SYM, liq, ticker, trade

T0 = datetime(2026, 10, 1, tzinfo=timezone.utc)
M0 = 1_792_000_000_000 - 1_792_000_000_000 % 3_600_000  # inicio de una hora (epoch ms)
REPORT = {"status": "ok", "title": "t", "summary": "s",
          "symbols": {SYM: {"liquidations": {"confidence": 0.5, "text": "clusters"}, "debate": [], "report": "r"}}}


@pytest.fixture(autouse=True)
def _clear_caches():
    # los caches se indexan por id() del objeto: entre tests un id puede reusarse
    for c in (panel_api._CACHE, panel._HISTORY_CACHE, panel._PER_SYMBOL_CACHE, panel._LAB_CACHE):
        c.clear()
    yield


def _client(ctx: PanelContext) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=create_panel_app(ctx)), base_url="http://panel")


def _hyp(sid, *, hours: int, direction=Direction.LONG, conf=0.6, run_id=None, symbol=SYM) -> PendingHypothesis:
    return PendingHypothesis(strategy_id=sid, symbol=symbol, created_at=T0 + timedelta(hours=hours), direction=direction,
                             confidence_score=conf, entry_reference_price=Decimal("100"), horizon_s=3600,
                             rationale="clusters similares", run_id=run_id)


def _resolve(store, h, exit_price: str, pnl: float, alpha: float) -> None:
    due = h.created_at + timedelta(seconds=h.horizon_s)
    store.resolve(Resolution(hypothesis_id=h.hypothesis_id, status=HypothesisStatus.RESOLVED, resolved_at=due,
                             exit_price=Decimal(exit_price), pnl_pct=pnl, win=pnl > 0),
                  ResolutionMetrics(gross_pnl_pct=pnl + 0.1, cost_bps=10, benchmark_pnl_pct=pnl - alpha, alpha_pct=alpha,
                                    exit_price_ts=due))


def _news(item_id: str, published_ms: int, *, symbols=("XRP",), primary=False, source="coindesk") -> NewsItem:
    return NewsItem(item_id=item_id, source=source, primary=primary, title=f"titulo {item_id}", summary="ETF de XRP",
                    url=f"https://x.test/{item_id}", published_ms=published_ms, fetched_ms=published_ms + 1000,
                    symbols=list(symbols), market_wide=False)


async def _full_ctx(tmp_path) -> tuple[PanelContext, dict]:
    now = int(time.time() * 1000)
    memory = MemoryWindowSink()
    health = HealthRegistry()
    sink = CountingSink(memory, health, "binance")
    await sink.write(trade(150.0, 2.0, side=Side.SELL, ts=now - 5_000))
    await sink.write(trade(151.0, 1.0, side=Side.BUY, source=Exchange.BYBIT, ts=now - 4_000))
    await sink.write(ticker(Exchange.BINANCE, now - 6_000, mark=150.0, funding=0.0001))
    await sink.write(ticker(Exchange.BINANCE, now - 4_000, mark=150.1))  # sin funding: se toma el anterior
    await sink.write(ticker(Exchange.OKX, now - 3_000, mark=150.2, funding=0.0003, oi=1000.0))
    await sink.write(liq(149.0, 25_000.0, side=Side.SELL, ts=now - 2_000))
    await sink.write(liq(152.0, 5_000.0, side=Side.BUY, source=Exchange.OKX, ts=now - 2 * 3_600_000))

    eval_db = tmp_path / "eval.sqlite"
    registry = StrategyRegistry(eval_db)
    s1 = StrategyHypothesis(description="rebote", entry_conditions={"symbol": SYM, "regla": "x"},
                            exit_conditions={"horizon_s": 3600}, author="tests")
    s2 = StrategyHypothesis(description="sin hipotesis", entry_conditions={}, exit_conditions={}, author="tests")
    registry.register(s1)
    registry.register(s2)
    registry.set_status(s1.strategy_id, StrategyStatus.RUNNING)
    store = PendingHypothesisStore(eval_db, registry)
    win = _hyp(s1.strategy_id, hours=0, run_id="cycle-1000")
    loss = _hyp(s1.strategy_id, hours=2, direction=Direction.SHORT, conf=0.8)
    expired = _hyp(s1.strategy_id, hours=4, symbol="XRP-USDT")
    pending = PendingHypothesis(strategy_id=s1.strategy_id, symbol=SYM, direction=Direction.LONG, confidence_score=0.5,
                                entry_reference_price=Decimal("150"), horizon_s=86_400, rationale="r")
    for h in (win, loss, expired, pending):
        store.add(h)
    _resolve(store, win, "102", 1.9, -0.1)
    _resolve(store, loss, "103", -3.1, -6.1)
    store.resolve(Resolution(hypothesis_id=expired.hypothesis_id, status=HypothesisStatus.EXPIRED_NO_DATA,
                             resolved_at=expired.created_at + timedelta(hours=2)))

    cl = CycleLog(eval_db)
    cl.append("cycle-1000", "ran", 1, f"{SYM}=hypothesis", [{"symbol": SYM, "outcome": "hypothesis", "reason": "ok",
                                                            "strategy_id": str(s1.strategy_id)}], REPORT)
    cl.append("cycle-2000", "warmup", 0, "calentando", [], None)
    cl.append("cycle-3000", "error", 0, "grafo: boom", [], None)
    loop = AnalysisLoop(memory, [SYM], registry, cycle_log=cl)

    hstore = HistoryStore(tmp_path / "history.sqlite")
    bars = []
    for ex, base in (("binance", 100.0), ("bybit", 100.5)):
        for i in range(10):  # 10 minutos: 2 barras de 5m
            m = M0 + i * 60_000
            bars.append(MinuteBar(ex, SYM, m, open=base + i, high=base + i + 2, low=base + i - 1, close=base + i + 1,
                                  buy_usd=10.0, sell_usd=4.0, n_trades=3, liq_long_usd=100.0 if i == 3 else 0.0,
                                  n_liqs=1 if i == 3 else 0, mark=base + i + 0.5,
                                  funding_8h=0.0001 if ex == "bybit" else None, oi_usd=1e6 if ex == "bybit" else None))
    hstore.upsert_bars(bars)
    hstore.add_liquidations([("bybit", SYM, M0 + 1000, "sell", 100.0, 2.0, 200.0, "full"),
                             ("binance", SYM, M0 + 2000, "buy", 101.0, 100.0, 10_100.0, "partial"),
                             ("okx", "XRP-USDT", M0 + 3000, "sell", 2.0, 10.0, 20.0, "partial")])
    recorder = MinuteRecorder(hstore)
    news = NewsWindow(feeds=[], on_new=recorder.record_news)
    news.ingest([_news("guardada", now - 60_000), _news("btc", now - 30_000, symbols=("BTC",), primary=True, source="sec")])
    news._on_new = None
    news.ingest([_news("solo-memoria", now - 10_000)])  # no llega a la base (grabacion caida)
    hstore.add_news([("vieja", "coindesk", 0, "noticia vieja de XRP", "", "https://x.test/v", M0, M0, "XRP", 0)])

    ctx = PanelContext(health=health, memory=memory, analysis=loop, registry=registry, store=store, news=news,
                       history=recorder, cycle_log=cl, config={"symbols": ["SOLUSDT"], "analysis_interval_s": 900},
                       lab_summary_path=tmp_path / "lab_summary.json", checkpoint_db=tmp_path / "ck.sqlite",
                       db_paths={"eval": eval_db, "history": tmp_path / "history.sqlite"},
                       settings={"news_interval_s": 300})
    return ctx, {"s1": s1, "s2": s2, "win": win, "loss": loss, "expired": expired, "pending": pending}


# ---------------------------------------------------------------------------- summary / system

async def test_summary_is_light_and_complete(tmp_path) -> None:
    ctx, _ = await _full_ctx(tmp_path)
    body = (await _client(ctx).get("/api/summary")).json()
    assert len(body["build"]) == 12 and body["now_at"].endswith("+00:00")
    assert body["workers"][0]["worker"] == "binance" and "events" not in body["workers"][0]
    sym = body["memory"]["symbols"][SYM]
    assert sym["counts"]["trade"] == 2 and sym["mark"] == pytest.approx(150.15)
    assert body["analysis"]["enabled"] and body["analysis"]["llm_providers"] == []
    assert body["analysis"]["last_ran"]["run_id"] == "cycle-1000"  # recuperado del cycle_log
    assert body["counts"]["hypotheses"] == {"total": 4, "pending": 1, "due": 0, "resolved": 2, "expired_no_data": 1,
                                            "wins": 1}
    assert body["counts"]["strategies"] == {"total": 2, "by_status": {"running": 1, "pending": 1}}
    assert body["counts"]["cycles"]["total"] == 3 and body["counts"]["news_window"] == 3
    assert body["recording"]["enabled"] and body["recording"]["rows"]["bars_1m"] == 20
    assert body["recording"]["status"] in ("starting", "ok")
    assert body["lab"] is None
    assert "features" not in json.dumps(body["analysis"])  # nada pesado en el poll


async def test_status_still_works_with_history(tmp_path) -> None:
    ctx, _ = await _full_ctx(tmp_path)
    body = (await _client(ctx).get("/api/status")).json()
    assert body["history"]["store"]["bars_1m"]["rows"] == 20 and "recorder" in body["history"]
    assert body["evaluation"]["total_hypotheses"] == 4


async def test_system_reports_operation(tmp_path) -> None:
    ctx, _ = await _full_ctx(tmp_path)
    (tmp_path / "last_upload.json").write_text(json.dumps({"ts_ms": M0, "remote": "gdrive:"}))
    body = (await _client(ctx).get("/api/system")).json()
    assert body["uptime_s"] >= 0 and body["python"].count(".") == 2
    assert body["databases"]["eval"]["exists"] and body["databases"]["eval"]["size_bytes"] > 0
    assert body["databases"]["checkpoints"]["exists"] is False
    assert body["export"]["last_upload"]["remote"] == "gdrive:" and body["export"]["last_upload"]["ts_at"]
    assert body["errors"]["cycles"][0]["run_id"] == "cycle-3000"
    assert body["settings"] == {"news_interval_s": 300} and body["config"]["symbols"] == ["SOLUSDT"]
    assert "commit" in body["version"] and body["disk"]["free_bytes"] > 0


# ---------------------------------------------------------------------------- hipotesis

async def test_hypotheses_filters_and_pagination(tmp_path) -> None:
    ctx, d = await _full_ctx(tmp_path)
    c = _client(ctx)
    body = (await c.get("/api/hypotheses")).json()
    assert body["total"] == 4 and len(body["items"]) == 4
    assert body["items"][0]["hypothesis_id"] == str(d["pending"].hypothesis_id)  # mas nueva primero

    page1 = (await c.get("/api/hypotheses", params={"limit": 2, "order": "asc"})).json()
    page2 = (await c.get("/api/hypotheses", params={"limit": 2, "offset": 2, "order": "asc"})).json()
    assert page1["total"] == page2["total"] == 4
    assert [h["hypothesis_id"] for h in page1["items"] + page2["items"]] == [
        str(d[k].hypothesis_id) for k in ("win", "loss", "expired", "pending")]

    res = (await c.get("/api/hypotheses", params={"status": "resolved"})).json()
    assert res["total"] == 2
    w = next(h for h in res["items"] if h["win"])
    assert w["real_move_pct"] == pytest.approx(2.0) and w["gross_pnl_pct"] == pytest.approx(2.0)
    assert w["alpha_pct"] == pytest.approx(-0.1) and w["benchmark_pnl_pct"] == pytest.approx(2.0)
    assert w["due_at"].startswith("2026-10-01T01:00") and w["resolved_ms"] == w["due_ms"]
    assert w["created_ms"] == int(T0.timestamp() * 1000) and w["run_id"] == "cycle-1000"

    assert (await c.get("/api/hypotheses", params={"win": "false"})).json()["total"] == 1
    assert (await c.get("/api/hypotheses", params={"status": "pending"})).json()["total"] == 1
    assert (await c.get("/api/hypotheses", params={"status": "expired_no_data"})).json()["items"][0]["symbol"] == "XRP-USDT"
    assert (await c.get("/api/hypotheses", params={"symbol": "XRPUSDT"})).json()["total"] == 1
    assert (await c.get("/api/hypotheses", params={"strategy_id": str(d["s2"].strategy_id)})).json()["total"] == 0
    since = (await c.get("/api/hypotheses", params={"since": "2026-10-01T01:00:00Z", "until": "2026-10-01T05:00:00Z"})).json()
    assert since["total"] == 2 and since["filters"]["since_ms"] == int(T0.timestamp() * 1000) + 3_600_000
    ms = int((T0 + timedelta(hours=1)).timestamp() * 1000)
    assert (await c.get("/api/hypotheses", params={"since": str(ms), "until": "2026-10-01T05:00:00"})).json()["total"] == 2


async def test_hypotheses_validation(tmp_path) -> None:
    ctx, _ = await _full_ctx(tmp_path)
    c = _client(ctx)
    assert (await c.get("/api/hypotheses", params={"status": "open"})).status_code == 422
    assert (await c.get("/api/hypotheses", params={"win": "quizas"})).status_code == 422
    assert (await c.get("/api/hypotheses", params={"limit": 0})).status_code == 422
    assert (await c.get("/api/hypotheses", params={"limit": 501})).status_code == 422
    assert (await c.get("/api/hypotheses", params={"strategy_id": "no-uuid"})).status_code == 422
    r = await c.get("/api/hypotheses", params={"since": "ayer"})
    assert r.status_code == 400 and "since" in r.json()["detail"]
    assert (await c.get("/api/hypotheses", params={"since": "2026-10-02", "until": "2026-10-01"})).status_code == 400
    assert (await c.get("/api/hypotheses", params={"symbol": "SOL;DROP"})).status_code == 400


async def test_hypothesis_detail_links_its_cycle(tmp_path) -> None:
    ctx, d = await _full_ctx(tmp_path)
    c = _client(ctx)
    body = (await c.get(f"/api/hypotheses/{d['win'].hypothesis_id}")).json()
    assert body["strategy"]["entry_conditions"]["regla"] == "x"
    assert body["cycle"]["run_id"] == "cycle-1000" and body["cycle"]["decision"]["outcome"] == "hypothesis"
    assert body["cycle"]["analysis"]["liquidations"]["text"] == "clusters" and body["cycle_url"] == "/api/cycles/cycle-1000"
    nocycle = (await c.get(f"/api/hypotheses/{d['loss'].hypothesis_id}")).json()
    assert nocycle["cycle"] is None and nocycle["cycle_url"] is None
    assert (await c.get("/api/hypotheses/00000000-0000-0000-0000-000000000000")).status_code == 404
    assert (await c.get("/api/hypotheses/xyz")).status_code == 422


async def test_eval_endpoints_503_without_analysis(tmp_path) -> None:
    c = _client(PanelContext(health=HealthRegistry()))
    for url in ("/api/hypotheses", "/api/strategies", "/api/cycles", "/api/history/summary", "/api/news",
                "/api/market/SOLUSDT"):
        assert (await c.get(url)).status_code == 503, url
    assert (await c.get("/api/lab")).status_code == 404
    assert (await c.get("/api/summary")).status_code == 200 and (await c.get("/api/system")).status_code == 200
    assert (await c.get("/api/analysis/latest")).json() == {"symbols": {}, "last_ran": None}


# ---------------------------------------------------------------------------- estrategias

async def test_strategies_track_record(tmp_path) -> None:
    ctx, d = await _full_ctx(tmp_path)
    body = (await _client(ctx).get("/api/strategies")).json()
    assert body["total"] == 2
    s1 = next(s for s in body["items"] if s["strategy_id"] == str(d["s1"].strategy_id))
    assert s1["status"] == "running" and s1["symbol"] == SYM and s1["horizon_s"] == 3600
    assert s1["hypotheses"] == {"total": 4, "pending": 1, "due": 0, "resolved": 2, "expired_no_data": 1, "wins": 1}
    tr = s1["track_record"]
    assert tr["resolved"] == 2 and tr["wins"] == 1 and tr["rate"] == 0.5
    assert 0 < tr["ci95"][0] < 0.5 < tr["ci95"][1] < 1
    assert tr["mean_pnl_pct"] == pytest.approx(-0.6) and tr["mean_alpha_pct"] == pytest.approx(-3.1)
    assert tr["brier"] == pytest.approx(((0.6 - 1) ** 2 + (0.8 - 0) ** 2) / 2)
    assert tr["brier_reference"] == pytest.approx(0.25) and tr["mean_confidence"] == pytest.approx(0.7)
    s2 = next(s for s in body["items"] if s["strategy_id"] == str(d["s2"].strategy_id))
    assert s2["track_record"]["resolved"] == 0 and s2["track_record"]["brier"] is None and s2["track_record"]["ci95"] is None


async def test_strategy_detail(tmp_path) -> None:
    ctx, d = await _full_ctx(tmp_path)
    c = _client(ctx)
    sid = d["s1"].strategy_id
    body = (await c.get(f"/api/strategies/{sid}", params={"limit": 2})).json()
    assert body["entry_conditions"] == {"symbol": SYM, "regla": "x"} and body["exit_conditions"] == {"horizon_s": 3600}
    assert [e["status"] for e in body["status_history"]] == ["pending", "running"]
    assert body["status_history"][0]["changed_ms"] > 0
    hp = body["hypotheses_page"]
    assert hp["total"] == 4 and len(hp["items"]) == 2 and hp["next"].endswith("offset=2")
    cal = body["calibration"]
    assert len(cal) == 10 and sum(b["n"] for b in cal) == 2
    assert cal[6]["n"] == 1 and cal[6]["hit_rate"] == 1.0 and cal[8]["hit_rate"] == 0.0
    assert (await c.get("/api/strategies/00000000-0000-0000-0000-000000000000")).status_code == 404


# ---------------------------------------------------------------------------- ciclos

async def test_cycles_list_and_detail(tmp_path) -> None:
    ctx, d = await _full_ctx(tmp_path)
    c = _client(ctx)
    body = (await c.get("/api/cycles")).json()
    assert body["total"] == 3 and [r["run_id"] for r in body["items"]] == ["cycle-3000", "cycle-2000", "cycle-1000"]
    assert "report" not in body["items"][0] and body["items"][2]["has_report"] is True
    assert body["items"][2]["ts_at"].startswith("1970-01-01") and body["counts"]["ran"] == 1
    assert (await c.get("/api/cycles", params={"status": "error"})).json()["total"] == 1
    assert (await c.get("/api/cycles", params={"status": "otro"})).status_code == 422
    assert (await c.get("/api/cycles", params={"limit": 1, "offset": 1})).json()["items"][0]["run_id"] == "cycle-2000"

    det = (await c.get("/api/cycles/cycle-1000")).json()
    assert det["report_source"] == "cycle_log" and det["report"]["symbols"][SYM]["report"] == "r"
    assert [h["hypothesis_id"] for h in det["hypotheses"]] == [str(d["win"].hypothesis_id)]
    warm = (await c.get("/api/cycles/cycle-2000")).json()
    assert warm["report"] is None and warm["report_source"] is None and "no corrio" in warm["notes"][0]
    assert (await c.get("/api/cycles/cycle-999")).status_code == 404
    assert (await c.get("/api/cycles/bad%20id")).status_code == 400


async def test_cycle_report_falls_back_to_checkpoint(tmp_path) -> None:
    from heatsignal.agents.checkpoint import make_run_config, sqlite_checkpointer
    from heatsignal.agents.graph import build_graph

    from _agent_fakes import ScriptedChatModel, full_script, make_feature_set

    run_id = "cycle-1792206109022"
    async with sqlite_checkpointer(tmp_path / "ck.sqlite") as saver:
        graph = build_graph(ScriptedChatModel(full_script(["XRP-USDT"])), checkpointer=saver)
        state = {"run_id": run_id, "features_by_symbol": {"XRP-USDT": make_feature_set("XRP-USDT")},
                 "allowed_strategies": [StrategyHypothesis(description="s", entry_conditions={}, exit_conditions={},
                                                           author="t")]}
        await graph.ainvoke(state, make_run_config(["XRP-USDT"], run_id))
    ctx, _ = await _full_ctx(tmp_path)
    ctx.cycle_log.append(run_id, "ran", 0, "XRP-USDT=no_hypothesis", [], None)  # sin reporte en el log
    c = _client(ctx)
    det = (await c.get(f"/api/cycles/{run_id}")).json()
    assert det["report_source"] == "checkpoint" and det["report"]["symbols"]["XRP-USDT"] is not None
    assert det["status"] == "ran" and det["detail"] == "XRP-USDT=no_hypothesis"
    # un run que solo esta en los checkpoints (antes del cycle_log) tambien aparece
    other = "cycle-1792210342356"
    async with sqlite_checkpointer(tmp_path / "ck.sqlite") as saver:
        graph = build_graph(ScriptedChatModel(full_script(["XRP-USDT"])), checkpointer=saver)
        await graph.ainvoke({**state, "run_id": other}, make_run_config(["XRP-USDT"], other))
    only = (await c.get(f"/api/cycles/{other}")).json()
    assert only["report_source"] == "checkpoint" and only["decisions"][0]["outcome"] == "no_hypothesis"
    assert only["ts_ms"] == 1792210342356


async def test_analysis_latest(tmp_path) -> None:
    ctx, _ = await _full_ctx(tmp_path)
    body = (await _client(ctx).get("/api/analysis/latest")).json()
    assert body["symbols"][SYM]["run_id"] == "cycle-1000" and body["symbols"][SYM]["decision"]["outcome"] == "hypothesis"
    assert body["last_ran"]["run_id"] == "cycle-1000"


# ---------------------------------------------------------------------------- noticias

async def test_news_from_db_and_window(tmp_path) -> None:
    ctx, _ = await _full_ctx(tmp_path)
    c = _client(ctx)
    body = (await c.get("/api/news")).json()
    assert body["total"] == 4 and body["sources"] == {"db": 3, "window_only": 1}
    assert [n["item_id"] for n in body["items"]] == ["solo-memoria", "btc", "guardada", "vieja"]
    assert body["items"][0]["stored"] is False and body["items"][1]["stored"] is True
    assert body["items"][1]["published_at"].endswith("+00:00") and body["items"][1]["primary"] is True
    xrp = (await c.get("/api/news", params={"symbol": "XRPUSDT"})).json()
    assert [n["item_id"] for n in xrp["items"]] == ["solo-memoria", "guardada", "vieja"]
    paged = (await c.get("/api/news", params={"symbol": "XRP", "limit": 1, "offset": 1})).json()
    assert paged["total"] == 3 and [n["item_id"] for n in paged["items"]] == ["guardada"]
    assert (await c.get("/api/news", params={"primary": "true"})).json()["total"] == 1
    assert (await c.get("/api/news", params={"source": "sec"})).json()["items"][0]["item_id"] == "btc"
    assert (await c.get("/api/news", params={"q": "vieja"})).json()["total"] == 1
    assert (await c.get("/api/news", params={"q": "100%"})).json()["total"] == 0  # comodines escapados
    assert (await c.get("/api/news", params={"since": str(M0 + 1)})).json()["total"] == 3
    assert (await c.get("/api/news", params={"q": "x"})).status_code == 422


# ---------------------------------------------------------------------------- historia

async def test_history_summary(tmp_path) -> None:
    ctx, _ = await _full_ctx(tmp_path)
    body = (await _client(ctx).get("/api/history/summary")).json()
    assert body["store"]["bars_1m"]["rows"] == 20 and body["store"]["liquidations"]["rows"] == 3
    assert body["store"]["news"]["rows"] == 3 and body["last_upload"] is None and "recorder" in body


async def test_history_bars_aggregation(tmp_path) -> None:
    ctx, _ = await _full_ctx(tmp_path)
    c = _client(ctx)
    p = {"symbol": "SOLUSDT", "since": str(M0), "until": str(M0 + 600_000)}
    one = (await c.get("/api/history/bars", params={**p, "exchange": "binance"})).json()
    assert one["count"] == 10 and one["bars"][0]["open"] == 100.0 and one["exchanges"] == ["binance"]
    five = (await c.get("/api/history/bars", params={**p, "exchange": "binance", "tf": "5m"})).json()
    b0, b1 = five["bars"]
    assert (b0["open"], b0["high"], b0["low"], b0["close"]) == (100.0, 106.0, 99.0, 105.0)
    assert b1["open"] == 105.0 and b1["close"] == 110.0 and b0["minutes"] == 5
    assert b0["buy_usd"] == 50.0 and b0["delta_usd"] == 30.0 and b0["liq_long_usd"] == 100.0 and b0["n_liqs"] == 1
    assert b0["mark"] == 104.5 and b0["t_ms"] == M0 and b0["t"].endswith("+00:00")
    both = (await c.get("/api/history/bars", params={**p, "tf": "5m"})).json()
    assert both["count"] == 4 and both["exchanges"] == ["binance", "bybit"]
    assert both["bars"][1]["funding_8h"] == 0.0001 and both["bars"][1]["oi_usd"] == 1e6
    comb = (await c.get("/api/history/bars", params={**p, "tf": "5m", "combine": "true"})).json()
    c0 = comb["bars"][0]
    assert comb["count"] == 2 and c0["exchange"] == "all" and c0["ref_exchange"] == "binance"
    assert c0["open"] == 100.0 and c0["buy_usd"] == 100.0 and c0["liq_long_usd"] == 200.0
    assert c0["oi_usd"] == 1e6 and c0["oi_exchanges"] == 1 and c0["funding_8h"] == 0.0001
    # since se alinea al borde de la temporalidad
    mid = (await c.get("/api/history/bars", params={"symbol": SYM, "exchange": "binance", "tf": "5m",
                                                    "since": str(M0 + 120_000), "until": str(M0 + 600_000)})).json()
    assert mid["since_ms"] == M0 and mid["count"] == 2


async def test_history_bars_validation(tmp_path) -> None:
    ctx, _ = await _full_ctx(tmp_path)
    c = _client(ctx)
    assert (await c.get("/api/history/bars")).status_code == 422  # symbol obligatorio
    assert (await c.get("/api/history/bars", params={"symbol": SYM, "tf": "3m"})).status_code == 422
    assert (await c.get("/api/history/bars", params={"symbol": SYM, "exchange": "kraken"})).status_code == 422
    r = await c.get("/api/history/bars", params={"symbol": SYM, "since": "2020-01-01", "until": "2026-01-01"})
    assert r.status_code == 400 and "demasiado grande" in r.json()["detail"]
    default = (await c.get("/api/history/bars", params={"symbol": SYM})).json()
    assert default["until_ms"] - default["since_ms"] <= 500 * 60_000 + 60_000 and default["count"] == 0


async def test_history_liquidations(tmp_path) -> None:
    ctx, _ = await _full_ctx(tmp_path)
    c = _client(ctx)
    body = (await c.get("/api/history/liquidations")).json()
    assert body["total"] == 3 and body["items"][0]["symbol"] == "XRP-USDT"  # mas nueva primero
    sol = (await c.get("/api/history/liquidations", params={"symbol": "SOLUSDT"})).json()
    assert sol["total"] == 2 and sol["totals"] == {"n": 2, "long_usd": 200.0, "short_usd": 10_100.0}
    assert sol["items"][0]["position"] == "short" and sol["items"][1]["position"] == "long"
    assert (await c.get("/api/history/liquidations", params={"min_usd": 1000})).json()["total"] == 1
    assert (await c.get("/api/history/liquidations", params={"side": "long"})).json()["total"] == 2
    assert (await c.get("/api/history/liquidations", params={"exchange": "okx"})).json()["total"] == 1
    assert (await c.get("/api/history/liquidations", params={"since": str(M0 + 1500)})).json()["total"] == 2
    lim = (await c.get("/api/history/liquidations", params={"limit": 1, "offset": 1})).json()
    assert lim["total"] == 3 and lim["items"][0]["exchange"] == "binance"
    assert (await c.get("/api/history/liquidations", params={"min_usd": -1})).status_code == 422
    assert (await c.get("/api/history/liquidations", params={"limit": 1001})).status_code == 422


# ---------------------------------------------------------------------------- laboratorio

async def test_lab_summary_and_detail(tmp_path) -> None:
    ctx, _ = await _full_ctx(tmp_path)
    c = _client(ctx)
    assert (await c.get("/api/lab")).status_code == 404
    (tmp_path / "lab_summary.json").write_text(
        '{"generated_at": "2026-10-17T00:00:00Z", "strategies": [{"strategy_id": "x-v1", "sharpe": NaN, '
        '"verdict": {"pasa": true}}]}')
    lab = (await c.get("/api/lab")).json()
    assert lab["strategies"][0]["sharpe"] is None  # NaN -> null (JSON valido)
    assert (await c.get("/api/summary")).json()["lab"] == {"generated_at": "2026-10-17T00:00:00Z", "strategies": 1,
                                                          "passing": 1}
    assert (await c.get("/api/lab/x-v1")).status_code == 404
    (tmp_path / "lab_detail_x-v1.json").write_text(json.dumps(
        {"strategy_id": "x-v1", "equity_daily": {"strategy": [["2026-01-01", 0.01]]}}))
    det = (await c.get("/api/lab/x-v1")).json()
    assert det["equity_daily"]["strategy"] == [["2026-01-01", 0.01]]
    assert (await c.get("/api/lab/..%2Fsecreto")).status_code in (400, 404)
    assert (await c.get("/api/lab/X_MAYUS")).status_code == 400


# ---------------------------------------------------------------------------- mercado

async def test_market_snapshot(tmp_path) -> None:
    ctx, _ = await _full_ctx(tmp_path)
    c = _client(ctx)
    body = (await c.get("/api/market/SOLUSDT", params={"trades": 1})).json()
    assert body["symbol"] == SYM
    ex = body["exchanges"]
    assert ex["binance"]["mark"] == 150.1 and ex["binance"]["funding_8h"] == 0.0001  # funding del ticker anterior
    assert ex["okx"]["oi_usd"] == pytest.approx(150_200.0) and body["oi_exchanges"] == 1
    assert body["mark_mean"] == pytest.approx(150.15) and body["funding_8h_mean"] == pytest.approx(0.0002)
    assert body["flow_5m"]["binance"]["sell_usd"] == 300.0 and body["flow_5m"]["bybit"]["delta_usd"] == 151.0
    assert len(body["last_trades"]) == 1 and body["last_trades"][0]["exchange"] == "bybit"  # mas nuevo primero
    lw = body["liquidations_window"]
    assert lw["1h"] == {"long_usd": 25_000.0, "short_usd": 0.0, "n": 1,
                        "by_exchange": {"bybit": {"long_usd": 25_000.0, "short_usd": 0.0, "n": 1}}}
    assert lw["24h"]["n"] == 2 and lw["24h"]["short_usd"] == 5_000.0
    assert body["last_liquidations"][0]["position"] == "long"
    assert body["open_hypotheses"] == 1 and body["last_analysis"]["run_id"] == "cycle-1000"
    assert (await c.get("/api/market/sol-usdt")).status_code == 200
    assert (await c.get("/api/market/DOGEUSDT")).status_code == 404
    assert (await c.get("/api/market/SOLUSDT", params={"trades": 501})).status_code == 422


async def test_wallet_reports_holding_vs_live_mark_and_is_optional(tmp_path, monkeypatch) -> None:
    import json

    ctx, _ = await _full_ctx(tmp_path)
    wallet = tmp_path / "wallet.json"
    monkeypatch.setenv("HEATSIGNAL_WALLET_FILE", str(wallet))
    async with _client(ctx) as c:
        assert (await c.get("/api/wallet")).json()["configured"] is False  # sin archivo: no es un error
        wallet.write_text(json.dumps({"holdings": [
            {"asset": "X", "symbol": SYM, "avg_price_usd": 200.0, "qty": 10, "note": "largo plazo"},
            {"asset": "Y", "symbol": "NO-USDT", "avg_price_usd": 1.0}]}))
        body = (await c.get("/api/wallet")).json()
    x, y = body["holdings"]
    assert body["configured"] and x["price_usd"] == pytest.approx(150.15)  # mark medio binance/okx
    assert x["pnl_pct"] == pytest.approx(150.15 / 200 - 1) and x["pnl_usd"] == pytest.approx(-498.5)
    assert y["price_usd"] is None and y["pnl_pct"] is None and y["value_usd"] is None
