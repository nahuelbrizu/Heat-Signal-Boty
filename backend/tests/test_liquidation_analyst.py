"""
Tests del Analista de Liquidaciones y del grafo mínimo que lo envuelve. El LLM
se reemplaza por un fake que respeta el contrato `with_structured_output(...)
.ainvoke(...)` — sin red, sin API key, determinístico.
"""

from __future__ import annotations

from heatsignal.agents.graph import build_graph
from heatsignal.agents.liquidation_analyst import (
    LiquidationAnalysis,
    LiquidationAnalysisBatch,
    _summarize_window,
    make_liquidation_analyst_node,
)
from heatsignal.schema.events import Coverage, Exchange, LiquidationEvent, Side


def _liquidation(symbol: str, side: Side, price: float, qty: float) -> LiquidationEvent:
    return LiquidationEvent(
        source=Exchange.BINANCE,
        symbol=symbol,
        side=side,
        price=price,
        qty=qty,
        notional_usd=price * qty,
        ts_event_ms=1,
        ts_ingest_ms=2,
        coverage=Coverage.FULL,
    )


class _FakeStructuredLLM:
    """Sustituto de `llm.with_structured_output(Schema)`: expone `.ainvoke`."""

    def __init__(self, canned_result: LiquidationAnalysisBatch) -> None:
        self._canned_result = canned_result
        self.received_messages: list | None = None

    async def ainvoke(self, messages: list) -> LiquidationAnalysisBatch:
        self.received_messages = messages
        return self._canned_result


class _FakeChatModel:
    """Sustituto de `BaseChatModel`: solo implementa `with_structured_output`."""

    def __init__(self, canned_result: LiquidationAnalysisBatch) -> None:
        self.structured = _FakeStructuredLLM(canned_result)
        self.requested_schema: type | None = None

    def with_structured_output(self, schema: type) -> _FakeStructuredLLM:
        self.requested_schema = schema
        return self.structured


# ---- _summarize_window --------------------------------------------------------------


def test_summarize_window_empty_reports_no_liquidations() -> None:
    assert _summarize_window("BTC-USDT", []) == "BTC-USDT: sin liquidaciones en la ventana."


def test_summarize_window_reports_counts_notional_and_sides() -> None:
    events = [
        _liquidation("BTC-USDT", Side.SELL, 50_000.0, 1.0),
        _liquidation("BTC-USDT", Side.SELL, 49_000.0, 0.5),
        _liquidation("BTC-USDT", Side.BUY, 51_000.0, 2.0),
    ]
    summary = _summarize_window("BTC-USDT", events)

    assert "3 liquidaciones" in summary
    assert "1 buy / 2 sell" in summary
    assert "49,000.00" in summary and "51,000.00" in summary


# ---- make_liquidation_analyst_node ---------------------------------------------------


async def test_node_returns_empty_analyses_when_no_liquidations() -> None:
    fake_llm = _FakeChatModel(LiquidationAnalysisBatch(analyses=[]))
    node = make_liquidation_analyst_node(fake_llm)

    result = await node({"liquidations_by_symbol": {}})

    assert result == {"liquidation_analyses": []}
    assert fake_llm.structured.received_messages is None  # no debe llamar al LLM sin datos


async def test_node_requests_structured_output_with_batch_schema() -> None:
    fake_llm = _FakeChatModel(LiquidationAnalysisBatch(analyses=[]))
    node = make_liquidation_analyst_node(fake_llm)

    await node({"liquidations_by_symbol": {"BTC-USDT": [_liquidation("BTC-USDT", Side.SELL, 100.0, 1.0)]}})

    assert fake_llm.requested_schema is LiquidationAnalysisBatch


async def test_node_returns_analyses_from_llm_and_includes_all_symbols_in_prompt() -> None:
    canned = LiquidationAnalysisBatch(
        analyses=[
            LiquidationAnalysis(
                symbol="BTC-USDT",
                cluster_summary="clusters similares reaccionaron al alza en el 60% de los casos",
                dominant_side=Side.SELL,
                notional_usd_total=50_000.0,
                n_liquidations=1,
                confidence=0.6,
            ),
            LiquidationAnalysis(
                symbol="ETH-USDT",
                cluster_summary="sin señal clara",
                dominant_side=None,
                notional_usd_total=0.0,
                n_liquidations=0,
                confidence=0.1,
            ),
        ]
    )
    fake_llm = _FakeChatModel(canned)
    node = make_liquidation_analyst_node(fake_llm)

    state = {
        "liquidations_by_symbol": {
            "BTC-USDT": [_liquidation("BTC-USDT", Side.SELL, 50_000.0, 1.0)],
            "ETH-USDT": [],
        }
    }
    result = await node(state)

    assert result == {"liquidation_analyses": canned.analyses}
    prompt_text = fake_llm.structured.received_messages[1].content
    assert "BTC-USDT" in prompt_text
    assert "ETH-USDT" in prompt_text  # un solo call batcheado, no uno por símbolo


# ---- build_graph (integración con LangGraph) -----------------------------------------


async def test_build_graph_runs_end_to_end_with_fake_llm() -> None:
    canned = LiquidationAnalysisBatch(
        analyses=[
            LiquidationAnalysis(
                symbol="BTC-USDT",
                cluster_summary="test",
                dominant_side=Side.SELL,
                notional_usd_total=100.0,
                n_liquidations=1,
                confidence=0.5,
            )
        ]
    )
    fake_llm = _FakeChatModel(canned)
    compiled = build_graph(fake_llm)

    result = await compiled.ainvoke(
        {"liquidations_by_symbol": {"BTC-USDT": [_liquidation("BTC-USDT", Side.SELL, 100.0, 1.0)]}}
    )

    assert result["liquidation_analyses"] == canned.analyses
