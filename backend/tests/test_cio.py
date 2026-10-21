"""CIO (decide sobre el debate) + CRO (reglas fijas de riesgo) + cio_verdicts y su evaluacion (graph-v2-cio)."""

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

from heatsignal.agents.cio import CioBatch, CioCandidate, CioDecision, CioSymbolVerdict, code_candidates, validate_verdicts
from heatsignal.agents.cro import is_altcoin, macro_veto, review
from heatsignal.agents.graph import build_graph
from heatsignal.agents.hypothesis_store import InMemoryPendingHypothesisStore
from heatsignal.agents.redactor import ReportDraft
from heatsignal.evaluation import cio_verdicts as cv
from heatsignal.evaluation.strategy_registry import StrategyHypothesis
from heatsignal.macro.calendar import Calendar, MacroEvent
from heatsignal.schema.features import HtfContext, HtfSetups

sys.path.insert(0, str(Path(__file__).parent))
from _agent_fakes import ScriptedChatModel, full_script, make_feature_set, prompt_text  # noqa: E402

SRC = Path(__file__).parents[1] / "src" / "heatsignal"
HARNESS = Path(__file__).parent / "js" / "render_view.mjs"
VIEW = SRC / "api/static/app/cio-ui.js"
H = 3_600_000
NOW = 1_792_312_920_000  # un instante cualquiera a mitad de hora


def _legacy(**entry) -> StrategyHypothesis:
    return StrategyHypothesis(description="x", entry_conditions=entry, exit_conditions={"horizon_s": 3600}, author="t")


def _cand(s: StrategyHypothesis, direction: str = "LONG") -> CioCandidate:
    return CioCandidate(strategy_id=str(s.strategy_id).lower(), direction=direction, setup="sin_setup")


def _verdict(sym, s=None, direction="LONG", postura="alcista", peso=0.7, decision="tomar") -> CioSymbolVerdict:
    return CioSymbolVerdict(symbol=sym, postura_ganadora=postura, peso=peso, decision=decision,
                            strategy_id=None if s is None else str(s.strategy_id), direction=direction,
                            razones=["en ventanas similares el comprador domino", "el bajista no respondio al OI"])


# ---- CIO: reglas duras en codigo ----------------------------------------------------------------------------

def test_cio_cannot_take_a_setup_outside_the_candidates_nor_flip_direction() -> None:
    s, other = _legacy(), _legacy()
    cands = {"BTC-USDT": [_cand(s, "LONG")], "ETH-USDT": [_cand(s, "LONG")], "SOL-USDT": [], "XRP-USDT": [_cand(s, "LONG")],
             "ADA-USDT": [_cand(s, "LONG")]}
    out = {d.symbol: d for d in validate_verdicts([
        _verdict("BTC-USDT", other),                      # estrategia que no es candidata
        _verdict("ETH-USDT", s, "SHORT", "bajista"),      # direccion dada vuelta
        _verdict("SOL-USDT", s),                          # sin candidatos: no puede inventar
        _verdict("XRP-USDT", s, "LONG", "bajista"),       # direccion contraria a su propia postura
        _verdict("ZZZ-USDT", s),                          # simbolo no pedido: se descarta
    ], cands)}
    assert "no esta entre los candidatos" in out["BTC-USDT"].rejection
    assert "distinta de la del candidato" in out["ETH-USDT"].rejection
    assert "no hay candidatos" in out["SOL-USDT"].rejection
    assert "contraria a la postura" in out["XRP-USDT"].rejection
    assert all(not d.takes and d.decision == "no_operar" for d in out.values())
    assert out["ADA-USDT"].razones == ["omitido por el modelo (sin veredicto)"] and out["ADA-USDT"].postura == "empate"
    assert "ZZZ-USDT" not in out
    [ok] = validate_verdicts([_verdict("BTC-USDT", s)], {"BTC-USDT": [_cand(s)]})
    assert ok.takes and ok.strategy_id == str(s.strategy_id).lower() and ok.peso == 0.7
    [bad] = validate_verdicts([_verdict("BTC-USDT", s, peso=1.4)], {"BTC-USDT": [_cand(s)]})
    assert not bad.takes and bad.peso is None and "peso" in bad.rejection


def test_candidates_come_from_timeframes_entry_check() -> None:
    s = StrategyHypothesis(description="1h pullback", author="t", exit_conditions={"timeframe": "1h", "horizon_bars": 4},
                           entry_conditions={"htf_setup": "pullback_ema20_1h_a_favor", "daily_trend_in": ["alcista", "bajista"],
                                             "alignment": "a_favor", "htf_model_id": "htf-trend-v1"})
    htf = HtfContext(symbol="BTC-USDT", ts_ms=0, status="ok", day_start_ms=0, daily_trend="alcista",
                     setups=HtfSetups(pullback_ema20_1h_a_favor=True))
    fs = make_feature_set("BTC-USDT").model_copy(update={"htf": htf})
    now = datetime.fromtimestamp(NOW / 1000, tz=timezone.utc)
    [c] = code_candidates(fs, [s], now=now)
    assert c.direction == "LONG" and c.setup == "pullback_ema20_1h_a_favor"  # a favor de la tendencia alcista
    off = fs.model_copy(update={"htf": htf.model_copy(update={"setups": HtfSetups()})})
    assert code_candidates(off, [s], now=now) == []  # setup no cumplido: no hay nada que tomar


# ---- CRO: cada regla veta cuando debe y no cuando no --------------------------------------------------------

def _take(sym, s, direction="LONG", peso=0.7) -> CioDecision:
    return CioDecision(symbol=sym, postura="alcista" if direction == "LONG" else "bajista", peso=peso, decision="tomar",
                       strategy_id=str(s.strategy_id).lower(), direction=direction, razones=["r"])


def _cal(*offsets_h: float, kind: str = "CPI") -> Calendar:
    return Calendar(calendar_id="t", verified_at="", sources={}, events=tuple(
        MacroEvent(kind=kind, label=kind, ts_ms=int(NOW + o * H), tentative=False, source="x") for o in offsets_h))


def _fs(*syms, gaps=None):
    return {s: make_feature_set(s, data_gaps=gaps) for s in syms}


def test_cro_real_gap_vetoes_but_coverage_notes_do_not() -> None:
    s = _legacy()
    [r] = review([_take("BTC-USDT", s)], _fs("BTC-USDT", gaps=["order_flow: sin trades en la ventana"]), [s], now_ms=NOW)
    assert not r.approved and r.rule == "hueco_real" and "hueco real" in r.veto
    [r] = review([_take("BTC-USDT", s)], _fs("BTC-USDT", gaps=["htf: 3 de las ultimas 48 velas de 1h vinieron del REST"]),
                 [s], now_ms=NOW)
    assert r.approved and r.veto is None


@pytest.mark.parametrize("offset_h, vetoed", [(1.5, True), (-0.5, True), (2.5, False), (-1.5, False)])
def test_cro_macro_window(offset_h, vetoed) -> None:
    s = _legacy()
    [r] = review([_take("BTC-USDT", s)], _fs("BTC-USDT"), [s], now_ms=NOW, calendar_fn=lambda: _cal(offset_h))
    assert r.approved is not vetoed
    if vetoed:
        assert r.rule == "macro" and "CPI" in r.veto


def test_cro_macro_unreadable_calendar_vetoes() -> None:
    s = _legacy()

    def broken():
        raise FileNotFoundError("calendar.json")
    [r] = review([_take("BTC-USDT", s)], _fs("BTC-USDT"), [s], now_ms=NOW, calendar_fn=broken)
    assert not r.approved and "calendario" in r.veto
    assert macro_veto(_cal(1.0, kind="FOMC"), NOW).startswith("evento macro FOMC")


def test_cro_session_cap() -> None:
    s = _legacy(family="fam", max_per_symbol_session=1)
    sid = str(s.strategy_id).lower()
    sess = {"name": "europa", "day": "2026-10-18"}
    [r] = review([_take("BTC-USDT", s)], _fs("BTC-USDT"), [s], now_ms=NOW, session=sess)
    assert r.approved
    [r] = review([_take("BTC-USDT", s)], _fs("BTC-USDT"), [s], now_ms=NOW, session=sess, session_taken={(sid, "BTC-USDT")})
    assert not r.approved and r.rule == "tope_sesion"
    [r] = review([_take("BTC-USDT", s)], _fs("BTC-USDT"), [s], now_ms=NOW, session={})
    assert not r.approved and "sin sesion" in r.veto
    [r] = review([_take("BTC-USDT", s)], _fs("BTC-USDT"), [s], now_ms=NOW, session=sess, session_taken={(sid, "ETH-USDT")})
    assert r.approved  # otra moneda no cuenta


def test_cro_correlated_altcoin_exposure() -> None:
    s = _legacy()
    syms = ("ETH-USDT", "SOL-USDT", "XRP-USDT", "BTC-USDT", "ADA-USDT")
    decs = [_take("ETH-USDT", s, peso=0.6), _take("SOL-USDT", s, peso=0.9), _take("XRP-USDT", s, peso=0.8),
            _take("BTC-USDT", s, peso=0.5), _take("ADA-USDT", s, "SHORT", peso=0.4)]
    out = {r.symbol: r for r in review(decs, _fs(*syms), [s], now_ms=NOW)}
    # 3 LONG en alts: pasan las 2 de mayor peso (SOL, XRP); ETH se veta. BTC no cuenta; el SHORT es otra direccion.
    assert out["SOL-USDT"].approved and out["XRP-USDT"].approved and out["BTC-USDT"].approved and out["ADA-USDT"].approved
    assert not out["ETH-USDT"].approved and out["ETH-USDT"].rule == "correlacion"
    assert out["ETH-USDT"].veto.startswith("exposicion correlacionada")
    # con 1 abierta LONG en alts (y una de BTC que no cuenta) solo entra una nueva
    exposure = [("h1", "DOGE-USDT", "LONG"), ("h2", "BTC-USDT", "LONG"), ("h1", "DOGE-USDT", "LONG")]
    out = {r.symbol: r for r in review(decs[:3], _fs(*syms[:3]), [s], now_ms=NOW, exposure=exposure)}
    assert [k for k, r in out.items() if r.approved] == ["SOL-USDT"]
    assert not is_altcoin("BTC-USDT") and not is_altcoin("BTCUSDT") and is_altcoin("ETH-USDT")


def test_cro_never_approves_what_the_cio_did_not_take() -> None:
    s = _legacy()
    d = CioDecision(symbol="BTC-USDT", postura="empate", peso=0.5, decision="no_operar")
    [r] = review([d], _fs("BTC-USDT"), [s], now_ms=NOW)
    assert not r.approved and r.veto is None


# ---- grafo: presupuesto de llamadas y cio_verdicts --------------------------------------------------------------

async def test_llm_calls_per_cycle_unchanged_and_verdicts_written_without_operation(tmp_path) -> None:
    syms = ["BTC-USDT", "ETH-USDT"]
    log = cv.CioVerdictLog(tmp_path / "e.sqlite")
    model = ScriptedChatModel(full_script(syms))  # CIO: empate y no operar en todo
    graph = build_graph(model, model, verdict_log=log)
    state = {"run_id": "r-1", "features_by_symbol": {s: make_feature_set(s) for s in syms},
             "allowed_strategies": [_legacy()]}
    final = await graph.ainvoke(state, {"configurable": {"thread_id": "t"}})
    # graph-v1: 3 analistas (el de noticias no llama sin titulares) + 2 debate (1 ronda) + 1 sintetizador + 1 redactor
    # = 7. graph-v2-cio: el CIO reemplaza al sintetizador; CRO y registro son codigo -> 7.
    assert len(model.calls) == 7 and len(model.calls_for(CioBatch)) == 1
    assert final["pending_hypotheses"] == []
    rows = log.by_run("r-1")
    assert [r["symbol"] for r in rows] == syms and {r["decision"] for r in rows} == {"no_operar"}
    assert {r["graph_version"] for r in rows} == {"graph-v2-cio"} and rows[0]["razones"] == ["evidencia debil"]
    assert "Veredicto del CIO" in prompt_text(model.calls_for(ReportDraft)[0])
    assert log.append([dict(rows[0], razones=["otra"])]) == 0  # idempotente por (run, simbolo)
    log.close()
    c = sqlite3.connect(tmp_path / "e.sqlite")
    with pytest.raises(sqlite3.DatabaseError):
        c.execute("UPDATE cio_verdicts SET postura='alcista'")
    with pytest.raises(sqlite3.DatabaseError):
        c.execute("DELETE FROM cio_verdicts")


async def test_graph_takes_candidate_records_graph_version_and_cro_veto(tmp_path) -> None:
    syms = ["ETH-USDT", "SOL-USDT", "XRP-USDT"]
    s = _legacy()
    store = InMemoryPendingHypothesisStore()
    log = cv.CioVerdictLog(tmp_path / "e.sqlite")
    model = ScriptedChatModel(full_script(syms, strategy_id=str(s.strategy_id)))
    graph = build_graph(model, model, hypothesis_store=store, verdict_log=log)
    final = await graph.ainvoke({"run_id": "r-2", "features_by_symbol": {x: make_feature_set(x) for x in syms},
                                 "allowed_strategies": [s]}, {"configurable": {"thread_id": "t2"}})
    assert len(final["pending_hypotheses"]) == 2 and {h.graph_version for h in store.all()} == {"graph-v2-cio"}
    rows = {r["symbol"]: r for r in log.by_run("r-2")}
    vetoed = [r for r in rows.values() if r["cro_veto"]]
    assert len(vetoed) == 1 and vetoed[0]["cro_veto"].startswith("exposicion correlacionada")
    assert vetoed[0]["outcome"] == "no_hypothesis" and vetoed[0]["decision"] == "tomar"
    assert all(r["candidates"] for r in rows.values())


# ---- evaluacion del CIO con velas sinteticas ------------------------------------------------------------------

class _Candles:
    def __init__(self, closes):  # {(symbol, end_ms): close}
        self.closes = closes

    def price_at(self, symbol, ts):
        return self.closes.get((symbol, int(ts.timestamp() * 1000)))


def test_cio_evaluation_with_synthetic_candles(tmp_path) -> None:
    db = tmp_path / "e.sqlite"
    log = cv.CioVerdictLog(db)
    t0 = NOW  # entrada = ultima vela 1h cerrada (termina en floor_hora), salida = la que termina 4 h despues
    e0, x0 = NOW - NOW % H, NOW - NOW % H + 4 * H
    base = {"ts_ms": t0, "decision": "no_operar", "razones": ["r"], "graph_version": "graph-v2-cio"}
    log.append([
        {**base, "run_id": "a", "symbol": "BTC-USDT", "postura": "alcista", "peso": 0.8},   # sube: acierta
        {**base, "run_id": "a", "symbol": "ETH-USDT", "postura": "bajista", "peso": 0.6},   # sube: falla
        {**base, "run_id": "a", "symbol": "SOL-USDT", "postura": "empate", "peso": 0.5},    # no se evalua
        {**base, "run_id": "a", "symbol": "XRP-USDT", "postura": "alcista", "peso": 0.7},   # sin velas
    ])
    candles = _Candles({("BTC-USDT", e0): 100.0, ("BTC-USDT", x0): 102.0, ("ETH-USDT", e0): 10.0, ("ETH-USDT", x0): 10.5})
    early = cv.evaluate_missing(db, candles, now=datetime.fromtimestamp((x0 - 1) / 1000, tz=timezone.utc))
    assert early["waiting"] == 3 and early["ok"] == 0
    first = cv.evaluate_missing(db, candles, now=datetime.fromtimestamp((x0 + 60_000) / 1000, tz=timezone.utc))
    assert first == {"ok": 2, "sin_datos": 0, "waiting": 1, "errors": 0}
    late = cv.evaluate_missing(db, candles, now=datetime.fromtimestamp((x0 + 7 * H) / 1000, tz=timezone.utc))
    assert late["sin_datos"] == 1 and late["ok"] == 0  # idempotente: lo ya evaluado no se repite
    conn = sqlite3.connect(db)
    acc = cv.accuracy(conn)
    assert acc["n"] == 2 and acc["hits"] == 1 and acc["sin_datos"] == 1 and acc["pending"] == 0
    assert acc["brier"] == pytest.approx(((0.8 - 1) ** 2 + (0.6 - 0) ** 2) / 2)
    assert acc["brier_ref"] == pytest.approx(0.25) and acc["ci"][0] < 0.5 < acc["ci"][1]
    [btc] = [r for r in acc["recent"] if r["symbol"] == "BTC-USDT"]
    assert btc["return_pct"] == pytest.approx(2.0) and btc["hit"] is True
    with pytest.raises(sqlite3.DatabaseError):
        conn.execute("DELETE FROM cio_verdict_outcomes")


# ---- endpoint y render ----------------------------------------------------------------------------------------

def _client(ctx):
    from heatsignal.api.panel import create_panel_app
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=create_panel_app(ctx)), base_url="http://panel")


async def test_endpoints_cycle_detail_and_accuracy(tmp_path) -> None:
    from heatsignal.api.panel import PanelContext
    from heatsignal.ingestion.health import HealthRegistry
    from heatsignal.pipeline.cycle_log import CycleLog

    db = tmp_path / "e.sqlite"
    cl = CycleLog(db)
    cl.append("run-1792312920000", "ran", 0, "x", [], {"symbols": {}})
    cv.CioVerdictLog(db).append([{"run_id": "run-1792312920000", "ts_ms": NOW, "symbol": "BTC-USDT", "postura": "alcista",
                                  "peso": 0.7, "decision": "tomar", "strategy_id": "abc", "direction": "LONG",
                                  "razones": ["r1", "r2"], "cro_veto": "evento macro CPI", "graph_version": "graph-v2-cio"}])
    async with _client(PanelContext(health=HealthRegistry(), cycle_log=cl, db_paths={"eval": db})) as c:
        d = (await c.get("/api/cycles/run-1792312920000")).json()
        [v] = d["cio_verdicts"]
        assert v["postura"] == "alcista" and v["cro_veto"] == "evento macro CPI" and v["razones"] == ["r1", "r2"]
        a = (await c.get("/api/cio/accuracy")).json()
        assert a["available"] and a["verdicts"] == 1 and a["cro_vetoes"] == 1 and a["n"] == 0
        assert (await c.get("/api/cio/verdicts/run-1792312920000")).json()["items"][0]["symbol"] == "BTC-USDT"
        assert "cioVerdictCard" in (await c.get("/static/app/views/ciclos.js")).text
        assert "cioAccuracyCard" in (await c.get("/static/app/views/analisis.js")).text
    async with _client(PanelContext(health=HealthRegistry(), db_paths={"eval": tmp_path / "otra.sqlite"})) as c:
        assert (await c.get("/api/cio/accuracy")).status_code == 503


@pytest.mark.skipif(shutil.which("node") is None, reason="sin node")
def test_cio_ui_renders_and_escapes(tmp_path) -> None:
    db = tmp_path / "e.sqlite"
    evil = "<img src=x onerror=alert(1)>"
    cv.CioVerdictLog(db).append([{"run_id": "r", "ts_ms": NOW, "symbol": "ETH-USDT", "postura": "bajista", "peso": 0.65,
                                  "decision": "tomar", "strategy_id": "abc", "direction": "SHORT", "razones": [evil],
                                  "cro_veto": "exposicion correlacionada: ya hay 2 SHORT", "graph_version": "graph-v2-cio"}])
    candles = _Candles({("ETH-USDT", NOW - NOW % H): 10.0, ("ETH-USDT", NOW - NOW % H + 4 * H): 9.0})
    cv.evaluate_missing(db, candles, now=datetime.fromtimestamp((NOW + 5 * H) / 1000, tz=timezone.utc))
    conn = sqlite3.connect(db)
    f = tmp_path / "d.json"
    f.write_text(json.dumps({"overview": cv.accuracy(conn), "detail": cv.by_run(conn, "r")}))
    out = subprocess.run(["node", str(HARNESS), str(VIEW), str(f)], capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    res = json.loads(out.stdout)
    assert "¿Acierta el CIO?" in res["overview"] and "acertó" in res["overview"] and "Brier" in res["overview"]
    assert "Veredicto del CIO" in res["detail"] and "exposicion correlacionada" in res["detail"]
    assert evil in res["detail"] and "img" not in res["tags"]
