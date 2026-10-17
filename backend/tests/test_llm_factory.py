"""
Tests de la factory de modelos: sin red ni API key real (solo construccion de clientes).
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from heatsignal.agents import llm_factory
from heatsignal.agents.debate import DebateArgumentBatch
from heatsignal.agents.liquidation_analyst import LiquidationAnalysisBatch
from heatsignal.agents.llm_factory import (
    DEFAULT_CHEAP_MODEL,
    DEFAULT_STRONG_MODEL,
    MissingApiKeyError,
    UnsupportedProviderError,
    make_llms,
)
from heatsignal.agents.market_context_analyst import MarketContextAnalysisBatch
from heatsignal.agents.order_flow_analyst import OrderFlowAnalysisBatch
from heatsignal.agents.redactor import ReportDraft
from heatsignal.agents.synthesizer import SynthesisBatch


def test_import_does_not_require_api_key_nor_provider_sdk() -> None:
    """Proceso limpio sin key: importar la factory (y el grafo) no falla ni importa langchain_anthropic."""
    env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}
    code = (
        "import sys; import heatsignal.agents.llm_factory, heatsignal.agents.graph; "
        "assert 'langchain_anthropic' not in sys.modules"
    )
    proc = subprocess.run([sys.executable, "-c", code], env=env, cwd=Path(__file__).parents[1] / "src", capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr


def test_missing_key_fails_explicitly_at_call_time(monkeypatch) -> None:
    for k in ("ANTHROPIC_API_KEY", "GOOGLE_API_KEY", "GEMINI_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    with pytest.raises(MissingApiKeyError):
        make_llms()


def test_unsupported_provider() -> None:
    with pytest.raises(UnsupportedProviderError):
        make_llms(provider="otro", api_key="x")


def test_default_routing_uses_current_haiku_and_sonnet(monkeypatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-no-real")
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    routing = make_llms()
    assert DEFAULT_CHEAP_MODEL == "claude-haiku-5-5"
    assert DEFAULT_STRONG_MODEL == "claude-sonnet-5-5"
    assert routing.cheap.model == "claude-haiku-5-5"
    assert routing.strong.model == "claude-sonnet-5-5"
    assert routing.strong.temperature is None  # Sonnet 5.5 rechaza temperature no default
    assert routing.strong.max_tokens == llm_factory.DEFAULT_MAX_TOKENS


def test_custom_models() -> None:
    routing = make_llms(cheap_model="claude-haiku-4-5", strong_model="claude-opus-5-5", api_key="x")
    assert (routing.cheap.model, routing.strong.model) == ("claude-haiku-4-5", "claude-opus-5-5")


@pytest.mark.parametrize(
    "schema",
    [
        LiquidationAnalysisBatch,
        OrderFlowAnalysisBatch,
        MarketContextAnalysisBatch,
        DebateArgumentBatch,
        SynthesisBatch,
        ReportDraft,
    ],
)
def test_structured_output_defaults_to_json_schema(schema, monkeypatch) -> None:
    """Todos los schemas de nodos se pueden atar al cliente real (sin llamar a la API)."""
    calls: list[str] = []
    parent = llm_factory._anthropic_chat_class().__mro__[1]
    original = parent.with_structured_output

    def spy(self, s, *, method="function_calling", **kw):
        calls.append(method)
        return original(self, s, method=method, **kw)

    monkeypatch.setattr(parent, "with_structured_output", spy)
    routing = make_llms(api_key="x")
    assert routing.strong.with_structured_output(schema) is not None
    assert calls == ["json_schema"]



# ---- fallback a Gemini --------------------------------------------------------------------------------


def test_gemini_only_and_fallback_routing(monkeypatch) -> None:
    from heatsignal.agents.llm_factory import FallbackChatModel

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("GOOGLE_API_KEY", "g-test-no-real")
    solo = make_llms()
    assert solo.providers == ("gemini", "gemini-backup")
    assert solo.cheap.models[0].model.endswith("gemini-3.5-flash-lite") and solo.strong.models[0].model.endswith("gemini-3.5-flash")
    assert solo.strong.models[1].model.endswith("gemini-2.5-flash")

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-no-real")
    both = make_llms()
    assert both.providers == ("anthropic", "gemini", "gemini-backup")
    assert isinstance(both.strong, FallbackChatModel)
    assert both.strong.models[0].model == "claude-sonnet-5-5" and both.strong.models[1].model.endswith("gemini-3.5-flash")


async def test_fallback_uses_second_model_when_first_fails() -> None:
    from langchain_core.runnables import RunnableLambda

    from heatsignal.agents.llm_factory import FallbackChatModel

    calls: list[str] = []

    class Fake:
        def __init__(self, name: str, fail: bool) -> None:
            self.name, self.fail = name, fail

        def with_structured_output(self, schema, **kw):
            def run(msgs):
                calls.append(self.name)
                if self.fail:
                    raise RuntimeError("credit balance is too low")
                return schema(symbol="XRP-USDT", argument="ok", strength=0.5)
            return RunnableLambda(run)

    from heatsignal.agents.debate import SymbolArgument

    out = await FallbackChatModel(Fake("anthropic", True), Fake("gemini", False)).with_structured_output(
        SymbolArgument).ainvoke(["hola"])
    assert out.symbol == "XRP-USDT" and calls == ["anthropic", "gemini"]

    calls.clear()
    with pytest.raises(RuntimeError, match="credit"):
        await FallbackChatModel(Fake("a", True), Fake("b", True)).with_structured_output(SymbolArgument).ainvoke(["x"])
    assert calls == ["a", "b"]  # si fallan todos, el error se propaga (ciclo = error, no silencio)
