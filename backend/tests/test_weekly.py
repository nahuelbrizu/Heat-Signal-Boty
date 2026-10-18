"""Revision semanal (paper/weekly.py): cierre de la semana UTC, idempotencia, calculos de cada fuente con bases falsas,
muestra chica, endpoint /api/weekly y render de la vista semanal.js en node. Sin red."""

from __future__ import annotations

import json
import shutil
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest

from heatsignal.api.panel import PanelContext, create_panel_app
from heatsignal.evaluation.report import ReportLog
from heatsignal.history.store import HistoryStore
from heatsignal.ingestion.health import HealthRegistry
from heatsignal.paper import weekly as wk
from heatsignal.paper.ft_bridge import BridgeStore, encode_tag
from heatsignal.paperbot.store import PaperbotStore
from heatsignal.pipeline.cycle_log import CycleLog
from heatsignal.pipeline.session_journal import SessionJournal

sys.path.insert(0, str(Path(__file__).parent))
from ft_bridge_fakes import S_1H, S_1H_B, add_hyp, make_eval_db, make_ft_db, resolve_hyp  # noqa: E402

WEEK = "2026-W42"
WS, WE = wk.week_bounds(WEEK)
H = 3_600_000
D = 86_400_000
MIN = 60_000
VIEW = Path(__file__).parents[1] / "src/heatsignal/api/static/app/views/semanal.js"
HARNESS = Path(__file__).parent / "js" / "render_view.mjs"


# ---- semana UTC ------------------------------------------------------------------------------------------


def _ms(s: str) -> int:
    return int(datetime.fromisoformat(s).replace(tzinfo=timezone.utc).timestamp() * 1000)


def test_week_bounds_are_monday_to_monday_utc():
    assert WS == _ms("2026-10-12T00:00:00") and WE == _ms("2026-10-19T00:00:00")
    assert datetime.fromtimestamp(WS / 1000, tz=timezone.utc).weekday() == 0
    assert wk.week_id(WS) == WEEK and wk.week_id(WE - 1) == WEEK
    # semana ISO 1 de 2026 empieza el lunes 2025-12-29
    assert wk.week_bounds("2026-W01")[0] == _ms("2025-12-29T00:00:00")
    for bad in ("2026-42", "2026-W99", "x", ""):
        with pytest.raises(ValueError):
            wk.week_bounds(bad)


def test_last_complete_week():
    assert wk.last_complete_week(_ms("2026-10-19T00:30:00")) == "2026-W42"   # lunes 00:30: cierra la anterior
    assert wk.last_complete_week(_ms("2026-10-19T00:00:00")) == "2026-W42"
    assert wk.last_complete_week(_ms("2026-10-18T23:59:59")) == "2026-W41"   # domingo: la W42 sigue abierta
    assert wk.last_complete_week(_ms("2026-10-21T12:00:00")) == "2026-W42"


def test_incomplete_week_is_refused(env):
    with pytest.raises(ValueError, match="todavia no termino"):
        wk.build(WEEK, env["paths"], now_ms=WE - 1)


# ---- bases falsas ----------------------------------------------------------------------------------------


def _equity(conn, sid, hour, eq, basket, realized=0.0, unreal=0.0, funding=0.0, fees=0.0, slip=0.0):
    conn.execute("INSERT INTO equity VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                 ("r", hour, sid, eq, basket, realized, unreal, funding, fees, slip, 0.5))


def make_paperbot(path: Path) -> Path:
    PaperbotStore(path).close()
    c = sqlite3.connect(path)
    # A: corre desde antes de la semana
    _equity(c, "a-v1", WS - H, 10_100, 10_050, 40, 60, -1, 5, 2)
    _equity(c, "a-v1", WS, 10_120, 10_060, 50, 77, -1, 5, 2)
    _equity(c, "a-v1", WS + D, 10_000, 10_040, 50, -40, -2, 6, 2.4)        # caida intra-semana (DD)
    _equity(c, "a-v1", WE, 10_300, 10_000, 200, 115.6, -3, 9, 3.6)
    _equity(c, "a-v1", WE + H, 99_999, 99_999)                             # posterior al cierre: se ignora
    # B: entra a mitad de semana ("desde hoy"): capital 10.000 propio
    _equity(c, "b-v1", WS + 3 * D, 9_990, 9_995, 0, 0, 0, 7, 3)
    _equity(c, "b-v1", WE, 10_050, 10_020, 0, 60, 1, 8, 3)
    for sid, exit_ms, pnl in (("a-v1", WS - D, 99.0), ("a-v1", WS + 2 * D, 10.0), ("a-v1", WS + 3 * D, -4.0),
                              ("a-v1", WE + H, 50.0), ("b-v1", WS + 4 * D, 3.0)):
        c.execute("INSERT INTO trades (run_id, strategy_id, symbol, direction, entry_ms, exit_ms, entry_price, exit_price, "
                  "max_units, pnl_price, funding, fees, slippage, pnl_net) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                  ("r", sid, "BTCUSDT", "long", exit_ms - H, exit_ms, 1, 1, 1, pnl + 1, 0, 0.7, 0.3, pnl))
    for i, (sid, fill, reason) in enumerate((("a-v1", WS + H, "senal"), ("a-v1", WS + 2 * H, "senal"),
                                             ("b-v1", WS + 3 * D, "inicio"), ("a-v1", WS - H, "senal"))):
        c.execute("INSERT INTO orders (run_id, strategy_id, symbol, decision_ms, fill_ms, price, price_source, weight_from, "
                  "weight_to, units_from, units_to, notional, fee, slippage, reason) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                  (f"r{i}", sid, "BTCUSDT", fill, fill, 1, "open_1m_siguiente", 0, 1, 0, 1, 1, 0, 0, reason))
    for h in range(0, 168):
        st = "error" if h == 30 else "ok"
        if h in (40, 41):
            continue  # dos horas sin corrida (hueco)
        c.execute("INSERT INTO runs VALUES (?,?,?,?,?,?,?,?)", (f"run{h}", WS + h * H, WS + h * H, WS + h * H, st,
                                                                  "hueco de datos: BTCUSDT" if st == "error" else "ok",
                                                                  "{}", 100))
    c.execute("INSERT INTO runs VALUES (?,?,?,?,?,?,?,?)", ("prev", WS - 5 * D, WS - 5 * D, WS - 5 * D, "ok", "ok", "{}", 1))
    c.execute("INSERT INTO gaps VALUES (?,?,?,?)", ("run42", WS + 40 * H, WS + 41 * H, 2))
    c.commit()
    c.close()
    return path


def make_eval(path: Path) -> dict[str, str]:
    make_eval_db(path)
    ids = {}
    c = sqlite3.connect(path)

    def hyp(name, created, resolved=None, win=None, pnl=0.5, strategy=S_1H, session="asia", trend="alcista", align="a_favor"):
        hid = add_hyp(path, f"{name:0<8}-0000-0000-0000-000000000000", strategy=strategy, created_ts=created / 1000,
                      due_ts=created / 1000 + 4 * 3600, timeframe=None)
        c.execute("INSERT INTO hypothesis_context (hypothesis_id, timeframe, horizon_bars, daily_trend, trend_alignment, "
                  "session) VALUES (?,?,?,?,?,?)", (hid, "1h", 4, trend, align, session))
        c.commit()
        if resolved is not None:
            resolve_hyp(path, hid, exit_price=100_500, pnl_pct=pnl if win else -abs(pnl), gross=pnl, win=win,
                        at_ts=resolved / 1000)
        ids[name] = hid
        return hid

    hyp("aaaa1111", WS + D, WS + D + 4 * H, True)
    hyp("bbbb2222", WS + 2 * D, WS + 2 * D + 4 * H, False, session="europa", trend="bajista", align="en_contra")
    hyp("cccc3333", WS - H, WS + 3 * H, True, strategy=S_1H_B)        # emitida antes, resuelta en la semana
    hyp("dddd4444", WE - 2 * H, WE + 2 * H, True)                    # resuelta despues: pendiente al cierre
    hyp("eeee5555", WS - 3 * D, WS - 3 * D + 4 * H, True)            # todo antes: solo acumulado
    c.close()
    cl = CycleLog(path)
    cl.close()
    sj = SessionJournal(path)
    sj.close()
    c = sqlite3.connect(path)
    for i, (ts, st, det) in enumerate(((WS + H, "ran", "ok"), (WS + 2 * H, "ran", "ok"), (WS + 3 * H, "error", "grafo: sin credito"),
                                       (WS + 4 * H, "error", "grafo: sin credito"), (WS + 5 * H, "warmup", "ventana"),
                                       (WE + H, "error", "fuera de la semana"))):
        c.execute("INSERT INTO cycle_log (run_id, ts_ms, status, n_hypotheses, detail, decisions) VALUES (?,?,?,?,?,?)",
                  (f"cycle-{ts}", ts, st, 0, det, "[]"))
    for d, s, st in (("2026-10-12", "asia", "con_llm"), ("2026-10-13", "asia", "error"), ("2026-10-19", "asia", "con_llm")):
        c.execute("INSERT INTO session_journal (day, session, kind, label, open_ms, trigger_ms, created_ms, status, summary) "
                  "VALUES (?,?,?,?,?,?,?,?,?)", (d, s, "llm", s, WS, WS, WS, st, "x"))
    c.commit()
    c.close()
    rl = ReportLog(path)
    rl.close()
    c = sqlite3.connect(path)
    c.execute("INSERT INTO strategy_reports (strategy_id, generated_at, publishable, report) VALUES (?,?,?,?)",
              (S_1H, "2026-10-18T23:55:00+00:00", 0, json.dumps({"publish_blockers": ["shadow_period_not_elapsed"]})))
    c.commit()
    c.close()
    return ids


def make_bridge(bridge: Path, ft: Path, ids: dict[str, str]) -> None:
    st = BridgeStore(bridge)
    h1, h2 = ids["aaaa1111"], ids["bbbb2222"]
    due1 = (WS + D) / 1000 + 4 * 3600
    base = {"run_id": "b1", "strategy_id": S_1H, "strategy_name": "1h-apertura-continuacion-a-favor-v1", "symbol": "BTC-USDT",
            "pair": "BTC/USDT:USDT", "direction": "LONG", "stake_usdt": 1000.0}
    st.add({**base, "ts_ms": WS + D + 5 * MIN, "hypothesis_id": h1, "created_ts": (WS + D) / 1000, "due_ts": int(due1),
            "entry_tag": encode_tag(h1, due1), "status": "sent", "trade_id": 1, "open_rate": 100_010.0})
    st.add({**base, "ts_ms": WS + 2 * D + 5 * MIN, "hypothesis_id": h2, "created_ts": (WS + 2 * D) / 1000,
            "due_ts": int((WS + 2 * D) / 1000 + 4 * 3600), "entry_tag": encode_tag(h2, (WS + 2 * D) / 1000 + 4 * 3600),
            "status": "skipped_pair_busy", "trade_id": None})
    st.close()
    make_ft_db(ft, [{"id": 1, "pair": "BTC/USDT:USDT", "tag": encode_tag(h1, due1), "open_ms": WS + D + 5 * MIN,
                     "open_rate": 100_010.0, "close_ms": WS + D + 4 * H + MIN, "close_rate": 100_400.0, "profit_abs": 3.4,
                     "profit": 0.0034, "exit_reason": "hyp_due"}])


def make_donchian(path: Path) -> None:
    make_ft_db(path, [
        {"id": 1, "pair": "BTC/USDT:USDT", "tag": "donchian", "open_ms": WS - 3 * D, "open_rate": 1, "close_ms": WS - D,
         "close_rate": 1, "profit_abs": 20.0, "profit": 0.02},
        {"id": 2, "pair": "ETH/USDT:USDT", "tag": "donchian", "open_ms": WS + H, "open_rate": 1, "close_ms": WS + 2 * D,
         "close_rate": 1, "profit_abs": 12.5, "profit": 0.0125, "funding": -0.4},
        {"id": 3, "pair": "SOL/USDT:USDT", "tag": "donchian", "open_ms": WS + D, "open_rate": 1, "close_ms": WS + 3 * D,
         "close_rate": 1, "profit_abs": -7.5, "profit": -0.0075},
        {"id": 4, "pair": "XRP/USDT:USDT", "tag": "donchian", "open_ms": WS + 5 * D, "open_rate": 1, "close_ms": WE + D,
         "close_rate": 1, "profit_abs": 100.0, "profit": 0.1},                      # cerrada despues: abierta al cierre
        {"id": 5, "pair": "BNB/USDT:USDT", "tag": "donchian", "open_ms": WS + 6 * D, "open_rate": 1},  # sigue abierta
    ])


def make_history(path: Path) -> None:
    HistoryStore(path).close()
    c = sqlite3.connect(path)
    rows = [("binance", "BTC-USDT", m) for m in range(WS, WE, MIN) if not (WS + 100 * MIN <= m < WS + 130 * MIN)]
    rows += [("bybit", "BTC-USDT", m) for m in range(WS, WS + D, MIN)]
    rows += [("hyperliquid", "BTC-USDT", m) for m in range(WS + 5 * D, WE, MIN)]  # empezo a grabarse en la semana
    c.executemany("INSERT INTO bars_1m (exchange, symbol, minute_ms) VALUES (?,?,?)", rows)
    c.commit()
    c.close()
    h = HistoryStore(path)
    h.add_klines([("binance", "BTC-USDT", "1m", m, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1, 0.5)
                  for m in range(WS, WE, MIN) if m != WS + 7 * MIN])
    h.close()


@pytest.fixture
def env(tmp_path):
    paths = wk.default_paths({"HEATSIGNAL_EVAL_DB": str(tmp_path / "heatsignal_eval.sqlite")})
    make_paperbot(paths["paperbot_db"])
    ids = make_eval(paths["eval_db"])
    make_bridge(paths["bridge_db"], paths["ft_hyp_db"], ids)
    make_donchian(paths["freqtrade_db"])
    make_history(paths["history_db"])
    (tmp_path / "paper_summary.json").write_text(json.dumps({
        "generated_at": "2026-10-18T01:00:00+00:00", "history": [{}, {}],
        "latest": {"status": "ok", "window": ["2026-10-11", "2026-10-18"],
                   "totals": {"paper": {"total": 5.0}, "sim": {"total": 7.0}, "diff": {"total": -2.0}}}}))
    return {"paths": paths, "ids": ids, "dir": tmp_path}


NOW = WE + 30 * MIN  # lunes 00:30 UTC
SYMS = ("BTCUSDT",)


# ---- calculos por fuente ---------------------------------------------------------------------------------


def test_paperbot_week(env):
    pb = wk.paperbot_week(env["paths"]["paperbot_db"], WS, WE)
    a, b = pb["strategies"]
    assert a["strategy_id"] == "a-v1" and not a["started_in_week"]
    assert a["pnl_week"] == pytest.approx(180) and a["basket_pnl_week"] == pytest.approx(-60)
    assert a["vs_basket_week"] == pytest.approx(240) and a["return_week"] == pytest.approx(0.018)
    assert a["costs_week"] == pytest.approx(4 + 1.6) and a["funding_week"] == pytest.approx(-2)
    assert a["price_pnl_week"] == pytest.approx((200 + 115.6) - (50 + 77))
    assert a["max_dd_week"] == pytest.approx(10_000 / 10_120 - 1)
    assert a["pnl_cum"] == pytest.approx(300) and a["vs_basket_cum"] == pytest.approx(300)
    assert a["end_hour_ms"] == WE  # la fila posterior al cierre no entra
    assert a["trades_week"]["n"] == 2 and a["trades_week"]["wins"] == 1 and a["trades_week"]["pnl_net"] == pytest.approx(6)
    assert a["trades_week"]["small_sample"] is True and a["trades_week"]["ci95"] is not None
    assert a["trades_cum"]["n"] == 3
    assert a["orders_week"] == 2 and a["orders_by_reason"] == {"senal": 2}
    # B entro a mitad de semana: su semana arranca en 10.000 (no hereda nada de A)
    assert b["started_in_week"] and b["started_ms"] == WS + 3 * D
    assert b["pnl_week"] == pytest.approx(50) and b["basket_pnl_week"] == pytest.approx(20)
    assert b["costs_week"] == pytest.approx(11) and b["orders_by_reason"] == {"inicio": 1}
    r = pb["runs"]
    assert r == {"ok": 165, "error": 1, "skipped": 0, "ok_hours": 165, "expected_hours": 168, "missing_hours": 3}
    assert pb["n_errors"] == 1 and "hueco de datos" in pb["errors"][0]["reason"]
    assert [g["hours"] for g in pb["gaps"]] == [2]


def test_freqtrade_week_and_compare(env):
    p = env["paths"]
    ft = wk.freqtrade_week(p["freqtrade_db"], WS, WE, p["paper_summary"])
    w = ft["week"]
    assert w["n"] == 2 and w["wins"] == 1 and w["pnl"] == pytest.approx(5.0) and w["small_sample"] is True
    assert w["funding"] == pytest.approx(-0.4) and w["opened"] == 4  # 2, 3, 4 y 5 abren en la semana
    assert ft["cum"]["n"] == 3 and ft["cum"]["pnl"] == pytest.approx(25.0)
    assert ft["open_at_end"] == 2 and "no realizado" in ft["unrealized_note"]
    assert ft["compare"]["status"] == "ok" and ft["compare"]["published_in_week"] is True
    assert ft["compare"]["paper_total"] == 5.0 and ft["compare"]["runs"] == 2


def test_hyp_bridge_week(env):
    p = env["paths"]
    hb = wk.hyp_bridge_week(p["bridge_db"], p["eval_db"], p["ft_hyp_db"], WS, WE)
    t = hb["totals"]
    assert (t["emitted"], t["sent"], t["skipped"], t["errors"], t["closed"]) == (2, 1, 1, 0, 1)
    assert t["ft_pnl"] == pytest.approx(3.4) and t["ft"]["n"] == 1 and t["ft"]["small_sample"] is True
    assert t["compared"] == 1 and t["same_outcome"] == 1
    assert t["mean_pnl_diff_pp"] == pytest.approx(0.34 - 0.5)
    (s,) = hb["strategies"]
    assert s["skipped_by_reason"]["skipped_pair_busy"] == 1


def test_hypotheses_week(env):
    hy = wk.hypotheses_week(env["paths"]["eval_db"], WS, WE)
    assert hy["emitted"] == 3 and hy["pending_at_end"] == 1      # aaaa, bbbb y dddd (dddd resuelta despues)
    rw = hy["resolved_week"]
    assert (rw["n"], rw["wins"]) == (3, 2) and rw["rate"] == pytest.approx(2 / 3)
    lo, hi = rw["ci95"]
    assert lo < 2 / 3 < hi and rw["small_sample"] is True
    assert rw["mean_alpha_pct"] == pytest.approx((0.5 - 0.5 + 0.5) / 3)
    assert hy["cum"]["n"] == 4  # eeee + las 3 de la semana; dddd no (resuelta despues del cierre)
    names = {s["name"]: s for s in hy["by_strategy"]}
    a = names["1h-apertura-continuacion-a-favor-v1"]
    assert a["emitted"] == 3 and a["n"] == 2 and a["report"]["publishable"] is False
    assert "shadow_period_not_elapsed" in a["report"]["blockers"]
    assert names["1h-retroceso-ema-a-favor-v1"]["n"] == 1 and names["1h-retroceso-ema-a-favor-v1"]["emitted"] == 0
    ses = {s["session"]: s for s in hy["by_session"]}
    assert ses["asia"]["n"] == 2 and ses["europa"]["n"] == 1 and ses["europa"]["wins"] == 0
    assert all(s["small_sample"] for s in hy["by_session"])
    assert {s["daily_trend"] for s in hy["by_daily_trend"]} == {"alcista", "bajista"}


def test_health_week(env):
    p = env["paths"]
    he = wk.health_week(p["eval_db"], p["history_db"], WS, WE, SYMS)
    assert he["cycles"] == {"ran": 2, "error": 2, "warmup": 1}
    assert he["cycle_errors"] == [{"detail": "grafo: sin credito", "count": 2}]
    assert he["sessions_llm"] == {"con_llm": 1, "error": 1}
    assert he["llm_calls"]["counted"] is False and he["llm_calls"]["cycles_reaching_llm"] == 4
    assert "no se pueden contar" in he["llm_calls"]["note"]
    rows = {r["exchange"]: r for r in he["bars_1m"]["rows"]}
    assert rows["binance"]["missing"] == 30 and rows["binance"]["longest_gap_min"] == 30
    assert rows["bybit"]["missing"] == 10080 - 1440 and rows["bybit"]["longest_gap_min"] == 10080 - 1440
    # lo anterior al primer minuto grabado no es un hueco: se espera desde ahi
    assert rows["hyperliquid"]["missing"] == 0 and rows["hyperliquid"]["expected"] == 2 * 1440
    assert rows["hyperliquid"]["since_ms"] == WS + 5 * D and rows["binance"]["since_ms"] is None
    assert he["klines_1m"]["missing"] == 1 and he["klines_1m"]["rows"][0]["longest_gap_min"] == 1


# ---- corrida: idempotencia, append-only, parcial -----------------------------------------------------------


def _count(path: Path) -> int:
    c = sqlite3.connect(path)
    try:
        return c.execute("SELECT COUNT(*) FROM weekly_reviews").fetchone()[0]
    finally:
        c.close()


def test_run_is_idempotent_and_append_only(env):
    p = env["paths"]
    out = wk.run(None, p, now_ms=NOW, symbols=SYMS)
    assert out["week"] == WEEK and out["written"] is True and out["status"] == "ok", out
    assert "Una semana no prueba nada" in out["text"] and "muestra chica" in out["text"]
    again = wk.run(None, p, now_ms=NOW + 3 * D, symbols=SYMS)   # ya no es "la ultima" pero se pide la misma
    assert again["written"] is False
    again = wk.run(WEEK, p, now_ms=NOW + H, symbols=SYMS)
    assert again["written"] is False and "ya estaba cerrada" in again["reason"]
    assert _count(p["eval_db"]) == 1
    c = sqlite3.connect(p["eval_db"])
    for sql in ("UPDATE weekly_reviews SET status='ok'", "DELETE FROM weekly_reviews"):
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            c.execute(sql)
    with pytest.raises(sqlite3.IntegrityError):
        c.execute("INSERT INTO weekly_reviews VALUES (?,?,?,?,?,?,?,?)", (WEEK, WS, WE, 0, "x", "ok", "{}", ""))
    c.close()
    st = wk.WeeklyStore(p["eval_db"])
    assert st.add({"week": WEEK, "week_start_ms": WS, "week_end_ms": WE, "generated_ms": 1, "model_id": "x",
                   "status": "ok"}, "otro") is False
    st.close()


def test_dry_run_does_not_write(env):
    out = wk.run(WEEK, env["paths"], now_ms=NOW, dry_run=True, symbols=SYMS)
    assert out["written"] is False and out["summary"]["week"] == WEEK
    c = sqlite3.connect(env["paths"]["eval_db"])
    assert c.execute("SELECT name FROM sqlite_master WHERE name='weekly_reviews'").fetchone() is None
    c.close()


def test_missing_source_is_partial_not_silent(env):
    p = {**env["paths"], "freqtrade_db": env["dir"] / "no-existe.sqlite"}
    out = wk.run(WEEK, p, now_ms=NOW, symbols=SYMS)
    assert out["written"] and out["status"] == "partial" and out["failed_sources"] == ["freqtrade"]
    rv = wk.get_review(p["eval_db"], WEEK)
    assert rv["summary"]["freqtrade"]["available"] is False and "no existe" in rv["summary"]["freqtrade"]["error"]
    assert rv["summary"]["paperbot"]["available"] is True
    assert "Freqtrade Donchian: sin datos" in rv["text"]


def test_small_sample_marked_everywhere(env):
    s = wk.build(WEEK, env["paths"], now_ms=NOW, symbols=SYMS)
    assert s["small_n"] == 30 and wk.ONE_WEEK_NOTE in s["notes"]
    assert s["hypotheses"]["resolved_week"]["small_sample"] is True
    assert s["freqtrade"]["week"]["small_sample"] is True
    assert all(st["trades_week"]["small_sample"] for st in s["paperbot"]["strategies"])
    assert s["narrative"] is None  # sin LLM


def test_week_over_week_compare(env):
    p = env["paths"]
    prev_week = "2026-W41"
    pws, pwe = wk.week_bounds(prev_week)
    assert wk.run(prev_week, p, now_ms=pwe + 30 * MIN, symbols=SYMS)["written"]
    assert wk.run(WEEK, p, now_ms=NOW, symbols=SYMS)["written"]
    ls = wk.list_reviews(p["eval_db"])
    assert [w["week"] for w in ls["weeks"]] == [WEEK, prev_week]
    latest = ls["latest"]
    assert latest["week"] == WEEK and latest["prev_week"] == prev_week
    c = latest["compare"]["freqtrade.pnl"]
    assert c["prev"] == pytest.approx(20.0) and c["value"] == pytest.approx(5.0)
    assert c["dir"] == "down" and c["good"] is False
    e = latest["compare"]["health.cycle_errors"]
    assert e["prev"] == 0 and e["value"] == 2 and e["dir"] == "up" and e["good"] is False
    assert ls["weeks"][1]["metrics"]["freqtrade.pnl"]["dir"] is None  # la primera no tiene contra que comparar


# ---- endpoint y render -----------------------------------------------------------------------------------


def _client(ctx: PanelContext) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=create_panel_app(ctx)), base_url="http://panel")


async def test_weekly_endpoints(env):
    p = env["paths"]
    ctx = PanelContext(health=HealthRegistry(), weekly_db=p["eval_db"])
    async with _client(ctx) as c:
        r = await c.get("/api/weekly")
        assert r.status_code == 200 and r.json()["weeks"] == [] and "ninguna semana" in r.json()["note"]
    wk.run(WEEK, p, now_ms=NOW, symbols=SYMS)
    async with _client(PanelContext(health=HealthRegistry(), weekly_db=p["eval_db"])) as c:
        r = await c.get("/api/weekly")
        assert r.status_code == 200
        body = r.json()
        assert [w["week"] for w in body["weeks"]] == [WEEK] and body["latest"]["summary"]["paperbot"]["available"]
        r = await c.get(f"/api/weekly/{WEEK}")
        assert r.status_code == 200 and r.json()["week"] == WEEK and "text" in r.json()
        assert (await c.get("/api/weekly/2026-W40")).status_code == 404
        assert (await c.get("/api/weekly/2026-W99")).status_code == 400
        assert (await c.get("/api/weekly/semana")).status_code == 400
    async with _client(PanelContext(health=HealthRegistry())) as c:
        assert (await c.get("/api/weekly")).status_code == 503
    async with _client(PanelContext(health=HealthRegistry(), weekly_db=env["dir"] / "nada.sqlite")) as c:
        assert (await c.get("/api/weekly")).status_code == 503


async def test_weekly_view_served_and_routed():
    async with _client(PanelContext(health=HealthRegistry())) as c:
        r = await c.get("/static/app/views/semanal.js")
        assert r.status_code == 200 and "renderDetail" in r.text
        assert 'semanal: { title: "Revisión semanal"' in (await c.get("/static/app/main.js")).text


@pytest.mark.skipif(shutil.which("node") is None, reason="sin node")
def test_weekly_view_renders_and_escapes(env, tmp_path):
    p = env["paths"]
    wk.run("2026-W41", p, now_ms=WS + 30 * MIN, symbols=SYMS)
    wk.run(WEEK, p, now_ms=NOW, symbols=SYMS)
    data = {"overview": wk._clean(wk.list_reviews(p["eval_db"])), "detail": wk._clean(wk.get_review(p["eval_db"], WEEK))}
    evil = "<img src=x onerror=alert(1)>"
    data["detail"]["summary"]["paperbot"]["strategies"][0]["strategy_id"] = evil
    data["detail"]["text"] = evil
    f = tmp_path / "data.json"
    f.write_text(json.dumps(data))
    out = subprocess.run(["node", str(HARNESS), str(VIEW), str(f)], capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    res = json.loads(out.stdout)
    assert "Una semana no prueba nada" in res["overview"] and WEEK in res["overview"]
    for want in ("Bot paper propio", "Freqtrade Donchian", "Freqtrade de hipótesis", "Hipótesis del grafo", "Salud",
                 "entró en la semana", "muestra chica", "Semana contra semana", "no se cuentan", "IC 95 %"):
        assert want in res["detail"], want
    assert "▼" in res["detail"] or "▲" in res["detail"]
    assert evil in res["detail"] and "img" not in res["tags"]


def test_weekly_module_does_not_import_pandas():
    code = ("import sys\n"
            "class B:\n"
            "    def find_spec(self, n, p=None, t=None):\n"
            "        if n.split('.')[0] in ('pandas', 'numpy'): raise ImportError('bloqueado')\n"
            "sys.meta_path.insert(0, B())\n"
            "import heatsignal.paper.weekly, heatsignal.paper.ft_bridge_view, heatsignal.api.panel_api\n")
    src = str(Path(__file__).parents[1] / "src")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env={"PYTHONPATH": src, "PATH": ""})
    assert out.returncode == 0, out.stderr


def test_cli_dry_run(env, capsys, monkeypatch):
    monkeypatch.setenv("HEATSIGNAL_EVAL_DB", str(env["paths"]["eval_db"]))
    monkeypatch.setenv("HEATSIGNAL_SYMBOLS", "BTCUSDT")
    assert wk.main(["run", "--week", WEEK, "--dry-run", "--now", "2026-10-19T00:30:00"]) == 0
    out = capsys.readouterr().out
    assert "no se escribio (dry-run)" in out and "Semana 2026-W42" in out
    assert wk.main(["run", "--week", "2026-W43", "--now", "2026-10-19T00:30:00"]) == 2
    assert wk.main(["show", WEEK]) == 1
