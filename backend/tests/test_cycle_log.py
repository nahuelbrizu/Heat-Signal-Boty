"""Historial persistente de ciclos: sobrevive reinicios y se recupera de los checkpoints del grafo."""

from __future__ import annotations

import sqlite3

import pytest

from heatsignal.agents.checkpoint import make_run_config, sqlite_checkpointer
from heatsignal.agents.graph import build_graph
from heatsignal.evaluation.strategy_registry import StrategyHypothesis, StrategyRegistry
from heatsignal.pipeline.analysis import AnalysisLoop
from heatsignal.pipeline.cycle_log import CycleLog, backfill_from_checkpoints

from _agent_fakes import ScriptedChatModel, full_script, make_feature_set
from test_analysis_loop import T0, Clock, _filled_memory
from features_helpers import SYM


def test_append_latest_and_append_only(tmp_path) -> None:
    cl = CycleLog(tmp_path / "eval.sqlite")
    assert cl.append("cycle-1000", "warmup", 0, "calentando", [], None)
    assert cl.append("cycle-2000", "ran", 0, "XRP=no_hypothesis", [{"symbol": "XRP-USDT", "outcome": "no_hypothesis",
                                                                    "reason": "funding positivo", "strategy_id": None}],
                     {"status": "ok", "symbols": {}})
    assert not cl.append("cycle-2000", "ran", 0, "dup", [], None)  # idempotente
    assert [r["run_id"] for r in cl.latest()] == ["cycle-2000", "cycle-1000"]
    assert cl.latest()[0]["report"] is None and cl.latest(with_report=True)[0]["report"]["status"] == "ok"
    assert cl.latest_ran()["decisions"][0]["reason"] == "funding positivo"
    assert cl.counts() == {"ran": 1, "warmup": 1}
    conn = sqlite3.connect(tmp_path / "eval.sqlite")
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        conn.execute("DELETE FROM cycle_log")


async def test_last_ran_survives_a_restart(tmp_path) -> None:
    clock = Clock(T0)
    memory = await _filled_memory(clock)
    strategy = StrategyHypothesis(description="s", entry_conditions={"symbol": SYM}, exit_conditions={"horizon_s": 60},
                                  author="t")
    registry = StrategyRegistry(tmp_path / "eval.sqlite")
    registry.register(strategy)
    graph = build_graph(ScriptedChatModel(full_script([SYM])))

    async def runner(state, run_id):
        return await graph.ainvoke(state)

    first = AnalysisLoop(memory, [SYM], registry, graph_runner=runner, clock_ms=clock,
                         cycle_log=CycleLog(tmp_path / "eval.sqlite"))
    ran = await first.run_cycle()
    assert ran.status == "ran"

    # "reinicio": proceso nuevo, sin nada en memoria
    second = AnalysisLoop(memory, [SYM], registry, clock_ms=clock, cycle_log=CycleLog(tmp_path / "eval.sqlite"))
    assert second.last_ran is not None and second.last_ran.run_id == ran.run_id
    assert second.last_ran.decisions == ran.decisions and second.last_ran.report == ran.report


async def test_backfill_recovers_runs_from_checkpoints(tmp_path) -> None:
    async with sqlite_checkpointer(tmp_path / "ck.sqlite") as saver:
        graph = build_graph(ScriptedChatModel(full_script(["XRP-USDT"])), checkpointer=saver)
        for run_id in ("cycle-1792206109022", "cycle-1792210342356"):
            state = {"run_id": run_id, "features_by_symbol": {"XRP-USDT": make_feature_set("XRP-USDT")},
                     "allowed_strategies": [StrategyHypothesis(description="s", entry_conditions={},
                                                               exit_conditions={}, author="t")]}
            await graph.ainvoke(state, make_run_config(["XRP-USDT"], run_id))
    cl = CycleLog(tmp_path / "eval.sqlite")
    assert await backfill_from_checkpoints(cl, tmp_path / "ck.sqlite") == 2
    assert await backfill_from_checkpoints(cl, tmp_path / "ck.sqlite") == 0  # idempotente
    rows = cl.latest(with_report=True)
    assert [r["ts_ms"] for r in rows] == [1792210342356, 1792206109022]
    assert rows[0]["decisions"][0]["outcome"] == "no_hypothesis" and rows[0]["report"]["symbols"]["XRP-USDT"]


def test_rotation_pick_cycles_through_all_symbols() -> None:
    from heatsignal.pipeline.analysis import rotation_pick

    others = ["BTC", "SOL", "ETH", "BNB", "TRX"]
    turns = [rotation_pick(others, 2, t) for t in range(5)]
    assert turns[0] == ["BTC", "SOL"] and turns[1] == ["ETH", "BNB"] and turns[2] == ["TRX", "BTC"]
    seen = {s for t in turns for s in t}
    assert seen == set(others)  # en pocos turnos pasan todos
    assert rotation_pick(others, 0, 3) == [] and rotation_pick([], 2, 1) == []
    assert rotation_pick(["A"], 3, 7) == ["A"]


async def test_rotation_adds_symbols_by_turns_and_continues_after_restart(tmp_path) -> None:
    clock = Clock(T0)
    memory = await _filled_memory(clock)
    strategy = StrategyHypothesis(description="s", entry_conditions={"symbol": SYM}, exit_conditions={"horizon_s": 60},
                                  author="t")
    registry = StrategyRegistry(tmp_path / "eval.sqlite")
    registry.register(strategy)
    symbols = [SYM, "BTC-USDT", "ETH-USDT", "XLM-USDT"]
    seen: list[list[str]] = []

    async def runner(state, run_id):
        syms = list(state["features_by_symbol"])
        seen.append(syms)
        return await build_graph(ScriptedChatModel(full_script(syms))).ainvoke(state)

    def make_loop():
        return AnalysisLoop(memory, symbols, registry, graph_runner=runner, clock_ms=clock,
                            cycle_log=CycleLog(tmp_path / "eval.sqlite"), rotation_per_cycle=1)

    loop = make_loop()
    r1 = await loop.run_cycle()
    clock.ms += 1000
    await loop.run_cycle()
    assert seen == [[SYM, "BTC-USDT"], [SYM, "ETH-USDT"]]  # el simbolo con estrategia siempre; el resto por turnos
    assert "turno de rotacion: BTC-USDT" in r1.detail and "sin LLM este ciclo" in r1.detail

    clock.ms += 1000
    await make_loop().run_cycle()  # "reinicio": sigue el turno, no vuelve a BTC
    assert seen[-1] == [SYM, "XLM-USDT"]

    per = CycleLog(tmp_path / "eval.sqlite").latest_per_symbol()
    assert set(per) == {SYM, "BTC-USDT", "ETH-USDT", "XLM-USDT"}
    assert per[SYM]["ts_ms"] > per["BTC-USDT"]["ts_ms"] and per["BTC-USDT"]["decision"]["outcome"] == "no_hypothesis"
