"""Hipotesis de 1h con tendencia diaria: prompt, horizonte alineado, entrada por codigo, resolucion con velas de 1h,
corte por tendencia/sesion con n e IC y migracion aditiva de la base vieja. Sin red ni LLM."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from _agent_fakes import ScriptedChatModel, make_feature_set, prompt_text

from heatsignal.agents._common import render_context, render_htf
from heatsignal.agents.market_context_analyst import (
    MarketContextAnalysis,
    MarketContextAnalysisBatch,
    make_market_context_analyst_node,
)
from heatsignal.agents.synthesizer import StrategyProposal, SynthesisBatch, apply_proposals, make_synthesizer_node
from heatsignal.evaluation.hypothesis_log import HypothesisLog
from heatsignal.evaluation.pending_store import PendingHypothesisStore, ResolutionMetrics
from heatsignal.evaluation.price_source import BinanceHourlyClosePriceSource, StaticPriceSource
from heatsignal.evaluation.register_strategy import validate_hourly
from heatsignal.evaluation.report import StrategyReporter
from heatsignal.evaluation.resolver import Resolver
from heatsignal.evaluation.segments import trend_segments, wilson
from heatsignal.evaluation.strategy_registry import StrategyHypothesis, StrategyRegistry
from heatsignal.evaluation.timeframes import hourly_horizon
from heatsignal.schema.features import HtfContext, HtfSetups
from heatsignal.schema.hypotheses import HypothesisStatus, PendingHypothesis, Resolution
from heatsignal.schema.sessions import SessionFlags, SessionSymbolAnalysis
from heatsignal.schema.signals import Direction

SYM = "BTC-USDT"
T = datetime(2026, 10, 18, 10, 17, 30, tzinfo=timezone.utc)


def _htf(trend="alcista", **setups) -> HtfContext:
    return HtfContext(symbol=SYM, ts_ms=int(T.timestamp() * 1000), status="ok", day_start_ms=1_792_195_200_000,
                      day_hours_closed=10, day_open=48_000.0, day_high=50_100.0, day_low=47_900.0, last_close=50_000.0,
                      change_from_open_pct=4.17, change_from_open_atr=4.0, pos_in_day_range=0.95, ema20_1h=49_900.0,
                      dist_ema20_1h_atr=0.2, ema20_1h_slope_atr=0.6, ema10_1d=47_000.0, atr14_1h=500.0,
                      daily_trend=trend, trend_reason="regla", setups=HtfSetups(**setups), sources={"klines": 48})


def _fs(trend="alcista", session=None, **setups):
    return make_feature_set(SYM).model_copy(update={"htf": _htf(trend, **setups), "session": session})


def _strategy(setup="pullback_ema20_1h_a_favor", align="a_favor", **exit_c) -> StrategyHypothesis:
    exit_c = exit_c or {"timeframe": "1h", "horizon_bars": 4}
    return StrategyHypothesis(description=f"1h {setup}", author="t", exit_conditions=exit_c,
                              entry_conditions={"scope": "rotation", "timeframe": "1h", "htf_model_id": "htf-trend-v1",
                                                "htf_setup": setup, "daily_trend_in": ["alcista", "bajista"],
                                                "alignment": align})


def _prop(strategy, direction="LONG"):
    return StrategyProposal(symbol=SYM, decision="hypothesis", strategy_id=str(strategy.strategy_id),
                            direction=direction, confidence=0.6, rationale="en dias similares...")


# ---- prompt ------------------------------------------------------------------------------------------

def test_context_prompt_has_1h_and_daily_trend() -> None:
    fs = _fs(pullback_ema20_1h_a_favor=True)
    text = render_context(fs)
    assert "tendencia diaria (codigo, regla fija): alcista" in text
    assert "SOLO velas de 1h CERRADAS" in text and "retroceso a la EMA20 1h" in text
    assert "EMA10 diaria" in render_htf(fs)


async def test_market_context_and_synthesizer_receive_context_and_journal() -> None:
    fs = _fs(pullback_ema20_1h_a_favor=True)
    strat = _strategy()
    model = ScriptedChatModel({
        MarketContextAnalysisBatch: [MarketContextAnalysisBatch(analyses=[MarketContextAnalysis(
            symbol=SYM, context_summary="x", regime_commentary="y", confidence=0.5)])],
        SynthesisBatch: [SynthesisBatch(proposals=[_prop(strat)])],
    })
    await make_market_context_analyst_node(model)({"features_by_symbol": {SYM: fs}})
    state = {"run_id": "r1", "features_by_symbol": {SYM: fs}, "allowed_strategies": [strat],
             "session": {"name": "europa", "label": "Europa (Londres)", "day": "2026-10-18"},
             "day_journal": "### Asia (Tokio) — codigo\nBTC-USDT Asia: vela de apertura verde"}
    out = await make_synthesizer_node(model)(state)
    ctx_prompt = prompt_text(model.calls_for(MarketContextAnalysisBatch)[0])
    syn_prompt = prompt_text(model.calls_for(SynthesisBatch)[0])
    assert "daily_trend" in ctx_prompt or "tendencia diaria" in ctx_prompt
    assert "daily_trend=alcista" in syn_prompt and "pullback_ema20_1h_a_favor" in syn_prompt
    assert "Diario del dia" in syn_prompt and "vela de apertura verde" in syn_prompt
    assert "velas de 1h CERRADAS" in syn_prompt  # instruccion del sistema
    [h] = out["pending_hypotheses"]
    assert h.session == "europa" and h.session_day == "2026-10-18"
    assert len(model.calls) == 2  # una llamada por nodo, como siempre


# ---- horizonte y validacion por codigo --------------------------------------------------------------

def test_hourly_horizon_aligns_to_candle_close() -> None:
    hs, bars = hourly_horizon({"timeframe": "1h", "horizon_bars": 4}, T)
    due = T + timedelta(seconds=hs)
    assert bars == 4 and due == datetime(2026, 10, 18, 15, 0, tzinfo=timezone.utc)
    hs, bars = hourly_horizon({"timeframe": "1h", "horizon": "end_of_day_utc", "min_bars": 2}, T)
    assert T + timedelta(seconds=hs) == datetime(2026, 10, 19, tzinfo=timezone.utc) and bars == 13
    late = datetime(2026, 10, 18, 22, 40, tzinfo=timezone.utc)
    assert isinstance(hourly_horizon({"timeframe": "1h", "horizon": "end_of_day_utc", "min_bars": 2}, late), str)
    assert isinstance(hourly_horizon({"timeframe": "1h"}, T), str)


def test_apply_proposals_1h_records_trend_and_alignment() -> None:
    strat = _strategy()
    hyps, dec = apply_proposals([_prop(strat)], {SYM: _fs(pullback_ema20_1h_a_favor=True)}, [strat],
                                run_id="r", now=T)
    [h] = hyps
    assert dec[0].outcome == "hypothesis"
    assert h.timeframe == "1h" and h.horizon_bars == 4 and h.daily_trend == "alcista"
    assert h.trend_alignment == "a_favor" and h.htf_model_id == "htf-trend-v1"
    assert (h.created_at + timedelta(seconds=h.horizon_s)).minute == 0
    assert h.htf_snapshot["setups"]["pullback_ema20_1h_a_favor"] is True


@pytest.mark.parametrize("fs_kw, direction, needle", [
    ({"pullback_ema20_1h_a_favor": False}, "LONG", "no se cumple"),
    ({"pullback_ema20_1h_a_favor": True}, "SHORT", "exige a_favor"),
])
def test_entry_conditions_are_enforced_by_code(fs_kw, direction, needle) -> None:
    strat = _strategy()
    hyps, dec = apply_proposals([_prop(strat, direction)], {SYM: _fs(**fs_kw)}, [strat], run_id="r", now=T)
    assert hyps == [] and dec[0].outcome == "rejected" and needle in dec[0].reason


def test_lateral_trend_rejects_trend_strategies() -> None:
    strat = _strategy()
    _, dec = apply_proposals([_prop(strat)], {SYM: _fs("lateral", pullback_ema20_1h_a_favor=True)}, [strat],
                             run_id="r", now=T)
    assert dec[0].outcome == "rejected" and "lateral" in dec[0].reason


def test_session_setup_requires_recent_flagged_session() -> None:
    strat = _strategy("apertura_continuacion_a_favor")
    strat.entry_conditions["sessions"] = ["europa", "eeuu"]
    t_ms = int(T.timestamp() * 1000)
    sess = SessionSymbolAnalysis(symbol=SYM, session="europa", day="2026-10-18", role="open", open_ms=t_ms - 75 * 60_000,
                                 trigger_ms=t_ms - 15 * 60_000, status="ok", daily_trend="alcista",
                                 flags=SessionFlags(apertura_continuacion_a_favor=True))
    hyps, _ = apply_proposals([_prop(strat)], {SYM: _fs(session=sess)}, [strat], run_id="r", now=T)
    assert len(hyps) == 1
    old = sess.model_copy(update={"trigger_ms": t_ms - 3 * 3_600_000})
    _, dec = apply_proposals([_prop(strat)], {SYM: _fs(session=old)}, [strat], run_id="r2", now=T)
    assert dec[0].outcome == "rejected" and "pasaron mas de" in dec[0].reason
    asia = sess.model_copy(update={"session": "asia"})
    _, dec = apply_proposals([_prop(strat)], {SYM: _fs(session=asia)}, [strat], run_id="r3", now=T)
    assert "aplica a" in dec[0].reason


def test_strategy_json_files_validate() -> None:
    from pathlib import Path

    files = sorted((Path(__file__).parents[1] / "strategies" / "tendencia-diaria-1h").glob("*.json"))
    assert len(files) == 5
    for f in files:
        st = StrategyHypothesis(**json.loads(f.read_text()))
        assert validate_hourly(st) is None, f
        assert st.entry_conditions["scope"] == "rotation"


# ---- resolucion con velas de 1h ---------------------------------------------------------------------

def _hour_http(open_ms: int, close: str, close_time_ms: int | None = None):
    calls = []

    def get(url, timeout):
        calls.append(url)
        ct = open_ms + 3_600_000 - 1 if close_time_ms is None else close_time_ms
        return 200, json.dumps([[open_ms, "1", "2", "0.5", close, "1", ct, "1", 1, "1", "1", "0"]]).encode()
    return get, calls


def test_hourly_close_source() -> None:
    due = datetime(2026, 10, 18, 15, tzinfo=timezone.utc)
    start = int(due.timestamp() * 1000) - 3_600_000
    get, calls = _hour_http(start, "101.5")
    src = BinanceHourlyClosePriceSource(http_get=get, clock_ms=lambda: start + 7_200_000)
    assert src.price_at(SYM, due) == Decimal("101.5")
    assert "markPriceKlines" in calls[0] and "interval=1h" in calls[0] and f"startTime={start}" in calls[0]
    wrong, _ = _hour_http(start - 3_600_000, "99")
    assert BinanceHourlyClosePriceSource(http_get=wrong, clock_ms=lambda: start + 7_200_000).price_at(SYM, due) is None
    still_open, _ = _hour_http(start, "99")
    assert BinanceHourlyClosePriceSource(http_get=still_open, clock_ms=lambda: start + 60_000).price_at(SYM, due) is None
    with pytest.raises(ValueError):
        src.price_at(SYM, due + timedelta(minutes=1))


def _setup(tmp_path):
    reg = StrategyRegistry(tmp_path / "eval.db")
    s = _strategy()
    reg.register(s)
    return reg, PendingHypothesisStore(tmp_path / "eval.db", reg), s


def test_resolver_uses_1h_candles_only_for_1h_hypotheses(tmp_path) -> None:
    reg, store, s = _setup(tmp_path)
    h1, _ = apply_proposals([_prop(s)], {SYM: _fs(pullback_ema20_1h_a_favor=True)}, [s], run_id="r", now=T)
    store.add(h1[0])
    other = PendingHypothesis(strategy_id=s.strategy_id, symbol="ETH-USDT", created_at=T, direction=Direction.LONG,
                              confidence_score=0.5, entry_reference_price=Decimal("100"), horizon_s=3600, rationale="r")
    store.add(other)
    due1 = h1[0].created_at + timedelta(seconds=h1[0].horizon_s)
    get, calls = _hour_http(int(due1.timestamp() * 1000) - 3_600_000, "51000")
    hourly = BinanceHourlyClosePriceSource(http_get=get, clock_ms=lambda: int(due1.timestamp() * 1000) + 60_000)
    general = StaticPriceSource({"ETH-USDT": [(T + timedelta(hours=1), Decimal("101"))]})
    summary = Resolver(store, general, hourly_price_source=hourly).resolve_due(due1 + timedelta(minutes=1))
    assert summary.resolved == 2 and len(calls) == 1
    rec = store.get_record(h1[0].hypothesis_id)
    assert rec.resolution.exit_price == Decimal("51000") and rec.metrics.exit_price_ts == due1


# ---- corte por tendencia / sesion -------------------------------------------------------------------

def test_wilson_and_segments() -> None:
    lo, hi = wilson(6, 10)
    assert lo == pytest.approx(0.3127, abs=1e-3) and hi == pytest.approx(0.8318, abs=1e-3)
    assert wilson(0, 0) is None
    rows = ([("alcista", "a_favor", "resolved", 1, 1.0, 0.5, 0.5, "europa")] * 6
            + [("alcista", "a_favor", "resolved", 0, -1.0, 0.2, -1.2, "europa")] * 4
            + [("bajista", "en_contra", "expired_no_data", None, None, None, None, "eeuu")]
            + [(None, None, "pending", None, None, None, None, None)])
    seg = trend_segments(rows)
    alc = next(x for x in seg["by_daily_trend"] if x["daily_trend"] == "alcista")
    assert alc["n"] == 10 and alc["wins"] == 6 and alc["ci95"][0] == pytest.approx(0.3127, abs=1e-3)
    assert alc["mean_pnl_pct"] == pytest.approx(0.2) and alc["mean_benchmark_pnl_pct"] == pytest.approx(0.38)
    baj = next(x for x in seg["by_daily_trend"] if x["daily_trend"] == "bajista")
    assert baj["n"] == 0 and baj["ci95"] is None and baj["expired_no_data"] == 1
    assert next(x for x in seg["by_daily_trend"] if x["daily_trend"] == "sin_registro")["pending"] == 1
    assert next(x for x in seg["by_alignment"] if x["trend_alignment"] == "a_favor")["n"] == 10
    eu = next(x for x in seg["by_session"] if x["session"] == "europa")
    assert eu["n"] == 10 and eu["rate"] == pytest.approx(0.6)
    assert seg["overall"]["total"] == 12


def test_store_filters_and_report_segments(tmp_path) -> None:
    reg, store, s = _setup(tmp_path)
    hyps, _ = apply_proposals([_prop(s)], {SYM: _fs(pullback_ema20_1h_a_favor=True)}, [s], run_id="r", now=T,
                              session={"name": "europa", "day": "2026-10-18"})
    h = hyps[0]
    store.add(h)
    due = h.created_at + timedelta(seconds=h.horizon_s)
    store.resolve(Resolution(hypothesis_id=h.hypothesis_id, status=HypothesisStatus.RESOLVED, resolved_at=due,
                             exit_price=Decimal("51000"), pnl_pct=1.9, win=True),
                  ResolutionMetrics(gross_pnl_pct=2.0, cost_bps=10, benchmark_pnl_pct=2.0, alpha_pct=-0.1))
    assert store.query(daily_trend="alcista")[0] == 1 and store.query(trend_alignment="en_contra")[0] == 0
    assert store.query(session="europa")[0] == 1 and store.query(timeframe="none")[0] == 0
    rep = StrategyReporter(store, reg, HypothesisLog(tmp_path / "eval.db")).evaluate(s.strategy_id, due)
    alc = next(x for x in rep.trend_segments["by_daily_trend"] if x["daily_trend"] == "alcista")
    assert alc["n"] == 1 and alc["wins"] == 1 and alc["ci95"] is not None


# ---- migracion de la base vieja ---------------------------------------------------------------------

OLD_SCHEMA = """
CREATE TABLE strategy_hypotheses (strategy_id TEXT PRIMARY KEY, description TEXT NOT NULL, entry_conditions TEXT NOT NULL,
  exit_conditions TEXT NOT NULL, registered_at TEXT NOT NULL, author TEXT NOT NULL);
CREATE TABLE strategy_status_events (id INTEGER PRIMARY KEY AUTOINCREMENT, strategy_id TEXT NOT NULL, status TEXT NOT NULL,
  changed_at TEXT NOT NULL);
CREATE TABLE pending_hypotheses (hypothesis_id TEXT PRIMARY KEY, strategy_id TEXT NOT NULL, symbol TEXT NOT NULL,
  created_at TEXT NOT NULL, created_ts REAL NOT NULL, due_ts REAL NOT NULL, direction TEXT NOT NULL,
  confidence_score REAL NOT NULL, entry_reference_price TEXT NOT NULL, horizon_s INTEGER NOT NULL,
  atr_normalized_trigger REAL, rationale TEXT NOT NULL, heatmap_model_id TEXT, run_id TEXT);
CREATE TABLE hypothesis_resolutions (id INTEGER PRIMARY KEY AUTOINCREMENT, hypothesis_id TEXT NOT NULL UNIQUE,
  status TEXT NOT NULL, resolved_at TEXT NOT NULL, exit_price TEXT, pnl_pct REAL, win INTEGER, gross_pnl_pct REAL,
  cost_bps REAL, benchmark_pnl_pct REAL, alpha_pct REAL, exit_price_ts TEXT);
CREATE TRIGGER trg_pending_hypotheses_no_update BEFORE UPDATE ON pending_hypotheses
BEGIN SELECT RAISE(ABORT, 'append-only'); END;
"""


def test_old_database_still_reads_and_migrates_additively(tmp_path) -> None:
    db = tmp_path / "old.sqlite"
    raw = sqlite3.connect(db)
    raw.executescript(OLD_SCHEMA)
    sid, hid = "11111111-1111-1111-1111-111111111111", "22222222-2222-2222-2222-222222222222"
    raw.execute("INSERT INTO strategy_hypotheses VALUES (?, 'vieja', '{}', '{\"horizon_s\": 3600}', ?, 'nahue')",
                (sid, T.isoformat()))
    raw.execute("INSERT INTO pending_hypotheses VALUES (?, ?, 'XRP-USDT', ?, ?, ?, 'LONG', 0.6, '2.5', 3600, NULL, "
                "'clusters similares', 'liqcluster-v1', 'cycle-1')",
                (hid, sid, T.isoformat(), T.timestamp(), T.timestamp() + 3600))
    raw.execute("INSERT INTO hypothesis_resolutions (hypothesis_id, status, resolved_at, exit_price, pnl_pct, win) "
                "VALUES (?, 'resolved', ?, '2.6', 3.9, 1)", (hid, (T + timedelta(hours=1)).isoformat()))
    # una tabla de contexto de una version intermedia (sin columnas de sesion)
    raw.execute("CREATE TABLE hypothesis_context (hypothesis_id TEXT PRIMARY KEY, timeframe TEXT, horizon_bars INTEGER, "
                "daily_trend TEXT, trend_alignment TEXT, htf_model_id TEXT, htf_snapshot TEXT)")
    raw.commit()
    raw.close()

    reg = StrategyRegistry(db)
    store = PendingHypothesisStore(db, reg)
    total, [rec] = store.query()
    assert total == 1 and rec.hypothesis.symbol == "XRP-USDT" and rec.resolution.win is True
    assert rec.hypothesis.daily_trend is None and rec.hypothesis.timeframe is None and rec.hypothesis.session is None
    seg = trend_segments(store.trend_rows())
    assert next(x for x in seg["by_daily_trend"] if x["daily_trend"] == "sin_registro")["n"] == 1
    assert store.query(daily_trend="none")[0] == 1
    # se pueden agregar hipotesis nuevas con contexto en la misma base
    s = _strategy()
    reg.register(s)
    hyps, _ = apply_proposals([_prop(s)], {SYM: _fs(pullback_ema20_1h_a_favor=True)}, [s], run_id="r", now=T,
                              session={"name": "asia", "day": "2026-10-18"})
    store.add(hyps[0])
    assert store.get_record(hyps[0].hypothesis_id).hypothesis.session == "asia"
    cols = {r[1] for r in sqlite3.connect(db).execute("PRAGMA table_info(hypothesis_context)")}
    assert {"session", "session_day"} <= cols
    with pytest.raises(sqlite3.DatabaseError):
        c = sqlite3.connect(db)
        c.execute("UPDATE hypothesis_context SET daily_trend='bajista'")
