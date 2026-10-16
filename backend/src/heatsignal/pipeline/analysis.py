"""
Ciclo de analisis end-to-end en el mismo proceso que la ingesta:

    MemoryWindowSink -> build_feature_set (por simbolo) -> grafo LangGraph -> PendingHypothesisStore

Corre en el mismo proceso porque en modo memory la ventana de eventos vive en la RAM de la ingesta
(decisions.md, "Datos de mercado solo en memoria"). La resolucion y evaluacion de hipotesis (Fase 3)
corre aparte (`python -m heatsignal.evaluation.job`, timer de systemd): solo necesita la base de
hipotesis y klines REST, no la ventana.

Cada ciclo termina con un estado explicito (sin fallos silenciosos):
  - `warmup`         la ventana todavia no cubre `window_s` desde el arranque: no se gasta LLM con
                     features incompletos.
  - `no_strategies`  no hay estrategias pre-registradas activas: el grafo no tiene que instanciar nada.
  - `no_llm`         falta el runner del grafo (sin ANTHROPIC_API_KEY): solo se calculan features.
  - `ran`            el grafo corrio; se informa cuantas hipotesis quedaron registradas.
  - `error`          fallo el grafo o el armado de features; se loguea con traceback.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

from ..evaluation.strategy_registry import StrategyHypothesis, StrategyStatus
from ..features.builder import build_feature_set
from ..schema.features import FeatureSet
from ..storage.memory import MemoryWindowSink

log = logging.getLogger("heatsignal.pipeline.analysis")

DEFAULT_INTERVAL_S = 900.0
DEFAULT_WINDOW_S = 1800

CycleStatus = Literal["warmup", "no_strategies", "no_llm", "ran", "error"]

# (estado de entrada del grafo, run_id) -> estado final del grafo
GraphRunner = Callable[[dict[str, Any], str], Awaitable[dict[str, Any]]]


class StrategySource:
    """Lo minimo que el ciclo necesita del `StrategyRegistry`."""

    def get_all(self) -> list[StrategyHypothesis]:  # pragma: no cover - protocolo
        raise NotImplementedError


@dataclass
class CycleResult:
    run_id: str
    status: CycleStatus
    features: dict[str, FeatureSet] = field(default_factory=dict)
    n_hypotheses: int = 0
    detail: str = ""


def active_strategies(registry: StrategySource) -> list[StrategyHypothesis]:
    """Estrategias que el Sintetizador puede instanciar: todas menos las ya evaluadas."""
    return [s for s in registry.get_all() if s.status != StrategyStatus.EVALUATED]


def _feature_summary(fs: FeatureSet) -> str:
    ctx, of = fs.context, fs.order_flow
    mark = f"{ctx.mark_price:.6g}" if ctx.mark_price is not None else "?"
    atr = f"{ctx.atr:.4g}" if ctx.atr is not None else "?"
    return (
        f"{fs.symbol}: mark={mark} atr={atr} regime={ctx.regime} clusters={len(fs.heatmap.clusters)} "
        f"cvd_usd={of.cvd_usd:,.0f} trades={of.n_trades} gaps={len(fs.data_gaps)}"
    )


class AnalysisLoop:
    def __init__(
        self,
        memory: MemoryWindowSink,
        symbols: Sequence[str],
        registry: StrategySource,
        *,
        graph_runner: GraphRunner | None = None,
        interval_s: float = DEFAULT_INTERVAL_S,
        window_s: int = DEFAULT_WINDOW_S,
        skip_during_warmup: bool = True,
        clock_ms: Callable[[], int] = lambda: int(time.time() * 1000),
    ) -> None:
        if interval_s <= 0 or window_s <= 0:
            raise ValueError("interval_s y window_s deben ser > 0")
        self._memory = memory
        self._symbols = list(symbols)
        self._registry = registry
        self._runner = graph_runner
        self._interval_s = interval_s
        self._window_s = window_s
        self._skip_during_warmup = skip_during_warmup
        self._clock_ms = clock_ms
        self.last_result: CycleResult | None = None

    @property
    def graph_enabled(self) -> bool:
        return self._runner is not None

    async def build_features(self, now_ms: int) -> dict[str, FeatureSet]:
        out: dict[str, FeatureSet] = {}
        for sym in self._symbols:
            # Copia en el hilo del event loop (las deques mutan con cada evento); el calculo, en otro hilo.
            liqs = self._memory.liquidations(sym)
            trades = self._memory.trades(sym)
            tickers = self._memory.tickers(sym)
            fs = await asyncio.to_thread(
                build_feature_set, sym, liqs, trades, tickers, now_ms, self._window_s
            )
            memory_gaps = self._memory.data_gaps(sym, self._window_s)
            out[sym] = fs.model_copy(update={"data_gaps": [*fs.data_gaps, *memory_gaps]})
        return out

    async def run_cycle(self) -> CycleResult:
        now_ms = self._clock_ms()
        run_id = f"cycle-{now_ms}"
        try:
            features = await self.build_features(now_ms)
        except Exception as exc:  # noqa: BLE001
            log.exception("%s: fallo el armado de features", run_id)
            return self._finish(CycleResult(run_id, "error", detail=f"features: {exc!r}"))

        for fs in features.values():
            log.info("%s %s", run_id, _feature_summary(fs))
            for gap in fs.data_gaps:
                log.info("%s %s gap: %s", run_id, fs.symbol, gap)

        warming = [s for s, fs in features.items() if any("calentamiento" in g for g in fs.data_gaps)]
        if warming and self._skip_during_warmup:
            return self._finish(CycleResult(
                run_id, "warmup", features,
                detail=f"ventana incompleta tras reinicio para {warming}; el grafo no corre",
            ))

        strategies = active_strategies(self._registry)
        if not strategies:
            return self._finish(CycleResult(
                run_id, "no_strategies", features,
                detail="no hay estrategias pre-registradas activas; el grafo no corre",
            ))

        if self._runner is None:
            return self._finish(CycleResult(
                run_id, "no_llm", features,
                detail="grafo deshabilitado (falta ANTHROPIC_API_KEY); solo features",
            ))

        state = {"run_id": run_id, "features_by_symbol": features, "allowed_strategies": strategies}
        try:
            final = await self._runner(state, run_id)
        except Exception as exc:  # noqa: BLE001
            log.exception("%s: el grafo fallo", run_id)
            return self._finish(CycleResult(run_id, "error", features, detail=f"grafo: {exc!r}"))

        hypotheses = final.get("pending_hypotheses") or []
        decisions = final.get("synthesis_decisions") or []
        detail = "; ".join(f"{d.symbol}={d.outcome}" for d in decisions)
        return self._finish(CycleResult(run_id, "ran", features, n_hypotheses=len(hypotheses), detail=detail))

    def _finish(self, result: CycleResult) -> CycleResult:
        level = logging.ERROR if result.status == "error" else logging.INFO
        if result.status in ("no_strategies", "no_llm"):
            level = logging.WARNING
        log.log(
            level, "%s: estado=%s hipotesis=%d %s",
            result.run_id, result.status, result.n_hypotheses, result.detail,
        )
        self.last_result = result
        return result

    async def run_forever(self, stop: asyncio.Event) -> None:
        """Un ciclo cada `interval_s` (el primero tras un intervalo: la ventana arranca vacia)."""
        log.info(
            "analisis cada %.0fs sobre ventana de %ds para %s (grafo %s)",
            self._interval_s, self._window_s, self._symbols,
            "habilitado" if self._runner is not None else "DESHABILITADO: falta ANTHROPIC_API_KEY",
        )
        while not stop.is_set():
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=self._interval_s)
            if stop.is_set():
                break
            await self.run_cycle()
