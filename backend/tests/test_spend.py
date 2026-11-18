"""Tope semanal de LLM (agents/spend.py): costo a precio de lista, ventana lun-vie, tope y freno del grafo."""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace as NS

import pytest

from heatsignal.agents import spend as S
from heatsignal.evaluation.strategy_registry import StrategyRegistry
from heatsignal.pipeline.analysis import AnalysisLoop

from test_analysis_loop import SYM, T0, Clock, _filled_memory, _strategy

WED = datetime(2026, 11, 18, 10, tzinfo=timezone.utc)  # miercoles
SAT = datetime(2026, 11, 21, 10, tzinfo=timezone.utc)  # sabado
MON = datetime(2026, 11, 16, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.delenv("HEATSIGNAL_LLM_WEEKLY_USD", raising=False)
    monkeypatch.delenv("HEATSIGNAL_LLM_DAYS", raising=False)


def _ms(d: datetime) -> int:
    return int(d.timestamp() * 1000)


def test_cost_at_list_price() -> None:
    assert S.cost_usd("claude-haiku-5-5", 1_000_000, 1_000_000) == pytest.approx(0.60)
    assert S.cost_usd("claude-sonnet-5-5", 10_000, 2_000) == pytest.approx(0.04)
    # cache: lectura 0,1x y escritura 1,25x del precio de entrada
    assert S.cost_usd("claude-sonnet-5-5", 10_000, 0, cache_read=10_000) == pytest.approx(0.002)
    assert S.cost_usd("desconocido", 1_000_000, 0) == pytest.approx(2.0)  # conservador: como Sonnet


def test_week_window_and_days(tmp_path) -> None:
    led = S.SpendLedger(tmp_path / "s.sqlite")
    assert S.week_start(WED) == MON
    led.add("graph", "claude-sonnet-5-5", cost=1.0, ts_ms=_ms(MON) - 1)  # semana anterior: no cuenta
    led.add("graph", "claude-sonnet-5-5", cost=1.5, ts_ms=_ms(WED))
    led.add("lessons", "claude-haiku-5-5", cost=0.25, ts_ms=_ms(WED))
    st = S.budget_state(led, now=WED)
    assert st.allowed and st.spent_usd == pytest.approx(1.75) and st.by_source == {"graph": 1.5, "lessons": 0.25}
    assert st.days == "lun,mar,mie,jue,vie" and st.limit_usd == 5.0
    sat = S.budget_state(led, now=SAT)
    assert not sat.allowed and "solo features" in sat.reason


def test_limit_blocks_until_monday(tmp_path, monkeypatch) -> None:
    led = S.SpendLedger(tmp_path / "s.sqlite")
    led.add("graph", "claude-sonnet-5-5", cost=5.01, ts_ms=_ms(WED))
    st = S.budget_state(led, now=WED)
    assert not st.allowed and "tope semanal" in st.reason
    nxt = datetime(2026, 11, 23, 1, tzinfo=timezone.utc)  # lunes siguiente: arranca de cero
    assert S.budget_state(led, now=nxt).allowed
    monkeypatch.setenv("HEATSIGNAL_LLM_WEEKLY_USD", "10")
    assert S.budget_state(led, now=WED).allowed
    monkeypatch.setenv("HEATSIGNAL_LLM_DAYS", "0-6")
    assert S.budget_state(led, now=SAT).allowed


def test_callback_records_usage_metadata(tmp_path) -> None:
    led = S.SpendLedger(tmp_path / "s.sqlite")
    cb = S.make_callback("graph", led)
    msg = NS(usage_metadata={"input_tokens": 12_000, "output_tokens": 800,
                             "input_token_details": {"cache_read": 2_000, "cache_creation": 0}},
             response_metadata={"model": "claude-haiku-5-5"})
    cb.on_llm_end(NS(generations=[[NS(message=msg)]]))
    cb.on_llm_end(NS(generations=[[NS(message=NS(usage_metadata=None, response_metadata={}))]]))  # sin uso: no anota
    total, by = led.spent_since(0)
    assert total == pytest.approx(S.cost_usd("claude-haiku-5-5", 12_000, 800, 2_000)) and set(by) == {"graph"}


async def test_graph_skips_llm_when_budget_says_no(tmp_path) -> None:
    clock = Clock(T0)
    memory = await _filled_memory(clock)
    reg = StrategyRegistry(tmp_path / "eval.sqlite")
    reg.register(_strategy())
    calls: list[str] = []

    async def runner(state, run_id):
        calls.append(run_id)
        return {}

    blocked = NS(allowed=False, reason="tope semanal alcanzado: US$5.02 de US$5.00")
    loop = AnalysisLoop(memory, [SYM], reg, graph_runner=runner, clock_ms=clock, budget_check=lambda: blocked)
    res = await loop.run_cycle()
    assert res.status == "no_llm" and "tope semanal" in res.detail and calls == []
    assert res.features[SYM].context.mark_price is not None  # las features se calculan igual

    def broken():
        raise OSError("disco")

    loop2 = AnalysisLoop(memory, [SYM], reg, graph_runner=runner, clock_ms=clock, budget_check=broken)
    res2 = await loop2.run_cycle()
    assert res2.status == "no_llm" and "no se pudo leer el gasto" in res2.detail and calls == []
