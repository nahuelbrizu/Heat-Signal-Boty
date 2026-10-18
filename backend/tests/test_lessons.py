"""Diario de lecciones (evaluation/lessons.py): una leccion por hipotesis resuelta, idempotente y append-only, con LLM
falso o por codigo, y la REGLA DURA: las lecciones no aparecen en ningun prompt del grafo ni en ninguna decision."""

from __future__ import annotations

import ast
import json
import shutil
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

from heatsignal.evaluation import lessons as ls
from heatsignal.evaluation.job import run_once
from heatsignal.evaluation.hypothesis_log import HypothesisLog
from heatsignal.evaluation.pending_store import PendingHypothesisStore, ResolutionMetrics
from heatsignal.evaluation.strategy_registry import StrategyHypothesis, StrategyRegistry
from heatsignal.schema.hypotheses import HypothesisStatus, PendingHypothesis, Resolution
from heatsignal.schema.signals import Direction

sys.path.insert(0, str(Path(__file__).parent))
from _agent_fakes import ScriptedChatModel, full_script, prompt_text  # noqa: E402

SRC = Path(__file__).parents[1] / "src" / "heatsignal"
VIEW = SRC / "api/static/app/views/lecciones.js"
HARNESS = Path(__file__).parent / "js" / "render_view.mjs"
T0 = datetime(2026, 10, 18, 8, tzinfo=timezone.utc)
SENTINEL = "LECCION_CENTINELA_9f3a no deberia verla ningun agente"


def _env(tmp_path):
    db = tmp_path / "eval.sqlite"
    reg = StrategyRegistry(db)
    strat = StrategyHypothesis(description="1h-continuacion-a-favor-v1", entry_conditions={}, author="t",
                               exit_conditions={"horizon_s": 3600})
    reg.register(strat)
    store = PendingHypothesisStore(db, reg)
    return db, reg, store, strat


def _hyp(store, strat, *, move=Decimal("101"), win=True, atr=500.0, last_close=100000.0, status="resolved",
         resolved_at=T0 + timedelta(hours=1)):
    h = PendingHypothesis(strategy_id=strat.strategy_id, symbol="BTC-USDT", created_at=T0, direction=Direction.LONG,
                          confidence_score=0.6, entry_reference_price=Decimal("100"), horizon_s=3600,
                          rationale="continuacion a favor tras la apertura de Europa", timeframe="1h",
                          daily_trend="alcista", trend_alignment="a_favor", session="europa", session_day="2026-10-18",
                          htf_snapshot={"atr14_1h": atr, "last_close": last_close})
    store.add(h)
    if status == "resolved":
        pnl = float((move / Decimal(100) - 1) * 100) - 0.1
        store.resolve(Resolution(hypothesis_id=h.hypothesis_id, status=HypothesisStatus.RESOLVED, resolved_at=resolved_at,
                                 exit_price=move, pnl_pct=pnl, win=win),
                      ResolutionMetrics(gross_pnl_pct=pnl + 0.1, cost_bps=10, benchmark_pnl_pct=pnl + 0.1, alpha_pct=-0.1))
    else:
        store.resolve(Resolution(hypothesis_id=h.hypothesis_id, status=HypothesisStatus.EXPIRED_NO_DATA,
                                 resolved_at=resolved_at))
    return h


def test_window_verdict_rule():
    assert ls.window_verdict(0.1, 0.5, 3600)[0] == "no_alcanza"
    assert ls.window_verdict(0.4, 0.5, 3600)[0] == "dudoso"
    assert ls.window_verdict(1.2, 0.5, 4 * 3600)[0] == "alcanza"     # ruido 0,5 x raiz(4) = 1,0
    assert ls.window_verdict(1.2, None, 3600) == ("sin_dato", None)


def test_code_lessons_are_idempotent_and_append_only(tmp_path):
    db, reg, store, strat = _env(tmp_path)
    h = _hyp(store, strat)                       # +1 % en 1 h con ATR 0,5 % -> la ventana alcanza
    _hyp(store, strat, status="expired")         # sin resultado: no lleva leccion
    lessons = ls.LessonStore(db)
    out = ls.write_missing(lessons, store, reg, llm=None, now=T0 + timedelta(hours=2))
    assert out["written"] == 1 and out["codigo"] == 1 and out["llm_available"] is False
    again = ls.write_missing(lessons, store, reg, llm=None, now=T0 + timedelta(hours=3))
    assert again["written"] == 0                 # idempotente: una por hipotesis
    conn = sqlite3.connect(db, isolation_level=None)
    row = ls.get_one(conn, str(h.hypothesis_id))
    assert row["source"] == "codigo" and row["window_verdict"] == "alcanza" and row["outcome"] == "acierto"
    assert "n = 1" in row["text"] and "alcista" in row["text"] and "europa" in row["text"]
    from heatsignal.agents.guardrails import find_predictive_phrases
    assert find_predictive_phrases(row["text"]) == []
    assert 2 <= ls._sentences(row["text"]) <= 4
    for sql in ("UPDATE hypothesis_lessons SET text='x'", "DELETE FROM hypothesis_lessons"):
        with pytest.raises(sqlite3.DatabaseError):
            conn.execute(sql)
    assert not lessons.add(ls.lesson_row(ls.facts_from_record(store.get_record(h.hypothesis_id)), "otra", source="codigo",
                                         model=None, llm_error=None, now_ms=0))


def test_llm_lesson_one_call_per_hypothesis_and_validated(tmp_path):
    db, reg, store, strat = _env(tmp_path)
    good = _hyp(store, strat, move=Decimal("100.05"), win=False)   # movimiento chico: dentro del ruido
    bad = _hyp(store, strat)
    texts = iter([
        ls.LessonText(text="El resultado quedo apenas negativo neto de costos frente a una tesis de continuacion. "
                           "La ventana no alcanza para juzgarla: el movimiento quedo dentro del ruido y es una sola hipotesis. "
                           "La tendencia del dia era alcista y la entrada salio en la sesion de Europa."),
        ls.LessonText(text="**El precio va a subir** otra vez. Seguro que si."),        # predictivo -> se descarta, version por codigo
    ])
    model = ScriptedChatModel({ls.LessonText: lambda msgs: next(texts)})
    lessons = ls.LessonStore(db)
    out = ls.write_missing(lessons, store, reg, llm=model, model_name="fake-cheap", now=T0 + timedelta(hours=2))
    assert out["written"] == 2 and out["llm"] == 1 and out["codigo"] == 1
    assert len(model.calls) == 2                                   # una llamada por hipotesis resuelta
    p = prompt_text(model.calls[0][1])
    assert "no_alcanza" in p and "alcista" in p and "1h-continuacion-a-favor-v1" in p
    conn = sqlite3.connect(db)
    g, b = ls.get_one(conn, str(good.hypothesis_id)), ls.get_one(conn, str(bad.hypothesis_id))
    assert g["source"] == "llm" and g["model"] == "fake-cheap" and g["window_verdict"] == "no_alcanza"
    assert b["source"] == "codigo" and "predictivo" in b["llm_error"]


def test_llm_failure_retries_then_falls_back_to_code(tmp_path):
    db, reg, store, strat = _env(tmp_path)
    h = _hyp(store, strat)
    model = ScriptedChatModel({ls.LessonText: lambda msgs: RuntimeError("503 UNAVAILABLE")})
    lessons = ls.LessonStore(db)
    first = ls.write_missing(lessons, store, reg, llm=model, now=T0 + timedelta(hours=2))
    assert first["written"] == 0 and first["deferred"] == 1 and "se reintenta" in first["errors"][0]
    later = ls.write_missing(lessons, store, reg, llm=model, now=T0 + timedelta(hours=1 + ls.LLM_RETRY_HOURS + 1))
    assert later["written"] == 1 and later["codigo"] == 1
    row = ls.get_one(sqlite3.connect(db), str(h.hypothesis_id))
    assert row["source"] == "codigo" and "503" in row["llm_error"]


def test_job_writes_lessons_after_resolving(tmp_path):
    db, reg, store, strat = _env(tmp_path)
    h = PendingHypothesis(strategy_id=strat.strategy_id, symbol="BTC-USDT", created_at=T0, direction=Direction.LONG,
                          confidence_score=0.6, entry_reference_price=Decimal("100"), horizon_s=3600, rationale="x")
    store.add(h)

    class Prices:
        def price_at(self, symbol, ts):
            return Decimal("102")

    lessons = ls.LessonStore(db)
    log = HypothesisLog(db)
    out = run_once(registry=reg, store=store, hypothesis_log=log, report_log=None, price_source=Prices(),
                   now=T0 + timedelta(hours=2),
                   lesson_writer=lambda now: ls.write_missing(lessons, store, reg, llm=None, now=now))
    assert out["resolution"]["resolved"] == 1 and out["lessons"]["written"] == 1
    assert ls.get_one(sqlite3.connect(db), str(h.hypothesis_id))["window_verdict"] == "sin_dato"  # sin ATR guardado


# ---------------------------------------------------------------- REGLA DURA: nunca en un prompt ni en una decision

ALLOWED = {"evaluation/lessons.py", "evaluation/job.py", "api/panel_macro.py"}


def test_no_decision_module_reads_lessons():
    """Solo el job (escribe), el modulo y el panel (lee para mostrar) tocan el diario. Nadie mas lo importa ni lo lee."""
    offenders = []
    for f in SRC.rglob("*.py"):
        rel = f.relative_to(SRC).as_posix()
        if rel in ALLOWED:
            continue
        text = f.read_text(encoding="utf-8")
        if "hypothesis_lessons" in text:
            offenders.append(rel + ": hypothesis_lessons")
        for node in ast.walk(ast.parse(text)):
            if isinstance(node, ast.ImportFrom) and node.module and node.module.split(".")[-1] == "lessons":
                offenders.append(f"{rel}: from {node.module} import ...")
            if isinstance(node, ast.ImportFrom) and node.module in ("evaluation", "..evaluation", None) and \
                    any(a.name == "lessons" for a in node.names):
                offenders.append(f"{rel}: import lessons")
            if isinstance(node, ast.Import) and any(a.name.endswith(".lessons") for a in node.names):
                offenders.append(f"{rel}: import lessons")
    assert offenders == []


async def test_lessons_never_reach_any_graph_prompt(tmp_path):
    """Con una leccion guardada en la MISMA base que usa el ciclo, un ciclo completo del grafo no ve su texto."""
    from features_helpers import SYM
    from test_analysis_loop import Clock, T0 as LT0, _filled_memory

    from heatsignal.agents.graph import build_graph
    from heatsignal.pipeline.analysis import AnalysisLoop
    from heatsignal.pipeline.cycle_log import CycleLog

    db, reg, store, strat = _env(tmp_path)
    h = _hyp(store, strat)
    lessons = ls.LessonStore(db)
    f = ls.facts_from_record(store.get_record(h.hypothesis_id), "x")
    assert lessons.add(ls.lesson_row(f, SENTINEL, source="llm", model="m", llm_error=None, now_ms=1))

    clock = Clock(LT0)
    memory = await _filled_memory(clock)
    model = ScriptedChatModel(full_script([SYM, "BTC-USDT"], strategy_id=str(strat.strategy_id)))
    graph = build_graph(model, model, hypothesis_store=store)

    async def runner(state, run_id):
        return await graph.ainvoke(state)

    loop = AnalysisLoop(memory, [SYM], reg, graph_runner=runner, clock_ms=clock, cycle_log=CycleLog(db))
    res = await loop.run_cycle()
    assert res.status == "ran" and model.calls
    for _, msgs in model.calls:
        assert SENTINEL not in prompt_text(msgs)
    assert SENTINEL not in json.dumps(res.report or {}, default=str)


# ---------------------------------------------------------------- panel

def _client(ctx):
    from heatsignal.api.panel import create_panel_app
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=create_panel_app(ctx)), base_url="http://panel")


async def test_lessons_endpoints_filter_and_404(tmp_path):
    from heatsignal.api.panel import PanelContext
    from heatsignal.ingestion.health import HealthRegistry

    db, reg, store, strat = _env(tmp_path)
    win = _hyp(store, strat)
    loss = _hyp(store, strat, move=Decimal("99"), win=False)
    pending = PendingHypothesis(strategy_id=strat.strategy_id, symbol="SOL-USDT", direction=Direction.SHORT,
                                confidence_score=0.5, entry_reference_price=Decimal("1"), horizon_s=60, rationale="x")
    store.add(pending)
    ls.write_missing(ls.LessonStore(db), store, reg, llm=None, now=T0 + timedelta(hours=2))
    async with _client(PanelContext(health=HealthRegistry(), db_paths={"eval": db})) as c:
        d = (await c.get("/api/lessons")).json()
        assert d["total"] == 2 and d["available"] and {x["value"] for x in d["facets"]["outcome"]} == {"acierto", "fallo"}
        d = (await c.get("/api/lessons", params={"outcome": "fallo", "symbol": "BTCUSDT", "daily_trend": "alcista",
                                                  "strategy_id": str(strat.strategy_id)})).json()
        assert d["total"] == 1 and d["items"][0]["hypothesis_id"] == str(loss.hypothesis_id)
        assert (await c.get("/api/lessons", params={"outcome": "x"})).status_code == 422
        one = await c.get(f"/api/lessons/{win.hypothesis_id}")
        assert one.status_code == 200 and one.json()["outcome"] == "acierto"
        assert (await c.get(f"/api/lessons/{pending.hypothesis_id}")).status_code == 404
        assert (await c.post("/api/lessons")).status_code == 405
        main = (await c.get("/static/app/main.js")).text
        assert 'lecciones: { title: "Lecciones"' in main
        assert "lessonBox" in (await c.get("/static/app/views/hipotesis.js")).text
    async with _client(PanelContext(health=HealthRegistry(), db_paths={"eval": tmp_path / "otra.sqlite"})) as c:
        assert (await c.get("/api/lessons")).status_code == 503
    fresh = tmp_path / "fresh.sqlite"
    sqlite3.connect(fresh).close()
    async with _client(PanelContext(health=HealthRegistry(), db_paths={"eval": fresh})) as c:
        assert (await c.get("/api/lessons")).json()["available"] is False


@pytest.mark.skipif(shutil.which("node") is None, reason="sin node")
def test_lessons_view_renders_and_escapes(tmp_path):
    db, reg, store, strat = _env(tmp_path)
    h = _hyp(store, strat)
    evil = "<img src=x onerror=alert(1)>"
    lessons = ls.LessonStore(db)
    lessons.add(ls.lesson_row(ls.facts_from_record(store.get_record(h.hypothesis_id), "x"), evil + " texto",
                              source="llm", model="m", llm_error=None, now_ms=1))
    conn = sqlite3.connect(db)
    data = {"overview": ls.query(conn), "detail": ls.get_one(conn, str(h.hypothesis_id))}
    f = tmp_path / "d.json"
    f.write_text(json.dumps(data, default=str))
    out = subprocess.run(["node", str(HARNESS), str(VIEW), str(f)], capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    res = json.loads(out.stdout)
    assert "BTC-USDT" in res["overview"] and "acertó" in res["overview"] and "LLM" in res["overview"]
    assert evil in res["detail"] and "img" not in res["tags"]
