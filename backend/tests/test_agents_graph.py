"""
Tests del grafo de Fase 2 completo con un chat model guionado (sin red, sin API key):
analistas en paralelo, loop de debate, sintetizador (validacion en codigo), redactor
con guardrail, ruteo de modelos y checkpoint resume sobre SQLite.
"""

from __future__ import annotations

import asyncio
import logging
from decimal import Decimal
from uuid import uuid4

import pytest
from _agent_fakes import ScriptedChatModel, full_script, make_feature_set, prompt_text

from heatsignal.agents.checkpoint import make_run_config, make_thread_id, sqlite_checkpointer
from heatsignal.agents.debate import DebateArgumentBatch
from heatsignal.agents.graph import build_graph
from heatsignal.agents.guardrails import find_predictive_phrases
from heatsignal.agents.hypothesis_store import (
    DuplicateHypothesisError,
    InMemoryPendingHypothesisStore,
    PendingHypothesisStore,
)
from heatsignal.agents.liquidation_analyst import LiquidationAnalysisBatch
from heatsignal.agents.market_context_analyst import MarketContextAnalysisBatch
from heatsignal.agents.order_flow_analyst import OrderFlowAnalysisBatch
from heatsignal.agents.redactor import ReportDraft
from heatsignal.agents.synthesizer import StrategyProposal, SynthesisBatch, apply_proposals
from heatsignal.evaluation.strategy_registry import StrategyHypothesis
from heatsignal.schema.signals import Direction

SYMBOLS = ["BTC-USDT", "ETH-USDT"]


def _strategy(**exit_conditions) -> StrategyHypothesis:
    return StrategyHypothesis(
        description="Rebote en cluster de longs liquidados a >1.5 ATR bajo el mark",
        entry_conditions={"cluster_distance_atr_lte": -1.5, "dominant_side": "sell"},
        exit_conditions=exit_conditions,
        author="test",
    )


def _input(symbols=SYMBOLS, strategies=None, run_id="run-1", **fs_kw) -> dict:
    return {
        "run_id": run_id,
        "features_by_symbol": {s: make_feature_set(s, **fs_kw) for s in symbols},
        "allowed_strategies": strategies or [],
    }


# ---- analistas en paralelo -----------------------------------------------------------


async def test_three_analysts_run_concurrently_before_debate() -> None:
    script = full_script(SYMBOLS)
    in_flight = {"now": 0, "max": 0}
    original = {k: script[k] for k in (LiquidationAnalysisBatch, OrderFlowAnalysisBatch, MarketContextAnalysisBatch)}
    order: list[str] = []

    class _SlowModel(ScriptedChatModel):
        def with_structured_output(self, schema, **kwargs):
            inner = super().with_structured_output(schema, **kwargs)
            if schema not in original:
                return inner

            class _Slow:
                async def ainvoke(self_inner, messages, config=None, **kw):
                    in_flight["now"] += 1
                    in_flight["max"] = max(in_flight["max"], in_flight["now"])
                    await asyncio.sleep(0.02)
                    in_flight["now"] -= 1
                    order.append(schema.__name__)
                    return await inner.ainvoke(messages)

            return _Slow()

    fake = _SlowModel(script)
    result = await build_graph(fake, fake).ainvoke(_input())

    assert in_flight["max"] == 3  # los tres analistas estuvieron en vuelo a la vez
    assert len(order) == 3
    for key in ("liquidation_analyses", "order_flow_analyses", "market_context_analyses"):
        assert [a.symbol for a in result[key]] == SYMBOLS
    # el debate corre despues del fan-in: ve las tres salidas
    first_debate_prompt = prompt_text(fake.calls_for(DebateArgumentBatch)[0])
    assert "Liquidaciones (conf" in first_debate_prompt
    assert "Order flow (conf" in first_debate_prompt
    assert "Contexto (conf" in first_debate_prompt


async def test_model_routing_cheap_for_analysts_strong_for_the_rest() -> None:
    strategy = _strategy(horizon_s=3600)
    cheap = ScriptedChatModel(full_script(SYMBOLS, strategy_id=str(strategy.strategy_id)))
    strong = ScriptedChatModel(full_script(SYMBOLS, strategy_id=str(strategy.strategy_id)))
    await build_graph(cheap, strong).ainvoke(_input(strategies=[strategy]))

    assert {s for s, _ in cheap.calls} == {
        LiquidationAnalysisBatch,
        OrderFlowAnalysisBatch,
        MarketContextAnalysisBatch,
    }
    assert {s for s, _ in strong.calls} == {DebateArgumentBatch, SynthesisBatch, ReportDraft}


async def test_data_gaps_propagate_into_every_prompt_and_outputs() -> None:
    gap = "okx: stream caido 120s"
    fake = ScriptedChatModel(full_script(SYMBOLS))
    result = await build_graph(fake).ainvoke(_input(data_gaps=[gap]))

    for schema in (
        LiquidationAnalysisBatch,
        OrderFlowAnalysisBatch,
        MarketContextAnalysisBatch,
        DebateArgumentBatch,
        ReportDraft,
    ):
        assert gap in prompt_text(fake.calls_for(schema)[0]), schema.__name__
    # aunque el LLM no lo repitio, el codigo lo une a cada analisis
    for key in ("liquidation_analyses", "order_flow_analyses", "market_context_analyses"):
        assert all(gap in a.data_gaps for a in result[key])


# ---- debate --------------------------------------------------------------------------


@pytest.mark.parametrize("rounds", [1, 3])
async def test_debate_loop_runs_n_rounds_with_transcript(rounds: int) -> None:
    fake = ScriptedChatModel(full_script(SYMBOLS))
    result = await build_graph(fake, max_debate_rounds=rounds).ainvoke(_input())

    transcript = result["debate_transcript"]
    assert [(t.round, t.side) for t in transcript] == [
        (r, side) for r in range(1, rounds + 1) for side in ("bull", "bear")
    ]
    assert result["debate_round"] == rounds
    debate_calls = fake.calls_for(DebateArgumentBatch)
    assert len(debate_calls) == 2 * rounds  # un call batcheado por turno, no por simbolo
    if rounds > 1:
        # el turno alcista de la ronda 2 ve el transcript de la ronda 1
        assert "ronda 1 BAJISTA" in prompt_text(debate_calls[2])
    assert all("ETH-USDT" in prompt_text(c) and "BTC-USDT" in prompt_text(c) for c in debate_calls)


def test_build_graph_rejects_zero_rounds() -> None:
    with pytest.raises(ValueError):
        build_graph(ScriptedChatModel(), max_debate_rounds=0)


# ---- sintetizador --------------------------------------------------------------------


async def test_synthesizer_emits_hypothesis_with_code_entry_price_and_registered_horizon() -> None:
    strategy = _strategy(horizon_s=7200)
    store = InMemoryPendingHypothesisStore()
    fake = ScriptedChatModel(full_script(SYMBOLS, strategy_id=str(strategy.strategy_id)))
    result = await build_graph(fake, hypothesis_store=store).ainvoke(
        _input(strategies=[strategy], mark=50_123.45)
    )

    hyps = result["pending_hypotheses"]
    assert len(hyps) == 2 and store.all() == hyps
    h = hyps[0]
    assert h.strategy_id == strategy.strategy_id
    assert h.entry_reference_price == Decimal("50123.45")  # desde FeatureSet.context.mark_price
    assert h.horizon_s == 7200
    assert h.direction is Direction.LONG
    assert h.run_id == "run-1" and h.heatmap_model_id == "liqcluster-v1"
    assert {d.outcome for d in result["synthesis_decisions"]} == {"hypothesis"}
    # el prompt lista las estrategias permitidas y NO menciona un precio de entrada a elegir
    synth_prompt = prompt_text(fake.calls_for(SynthesisBatch)[0])
    assert str(strategy.strategy_id) in synth_prompt


async def test_synthesizer_does_not_overlap_open_hypotheses() -> None:
    strategy = _strategy(horizon_s=90 * 86400)
    store = InMemoryPendingHypothesisStore()
    first = await build_graph(
        ScriptedChatModel(full_script(SYMBOLS, strategy_id=str(strategy.strategy_id))), hypothesis_store=store
    ).ainvoke(_input(strategies=[strategy], run_id="run-1"))
    assert len(first["pending_hypotheses"]) == 2

    fake = ScriptedChatModel(full_script(SYMBOLS, strategy_id=str(strategy.strategy_id)))
    second = await build_graph(fake, hypothesis_store=store).ainvoke(_input(strategies=[strategy], run_id="run-2"))
    assert second["pending_hypotheses"] == [] and len(store) == 2
    assert {d.outcome for d in second["synthesis_decisions"]} == {"no_hypothesis"}
    assert all("abierta" in d.reason for d in second["synthesis_decisions"])
    # el LLM ve cuales estan abiertas
    assert f"strategy_id={str(strategy.strategy_id).lower()} symbol=BTC-USDT" in prompt_text(
        fake.calls_for(SynthesisBatch)[0]
    )


def test_apply_proposals_allows_one_per_strategy_and_symbol_within_a_batch() -> None:
    strategy = _strategy(horizon_s=3600)
    proposal = StrategyProposal(
        symbol="BTC-USDT", decision="hypothesis", strategy_id=str(strategy.strategy_id),
        direction="LONG", confidence=0.6, rationale="clusters similares reaccionaron en 55% de los casos",
    )
    hyps, decisions = apply_proposals(
        [proposal, proposal], {"BTC-USDT": make_feature_set("BTC-USDT")}, [strategy], run_id="r"
    )
    assert len(hyps) == 1
    assert [d.outcome for d in decisions] == ["hypothesis", "no_hypothesis"]


async def test_synthesizer_rejects_unknown_strategy_id_and_logs(caplog) -> None:
    strategy = _strategy(horizon_s=3600)
    invented = str(uuid4())
    script = full_script(SYMBOLS)
    script[SynthesisBatch] = [
        SynthesisBatch(
            proposals=[
                StrategyProposal(
                    symbol="BTC-USDT", decision="hypothesis", strategy_id=invented,
                    direction="LONG", confidence=0.7, rationale="variante inventada",
                ),
                StrategyProposal(
                    symbol="ETH-USDT", decision="hypothesis", strategy_id=str(strategy.strategy_id),
                    direction="SHORT", confidence=1.4, rationale="confianza invalida",
                ),
            ]
        )
    ]
    store = InMemoryPendingHypothesisStore()
    with caplog.at_level(logging.WARNING):
        result = await build_graph(ScriptedChatModel(script), hypothesis_store=store).ainvoke(
            _input(strategies=[strategy])
        )

    assert result["pending_hypotheses"] == [] and len(store) == 0
    decisions = {d.symbol: d for d in result["synthesis_decisions"]}
    assert decisions["BTC-USDT"].outcome == "rejected"
    assert invented in decisions["BTC-USDT"].reason
    assert decisions["ETH-USDT"].outcome == "rejected" and "confidence" in decisions["ETH-USDT"].reason
    assert any("RECHAZADA" in r.message and invented in r.message for r in caplog.records)


async def test_synthesizer_no_hypothesis_path_and_omitted_symbol() -> None:
    strategy = _strategy()
    script = full_script(SYMBOLS)
    script[SynthesisBatch] = [
        SynthesisBatch(
            proposals=[StrategyProposal(symbol="BTC-USDT", decision="no_hypothesis", rationale="sin encaje")]
        )
    ]
    result = await build_graph(ScriptedChatModel(script)).ainvoke(_input(strategies=[strategy]))

    decisions = {d.symbol: d for d in result["synthesis_decisions"]}
    assert decisions["BTC-USDT"].outcome == "no_hypothesis" and decisions["BTC-USDT"].reason == "sin encaje"
    assert decisions["ETH-USDT"].outcome == "no_hypothesis" and "omitido" in decisions["ETH-USDT"].reason
    assert result["pending_hypotheses"] == []


async def test_synthesizer_skips_llm_without_allowed_strategies() -> None:
    fake = ScriptedChatModel(full_script(SYMBOLS))
    result = await build_graph(fake).ainvoke(_input(strategies=[]))

    assert fake.calls_for(SynthesisBatch) == []
    assert all(d.outcome == "no_hypothesis" for d in result["synthesis_decisions"])


async def test_synthesizer_rejects_when_mark_price_missing_and_defaults_horizon() -> None:
    strategy = _strategy()  # sin horizon_s pre-registrado
    fake = ScriptedChatModel(full_script(["BTC-USDT", "ETH-USDT"], strategy_id=str(strategy.strategy_id)))
    state = _input(strategies=[strategy])
    state["features_by_symbol"]["ETH-USDT"] = make_feature_set("ETH-USDT", mark=None)
    result = await build_graph(fake, default_horizon_s=900).ainvoke(state)

    decisions = {d.symbol: d for d in result["synthesis_decisions"]}
    assert decisions["ETH-USDT"].outcome == "rejected" and "mark_price" in decisions["ETH-USDT"].reason
    [h] = result["pending_hypotheses"]
    assert h.symbol == "BTC-USDT" and h.horizon_s == 900


def test_in_memory_store_is_append_only_and_matches_protocol() -> None:
    from heatsignal.schema.hypotheses import PendingHypothesis

    store = InMemoryPendingHypothesisStore()
    assert isinstance(store, PendingHypothesisStore)
    h = PendingHypothesis(
        strategy_id=uuid4(), symbol="BTC-USDT", direction=Direction.LONG, confidence_score=0.5,
        entry_reference_price=Decimal("1"), horizon_s=60, rationale="x",
    )
    store.add(h)
    with pytest.raises(DuplicateHypothesisError):
        store.add(h)


# ---- redactor + guardrail ------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "BTC va a subir",
        "El precio subirá mañana",
        "Esto garantiza ganancias",
        "Seguro que rebota",
        "ETH will rise this week",
        "price is going to drop",
        "guaranteed profit",
    ],
)
def test_guardrail_flags_predictive_phrases(text: str) -> None:
    assert find_predictive_phrases(text)


@pytest.mark.parametrize(
    "text",
    [
        "En ventanas similares el precio subió en el 58% de los casos.",
        "Clusters similares reaccionaron en el 60% de los casos (n=42).",
        "El rendimiento pasado no garantiza resultados futuros.",
        "Past performance is not guaranteed to repeat.",
    ],
)
def test_guardrail_accepts_historical_language(text: str) -> None:
    assert find_predictive_phrases(text) == []


def _draft(summary: str) -> ReportDraft:
    return ReportDraft(title="Reporte", summary=summary, sections=[])


async def test_redactor_retries_once_with_feedback_then_succeeds() -> None:
    script = full_script(SYMBOLS)
    script[ReportDraft] = [_draft("BTC va a subir"), _draft("En casos similares el precio subio en el 58%.")]
    fake = ScriptedChatModel(script)
    result = await build_graph(fake).ainvoke(_input())

    report = result["report"]
    assert report.status == "ok_after_retry" and report.attempts == 2 and report.violations == []
    retry_prompt = prompt_text(fake.calls_for(ReportDraft)[1])
    assert "RECHAZADO" in retry_prompt and "va a subir" in retry_prompt


async def test_redactor_marks_guardrail_failed_after_second_violation() -> None:
    script = full_script(SYMBOLS)
    script[ReportDraft] = [_draft("ETH subirá"), _draft("ETH will rise, guaranteed")]
    fake = ScriptedChatModel(script)
    result = await build_graph(fake).ainvoke(_input())

    report = result["report"]
    assert report.status == "guardrail_failed"
    assert report.attempts == 2
    assert "will rise" in report.violations and "guaranteed" in report.violations
    assert len(fake.calls_for(ReportDraft)) == 2  # un solo reintento, no un loop


async def test_redactor_ok_on_first_attempt() -> None:
    result = await build_graph(ScriptedChatModel(full_script(SYMBOLS))).ainvoke(_input())
    assert result["report"].status == "ok" and result["report"].attempts == 1
    assert result["report"].run_id == "run-1"


# ---- checkpoint resume ---------------------------------------------------------------


def test_thread_id_is_symbol_set_plus_run_id() -> None:
    assert make_thread_id(["ETH-USDT", "BTC-USDT", "ETH-USDT"], "r7") == "BTC-USDT,ETH-USDT:r7"
    with pytest.raises(ValueError):
        make_thread_id([], "r7")


async def test_interrupted_run_resumes_from_last_successful_node(tmp_path) -> None:
    strategy = _strategy(horizon_s=3600)
    script = full_script(SYMBOLS, strategy_id=str(strategy.strategy_id))
    ok_synth = script[SynthesisBatch]
    script[SynthesisBatch] = [RuntimeError("caida simulada del proveedor"), ok_synth]
    fake = ScriptedChatModel(script)
    store = InMemoryPendingHypothesisStore()
    db = tmp_path / "checkpoints.sqlite"
    config = make_run_config(SYMBOLS, "run-42")

    # 1er intento: falla en el sintetizador
    async with sqlite_checkpointer(db) as saver:
        graph = build_graph(fake, max_debate_rounds=2, checkpointer=saver, hypothesis_store=store)
        with pytest.raises(RuntimeError, match="caida simulada"):
            await graph.ainvoke(_input(strategies=[strategy], run_id="run-42"), config)
        snapshot = await graph.aget_state(config)
        assert snapshot.next == ("synthesizer",)

    calls_before = {s: len(fake.calls_for(s)) for s in (LiquidationAnalysisBatch, DebateArgumentBatch)}
    assert calls_before == {LiquidationAnalysisBatch: 1, DebateArgumentBatch: 4}

    # "reinicio del proceso": saver y grafo nuevos sobre el mismo archivo, input None
    async with sqlite_checkpointer(db) as saver:
        graph = build_graph(fake, max_debate_rounds=2, checkpointer=saver, hypothesis_store=store)
        result = await graph.ainvoke(None, config)

    # los nodos ya completados NO se re-ejecutan
    assert len(fake.calls_for(LiquidationAnalysisBatch)) == 1
    assert len(fake.calls_for(OrderFlowAnalysisBatch)) == 1
    assert len(fake.calls_for(MarketContextAnalysisBatch)) == 1
    assert len(fake.calls_for(DebateArgumentBatch)) == 4
    assert len(fake.calls_for(SynthesisBatch)) == 2
    # el estado previo (FeatureSet, transcript) sobrevivio la serializacion
    assert len(result["debate_transcript"]) == 4
    assert result["features_by_symbol"]["BTC-USDT"].heatmap.heatmap_model_id == "liqcluster-v1"
    assert len(result["pending_hypotheses"]) == 2 and len(store) == 2
    assert result["report"].status == "ok"
