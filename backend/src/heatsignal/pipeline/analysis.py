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
  - `ran`            el grafo corrio; se informa cuantas hipotesis quedaron registradas. Las estrategias
                     que el grafo tuvo a la vista por primera vez pasan de `pending` a `running`
                     (estado de la estrategia: pending = registrada sin evaluar todavia, running = el
                     ciclo la considera; evaluated = cerrada a mano, ya no entra al grafo).
  - `error`          fallo el grafo o el armado de features; se loguea con traceback.

`run_symbols()` corre un ciclo fuera de turno para algunos simbolos ("Analizar ahora" del panel): mismos
estados, mismo historial, detalle con el motivo adelante ("manual: ...") y sin avanzar la rotacion.
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
from ..news.window import NewsWindow
from ..schema.features import FeatureSet
from ..schema.news import NEWS_MODEL_ID
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
    decisions: list[dict[str, Any]] = field(default_factory=list)  # por simbolo: outcome + motivo
    report: dict[str, Any] | None = None  # razonamiento del grafo para el panel (ver `run_view`)


def active_strategies(registry: StrategySource) -> list[StrategyHypothesis]:
    """Estrategias que el Sintetizador puede instanciar: todas menos las ya evaluadas."""
    return [s for s in registry.get_all() if s.status != StrategyStatus.EVALUATED]


def _norm(symbol: str) -> str:
    return symbol.replace("-", "").replace("/", "").upper()


def symbols_for_graph(strategies: Sequence[StrategyHypothesis], symbols: Sequence[str]) -> list[str]:
    """Simbolos que el grafo tiene que analizar: los que alguna estrategia activa puede operar.

    Una estrategia con `entry_conditions["symbol"]` solo aplica a ese simbolo; una sin `symbol` aplica
    a todos. El resto se sigue calculando (panel, features) pero no gasta LLM.
    """
    wanted: set[str] = set()
    for s in strategies:
        sym = s.entry_conditions.get("symbol")
        if not isinstance(sym, str) or not sym.strip():
            return list(symbols)
        wanted.add(_norm(sym))
    return [sym for sym in symbols if _norm(sym) in wanted]


def run_view(final: dict[str, Any]) -> dict[str, Any]:
    """Lo que razono el grafo, por simbolo, en una forma liviana para el panel (sin features crudos)."""
    def by_symbol(key: str) -> dict[str, Any]:
        return {a.symbol: a for a in final.get(key) or []}

    liq, flow = by_symbol("liquidation_analyses"), by_symbol("order_flow_analyses")
    ctx, news = by_symbol("market_context_analyses"), by_symbol("news_analyses")
    report = final.get("report")
    draft = getattr(report, "draft", None)
    sections = {sec.symbol: sec.text for sec in (draft.sections if draft else [])}
    debate: dict[str, list[dict[str, Any]]] = {}
    for turn in final.get("debate_transcript") or []:
        for arg in turn.arguments:
            debate.setdefault(arg.symbol, []).append(
                {"side": turn.side, "round": turn.round, "strength": arg.strength, "text": arg.argument})
    symbols: dict[str, Any] = {}
    for sym in final.get("features_by_symbol") or {}:
        symbols[sym] = {
            "liquidations": None if sym not in liq else
                {"confidence": liq[sym].confidence, "text": liq[sym].cluster_summary},
            "order_flow": None if sym not in flow else
                {"confidence": flow[sym].confidence, "text": flow[sym].flow_summary},
            "context": None if sym not in ctx else
                {"confidence": ctx[sym].confidence, "text": f"{ctx[sym].context_summary} {ctx[sym].regime_commentary}"},
            "news": None if sym not in news else
                {"confidence": news[sym].confidence, "tone": news[sym].tone, "text": news[sym].news_summary,
                 "material_primary_negative": news[sym].material_primary_negative},
            "debate": debate.get(sym, []),
            "report": sections.get(sym),
        }
    return {
        "status": getattr(report, "status", None),
        "title": draft.title if draft else None,
        "summary": draft.summary if draft else None,
        "symbols": symbols,
    }


def rotation_pick(others: Sequence[str], per_cycle: int, turn: int) -> list[str]:
    """Turno `turn` de la rotacion: `per_cycle` simbolos de `others`, en orden circular.

    Con 6 simbolos y 2 por ciclo, cada uno pasa por el grafo 1 de cada 3 ciclos con LLM.
    """
    if per_cycle <= 0 or not others:
        return []
    n = min(per_cycle, len(others))
    start = (turn * n) % len(others)
    return [others[(start + i) % len(others)] for i in range(n)]


def _feature_summary(fs: FeatureSet) -> str:
    ctx, of = fs.context, fs.order_flow
    mark = f"{ctx.mark_price:.6g}" if ctx.mark_price is not None else "?"
    atr = f"{ctx.atr:.4g}" if ctx.atr is not None else "?"
    return (
        f"{fs.symbol}: mark={mark} atr={atr} regime={ctx.regime} clusters={len(fs.heatmap.clusters)} "
        f"cvd_usd={of.cvd_usd:,.0f} trades={of.n_trades} news={len(fs.news)} gaps={len(fs.data_gaps)}"
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
        news: NewsWindow | None = None,
        cycle_log: Any = None,  # pipeline.cycle_log.CycleLog: historial persistente (sobrevive reinicios)
        rotation_per_cycle: int = 0,
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
        self._news = news
        self.last_result: CycleResult | None = None
        self.llm_providers: list[str] = []  # orden de proveedores (primario -> fallback), para el panel
        self.last_ran: CycleResult | None = None  # ultimo ciclo en que corrio el grafo (sobrevive a warmups)
        self._cycle_log = cycle_log
        # Rotacion: ademas de los simbolos con estrategia, cada ciclo con grafo analiza `rotation_per_cycle`
        # simbolos mas, por turnos, para cubrir todas las criptos sin pagar LLM por todas en cada ciclo.
        # El turno arranca de la cantidad de ciclos con grafo ya guardados: sigue tras un reinicio.
        self._rotation_per_cycle = max(0, rotation_per_cycle)
        self._graph_lock = asyncio.Lock()
        self._turn = 0
        if cycle_log is not None:
            try:
                prev = cycle_log.latest_ran()
            except Exception:  # noqa: BLE001
                log.exception("no se pudo leer el historial de ciclos")
                prev = None
            try:
                self._turn = int(cycle_log.counts().get("ran", 0))
            except Exception:  # noqa: BLE001
                self._turn = 0
            if prev is not None:
                self.last_ran = CycleResult(prev["run_id"], "ran", {}, prev["n_hypotheses"], prev["detail"],
                                            prev["decisions"], prev["report"])

    @property
    def symbols(self) -> list[str]:
        """Simbolos del ciclo (formato interno, p.ej. 'XRP-USDT')."""
        return list(self._symbols)

    @property
    def graph_enabled(self) -> bool:
        return self._runner is not None

    async def build_features(self, now_ms: int, symbols: Sequence[str] | None = None) -> dict[str, FeatureSet]:
        out: dict[str, FeatureSet] = {}
        for sym in self._symbols if symbols is None else symbols:
            # Copia en el hilo del event loop (las deques mutan con cada evento); el calculo, en otro hilo.
            liqs = self._memory.liquidations(sym)
            trades = self._memory.trades(sym)
            tickers = self._memory.tickers(sym)
            fs = await asyncio.to_thread(
                build_feature_set, sym, liqs, trades, tickers, now_ms, self._window_s
            )
            update: dict = {"data_gaps": [*fs.data_gaps, *self._memory.data_gaps(sym, self._window_s)]}
            if fs.quality is not None:
                update["quality"] = fs.quality.model_copy(update={"warmup_have_s": self._memory.accumulated_s()})
            if self._news is not None:
                update["news"] = self._news.items_for(sym, now_ms)
                update["news_model_id"] = NEWS_MODEL_ID
                update["data_gaps"] += self._news.data_gaps(sym, now_ms)
            out[sym] = fs.model_copy(update=update)
        return out

    async def run_cycle(self) -> CycleResult:
        """Ciclo agendado: simbolos con estrategia + turno de rotacion."""
        return await self._run(None)

    async def run_symbols(self, symbols: Sequence[str], *, reason: str = "manual",
                          run_id: str | None = None) -> CycleResult:
        """Ciclo fuera de turno SOLO para `symbols` (p.ej. "Analizar ahora" del panel).

        Mismas reglas que el ciclo agendado (warmup, estrategias activas, sin LLM) y se registra igual en el
        historial, con `reason` al principio del detalle. Los simbolos van al grafo aunque ninguna estrategia
        los nombre: el Sintetizador decide (no_hypothesis si no aplica). No avanza la rotacion.
        """
        wanted = [s for s in self._symbols if s in set(symbols)]
        unknown = [s for s in symbols if s not in self._symbols]
        if unknown:
            raise ValueError(f"simbolos no configurados: {unknown}")
        return await self._run(wanted, reason=reason, run_id=run_id)

    async def _run(self, only: list[str] | None, *, reason: str | None = None,
                   run_id: str | None = None) -> CycleResult:
        now_ms = self._clock_ms()
        run_id = run_id or f"cycle-{now_ms}"
        prefix = f"{reason}: " if reason else ""
        try:
            features = await self.build_features(now_ms, only)
        except Exception as exc:  # noqa: BLE001
            log.exception("%s: fallo el armado de features", run_id)
            return self._finish(CycleResult(run_id, "error", detail=f"{prefix}features: {exc!r}"))

        for fs in features.values():
            log.info("%s %s", run_id, _feature_summary(fs))
            for gap in fs.data_gaps:
                log.info("%s %s gap: %s", run_id, fs.symbol, gap)

        warming = [s for s, fs in features.items() if any("calentamiento" in g for g in fs.data_gaps)]
        if warming and self._skip_during_warmup:
            return self._finish(CycleResult(
                run_id, "warmup", features,
                detail=f"{prefix}ventana incompleta tras reinicio para {warming}; el grafo no corre",
            ))

        strategies = active_strategies(self._registry)
        if not strategies:
            return self._finish(CycleResult(
                run_id, "no_strategies", features,
                detail=f"{prefix}no hay estrategias pre-registradas activas; el grafo no corre",
            ))

        if self._runner is None:
            return self._finish(CycleResult(
                run_id, "no_llm", features,
                detail=f"{prefix}grafo deshabilitado (falta ANTHROPIC_API_KEY o GOOGLE_API_KEY); solo features",
            ))

        rotated: list[str] = []
        if only is None:
            with_strategy = symbols_for_graph(strategies, list(features))
            rotated = rotation_pick([s for s in features if s not in with_strategy], self._rotation_per_cycle,
                                    self._turn)
            graph_symbols = [s for s in features if s in with_strategy or s in rotated]
        else:
            graph_symbols = list(features)
        if not graph_symbols:
            return self._finish(CycleResult(
                run_id, "no_strategies", features,
                detail=f"{prefix}ninguna estrategia activa opera los simbolos configurados; el grafo no corre",
            ))
        skipped = [s for s in features if s not in graph_symbols]
        state = {
            "run_id": run_id,
            "features_by_symbol": {s: features[s] for s in graph_symbols},
            "allowed_strategies": strategies,
        }
        try:
            async with self._graph_lock:  # un ciclo manual y uno agendado no corren el grafo a la vez
                final = await self._runner(state, run_id)
        except Exception as exc:  # noqa: BLE001
            log.exception("%s: el grafo fallo", run_id)
            return self._finish(CycleResult(run_id, "error", features, detail=f"{prefix}grafo: {exc!r}"))

        if only is None:
            self._turn += 1
        self._mark_running(strategies)
        hypotheses = final.get("pending_hypotheses") or []
        decisions = final.get("synthesis_decisions") or []
        detail = prefix + "; ".join(f"{d.symbol}={d.outcome}" for d in decisions)
        if rotated:
            detail += f" (turno de rotacion: {', '.join(rotated)})"
        if skipped:
            detail += f" (sin LLM este ciclo: {', '.join(skipped)})"
        views = [{"symbol": d.symbol, "outcome": d.outcome, "reason": d.reason, "strategy_id": d.strategy_id}
                 for d in decisions]
        try:
            report = run_view(final)
        except Exception:  # noqa: BLE001 - el resumen para el panel nunca tumba el ciclo
            log.exception("%s: no se pudo armar el resumen del grafo para el panel", run_id)
            report = None
        return self._finish(CycleResult(run_id, "ran", features, n_hypotheses=len(hypotheses), detail=detail,
                                        decisions=views, report=report))

    def _mark_running(self, strategies: list[StrategyHypothesis]) -> None:
        set_status = getattr(self._registry, "set_status", None)
        if not callable(set_status):
            return
        for s in strategies:
            if s.status == StrategyStatus.PENDING:
                try:
                    set_status(s.strategy_id, StrategyStatus.RUNNING)
                    log.info("estrategia %s: pending -> running (primer ciclo del grafo)", s.strategy_id)
                except Exception:  # noqa: BLE001 - no tumbar el ciclo por un evento de estado
                    log.exception("no se pudo marcar running la estrategia %s", s.strategy_id)

    def _finish(self, result: CycleResult) -> CycleResult:
        level = logging.ERROR if result.status == "error" else logging.INFO
        if result.status in ("no_strategies", "no_llm"):
            level = logging.WARNING
        log.log(
            level, "%s: estado=%s hipotesis=%d %s",
            result.run_id, result.status, result.n_hypotheses, result.detail,
        )
        self.last_result = result
        if result.status == "ran":
            self.last_ran = result
        if self._cycle_log is not None:
            try:
                self._cycle_log.append(result.run_id, result.status, result.n_hypotheses, result.detail,
                                       result.decisions, result.report)
            except Exception:  # noqa: BLE001 - el historial no frena el ciclo
                log.exception("%s: no se pudo guardar en el historial de ciclos", result.run_id)
        return result

    async def run_forever(self, stop: asyncio.Event) -> None:
        """Un ciclo cada `interval_s` (el primero tras un intervalo: la ventana arranca vacia)."""
        log.info(
            "analisis cada %.0fs sobre ventana de %ds para %s (grafo %s)",
            self._interval_s, self._window_s, self._symbols,
            "habilitado" if self._runner is not None else "DESHABILITADO: falta ANTHROPIC_API_KEY o GOOGLE_API_KEY",
        )
        while not stop.is_set():
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=self._interval_s)
            if stop.is_set():
                break
            await self.run_cycle()
