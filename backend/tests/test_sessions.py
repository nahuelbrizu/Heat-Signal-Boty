"""Sesiones de mercado (features/sessions.py, history/session_source.py, pipeline/session_journal.py y el modo
`sessions` del AnalysisLoop): DST, disparo con vela cerrada, diario acumulativo y presupuesto de LLM. Sin red."""

from __future__ import annotations

import asyncio
import json
from datetime import date, datetime, timezone

import pytest
from _agent_fakes import ScriptedChatModel, full_script

from heatsignal.agents.graph import build_graph
from heatsignal.evaluation.strategy_registry import StrategyHypothesis, StrategyRegistry
from heatsignal.features.sessions import (
    DAY_MS,
    HOUR_MS,
    analyze_symbol,
    next_trigger,
    opening_candle,
    opens_for_utc_day,
    parse_sessions,
    prev_open,
    render_journal,
    session_at,
    triggers_between,
)
from heatsignal.history.session_source import SessionDataSource
from heatsignal.history.store import HistoryStore, MinuteBar
from heatsignal.pipeline.analysis import SESSION_GRACE_MS, AnalysisLoop
from heatsignal.pipeline.session_journal import SessionJournal
from heatsignal.storage.memory import MemoryWindowSink

DEFS = parse_sessions()
BY = {d.name: d for d in DEFS}


def ms(y, mo, d, h, mi=0) -> int:
    return int(datetime(y, mo, d, h, mi, tzinfo=timezone.utc).timestamp() * 1000)


# ---- horarios con DST ---------------------------------------------------------------------------------

@pytest.mark.parametrize("name, local_day, utc", [
    ("europa", date(2026, 10, 23), (2026, 10, 23, 7, 0)),    # BST (+1)
    ("europa", date(2026, 10, 26), (2026, 10, 26, 8, 0)),    # GMT tras el 25/10
    ("eeuu", date(2026, 10, 30), (2026, 10, 30, 13, 30)),    # EDT; Europa ya cambio, EE.UU. todavia no
    ("eeuu", date(2026, 11, 2), (2026, 11, 2, 14, 30)),      # EST tras el 1/11
    ("eeuu", date(2026, 3, 6), (2026, 3, 6, 14, 30)),        # EST
    ("eeuu", date(2026, 3, 9), (2026, 3, 9, 13, 30)),        # EDT desde el 8/3
    ("europa", date(2026, 3, 27), (2026, 3, 27, 8, 0)),      # GMT
    ("europa", date(2026, 3, 30), (2026, 3, 30, 7, 0)),      # BST desde el 29/3
    ("asia", date(2026, 7, 1), (2026, 7, 1, 0, 0)),          # Tokio sin DST
    ("cierre", date(2026, 7, 1), (2026, 7, 1, 23, 0)),
])
def test_session_open_with_dst(name, local_day, utc) -> None:
    assert BY[name].open_utc_ms(local_day) == ms(*utc)


def test_order_within_utc_day_and_parse() -> None:
    day = ms(2026, 10, 18, 0)
    assert [d.name for d, _ in opens_for_utc_day(DEFS, day)] == ["asia", "europa", "eeuu", "cierre"]
    custom = parse_sessions("asia=Asia/Tokyo@09:00, cierre=UTC@23:00")
    assert [d.role for d in custom] == ["open", "close"]
    with pytest.raises(ValueError):
        parse_sessions("x=Marte/Olimpo@09:00")
    with pytest.raises(ValueError):
        parse_sessions("x=UTC@25:00")


# ---- disparo solo con la vela cerrada ---------------------------------------------------------------

def test_trigger_only_after_first_candle_closes() -> None:
    o = ms(2026, 10, 18, 13, 30)
    assert not any(d.name == "eeuu" for d, _, _ in triggers_between(DEFS, o, o + HOUR_MS - 1))
    [(d, op, t)] = [x for x in triggers_between(DEFS, o, o + HOUR_MS) if x[0].name == "eeuu"]
    assert op == o and t == o + HOUR_MS
    nd, _, nt = next_trigger(DEFS, o + 10 * 60_000)
    assert nd.name == "eeuu" and nt == o + HOUR_MS
    assert len(triggers_between(DEFS, ms(2026, 10, 18, 0) + 1, ms(2026, 10, 19, 0) + 1)) == 4  # 4 por dia


def test_session_at_and_prev_open() -> None:
    assert session_at(DEFS, ms(2026, 10, 18, 9, 0)) == ("europa", "2026-10-18")
    assert session_at(DEFS, ms(2026, 10, 18, 7, 30)) == ("asia", "2026-10-18")   # Londres todavia no cerro su vela
    assert session_at(DEFS, ms(2026, 10, 19, 0, 30)) == ("cierre", "2026-10-18")
    d, o = prev_open(DEFS, ms(2026, 10, 18, 0))
    assert d.name == "eeuu" and o == ms(2026, 10, 17, 13, 30)


def test_opening_candle_and_flags() -> None:
    o = ms(2026, 10, 18, 7)
    mins = [(o + i * 60_000, 100 + i * 0.1, 100 + i * 0.1 + 0.05, 100 + i * 0.1 - 0.02, 100 + (i + 1) * 0.1, 10.0)
            for i in range(60)]
    oc = opening_candle(mins + [(o + HOUR_MS, 1, 1, 1, 1, 1)], o, atr=2.0, vol_history=[300.0, 300.0])
    assert oc.minutes == 60 and oc.close == pytest.approx(106.0) and oc.body_frac > 0.9
    assert oc.vol_ratio == pytest.approx(2.0) and oc.range_atr == pytest.approx((oc.high - oc.low) / 2.0)
    from heatsignal.schema.features import HtfContext

    htf = HtfContext(symbol="X", ts_ms=o, status="ok", day_start_ms=o - o % DAY_MS, daily_trend="alcista",
                     day_open=95.0, last_close=100.0)
    a = analyze_symbol(BY["europa"], "XRP-USDT", o, oc, htf, ("asia", 101.0, 99.0), None)
    assert a.flags.apertura_continuacion_a_favor and a.broke_prev_range == "arriba"
    assert not a.flags.apertura_reversion_en_contra  # cerro por encima del maximo anterior
    assert "vela de apertura verde" in a.summary
    wick = oc.model_copy(update={"high": 107.0, "close": 104.5})  # rompe el maximo previo y vuelve a entrar
    rev = analyze_symbol(BY["europa"], "XRP-USDT", o, wick, htf, ("asia", 105.0, 99.0), None)
    assert rev.flags.apertura_reversion_en_contra
    miss = analyze_symbol(BY["europa"], "XRP-USDT", o, None, None, None, None)
    assert miss.status == "missing" and any("sin minutos" in g for g in miss.data_gaps)


# ---- fuente de datos (base + REST) ------------------------------------------------------------------

def test_session_source_builds_from_db_and_fills_with_rest(tmp_path) -> None:
    sym = "XRP-USDT"
    o = BY["eeuu"].open_utc_ms(date(2026, 10, 16))  # 13:30 UTC
    st = HistoryStore(tmp_path / "h.sqlite")
    rows = [("binance", sym, "1m", o + i * 60_000, 2.0, 2.01, 1.99, 2.005, 1.0, 50.0, 3, 25.0) for i in range(50)]
    for k in range(1, 4):  # misma hora local, dias previos (volumen de referencia)
        ok = BY["eeuu"].open_utc_ms(date(2026, 10, 16 - k))
        rows += [("binance", sym, "1m", ok + i * 60_000, 2.0, 2.0, 2.0, 2.0, 1.0, 25.0, 1, 1.0) for i in range(60)]
    st.add_klines(rows)
    day0 = o - o % DAY_MS
    st.upsert_bars([MinuteBar("bybit", sym, day0 + i * 60_000, buy_usd=10.0, sell_usd=4.0, liq_long_usd=1.0, n_liqs=1,
                              funding_8h=0.0001, oi_usd=1000.0 + i) for i in range(10)])
    st.close()
    calls = []

    def http(url, timeout):
        calls.append(url)
        body = [[o + i * 60_000, "2", "2.2", "1.9", "2.1", "1", o + i * 60_000 + 59_999, "40", 1, "1", "1", "0"]
                for i in range(60)]
        return 200, json.dumps(body).encode()

    src = SessionDataSource(tmp_path / "h.sqlite", None, http_get=http)
    an = src.analyze(DEFS, BY["eeuu"], o, [sym], now_ms=o + HOUR_MS + 120_000)
    a = an.symbols[sym]
    assert a.opening.minutes == 60 and a.opening.sources == {"klines": 50, "rest": 10}
    assert a.opening.vol_days == 3 and a.opening.vol_ratio == pytest.approx((50 * 50 + 10 * 40) / 1500)
    assert any("REST de Binance" in g for g in a.data_gaps)
    assert a.own.cvd_usd == pytest.approx(60.0) and a.own.oi_change_pct == pytest.approx(0.9, abs=0.01)
    assert a.own.funding_8h_mean == pytest.approx(0.0001)
    assert any("interval=1m" in c for c in calls)


# ---- diario acumulativo + modo sessions + presupuesto ----------------------------------------------

class FakeSource:
    def __init__(self) -> None:
        self.calls = []

    def analyze(self, defs, d, open_ms, symbols, now_ms):
        from heatsignal.features.sessions import build_analysis

        self.calls.append((d.name, open_ms, now_ms))
        assert now_ms >= open_ms + HOUR_MS  # nunca antes de que cierre la vela
        return build_analysis(d, open_ms, {s: analyze_symbol(d, s, open_ms, None, None, None, None,
                                                             [f"prueba {d.name}"]) for s in symbols})


def _loop(tmp_path, clock, runner=None, strategies=True):
    reg = StrategyRegistry(tmp_path / "eval.db")
    if strategies:
        reg.register(StrategyHypothesis(description="1h", entry_conditions={"scope": "rotation"},
                                        exit_conditions={"timeframe": "1h", "horizon_bars": 4}, author="t"))
    journal = SessionJournal(tmp_path / "eval.db")
    loop = AnalysisLoop(MemoryWindowSink(), ["BTC-USDT", "XRP-USDT"], reg, graph_runner=runner, mode="sessions",
                        sessions=DEFS, session_source=FakeSource(), journal=journal, clock_ms=clock,
                        skip_during_warmup=False)
    return loop, journal


async def test_journal_accumulates_through_the_day(tmp_path) -> None:
    states = []

    async def runner(state, run_id):
        states.append(state)
        return {"pending_hypotheses": [], "synthesis_decisions": [], "report": None,
                "features_by_symbol": state["features_by_symbol"]}

    now = {"t": ms(2026, 10, 18, 1, 2)}
    loop, journal = _loop(tmp_path, lambda: now["t"], runner)
    asia_o = ms(2026, 10, 18, 0)
    await loop.run_session(BY["asia"], asia_o)
    now["t"] = ms(2026, 10, 18, 8, 2)
    await loop.run_session(BY["europa"], ms(2026, 10, 18, 7))
    assert set(states[0]["features_by_symbol"]) == {"BTC-USDT", "XRP-USDT"}  # todas las criptos, un solo ciclo
    assert states[0]["session"]["name"] == "asia"
    assert "Asia (Tokio)" in states[0]["day_journal"]  # su propio analisis por codigo ya esta en el diario
    assert "Asia (Tokio)" in states[1]["day_journal"] and "Europa (Londres)" in states[1]["day_journal"]
    fs = states[1]["features_by_symbol"]["XRP-USDT"]
    assert fs.session is not None and fs.session.session == "europa"
    entries = journal.day("2026-10-18")
    assert [(e["session"], e["kind"]) for e in entries] == [("asia", "code"), ("asia", "llm"), ("europa", "code"),
                                                             ("europa", "llm")]
    assert entries[1]["llm_status"] == "sin_llm"  # el runner no devolvio reporte del Redactor
    # re-correr la misma sesion no duplica ni pisa
    await loop.run_session(BY["asia"], asia_o)
    assert len(journal.day("2026-10-18")) == 4
    assert "Asia" in render_journal(entries)


async def test_without_llm_code_analysis_is_saved_and_marked(tmp_path) -> None:
    loop, journal = _loop(tmp_path, lambda: ms(2026, 10, 18, 14, 32))
    res = await loop.run_session(BY["eeuu"], ms(2026, 10, 18, 13, 30))
    assert res.status == "no_llm"
    code, llm = journal.day("2026-10-18")
    assert code["kind"] == "code" and code["status"] == "missing" and "prueba eeuu" in json.dumps(journal.day(
        "2026-10-18", with_data=True)[0]["data"])
    assert llm["status"] == "sin_llm" and "no_llm" in llm["summary"]


async def test_sessions_mode_runs_due_sessions_once_and_waits(tmp_path) -> None:
    stop = asyncio.Event()
    runs = []

    async def runner(state, run_id):
        runs.append(state["session"]["name"])
        stop.set()
        return {"pending_hypotheses": [], "synthesis_decisions": []}

    clock = ms(2026, 10, 18, 8, 0) + SESSION_GRACE_MS + 1000  # Londres (07:00 UTC con BST) recien cerro
    loop, journal = _loop(tmp_path, lambda: clock, runner)
    assert [d.name for d, _, _ in loop.due_sessions(clock)] == ["europa"]
    assert loop.due_sessions(ms(2026, 10, 18, 8, 0) + 10_000) == []  # dentro de la gracia: todavia no
    await asyncio.wait_for(loop.run_forever(stop), timeout=5)
    assert runs == ["europa"]
    assert journal.has("2026-10-18", "europa", "llm")
    assert loop.due_sessions(clock) == []


async def test_interval_mode_still_writes_code_journal_without_graph(tmp_path) -> None:
    calls = []

    async def runner(state, run_id):
        calls.append(run_id)
        return {}

    loop, journal = _loop(tmp_path, lambda: ms(2026, 10, 18, 8, 5), runner)
    res = await loop.run_session(BY["europa"], ms(2026, 10, 18, 7), with_graph=False)
    assert res is None and calls == []
    assert [e["kind"] for e in journal.day("2026-10-18")] == ["code"]


async def test_budget_session_context_adds_no_llm_calls(tmp_path) -> None:
    """Con contexto 1h y diario del dia el grafo hace EXACTAMENTE las mismas llamadas (solo crece el prompt)."""
    from _agent_fakes import make_feature_set

    from heatsignal.schema.features import HtfContext

    syms = ["BTC-USDT", "ETH-USDT"]

    async def calls_for(extra: bool) -> int:
        model = ScriptedChatModel(full_script(syms))
        graph = build_graph(model, model)
        fs = {s: make_feature_set(s) for s in syms}
        state = {"run_id": f"r-{extra}", "features_by_symbol": fs, "allowed_strategies": [StrategyHypothesis(
            description="x", entry_conditions={}, exit_conditions={"horizon_s": 3600}, author="t")]}
        if extra:
            state["features_by_symbol"] = {s: f.model_copy(update={"htf": HtfContext(
                symbol=s, ts_ms=0, status="ok", day_start_ms=0, daily_trend="alcista")}) for s, f in fs.items()}
            state["day_journal"] = "### Asia — codigo\n" + "x" * 3000
            state["session"] = {"name": "europa", "label": "Europa", "day": "2026-10-18"}
        await graph.ainvoke(state, {"configurable": {"thread_id": f"t-{extra}"}})
        return len(model.calls)

    assert await calls_for(True) == await calls_for(False)
    # 4 sesiones por dia -> 4 corridas del grafo con todas las criptos (antes: 96 con 15 min, 8 con 3 h)
    assert len(triggers_between(DEFS, ms(2026, 10, 20, 0) + 1, ms(2026, 10, 21, 0) + 1)) == 4


def test_journal_is_append_only(tmp_path) -> None:
    import sqlite3

    j = SessionJournal(tmp_path / "e.db")
    assert j.append(day="2026-10-18", session="asia", kind="code", label="Asia", open_ms=1, trigger_ms=2,
                    status="ok", summary="s")
    assert not j.append(day="2026-10-18", session="asia", kind="code", label="Asia", open_ms=1, trigger_ms=2,
                        status="ok", summary="otro")
    assert j.day("2026-10-18")[0]["summary"] == "s"
    c = sqlite3.connect(tmp_path / "e.db")
    with pytest.raises(sqlite3.DatabaseError):
        c.execute("UPDATE session_journal SET summary='x'")
    with pytest.raises(sqlite3.DatabaseError):
        c.execute("DELETE FROM session_journal")
    assert j.days()[0]["sessions"] == 1
