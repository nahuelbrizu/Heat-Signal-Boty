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

Fallback a Gemini (decisions.md, 2026-10-17): si hay `GOOGLE_API_KEY` (o `GEMINI_API_KEY`), cada pedido va
primero a Anthropic y, si falla (sin credito, caida, rate limit), el MISMO pedido estructurado va a Gemini
(`langchain-google-genai`, `with_structured_output(method="json_schema")`, verificado contra la API real
el 2026-10-17: gemini-3.5-flash-lite y gemini-3.5-flash devuelven JSON segun schema). Con una sola key se
usa ese proveedor solo. `ModelRouting.providers` dice cual es el orden, para el log y el panel.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

from langchain_core.language_models import BaseChatModel

logger = logging.getLogger("heatsignal.agents.llm")

DEFAULT_CHEAP_MODEL = "claude-haiku-5-5"
DEFAULT_STRONG_MODEL = "claude-sonnet-5-5"
DEFAULT_MAX_TOKENS = 16_000  # sin streaming: se mantiene bajo el timeout HTTP del SDK
DEFAULT_GEMINI_CHEAP = "gemini-3.5-flash-lite"
DEFAULT_GEMINI_STRONG = "gemini-3.5-flash"
# Segundo escalon dentro de Gemini: los modelos nuevos devuelven 503 "high demand" en picos (visto el
# 2026-10-17); los 2.5 son estables. Vacio en la variable de entorno = sin segundo escalon.
# Los 2.5 dejaron de estar disponibles para cuentas nuevas (404, verificado el 2026-10-17 desde la VM);
# respaldos verificados: gemini-3.1-flash-lite y gemini-3.8-flash.
DEFAULT_GEMINI_CHEAP_BACKUP = "gemini-3.1-flash-lite"
DEFAULT_GEMINI_STRONG_BACKUP = "gemini-3.8-flash"


class MissingApiKeyError(RuntimeError):
    """No hay API key del proveedor configurada."""


class UnsupportedProviderError(ValueError):
    """Proveedor de LLM no soportado por la factory."""


@dataclass(frozen=True)
class ModelRouting:
    cheap: Any  # analistas (BaseChatModel o FallbackChatModel)
    strong: Any  # debate, sintetizador, redactor
    providers: tuple[str, ...] = ("anthropic",)


class AllProvidersFailedError(RuntimeError):
    """Fallaron todos los modelos de la cadena; el mensaje trae el error de CADA uno."""


def _model_name(m: Any) -> str:
    return str(getattr(m, "model", None) or getattr(m, "model_name", None) or type(m).__name__)


class _FallbackStructured:
    def __init__(self, pairs: list[tuple[str, Any]]) -> None:
        self.pairs = pairs

    async def ainvoke(self, messages: Any, config: Any = None, **kwargs: Any) -> Any:
        errors: list[str] = []
        for i, (name, runnable) in enumerate(self.pairs):
            try:
                out = await runnable.ainvoke(messages, config, **kwargs)
                if i > 0:
                    logger.warning("LLM: respondio el fallback %s (fallaron antes: %s)", name, "; ".join(errors))
                return out
            except Exception as exc:  # noqa: BLE001 - cualquier falla pasa al siguiente proveedor
                errors.append(f"{name}: {type(exc).__name__}: {str(exc)[:200]}")
                logger.warning("LLM: fallo %s (%s); paso al siguiente", name, type(exc).__name__)
        raise AllProvidersFailedError("fallaron todos los modelos -> " + " | ".join(errors))


class FallbackChatModel:
    """Lo minimo que usan los nodos (`with_structured_output(schema).ainvoke(msgs)`), con fallback.

    El primario se intenta primero; ante CUALQUIER excepcion el pedido se repite con el siguiente modelo y la
    falla queda en el log. Si fallan todos, se lanza `AllProvidersFailedError` con el error de cada uno (antes
    solo se veia el del primero, y eso escondio que el respaldo de Gemini no existia).
    """

    def __init__(self, *models: Any) -> None:
        if not models:
            raise ValueError("hace falta al menos un modelo")
        self.models = models

    def with_structured_output(self, schema: Any, **kwargs: Any) -> Any:
        return _FallbackStructured([(_model_name(m), m.with_structured_output(schema, **kwargs)) for m in self.models])


@lru_cache(maxsize=1)
def _gemini_chat_class() -> type:
    from langchain_google_genai import ChatGoogleGenerativeAI

    return ChatGoogleGenerativeAI


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
    """Construye el par (cheap, strong). Las keys se leen del entorno en ESTE momento.

    Orden: Anthropic (si hay key) y despues Gemini (si hay key) como fallback.
    """
    if provider not in ("anthropic", "gemini"):
        raise UnsupportedProviderError(f"proveedor {provider!r} no soportado ('anthropic' o 'gemini')")
    a_key = api_key or os.environ.get("ANTHROPIC_API_KEY")
    g_key = os.environ.get("GOOGLE_API_KEY") or os.environ.get("GEMINI_API_KEY")
    chains: list[tuple[str, Any, Any]] = []
    if a_key and provider == "anthropic":
        chat_cls = _anthropic_chat_class()
        chains.append(("anthropic",
                       chat_cls(model=cheap_model, api_key=a_key, max_tokens=max_tokens, max_retries=max_retries),
                       chat_cls(model=strong_model, api_key=a_key, max_tokens=max_tokens, max_retries=max_retries)))
    if g_key:
        g_cls = _gemini_chat_class()
        g_cheap = os.environ.get("HEATSIGNAL_GEMINI_CHEAP_MODEL", DEFAULT_GEMINI_CHEAP)
        g_strong = os.environ.get("HEATSIGNAL_GEMINI_STRONG_MODEL", DEFAULT_GEMINI_STRONG)
        chains.append(("gemini",
                       g_cls(model=g_cheap, google_api_key=g_key, max_output_tokens=max_tokens, max_retries=max_retries),
                       g_cls(model=g_strong, google_api_key=g_key, max_output_tokens=max_tokens, max_retries=max_retries)))
        b_cheap = os.environ.get("HEATSIGNAL_GEMINI_CHEAP_BACKUP", DEFAULT_GEMINI_CHEAP_BACKUP)
        b_strong = os.environ.get("HEATSIGNAL_GEMINI_STRONG_BACKUP", DEFAULT_GEMINI_STRONG_BACKUP)
        if b_cheap and b_strong:
            chains.append(("gemini-backup",
                           g_cls(model=b_cheap, google_api_key=g_key, max_output_tokens=max_tokens, max_retries=max_retries),
                           g_cls(model=b_strong, google_api_key=g_key, max_output_tokens=max_tokens, max_retries=max_retries)))
    if not chains:
        raise MissingApiKeyError("falta ANTHROPIC_API_KEY o GOOGLE_API_KEY (variable de entorno o argumento api_key)")
    names = tuple(c[0] for c in chains)
    if len(chains) == 1:
        return ModelRouting(cheap=chains[0][1], strong=chains[0][2], providers=names)
    return ModelRouting(cheap=FallbackChatModel(*(c[1] for c in chains)),
                        strong=FallbackChatModel(*(c[2] for c in chains)), providers=names)
