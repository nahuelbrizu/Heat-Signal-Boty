"""
Ruteo de modelos (regla de `phase-2-agents.md`): modelo economico para los
analistas (tarea repetitiva) y modelo fuerte para debate, sintesis y redactor.

Proveedor por defecto: Anthropic via `langchain-anthropic` (`ChatAnthropic`).
DECISION PROPUESTA, no cerrada (ver `decisions.md` -> Abiertas).

- Importar este modulo NO requiere API key ni importa `langchain_anthropic`:
  ambos se resuelven recien al llamar `make_llms` (lazy).
- Sin `ANTHROPIC_API_KEY` (ni `api_key` explicito), `make_llms` falla con
  `MissingApiKeyError` — explicito, no una falla diferida en el primer call.
- `with_structured_output` usa por defecto `method="json_schema"` (structured
  outputs nativos de Claude). El metodo por defecto de langchain-anthropic
  ("function_calling") fuerza `tool_choice`, que Claude Sonnet 5.5 / Opus 5.5
  rechazan; json_schema evita esa dependencia.
- No se setea `temperature` (Sonnet 5.5 / Haiku 5.5 rechazan valores no default).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

from langchain_core.language_models import BaseChatModel

DEFAULT_CHEAP_MODEL = "claude-haiku-5-5"
DEFAULT_STRONG_MODEL = "claude-sonnet-5-5"
DEFAULT_MAX_TOKENS = 16_000  # sin streaming: se mantiene bajo el timeout HTTP del SDK


class MissingApiKeyError(RuntimeError):
    """No hay API key del proveedor configurada."""


class UnsupportedProviderError(ValueError):
    """Proveedor de LLM no soportado por la factory."""


@dataclass(frozen=True)
class ModelRouting:
    cheap: BaseChatModel  # analistas
    strong: BaseChatModel  # debate, sintetizador, redactor


@lru_cache(maxsize=1)
def _anthropic_chat_class() -> type:
    from langchain_anthropic import ChatAnthropic

    class _StructuredJsonChatAnthropic(ChatAnthropic):
        """ChatAnthropic con `method="json_schema"` por defecto en `with_structured_output`."""

        def with_structured_output(self, schema: Any, *, method: str = "json_schema", **kwargs: Any):  # type: ignore[override]
            return super().with_structured_output(schema, method=method, **kwargs)

    return _StructuredJsonChatAnthropic


def make_llms(
    provider: str = "anthropic",
    cheap_model: str = DEFAULT_CHEAP_MODEL,
    strong_model: str = DEFAULT_STRONG_MODEL,
    *,
    api_key: str | None = None,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    max_retries: int = 2,
) -> ModelRouting:
    """Construye el par (cheap, strong). La key se lee del entorno en ESTE momento."""
    if provider != "anthropic":
        raise UnsupportedProviderError(f"proveedor {provider!r} no soportado (solo 'anthropic' por ahora)")
    key = api_key or os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        raise MissingApiKeyError("falta ANTHROPIC_API_KEY (variable de entorno o argumento api_key)")
    chat_cls = _anthropic_chat_class()

    def _build(model: str) -> BaseChatModel:
        return chat_cls(model=model, api_key=key, max_tokens=max_tokens, max_retries=max_retries)

    return ModelRouting(cheap=_build(cheap_model), strong=_build(strong_model))
