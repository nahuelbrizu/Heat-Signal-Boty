"""
Tests del Analista de Liquidaciones (lee `FeatureSet.heatmap`, clusters
normalizados por ATR). El LLM se reemplaza por un fake que respeta el contrato
`with_structured_output(...).ainvoke(...)` — sin red, sin API key.
"""

from __future__ import annotations

from _agent_fakes import ScriptedChatModel, full_script, make_feature_set, prompt_text

from heatsignal.agents.graph import build_graph
from heatsignal.agents.liquidation_analyst import (
    LiquidationAnalysis,
    LiquidationAnalysisBatch,
    _summarize_heatmap,
    make_liquidation_analyst_node,
)
from heatsignal.schema.events import Side


def _analysis(symbol: str, **kw) -> LiquidationAnalysis:
    base = dict(
        symbol=symbol,
        cluster_summary="clusters similares reaccionaron en el 60% de los casos",
        dominant_side=Side.SELL,
        confidence=0.6,
    )
    base.update(kw)
    return LiquidationAnalysis(**base)


# ---- _summarize_heatmap --------------------------------------------------------------


def test_summarize_heatmap_without_clusters_reports_no_activity() -> None:
    text = _summarize_heatmap(make_feature_set(clusters=[]))
    assert "sin clusters de liquidacion" in text
    assert "ninguno declarado" in text


def test_summarize_heatmap_reports_atr_distance_and_partial_coverage() -> None:
    text = _summarize_heatmap(make_feature_set(data_gaps=["binance liq PARTIAL"]))
    assert "-1.80 ATR" in text and "+2.10 ATR" in text
    assert "cobertura PARTIAL 30%" in text
    assert "liqcluster-v1" in text
    assert "binance liq PARTIAL" in text


# ---- make_liquidation_analyst_node ---------------------------------------------------


async def test_node_returns_empty_analyses_without_features() -> None:
    fake = ScriptedChatModel({LiquidationAnalysisBatch: []})
    node = make_liquidation_analyst_node(fake)

    assert await node({"features_by_symbol": {}}) == {"liquidation_analyses": []}
    assert fake.calls == []  # no llama al LLM sin datos


async def test_node_requests_structured_output_with_batch_schema() -> None:
    fake = ScriptedChatModel({LiquidationAnalysisBatch: [LiquidationAnalysisBatch(analyses=[])]})
    node = make_liquidation_analyst_node(fake)
    await node({"features_by_symbol": {"BTC-USDT": make_feature_set()}})
    assert fake.requested_schemas == [LiquidationAnalysisBatch]


async def test_node_single_batched_call_and_code_fills_numbers_and_gaps() -> None:
    canned = LiquidationAnalysisBatch(
        analyses=[
            # el LLM "transcribe mal" los numeros: el codigo los pisa desde el heatmap
            _analysis("BTC-USDT", notional_usd_total=1.0, n_liquidations=999),
            _analysis("ETH-USDT", dominant_side=None, data_gaps=["volumen bajo"]),
        ]
    )
    fake = ScriptedChatModel({LiquidationAnalysisBatch: [canned]})
    node = make_liquidation_analyst_node(fake)
    state = {
        "features_by_symbol": {
            "BTC-USDT": make_feature_set("BTC-USDT"),
            "ETH-USDT": make_feature_set("ETH-USDT", clusters=[], data_gaps=["okx: stream caido 120s"]),
        }
    }

    result = (await node(state))["liquidation_analyses"]

    assert len(fake.calls) == 1  # un solo call batcheado
    text = prompt_text(fake.calls[0][1])
    assert "BTC-USDT" in text and "ETH-USDT" in text
    btc, eth = result
    assert btc.notional_usd_total == 2_500_000.0 and btc.n_liquidations == 50
    assert btc.nearest_cluster_distance_atr == -1.8
    assert eth.n_liquidations == 0 and eth.nearest_cluster_distance_atr is None
    assert eth.data_gaps == ["okx: stream caido 120s", "volumen bajo"]


async def test_node_marks_omitted_symbol_and_drops_invented_one() -> None:
    canned = LiquidationAnalysisBatch(analyses=[_analysis("BTC-USDT"), _analysis("DOGE-USDT")])
    fake = ScriptedChatModel({LiquidationAnalysisBatch: [canned]})
    node = make_liquidation_analyst_node(fake)
    state = {"features_by_symbol": {"BTC-USDT": make_feature_set("BTC-USDT"), "ETH-USDT": make_feature_set("ETH-USDT")}}

    result = (await node(state))["liquidation_analyses"]

    assert [a.symbol for a in result] == ["BTC-USDT", "ETH-USDT"]
    assert result[1].confidence == 0.0
    assert "SIN ANALISIS" in result[1].cluster_summary


# ---- build_graph (compat con la firma previa build_graph(llm)) ------------------------


async def test_build_graph_single_llm_still_populates_liquidation_analyses() -> None:
    fake = ScriptedChatModel(full_script(["BTC-USDT"]))
    compiled = build_graph(fake)

    result = await compiled.ainvoke({"run_id": "r1", "features_by_symbol": {"BTC-USDT": make_feature_set()}})

    assert [a.symbol for a in result["liquidation_analyses"]] == ["BTC-USDT"]
