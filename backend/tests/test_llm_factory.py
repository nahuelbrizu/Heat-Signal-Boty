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
    assert solo.providers == ("gemini", "gemini-backup", "gemini-last")
    assert solo.cheap.models[0].model.endswith("gemini-3.5-flash-lite") and solo.strong.models[0].model.endswith("gemini-3.5-flash")
    assert solo.strong.models[1].model.endswith("gemini-3.8-flash") and solo.cheap.models[1].model.endswith("gemini-3.1-flash-lite")

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-no-real")
    both = make_llms()
    assert both.providers == ("anthropic", "gemini", "gemini-backup", "gemini-last")
    assert isinstance(both.strong, FallbackChatModel)
    assert both.strong.models[0].model == "claude-sonnet-5-5" and both.strong.models[1].model.endswith("gemini-3.5-flash")


async def test_fallback_uses_second_model_when_first_fails() -> None:
    from langchain_core.runnables import RunnableLambda

    from heatsignal.agents.llm_factory import FallbackChatModel

    calls: list[str] = []

    class Fake:
        def __init__(self, name: str, fail: bool) -> None:
            self.name, self.fail, self.model = name, fail, name

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
    from heatsignal.agents.llm_factory import AllProvidersFailedError

    with pytest.raises(AllProvidersFailedError) as err:
        await FallbackChatModel(Fake("a", True), Fake("b", True)).with_structured_output(SymbolArgument).ainvoke(["x"])
    assert calls == ["a", "b"]  # si fallan todos, el error se propaga (ciclo = error, no silencio)
    assert "a#1: RuntimeError" in str(err.value) and "b#1: RuntimeError" in str(err.value)  # el de CADA proveedor



async def test_transient_errors_retry_same_model_definitive_errors_skip() -> None:
    from langchain_core.runnables import RunnableLambda

    from heatsignal.agents.debate import SymbolArgument
    from heatsignal.agents.llm_factory import FallbackChatModel, is_transient

    calls: list[str] = []

    class Flaky:
        def __init__(self, name, errors):
            self.model, self.errors = name, list(errors)

        def with_structured_output(self, schema, **kw):
            def run(msgs):
                calls.append(self.model)
                if self.errors:
                    raise self.errors.pop(0)
                return schema(symbol="XRP-USDT", argument="ok", strength=0.5)
            return RunnableLambda(run)

    assert is_transient(RuntimeError("503 UNAVAILABLE high demand")) and not is_transient(RuntimeError("credit balance is too low"))
    fm = FallbackChatModel(Flaky("claude", [RuntimeError("credit balance is too low")]),
                           Flaky("gemini", [RuntimeError("503 UNAVAILABLE"), RuntimeError("503 UNAVAILABLE")]))
    structured = fm.with_structured_output(SymbolArgument)
    structured.retry_delays = (0.0, 0.0)
    out = await structured.ainvoke(["x"])
    # claude: definitivo -> 1 intento; gemini: 2 transitorios y responde al 3er intento
    assert out.symbol == "XRP-USDT" and calls == ["claude", "gemini", "gemini", "gemini"]



async def test_model_in_cooldown_is_skipped_after_exhausting_retries() -> None:
    from langchain_core.runnables import RunnableLambda

    from heatsignal.agents import llm_factory
    from heatsignal.agents.debate import SymbolArgument
    from heatsignal.agents.llm_factory import FallbackChatModel

    llm_factory._cooldown_until.clear()
    calls: list[str] = []

    class M:
        def __init__(self, name, fail):
            self.model, self.fail = name, fail

        def with_structured_output(self, schema, **kw):
            def run(msgs):
                calls.append(self.model)
                if self.fail:
                    raise RuntimeError("GoogleRateLimitError 429 RESOURCE_EXHAUSTED")
                return schema(symbol="XRP-USDT", argument="ok", strength=0.5)
            return RunnableLambda(run)

    fm = FallbackChatModel(M("g35", True), M("g38", False))
    for _ in range(2):
        st = fm.with_structured_output(SymbolArgument)
        st.retry_delays = (0.0, 0.0)
        assert (await st.ainvoke(["x"])).symbol == "XRP-USDT"
    # 1er pedido: g35 x3 (reintentos) -> enfriamiento -> g38; 2do pedido: g35 salteado, directo a g38
    assert calls == ["g35", "g35", "g35", "g38", "g38"]
    llm_factory._cooldown_until.clear()


async def test_daily_quota_and_no_credit_are_not_retried_and_skip_even_the_last_model() -> None:
    from langchain_core.runnables import RunnableLambda

    from heatsignal.agents import llm_factory
    from heatsignal.agents.debate import SymbolArgument
    from heatsignal.agents.llm_factory import AllProvidersFailedError, FallbackChatModel

    llm_factory._cooldown_until.clear()
    llm_factory._hard_cooldown.clear()
    calls: list[str] = []
    errors = {
        "claude": "AnthropicInvalidRequestError 400 Your credit balance is too low to access the Anthropic API",
        "g35": "GoogleRateLimitError 429 RESOURCE_EXHAUSTED You exceeded your current quota "
               "quotaId: GenerateRequestsPerDayPerProjectPerModel-FreeTier",
    }

    class M:
        def __init__(self, name):
            self.model = name

        def with_structured_output(self, schema, **kw):
            def run(msgs):
                calls.append(self.model)
                raise RuntimeError(errors[self.model])
            return RunnableLambda(run)

    fm = FallbackChatModel(M("claude"), M("g35"))
    for _ in range(2):
        st = fm.with_structured_output(SymbolArgument)
        st.retry_delays = (0.0, 0.0)
        try:
            await st.ainvoke(["x"])
        except AllProvidersFailedError as exc:
            last = str(exc)
    # 1 intento cada uno (sin reintentos); en el 2do pedido los dos estan en enfriamiento duro, incluso el ultimo
    assert calls == ["claude", "g35"]
    assert "cuota diaria" in last and "sin credito" in last
    st_ = llm_factory.llm_status()
    assert st_["g35"]["segundos"] > 0 and "cuota diaria" in st_["g35"]["motivo"]
    assert 3000 < st_["claude"]["segundos"] <= 3600
    llm_factory._cooldown_until.clear()
    llm_factory._hard_cooldown.clear()


def test_daily_quota_cooldown_lasts_until_pacific_midnight() -> None:
    from datetime import datetime, timezone

    from heatsignal.agents.llm_factory import hard_cooldown

    exc = RuntimeError("429 quotaId GenerateRequestsPerDayPerProjectPerModel-FreeTier")
    # 2026-10-18 06:00 UTC = 23:00 PDT del 17 -> renueva en ~1 h (+ margen de 2 min)
    secs, why = hard_cooldown(exc, datetime(2026, 10, 18, 6, 0, tzinfo=timezone.utc).timestamp())
    assert abs(secs - 3720) < 1 and "cuota diaria" in why
    assert hard_cooldown(RuntimeError("503 UNAVAILABLE high demand"), 0.0) is None


def test_vertex_express_key_routes_gemini_through_vertex(monkeypatch) -> None:
    from heatsignal.agents.llm_factory import gemini_uses_vertex

    monkeypatch.delenv("HEATSIGNAL_GEMINI_VERTEX", raising=False)
    assert gemini_uses_vertex("AQ.fake") and not gemini_uses_vertex("AIzaFake")
    monkeypatch.setenv("HEATSIGNAL_GEMINI_VERTEX", "0")
    assert not gemini_uses_vertex("AQ.fake")
    monkeypatch.setenv("HEATSIGNAL_GEMINI_VERTEX", "1")
    assert gemini_uses_vertex("AIzaFake")
    monkeypatch.delenv("HEATSIGNAL_GEMINI_VERTEX")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("GOOGLE_API_KEY", "AQ.test-no-real")
    llms = make_llms()
    assert all(getattr(m, "vertexai", None) is True for m in llms.strong.models + llms.cheap.models)
    monkeypatch.setenv("GOOGLE_API_KEY", "AIza-test-no-real")
    assert not any(getattr(m, "vertexai", None) for m in make_llms().strong.models)
