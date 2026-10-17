"""Tests del ciclo end-to-end: ventana en memoria -> features -> grafo (LLM falso) -> hipotesis en SQLite."""

from __future__ import annotations

import asyncio

from heatsignal.agents.graph import build_graph
from heatsignal.evaluation.pending_store import PendingHypothesisStore
from heatsignal.evaluation.strategy_registry import StrategyHypothesis, StrategyRegistry, StrategyStatus
from heatsignal.pipeline.analysis import AnalysisLoop, active_strategies, symbols_for_graph
from heatsignal.schema.events import Exchange, Side
from heatsignal.storage.memory import MemoryWindowSink

from _agent_fakes import ScriptedChatModel, full_script
from features_helpers import SYM, liq, ticker, trade

T0 = 1_760_000_000_000


class Clock:
    def __init__(self, ms: int) -> None:
        self.ms = ms

    def __call__(self) -> int:
        return self.ms


async def _filled_memory(clock: Clock) -> MemoryWindowSink:
    """Una hora despues del arranque, con 29 min de trades/tickers y algunas liquidaciones."""
    memory = MemoryWindowSink(clock_ms=clock)
    clock.ms = T0 + 3_600_000
    now = clock.ms
    for i in range(29 * 6):
        ts = now - 29 * 60_000 + i * 10_000
        price = 150.0 + (i % 12) * 0.1
        await memory.write(trade(price, 2.0, side=Side.BUY if i % 3 else Side.SELL, ts=ts))
        if i % 6 == 0:
            await memory.write(ticker(Exchange.BINANCE, ts, mark=price, oi_usd=1e9 + i))
    for k in range(5):
        await memory.write(liq(148.0 + k * 0.05, 50_000.0, ts=now - 600_000 + k * 1000))
    return memory


def _strategy(**kw) -> StrategyHypothesis:
    return StrategyHypothesis(
        description="rebote en cluster de longs liquidados",
        entry_conditions={"cluster": "sell-dominant"},
        exit_conditions={"horizon_s": 3600},
        author="tests",
        **kw,
    )


def _registry(tmp_path, *strategies: StrategyHypothesis) -> StrategyRegistry:
    registry = StrategyRegistry(tmp_path / "eval.sqlite")
    for s in strategies:
        registry.register(s)
    return registry


async def test_warmup_skips_graph(tmp_path) -> None:
    clock = Clock(T0)
    memory = MemoryWindowSink(clock_ms=clock)
    clock.ms += 60_000
    calls: list[str] = []

    async def runner(state, run_id):
        calls.append(run_id)
        return {}

    loop = AnalysisLoop(memory, [SYM], _registry(tmp_path, _strategy()), graph_runner=runner, clock_ms=clock)
    result = await loop.run_cycle()
    assert result.status == "warmup" and calls == []
    assert any("calentamiento" in g for g in result.features[SYM].data_gaps)


async def test_no_strategies_and_no_llm_are_explicit(tmp_path) -> None:
    clock = Clock(T0)
    memory = await _filled_memory(clock)
    empty = AnalysisLoop(memory, [SYM], _registry(tmp_path), clock_ms=clock)
    assert (await empty.run_cycle()).status == "no_strategies"

    registry = StrategyRegistry(tmp_path / "b.sqlite")
    registry.register(_strategy())
    no_llm = AnalysisLoop(memory, [SYM], registry, clock_ms=clock)
    result = await no_llm.run_cycle()
    assert result.status == "no_llm"
    fs = result.features[SYM]
    assert fs.context.mark_price is not None and fs.order_flow.n_trades > 0
    assert fs.heatmap.clusters  # las liquidaciones de la ventana armaron al menos un cluster


async def test_end_to_end_persists_hypotheses(tmp_path) -> None:
    clock = Clock(T0)
    memory = await _filled_memory(clock)
    strategy = _strategy()
    registry = _registry(tmp_path, strategy)
    store = PendingHypothesisStore(tmp_path / "eval.sqlite", registry)
    fake = ScriptedChatModel(full_script([SYM], strategy_id=str(strategy.strategy_id)))
    graph = build_graph(fake, fake, hypothesis_store=store)

    async def runner(state, run_id):
        return await graph.ainvoke(state)

    loop = AnalysisLoop(memory, [SYM], registry, graph_runner=runner, clock_ms=clock)
    result = await loop.run_cycle()
    assert result.status == "ran" and result.n_hypotheses == 1
    [stored] = store.list_by_strategy(strategy.strategy_id)
    h = stored.hypothesis
    assert h.symbol == SYM and h.run_id == result.run_id
    assert float(h.entry_reference_price) == result.features[SYM].context.mark_price
    assert h.horizon_s == 3600
    assert store.pending_count() == 1
    # la estrategia paso a running y el ciclo expone la decision por simbolo (panel)
    assert registry.get(strategy.strategy_id).status == StrategyStatus.RUNNING
    assert result.decisions == [{"symbol": SYM, "outcome": "hypothesis", "reason": h.rationale,
                                 "strategy_id": str(strategy.strategy_id)}]
    await loop.run_cycle()  # ya running: no se agrega otro evento de estado


async def test_graph_error_is_reported_not_raised(tmp_path) -> None:
    clock = Clock(T0)
    memory = await _filled_memory(clock)

    async def runner(state, run_id):
        raise RuntimeError("api caida")

    loop = AnalysisLoop(memory, [SYM], _registry(tmp_path, _strategy()), graph_runner=runner, clock_ms=clock)
    result = await loop.run_cycle()
    assert result.status == "error" and "api caida" in result.detail
    assert all(st.status == StrategyStatus.PENDING for st in loop._registry.get_all())  # sin ciclo exitoso


def test_evaluated_strategies_are_not_offered(tmp_path) -> None:
    done = _strategy()
    live = _strategy()
    registry = _registry(tmp_path, done, live)
    registry.set_status(done.strategy_id, StrategyStatus.EVALUATED)
    assert [s.strategy_id for s in active_strategies(registry)] == [live.strategy_id]


async def test_run_forever_stops_promptly(tmp_path) -> None:
    clock = Clock(T0)
    loop = AnalysisLoop(MemoryWindowSink(clock_ms=clock), [SYM], _registry(tmp_path), interval_s=3600, clock_ms=clock)
    stop = asyncio.Event()
    task = asyncio.create_task(loop.run_forever(stop))
    await asyncio.sleep(0.01)
    stop.set()
    await asyncio.wait_for(task, timeout=1)
    assert loop.last_result is None  # el primer ciclo espera un intervalo


async def test_features_carry_structured_quality_for_the_panel() -> None:
    clock = Clock(T0)
    memory = await _filled_memory(clock)
    loop = AnalysisLoop(memory, [SYM], registry=None, clock_ms=clock)  # type: ignore[arg-type]
    q = (await loop.build_features(clock.ms))[SYM].quality
    assert q is not None
    assert q.warmup_have_s == 3600 and q.window_s == 1800
    assert q.bars_closed > 0 and q.atr_bars_needed == 15 and q.regime_bars_needed == 21
    assert q.coverage["binance"]["trades"] == "ok" and q.coverage["binance"]["ticker"] == "ok"
    assert q.coverage["bybit"]["liquidations"] == "ok"
    assert q.coverage["hyperliquid"]["liquidations"] == "na"  # no publica liquidaciones: no es un hueco
    assert q.coverage["okx"] == {"trades": "missing", "liquidations": "missing", "ticker": "missing",
                                 "funding": "missing", "open_interest": "missing"}


def test_symbols_for_graph() -> None:
    xrp = StrategyHypothesis(description="x", entry_conditions={"symbol": "XRP-USDT"}, exit_conditions={}, author="t")
    anyone = StrategyHypothesis(description="a", entry_conditions={}, exit_conditions={}, author="t")
    syms = ["BTC-USDT", "SOL-USDT", "XRP-USDT"]
    assert symbols_for_graph([xrp], syms) == ["XRP-USDT"]
    assert symbols_for_graph([xrp, anyone], syms) == syms  # una sin simbolo aplica a todos
    assert symbols_for_graph([xrp], ["BTC-USDT"]) == []


async def test_graph_only_sees_symbols_with_strategies_and_last_ran_survives(tmp_path) -> None:
    clock = Clock(T0)
    memory = await _filled_memory(clock)
    strategy = StrategyHypothesis(description="solo SOL", entry_conditions={"symbol": SYM},
                                  exit_conditions={"horizon_s": 3600}, author="tests")
    registry = _registry(tmp_path, strategy)
    store = PendingHypothesisStore(tmp_path / "eval.sqlite", registry)
    graph = build_graph(ScriptedChatModel(full_script([SYM])), hypothesis_store=store)
    seen: list[list[str]] = []

    async def runner(state, run_id):
        seen.append(list(state["features_by_symbol"]))
        return await graph.ainvoke(state)

    loop = AnalysisLoop(memory, [SYM, "BTC-USDT"], registry, graph_runner=runner, clock_ms=clock)
    result = await loop.run_cycle()
    assert result.status == "ran" and seen == [[SYM]]
    assert set(result.features) == {SYM, "BTC-USDT"}  # los features se calculan igual (panel)
    assert "sin LLM este ciclo: BTC-USDT" in result.detail
    rep = result.report
    assert rep is not None and rep["status"] == "ok" and list(rep["symbols"]) == [SYM]
    sol = rep["symbols"][SYM]
    assert sol["news"]["tone"] == "none"  # sin ventana de noticias el analista no llama al LLM
    assert sol["order_flow"]["text"] == "comprador domino"
    assert {t["side"] for t in sol["debate"]} == {"bull", "bear"}

    loop._skip_during_warmup = True
    memory.started_at_ms = clock.ms  # simula un reinicio: el proximo ciclo es warmup
    warm = await loop.run_cycle()
    assert warm.status == "warmup" and loop.last_result is warm and loop.last_ran is result
