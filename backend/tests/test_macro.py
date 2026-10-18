"""Contexto macro (heatsignal.macro): FRED point-in-time, calendario, Polymarket, fuente apagada sin key, tope del
bloque de contexto, ciclo/grafo sin llamadas extra, endpoints, render de la vista y carga para el laboratorio.
Todo sin red (http_get falsos)."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from heatsignal.macro import calendar as mcal
from heatsignal.macro import context as mctx
from heatsignal.macro import job as mjob
from heatsignal.macro import polymarket as pm
from heatsignal.macro.fred import FredClient, FredError, parse_value
from heatsignal.macro.store import MacroStore, available_ms_for

sys.path.insert(0, str(Path(__file__).parent))

KEY = "abcdef0123456789abcdef0123456789"
H = 3_600_000
D = 86_400_000
VIEW = Path(__file__).parents[1] / "src/heatsignal/api/static/app/views/macro.js"
HARNESS = Path(__file__).parent / "js" / "render_view.mjs"


def ms(s: str) -> int:
    return int(datetime.fromisoformat(s).replace(tzinfo=timezone.utc).timestamp() * 1000)


# ---------------------------------------------------------------- FRED

def _obs(date: str, value: str, rs: str, re: str = "9999-12-31") -> dict:
    return {"realtime_start": rs, "realtime_end": re, "date": date, "value": value}


class FakeFred:
    def __init__(self, by_series: dict[str, list[dict]], status: int = 200):
        self.by_series, self.status, self.urls = by_series, status, []

    def __call__(self, url: str, timeout: float):
        self.urls.append(url)
        q = parse_qs(urlsplit(url).query)
        if self.status != 200:
            return self.status, json.dumps({"error_message": f"Bad Request. api_key={q['api_key'][0]} invalida"}).encode()
        sid = q["series_id"][0]
        rs = q["realtime_start"][0]
        out = []
        for o in self.by_series.get(sid, []):
            o = dict(o)
            if o["realtime_start"] < rs:  # FRED recorta el vintage al inicio de la ventana
                o["realtime_start"] = rs
            out.append(o)
        return 200, json.dumps({"observations": out}).encode()


CPI = [_obs("2026-08-01", "320.0", "2026-09-11", "2026-10-14"), _obs("2026-08-01", "320.5", "2026-10-15"),
       _obs("2026-09-01", "321.0", "2026-10-15")]


def test_fred_value_does_not_exist_before_publication(tmp_path):
    store = MacroStore(tmp_path / "m.sqlite")
    client = FredClient(KEY, http_get=FakeFred({"CPIAUCSL": CPI}))
    rows = client.vintages("CPIAUCSL", "2026-01-01", "2025-01-01")
    store.add_fred(rows, ms("2026-10-16T12:00:00"))
    # publicado el 11/09 (Chicago) -> disponible desde el 12/09 00:00 Chicago = 05:00 UTC
    assert available_ms_for("2026-09-11") == ms("2026-09-12T05:00:00")
    assert store.fred_as_of("CPIAUCSL", ms("2026-09-11T23:00:00")) == []          # el mismo dia de la publicacion: todavia no
    assert store.fred_as_of("CPIAUCSL", ms("2026-09-12T04:59:59")) == []
    before = store.fred_as_of("CPIAUCSL", ms("2026-09-12T05:00:00"))
    assert [(o.obs_date, o.value) for o in before] == [("2026-08-01", 320.0)]
    # la revision del 15/10 NO reescribe el pasado: el 14/10 se sigue viendo 320.0 y no existe septiembre
    mid = store.fred_as_of("CPIAUCSL", ms("2026-10-15T12:00:00"))
    assert [(o.obs_date, o.value) for o in mid] == [("2026-08-01", 320.0)]
    after = store.fred_as_of("CPIAUCSL", ms("2026-10-16T06:00:00"))
    assert [(o.obs_date, o.value) for o in after] == [("2026-08-01", 320.5), ("2026-09-01", 321.0)]
    # idempotente (append-only): repetir la descarga no agrega filas
    assert store.add_fred(client.vintages("CPIAUCSL", "2026-01-01", "2025-01-01"), ms("2026-10-17T12:00:00")) == 0


def test_fred_clipped_vintages_are_marked_and_not_duplicated(tmp_path):
    store = MacroStore(tmp_path / "m.sqlite")
    fake = FakeFred({"DFF": [_obs("2026-10-01", "4.33", "2026-10-02")]})
    client = FredClient(KEY, http_get=fake)
    assert store.add_fred(client.vintages("DFF", "2026-09-01", "2026-09-01"), 1) == 1
    clipped = client.vintages("DFF", "2026-10-10", "2026-09-01")   # ventana que empieza despues de la publicacion
    assert clipped[0].realtime_clipped and clipped[0].realtime_start == "2026-10-10"
    assert store.add_fred(clipped, 2) == 0  # mismo valor ya guardado con su fecha real: no se suma una copia


def test_fred_missing_value_and_errors_never_leak_the_key(tmp_path):
    assert parse_value(".") is None and parse_value("4.5") == 4.5 and parse_value("") is None
    client = FredClient(KEY, http_get=FakeFred({}, status=400))
    with pytest.raises(FredError) as e:
        client.vintages("DFF", "2026-01-01", "2026-01-01")
    assert KEY not in str(e.value) and "***" in str(e.value)

    def boom(url, timeout):  # aunque un transporte meta la URL (con la key) en el error, sale tapada
        raise FredError("error de red contra api.stlouisfed.org: " + url)
    with pytest.raises(FredError) as e2:
        FredClient(KEY, http_get=boom).vintages("DFF", "2026-01-01", "2026-01-01")
    assert KEY not in str(e2.value)


def test_fred_without_key_is_off_and_says_so(tmp_path, monkeypatch):
    monkeypatch.delenv("FRED_API_KEY", raising=False)
    store = MacroStore(tmp_path / "m.sqlite")
    res = mjob.run_fred(store, FredClient(None))
    assert res["status"] == "off" and "FRED_API_KEY" in res["detail"]
    runs = store.last_runs()["fred"]
    assert runs[0]["status"] == "off"
    st = mctx.source_status(store, ms("2026-10-19T12:00:00"), None, None)
    assert st["fred"]["state"] == "apagado" and st["fred"]["key_loaded"] is False and "FRED_API_KEY" in st["fred"]["detail"]
    block = mctx.build_block(store, ms("2026-10-19T12:00:00"), calendar=mcal.load_calendar())
    assert "FRED: apagado (falta FRED_API_KEY)" in block


def test_fred_run_with_key_stores_all_series(tmp_path, monkeypatch):
    monkeypatch.setenv("FRED_API_KEY", KEY)
    store = MacroStore(tmp_path / "m.sqlite")
    data = {"DFF": [_obs("2026-10-15", "4.33", "2026-10-16"), _obs("2026-10-16", ".", "2026-10-17")],
            "CPIAUCSL": CPI, "DGS10": [_obs("2026-10-16", "4.10", "2026-10-17")],
            "DTWEXBGS": [_obs("2026-10-10", "120.3", "2026-10-14")], "VIXCLS": [_obs("2026-10-16", "16.2", "2026-10-17")]}
    fake = FakeFred(data)
    res = mjob.run_fred(store, FredClient(KEY, http_get=fake), now=datetime(2026, 10, 19, 12, tzinfo=timezone.utc))
    assert res["status"] == "ok" and res["rows"] == 8
    assert all("file_type=json" in u and "realtime_end=9999-12-31" in u for u in fake.urls)
    st = mctx.source_status(store, ms("2026-10-19T12:00:00"), None, None)
    assert st["fred"]["state"] == "ok" and st["fred"]["key_loaded"]
    sums = {s["series_id"]: s for s in mctx.fred_summaries(store, ms("2026-10-19T12:00:00"))}
    assert sums["DFF"]["value"] == 4.33 and sums["DFF"]["realtime_start"] == "2026-10-16"  # el "." no es un valor
    assert round(sums["CPIAUCSL"]["change_1"], 3) == round((321.0 / 320.5 - 1) * 100, 3)


# ---------------------------------------------------------------- calendario

def test_calendar_file_is_valid_and_converts_with_dst():
    cal = mcal.load_calendar()
    by = {(e.kind, datetime.fromtimestamp(e.ts_ms / 1000, tz=timezone.utc).date().isoformat()): e for e in cal.events}
    assert by[("FOMC", "2026-10-28")].ts_ms == ms("2026-10-28T18:00:00")   # 14:00 EDT
    assert by[("CPI", "2026-11-10")].ts_ms == ms("2026-11-10T13:30:00")    # 08:30 EST (despues del cambio de hora)
    assert by[("NFP", "2026-11-06")].ts_ms == ms("2026-11-06T13:30:00")
    assert by[("FOMC", "2027-01-27")].tentative and not by[("FOMC", "2026-12-09")].tentative
    assert set(cal.sources) == {"FOMC", "CPI", "NFP"} and all(u.startswith("https://www.") for u in cal.sources.values())
    up = cal.upcoming(ms("2026-10-27T00:00:00"), 48)
    assert [e.kind for e in up] == ["FOMC"]
    assert cal.upcoming(ms("2026-10-20T00:00:00"), 48) == []
    assert cal.next_events(ms("2026-10-20T00:00:00"), 1)[0].kind == "FOMC"
    assert cal.coverage_warning(ms("2026-10-19T00:00:00")) is None
    assert "actualizar" in cal.coverage_warning(ms("2027-11-20T00:00:00"))


def test_calendar_rejects_bad_entries():
    with pytest.raises(ValueError):
        mcal.parse_calendar({"sources": {"FOMC": "x"}, "events": [{"kind": "ECB", "date": "2026-11-01"}]})
    with pytest.raises(ValueError):
        mcal.parse_calendar({"sources": {}, "events": [{"kind": "CPI", "date": "2026-11-01"}]})
    with pytest.raises(ValueError):
        mcal.parse_calendar({"sources": {"CPI": "x"}, "events": [{"kind": "CPI", "date": "11/01/2026"}]})


# ---------------------------------------------------------------- Polymarket

NOW = ms("2026-10-19T14:07:00")


def _market(mid, q, p, vol, *, closed=False, end="2026-10-31T12:00:00Z", as_str=True):
    return {"id": mid, "slug": f"m-{mid}", "question": q, "outcomes": json.dumps(["Yes", "No"]),
            "outcomePrices": json.dumps([str(p), str(round(1 - p, 3))]), "volumeNum": str(vol) if as_str else vol,
            "liquidityNum": 5000, "volume24hr": 900.5, "oneDayPriceChange": "0.03", "bestBid": 0.4, "bestAsk": "0.42",
            "endDate": end, "closed": closed, "active": True, "clobTokenIds": json.dumps([f"tok{mid}", f"tokn{mid}"])}


class FakeGamma:
    def __init__(self):
        self.calls = []
        self.events = {
            "Fed decision": [
                {"slug": "fed-decision-in-october", "title": "Fed decision in October?", "volume": 9e6, "closed": False,
                 "endDate": "2026-10-28T18:00:00Z", "markets": [
                     _market("1", "No change", 0.15, 2e6), _market("2", "25 bps decrease", 0.82, 5e6),
                     _market("3", "50+ bps decrease", 0.03, 1e5), _market("4", "Old", 0.5, 9e9, closed=True)]},
                {"slug": "fed-decision-2025", "title": "Fed decision in December 2025?", "volume": 1e9, "closed": True,
                 "markets": []},
                {"slug": "rate-hike", "title": "Will the ECB hike?", "volume": 1e10, "closed": False, "markets": []}],
            "Bitcoin above": [
                {"slug": "bitcoin-above-on-october-31", "title": "Bitcoin above ___ on October 31?", "volume": 3e6,
                 "endDate": "2026-10-31T16:00:00Z", "markets": [
                     _market("10", "Bitcoin above 100k on Oct 31?", 0.99, 9e6), _market("11", "Bitcoin above 110k on Oct 31?", 0.41, 2e6),
                     _market("12", "Bitcoin above 120k on Oct 31?", 0.12, 1e6, as_str=False)]}],
        }

    def __call__(self, url, timeout):
        self.calls.append(url)
        u = urlsplit(url)
        q = parse_qs(u.query)
        if u.path == "/public-search":
            return 200, json.dumps({"events": self.events.get(q["q"][0], []), "tags": [], "pagination": {"hasMore": False}}).encode()
        if u.path.startswith("/markets/slug/"):
            slug = u.path.rsplit("/", 1)[1]
            if slug == "cerrado":
                return 200, json.dumps(_market("99", "x", 0.5, 1, closed=True)).encode()
            return 404, b'{"error":"not found"}'
        if u.path == "/prices-history":
            return 200, json.dumps({"history": [{"t": 1760000000, "p": 0.4}, {"t": 1760003600, "p": "0.45"}]}).encode()
        return 500, b"boom"


def _cfg(tmp_path, entries, max_total=12):
    p = tmp_path / "pm.json"
    p.write_text(json.dumps({"config_id": "test-v1", "max_markets_total": max_total, "entries": entries}))
    return pm.load_config(p)


def test_versioned_polymarket_config_is_valid():
    cfg = pm.load_config()
    assert cfg.config_id and cfg.entries and cfg.max_markets_total <= 20
    assert {e.key for e in cfg.entries} >= {"fed", "btc"}


def test_polymarket_collect_parses_and_picks(tmp_path):
    cfg = _cfg(tmp_path, [
        {"key": "fed", "label": "Fed", "kind": "search", "q": "Fed decision", "must_match": ["fed"], "max_markets": 2,
         "end_within_days": 30},
        {"key": "btc", "label": "BTC", "kind": "search", "q": "Bitcoin above", "must_match": ["bitcoin"], "max_markets": 2,
         "end_within_days": 30, "prefer_uncertain": True},
        {"key": "fijo", "label": "x", "kind": "market", "slug": "cerrado"},
        {"key": "nada", "label": "x", "kind": "market", "slug": "no-existe"}])
    rows, errors = pm.collect(pm.PolymarketClient(http_get=FakeGamma()), cfg, NOW)
    fed = [r for r in rows if r["config_key"] == "fed"]
    assert [r["question"] for r in fed] == ["25 bps decrease", "No change"]   # por volumen, sin el cerrado
    assert fed[0]["prob"] == 0.82 and fed[0]["volume"] == 5e6 and fed[0]["liquidity"] == 5000 and fed[0]["best_ask"] == 0.42
    assert fed[0]["fetched_ms"] == NOW and fed[0]["token_id"] == "tok2" and fed[0]["event_slug"] == "fed-decision-in-october"
    btc = [r["question"] for r in rows if r["config_key"] == "btc"]
    assert "Bitcoin above 100k on Oct 31?" not in btc and len(btc) == 2           # 99 %: poco informativo
    assert any("cerrado" in e for e in errors) and any("no existe" in e for e in errors)


def test_polymarket_total_cap_and_bad_config(tmp_path):
    cfg = _cfg(tmp_path, [{"key": "fed", "label": "Fed", "kind": "search", "q": "Fed decision", "max_markets": 3,
                           "must_match": ["fed"], "end_within_days": 30}], max_total=1)
    rows, errors = pm.collect(pm.PolymarketClient(http_get=FakeGamma()), cfg, NOW)
    assert len(rows) == 1 and any("tope" in e for e in errors)
    nofilter = _cfg(tmp_path, [{"key": "x", "label": "x", "kind": "search", "q": "Fed decision", "end_within_days": 30}])
    rows2, errors2 = pm.collect(pm.PolymarketClient(http_get=FakeGamma()), nofilter, NOW)
    assert rows2 == [] and any("no tiene mercados abiertos" in e for e in errors2)  # explicito, no silencio
    with pytest.raises(ValueError):
        _cfg(tmp_path, [{"key": "a", "kind": "search"}])
    with pytest.raises(ValueError):
        _cfg(tmp_path, [{"key": "a", "kind": "event", "slug": "x"}, {"key": "a", "kind": "event", "slug": "y"}])


def test_polymarket_run_records_errors_explicitly(tmp_path):
    store = MacroStore(tmp_path / "m.sqlite")

    def down(url, timeout):
        raise pm.PolymarketError("error de red contra gamma-api.polymarket.com: URLError")
    res = mjob.run_polymarket(store, pm.PolymarketClient(http_get=down))
    assert res["status"] == "error" and res["errors"]
    assert store.last_runs()["polymarket"][0]["status"] == "error"
    st = mctx.source_status(store, NOW, None, None)
    assert st["polymarket"]["state"] == "error"


def test_polymarket_price_history_backfill_parse():
    pts = pm.PolymarketClient(http_get=FakeGamma()).price_history("tok", 1760000000, 1760007200)
    assert pts == [(1760000000000, 0.4), (1760003600000, 0.45)]


# ---------------------------------------------------------------- bloque de contexto

def _fill(store: MacroStore, n_markets: int = 3, long_q: bool = False):
    rows_now, rows_day = [], []
    for i in range(n_markets):
        q = ("Will the very long question about Bitcoin price thresholds number %d resolve yes " % i) * (3 if long_q else 1)
        base = {"market_id": str(i), "source": "gamma", "slug": f"s{i}", "event_slug": "e", "config_key": "btc" if i else "fed",
                "question": q, "outcome": "Yes", "volume": 1e6 - i, "liquidity": 1e4, "one_day_change": 0.05, "closed": 0}
        rows_now.append({**base, "fetched_ms": NOW, "prob": 0.5 + i / 100})
        if i == 0:
            rows_day.append({**base, "fetched_ms": NOW - D, "prob": 0.40})
    store.add_polymarket(rows_day + rows_now)


def test_block_is_small_point_in_time_and_historical(tmp_path, monkeypatch):
    monkeypatch.setenv("FRED_API_KEY", KEY)
    store = MacroStore(tmp_path / "m.sqlite")
    mjob.run_fred(store, FredClient(KEY, http_get=FakeFred({"CPIAUCSL": CPI, "DFF": [_obs("2026-10-15", "4.33", "2026-10-16")]})),
                  now=datetime(2026, 10, 19, tzinfo=timezone.utc))
    store.add_run("polymarket", NOW, NOW, "ok", 3)
    _fill(store, 3)
    cal = mcal.load_calendar()
    text = mctx.build_block(store, NOW, calendar=cal, labels={"fed": "Decisión de la Fed", "btc": "BTC"})
    assert text.startswith("Macro y eventos") and mctx.estimate_tokens(text) < 400
    assert "fed funds 4,33 %" in text and "publ. 16/10" in text
    assert "+10 pp)" in text                      # variacion contra nuestra lectura de hace 24 h
    assert "+5 pp API)" in text                   # sin lectura propia: la de la API, marcada
    assert "ninguno (próximo: Decisión FOMC" in text
    from heatsignal.agents.guardrails import find_predictive_phrases
    assert find_predictive_phrases(text) == []
    # point-in-time: el 15/10 la serie CPI todavia no tenia septiembre ni la revision, y el 15/10 no hay lecturas de Polymarket
    old = mctx.build_block(store, ms("2026-10-15T12:00:00"), calendar=cal)
    assert "321" not in old and "320,5" not in old and "sin lecturas guardadas" in old
    # una lectura de Polymarket de hace mas de 3 h no se muestra como actual
    stale = mctx.build_block(store, NOW + 4 * H, calendar=cal)
    assert "sin lectura en las últimas 3 h" in stale
    # 48 h antes del FOMC aparece como evento proximo
    assert "Próximas 48 h: Decisión FOMC" in mctx.build_block(store, ms("2026-10-27T12:00:00"), calendar=cal)


def test_block_size_cap_with_worst_case(tmp_path, monkeypatch):
    monkeypatch.setenv("FRED_API_KEY", KEY)
    store = MacroStore(tmp_path / "m.sqlite")
    data = {sid: [_obs(f"2026-10-{d:02d}", f"{1000 + d}.123456", f"2026-10-{d + 1:02d}") for d in range(1, 17)]
            for sid in ("DFF", "DGS10", "DTWEXBGS", "VIXCLS")}
    data["CPIAUCSL"] = [_obs(f"2025-{m:02d}-01", f"{300 + m}.5", f"2025-{m + 1:02d}-12") for m in range(1, 12)] + CPI
    mjob.run_fred(store, FredClient(KEY, http_get=FakeFred(data)), now=datetime(2026, 10, 19, tzinfo=timezone.utc))
    _fill(store, 20, long_q=True)
    cal = mcal.load_calendar()
    text = mctx.build_block(store, ms("2026-11-09T14:07:00"), calendar=cal)  # CPI y NFP cerca, todo lleno
    assert len(text) <= mctx.MAX_BLOCK_CHARS and mctx.estimate_tokens(text) < 400
    assert text.startswith("Macro y eventos")


class FakeProvider:
    def __init__(self, text):
        self.text, self.calls = text, []

    def context_block(self, now_ms):
        self.calls.append(now_ms)
        return self.text


async def test_graph_gets_macro_block_once_without_extra_llm_calls():
    from _agent_fakes import ScriptedChatModel, full_script, make_feature_set, prompt_text

    from heatsignal.agents.graph import build_graph
    from heatsignal.agents.market_context_analyst import MarketContextAnalysisBatch
    from heatsignal.evaluation.strategy_registry import StrategyHypothesis

    syms = ["BTC-USDT", "ETH-USDT"]
    block = "Macro y eventos (macro-context-v1; prueba):\n- FRED: MARCA_MACRO_123"

    async def run(with_macro: bool) -> ScriptedChatModel:
        model = ScriptedChatModel(full_script(syms))
        state = {"run_id": f"r{with_macro}", "features_by_symbol": {s: make_feature_set(s) for s in syms},
                 "allowed_strategies": [StrategyHypothesis(description="x", entry_conditions={},
                                                           exit_conditions={"horizon_s": 3600}, author="t")]}
        if with_macro:
            state["macro_context"] = block
        await build_graph(model, model).ainvoke(state, {"configurable": {"thread_id": f"t{with_macro}"}})
        return model

    a, b = await run(True), await run(False)
    assert len(a.calls) == len(b.calls)
    ctx_prompts = [prompt_text(m) for m in a.calls_for(MarketContextAnalysisBatch)]
    assert len(ctx_prompts) == 1 and ctx_prompts[0].count("MARCA_MACRO_123") == 1
    others = [prompt_text(m) for s, m in a.calls if s is not MarketContextAnalysisBatch]
    assert not any("MARCA_MACRO_123" in p for p in others)   # solo el Analista de Contexto lo recibe


async def test_analysis_loop_passes_macro_block_to_state(tmp_path):
    from features_helpers import SYM
    from test_analysis_loop import Clock, T0, _filled_memory, _registry, _strategy

    from heatsignal.pipeline.analysis import AnalysisLoop

    clock = Clock(T0)
    memory = await _filled_memory(clock)
    seen = []

    async def runner(state, run_id):
        seen.append(state)
        return {}

    prov = FakeProvider("Macro y eventos: MARCA")
    loop = AnalysisLoop(memory, [SYM], _registry(tmp_path, _strategy()), graph_runner=runner, clock_ms=clock, macro=prov)
    assert (await loop.run_cycle()).status == "ran"
    assert seen[0]["macro_context"] == "Macro y eventos: MARCA" and prov.calls == [clock.ms]

    class Broken:
        def context_block(self, now_ms):
            raise RuntimeError("base rota")
    loop2 = AnalysisLoop(memory, [SYM], _registry(tmp_path / "b", _strategy()) if False else _registry(tmp_path, _strategy()),
                         graph_runner=runner, clock_ms=clock, macro=Broken())
    assert (await loop2.run_cycle()).status == "ran"          # el contexto macro nunca frena el ciclo
    assert "no disponible" in seen[-1]["macro_context"]


def test_provider_without_db_still_returns_a_block(tmp_path, monkeypatch):
    monkeypatch.delenv("FRED_API_KEY", raising=False)
    text = mctx.MacroContextProvider(tmp_path / "no-existe.sqlite").context_block(NOW)
    assert "apagado" in text and "Polymarket: sin lecturas" in text and len(text) <= mctx.MAX_BLOCK_CHARS


# ---------------------------------------------------------------- export, endpoints y vista

def test_export_closed_days_once(tmp_path):
    store = MacroStore(tmp_path / "m.sqlite")
    _fill(store, 2)
    out = tmp_path / "export" / "macro"
    today = datetime.fromtimestamp((NOW + D) / 1000, tz=timezone.utc).date()
    done = mjob.export_days(store, out, today)
    assert "2026-10-19" in done and "2026-10-18" in done
    assert (out / "2026/10/19/polymarket_snapshots.csv.gz").exists()
    assert mjob.export_days(store, out, today) == []          # un dia exportado no se rehace
    assert not list(out.rglob("*.tmp"))


def _client(ctx):
    from heatsignal.api.panel import create_panel_app
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=create_panel_app(ctx)), base_url="http://panel")


async def test_macro_endpoint(tmp_path, monkeypatch):
    from heatsignal.api.panel import PanelContext
    from heatsignal.ingestion.health import HealthRegistry

    monkeypatch.delenv("FRED_API_KEY", raising=False)
    db = tmp_path / "m.sqlite"
    store = MacroStore(db)
    _fill(store, 2)
    store.add_run("polymarket", NOW, NOW, "ok", 2)
    mjob.run_fred(store, FredClient(None))
    store.close()
    async with _client(PanelContext(health=HealthRegistry(), db_paths={"macro": db})) as c:
        r = await c.get("/api/macro")
        assert r.status_code == 200
        d = r.json()
        assert d["sources"]["fred"]["state"] == "apagado" and d["sources"]["fred"]["key_loaded"] is False
        assert d["calendar"]["sources"]["FOMC"].startswith("https://www.federalreserve.gov")
        assert d["block"].startswith("Macro y eventos") and d["block_tokens_est"] < 400
        assert isinstance(d["polymarket"], list)
        r2 = await c.post("/api/macro")
        assert r2.status_code == 405                          # solo lectura
    async with _client(PanelContext(health=HealthRegistry(), db_paths={"macro": tmp_path / "nada.sqlite"})) as c:
        d = (await c.get("/api/macro")).json()
        assert "no corrio" in d["db_note"]
    async with _client(PanelContext(health=HealthRegistry())) as c:
        assert 'macro: { title: "Macro y eventos"' in (await c.get("/static/app/main.js")).text
        assert (await c.get("/static/app/views/macro.js")).status_code == 200


@pytest.mark.skipif(shutil.which("node") is None, reason="sin node")
def test_macro_view_renders_and_escapes(tmp_path, monkeypatch):
    from heatsignal.api.panel_macro import macro_view

    monkeypatch.setenv("FRED_API_KEY", KEY)
    db = tmp_path / "m.sqlite"
    store = MacroStore(db)
    mjob.run_fred(store, FredClient(KEY, http_get=FakeFred({"DFF": [_obs("2026-10-15", "4.33", "2026-10-16")]})),
                  now=datetime(2026, 10, 19, tzinfo=timezone.utc))
    evil = "<img src=x onerror=alert(1)>"
    store.add_polymarket([{"market_id": "1", "fetched_ms": NOW - 2 * H, "source": "gamma", "config_key": "fed",
                           "question": evil, "prob": 0.4},
                          {"market_id": "1", "fetched_ms": NOW, "source": "gamma", "config_key": "fed",
                           "question": evil, "prob": 0.5, "volume": 1000.0, "liquidity": 50.0}])
    store.add_run("polymarket", NOW, NOW, "ok", 1)
    store.close()
    d = macro_view(db, NOW)
    f = tmp_path / "data.json"
    f.write_text(json.dumps({"overview": d, "detail": d}, default=str))
    out = subprocess.run(["node", str(HARNESS), str(VIEW), str(f)], capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    res = json.loads(out.stdout)
    for want in ("Estado de las fuentes", "Series de FRED", "DFF", "2026-10-16", "Calendario de eventos", "FOMC",
                 "Polymarket", "50 %", "key cargada"):
        assert want in res["overview"], want
    assert "Bloque que recibe el grafo" in res["detail"] and "Macro y eventos" in res["detail"]
    assert evil in res["overview"] and "img" not in res["tags"] and "svg" in res["tags"]


# ---------------------------------------------------------------- laboratorio (pandas)

def test_lab_loader_is_point_in_time(tmp_path, monkeypatch):
    pd = pytest.importorskip("pandas")
    from heatsignal.lab.macro_features import load_macro_features

    monkeypatch.setenv("FRED_API_KEY", KEY)
    db = tmp_path / "m.sqlite"
    store = MacroStore(db)
    store.add_fred(FredClient(KEY, http_get=FakeFred({"CPIAUCSL": CPI})).vintages("CPIAUCSL", "2026-01-01", "2025-01-01"),
                   ms("2026-10-16T12:00:00"))
    _fill(store, 1)
    store.close()
    idx = pd.DatetimeIndex(["2026-09-12T04:00:00Z", "2026-09-13T00:00:00Z", "2026-10-15T12:00:00Z",
                            "2026-10-16T06:00:00Z", "2026-10-19T14:07:00Z", "2026-10-19T20:00:00Z"])
    f = load_macro_features(db, idx)
    cpi = f["fred_CPIAUCSL"].tolist()
    assert pd.isna(cpi[0])                       # antes de la publicacion: no existe
    assert cpi[1] == 320.0 and cpi[2] == 320.0   # la revision no se filtra hacia atras
    assert cpi[3] == 321.0                       # 16/10: ya esta septiembre
    col = "pm_fed_0"
    assert f[col].iloc[4] == 0.5 and pd.isna(f[col].iloc[3]) and pd.isna(f[col].iloc[5])  # >3 h: viejo
    assert abs(f[col + "_chg24h"].iloc[4] - 0.10) < 1e-9
    # sin look-ahead: borrar todo lo posterior a t no cambia el valor en t
    cut = MacroStore(tmp_path / "cut.sqlite")
    cut.add_fred([o for o in FredClient(KEY, http_get=FakeFred({"CPIAUCSL": CPI})).vintages("CPIAUCSL", "2026-01-01", "2025-01-01")
                  if o.available_ms <= ms("2026-10-15T12:00:00")], 1)
    cut.close()
    g = load_macro_features(tmp_path / "cut.sqlite", idx[:3])
    assert g["fred_CPIAUCSL"].tolist()[1:] == cpi[1:3]
    # tambien lee la carpeta de export
    exp = tmp_path / "exp"
    mjob.export_days(MacroStore(db), exp, datetime(2026, 10, 21, tzinfo=timezone.utc).date())
    h = load_macro_features(exp, idx)
    assert h["pm_fed_0"].iloc[4] == 0.5
