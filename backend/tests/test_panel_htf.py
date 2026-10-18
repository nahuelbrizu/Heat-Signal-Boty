"""Panel: contexto 1h por moneda, corte por tendencia/sesion con n e IC, diario de sesiones y vistas (sin red)."""

from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

from heatsignal.api import panel, panel_api
from heatsignal.api.panel import PanelContext, create_panel_app
from heatsignal.evaluation.pending_store import PendingHypothesisStore, ResolutionMetrics
from heatsignal.evaluation.strategy_registry import StrategyHypothesis, StrategyRegistry
from heatsignal.features.sessions import HOUR_MS, parse_sessions
from heatsignal.history.hourly import HourlyCandleProvider
from heatsignal.ingestion.health import HealthRegistry
from heatsignal.pipeline.analysis import AnalysisLoop
from heatsignal.pipeline.session_journal import SessionJournal
from heatsignal.schema.hypotheses import HypothesisStatus, PendingHypothesis, Resolution
from heatsignal.schema.signals import Direction
from heatsignal.storage.memory import MemoryWindowSink

SYM = "XRP-USDT"
APP = Path(panel.__file__).with_name("static") / "app"


@pytest.fixture(autouse=True)
def _clear_caches():
    for c in (panel_api._CACHE, panel._HISTORY_CACHE, panel._PER_SYMBOL_CACHE, panel._LAB_CACHE):
        c.clear()
    yield


def _http(url, timeout):
    now = int(time.time() * 1000)
    last = now - now % HOUR_MS - HOUR_MS
    body = [[last - i * HOUR_MS, "2", "2.1", "1.9", "2.05", "1", last - i * HOUR_MS + HOUR_MS - 1, "1", 1, "1", "1", "0"]
            for i in range(60)][::-1]
    return 200, json.dumps(body).encode()


def _ctx(tmp_path) -> PanelContext:
    db = tmp_path / "eval.sqlite"
    reg = StrategyRegistry(db)
    s = StrategyHypothesis(description="1h", entry_conditions={"scope": "rotation"},
                           exit_conditions={"timeframe": "1h", "horizon_bars": 4}, author="t")
    reg.register(s)
    store = PendingHypothesisStore(db, reg)
    t0 = datetime(2026, 10, 1, 8, 10, tzinfo=timezone.utc)
    for i, (trend, align, sess, win) in enumerate([("alcista", "a_favor", "europa", True),
                                                   ("alcista", "a_favor", "europa", False),
                                                   ("alcista", "en_contra", "eeuu", True)]):
        h = PendingHypothesis(strategy_id=s.strategy_id, symbol=SYM, created_at=t0 + timedelta(hours=5 * i),
                              direction=Direction.LONG if align == "a_favor" else Direction.SHORT, confidence_score=0.6,
                              entry_reference_price=Decimal("2"), horizon_s=3000, rationale="<b>r</b>",
                              timeframe="1h", horizon_bars=1, daily_trend=trend, trend_alignment=align,
                              htf_model_id="htf-trend-v1", session=sess, session_day="2026-10-01")
        store.add(h)
        pnl = 1.0 if win else -1.0
        store.resolve(Resolution(hypothesis_id=h.hypothesis_id, status=HypothesisStatus.RESOLVED,
                                 resolved_at=h.created_at + timedelta(seconds=3000), exit_price=Decimal("2.1"),
                                 pnl_pct=pnl, win=win),
                      ResolutionMetrics(gross_pnl_pct=pnl, cost_bps=10, benchmark_pnl_pct=0.5, alpha_pct=pnl - 0.5))
    old = PendingHypothesis(strategy_id=s.strategy_id, symbol=SYM, created_at=t0, direction=Direction.LONG,
                            confidence_score=0.5, entry_reference_price=Decimal("2"), horizon_s=3600, rationale="vieja")
    store.add(old)
    journal = SessionJournal(db)
    journal.append(day="2026-10-01", session="asia", kind="code", label="Asia (Tokio)", open_ms=1, trigger_ms=2,
                   status="ok", summary="XRP-USDT Asia: <script>x</script>", data={"symbols": {}})
    loop = AnalysisLoop(MemoryWindowSink(), [SYM], reg, htf=HourlyCandleProvider(None, lookback_h=60, http_get=_http),
                        mode="sessions", sessions=parse_sessions(), journal=journal)
    return PanelContext(health=HealthRegistry(), memory=MemoryWindowSink(), analysis=loop, registry=reg, store=store,
                        config={"symbols": ["XRPUSDT"]})


def _client(ctx):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=create_panel_app(ctx)), base_url="http://panel")


async def test_htf_endpoint(tmp_path) -> None:
    async with _client(_ctx(tmp_path)) as c:
        r = await c.get("/api/htf/XRPUSDT")
        assert r.status_code == 200
        d = r.json()
        assert len(d["candles_1h"]) == 48 and d["candles_1h"][-1]["source"] == "rest"
        assert d["daily_trend"] in ("alcista", "bajista", "lateral", "sin_datos")
        assert any("REST de Binance" in g for g in d["data_gaps"]) and d["htf_model_id"] == "htf-trend-v1"
        assert (await c.get("/api/htf/DOGEUSDT")).status_code == 404


async def test_by_trend_endpoint_has_n_and_ci(tmp_path) -> None:
    async with _client(_ctx(tmp_path)) as c:
        d = (await c.get("/api/hypotheses/by-trend", params={"symbol": SYM})).json()
        alc = next(x for x in d["by_daily_trend"] if x["daily_trend"] == "alcista")
        assert alc["n"] == 3 and alc["wins"] == 2 and len(alc["ci95"]) == 2
        fav = next(x for x in d["by_alignment"] if x["trend_alignment"] == "a_favor")
        assert fav["n"] == 2 and fav["rate"] == 0.5
        eu = next(x for x in d["by_session"] if x["session"] == "europa")
        assert eu["n"] == 2
        old = next(x for x in d["by_daily_trend"] if x["daily_trend"] == "sin_registro")
        assert old["total"] == 1 and old["pending"] == 1
        d1h = (await c.get("/api/hypotheses/by-trend", params={"timeframe": "1h"})).json()
        assert d1h["overall"]["total"] == 3
        lst = (await c.get("/api/hypotheses", params={"daily_trend": "alcista", "session": "eeuu"})).json()
        assert lst["total"] == 1 and lst["items"][0]["trend_alignment"] == "en_contra"
        assert lst["items"][0]["timeframe"] == "1h" and lst["items"][0]["session"] == "eeuu"
        assert (await c.get("/api/hypotheses", params={"daily_trend": "nada"})).status_code == 422


async def test_sessions_endpoint(tmp_path) -> None:
    async with _client(_ctx(tmp_path)) as c:
        d = (await c.get("/api/sessions", params={"day": "2026-10-01"})).json()
        assert [s["name"] for s in d["sessions"]] == ["asia", "europa", "eeuu", "cierre"]
        asia = d["sessions"][0]
        assert asia["code"]["summary"].startswith("XRP-USDT Asia") and asia["llm"] is None
        assert d["sessions"][1]["open_at"].startswith("2026-10-01T07:00")  # Londres con BST
        assert d["mode"] == "sessions" and d["days"][0]["day"] == "2026-10-01"
        assert (await c.get("/api/sessions", params={"day": "2026-13-45"})).status_code == 400
        assert (await c.get("/api/sessions", params={"day": "x"})).status_code == 422


async def test_views_are_served_and_escape_text(tmp_path) -> None:
    async with _client(_ctx(tmp_path)) as c:
        for f in ("htf-ui.js", "views/sesiones.js", "hyp-ui.js", "views/hipotesis.js", "views/moneda.js"):
            r = await c.get("/static/app/" + f)
            assert r.status_code == 200, f
            assert "innerHTML" not in r.text, f  # todo texto (LLM, diario) va como nodos de texto
    main = (APP / "main.js").read_text()
    assert 'sesiones: { title: "Sesiones del día", mod: "sesiones" }' in main
    hyp = (APP / "hyp-ui.js").read_text()
    assert "trendChip(h.daily_trend)" in hyp and "alignChip(h.trend_alignment)" in hyp and "sessionText(h)" in hyp
    assert "/api/hypotheses/by-trend" in (APP / "views" / "hipotesis.js").read_text()
    moneda = (APP / "views" / "moneda.js").read_text()
    assert "htfPanel(ctx, symbol)" in moneda and "ctx.every(60000" in moneda
