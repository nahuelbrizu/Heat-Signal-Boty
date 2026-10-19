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

Modos (`mode`, `HEATSIGNAL_ANALYSIS_MODE`):
  - `interval`  un ciclo cada `interval_s` (con rotacion de simbolos), como hasta el 2026-10-18.
  - `sessions`  el grafo se ancla a las sesiones de mercado (`features/sessions.py`: Asia, Europa, EE.UU. y cierre
                del dia UTC): al cerrar la primera vela de 1h de cada sesion se guarda el analisis por CODIGO en el
                diario del dia (`session_journal`) y se corre el grafo UNA vez con TODOS los simbolos batcheados en
                las mismas llamadas de siempre (4 ciclos/dia en vez de 8 o 96). El diario del dia (sesiones previas,
                codigo + resumen del Redactor) entra al estado del grafo. Sin LLM (o sin estrategias), el analisis
                por codigo se guarda igual y la fila 'llm' queda 'sin_llm'. Una sesion perdida por un reinicio se
                recupera si pasaron < 2 h desde su disparo (catch-up); nunca se duplica (UNIQUE en el diario).

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
from ..features.gaps import classify_gap
from ..features.sessions import (
    HOUR_MS,
    SessionDef,
    analyze_symbol,
    build_analysis,
    day_str,
    next_trigger,
    render_journal,
    session_at,
    triggers_between,
)
from ..schema.sessions import SessionSymbolAnalysis
from ..news.window import NewsWindow
from ..schema.features import FeatureSet
from ..schema.news import NEWS_MODEL_ID
from ..storage.memory import MemoryWindowSink

log = logging.getLogger("heatsignal.pipeline.analysis")

DEFAULT_INTERVAL_S = 900.0
SESSION_GRACE_MS = 90_000          # el grabador escribe el ultimo minuto de la vela ~15 s despues de cerrar
SESSION_CATCHUP_MS = 2 * 3_600_000  # una sesion perdida por reinicio se recupera si su disparo fue hace < 2 h
AnalysisMode = Literal["interval", "sessions"]
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
    Excepcion: `entry_conditions["scope"] == "rotation"` (familias 1h con tendencia diaria) aplica a los simbolos
    que el ciclo ya analiza (con estrategia o por turno de rotacion) y NO fuerza a mandar todos al grafo: asi
    no crece el prompt ni el costo de LLM por ciclo.
    """
    wanted: set[str] = set()
    for s in strategies:
        if s.entry_conditions.get("scope") == "rotation" and not s.entry_conditions.get("symbol"):
            continue
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
    for sym, fs in (final.get("features_by_symbol") or {}).items():
        symbols[sym] = {
            "htf": htf_brief(getattr(fs, "htf", None)),
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


def htf_brief(h: Any) -> dict[str, Any] | None:
    """Lo minimo del contexto 1h para el historial de ciclos y el panel (sin las 48 velas)."""
    if h is None:
        return None
    return {
        "htf_model_id": h.htf_model_id, "status": h.status, "daily_trend": h.daily_trend, "trend_reason": h.trend_reason,
        "last_closed_open_ms": h.last_closed_open_ms, "change_from_open_pct": h.change_from_open_pct,
        "change_from_open_atr": h.change_from_open_atr, "pos_in_day_range": h.pos_in_day_range,
        "dist_ema20_1h_atr": h.dist_ema20_1h_atr, "setups": h.setups.model_dump(), "n_gaps": len(h.data_gaps),
        "levels": None if getattr(h, "levels", None) is None else h.levels.brief(),
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
        f"cvd_usd={of.cvd_usd:,.0f} trades={of.n_trades} news={len(fs.news)} gaps={len(fs.data_gaps)} "
        f"daily_trend={fs.htf.daily_trend if fs.htf is not None else 'n/a'}"
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
        htf: Any = None,  # history.hourly.HourlyCandleProvider: velas de 1h + tendencia del dia (None = apagado)
        mode: AnalysisMode = "interval",
        sessions: Sequence[SessionDef] | None = None,
        session_source: Any = None,  # history.session_source.SessionDataSource
        journal: Any = None,  # pipeline.session_journal.SessionJournal
        macro: Any = None,  # macro.context.MacroContextProvider: bloque "Macro y eventos" (None = apagado)
        oi_poller: Any = None,  # ingestion.binance_oi_poller.BinanceOiPoller: sus fallos van a data_gaps
    ) -> None:
        if mode not in ("interval", "sessions"):
            raise ValueError(f"mode debe ser 'interval' o 'sessions', no {mode!r}")
        if mode == "sessions" and not sessions:
            raise ValueError("mode='sessions' necesita sesiones configuradas")
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
        self.oi_poller = oi_poller
        self.htf = htf
        self.mode: AnalysisMode = mode
        self.sessions: list[SessionDef] = list(sessions or [])
        self._session_source = session_source
        self.journal = journal
        self.macro = macro
        self._sessions_done: set[tuple[str, str]] = set()  # sin diario: no repetir en este proceso
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
            if self.oi_poller is not None:
                update["data_gaps"] += self.oi_poller.data_gaps(sym, now_ms)
            if fs.quality is not None:
                update["quality"] = fs.quality.model_copy(update={"warmup_have_s": self._memory.accumulated_s()})
            if self.htf is not None:
                htf_ctx = await asyncio.to_thread(self.htf.context, sym, now_ms)
                update["htf"] = htf_ctx
                update["data_gaps"] += [g for g in htf_ctx.data_gaps if g not in update["data_gaps"]]
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

    def session_context(self, now_ms: int, override: dict[str, Any] | None = None
                        ) -> tuple[dict[str, Any] | None, str | None, dict[str, SessionSymbolAnalysis]]:
        """(sesion de la corrida, diario del dia en texto, ultimo analisis de sesion por simbolo)."""
        if not self.sessions:
            return None, None, {}
        sess = override
        if sess is None:
            found = session_at(self.sessions, now_ms)
            if found is None:
                return None, None, {}
            label = next((d.label for d in self.sessions if d.name == found[0]), found[0])
            sess = {"name": found[0], "label": label, "day": found[1]}
        if self.journal is None:
            return sess, None, {}
        try:
            entries = self.journal.day(sess["day"], with_data=True)
        except Exception:  # noqa: BLE001 - el diario no frena el ciclo
            log.exception("no se pudo leer el diario del dia %s", sess.get("day"))
            return sess, None, {}
        latest: dict[str, SessionSymbolAnalysis] = {}
        for e in entries:
            if e["kind"] == "code" and e.get("data"):
                for sym, a in (e["data"].get("symbols") or {}).items():
                    try:
                        latest[sym] = SessionSymbolAnalysis(**a)
                    except Exception:  # noqa: BLE001
                        log.warning("diario %s %s: analisis de %s ilegible", e["day"], e["session"], sym)
        return sess, render_journal(entries), latest

    async def _run(self, only: list[str] | None, *, reason: str | None = None,
                   run_id: str | None = None, session: dict[str, Any] | None = None) -> CycleResult:
        now_ms = self._clock_ms()
        run_id = run_id or f"cycle-{now_ms}"
        prefix = f"{reason}: " if reason else ""
        try:
            features = await self.build_features(now_ms, only)
        except Exception as exc:  # noqa: BLE001
            log.exception("%s: fallo el armado de features", run_id)
            return self._finish(CycleResult(run_id, "error", detail=f"{prefix}features: {exc!r}"))
        sess, journal_text, per_symbol = self.session_context(now_ms, session)
        if per_symbol:
            features = {s: (fs.model_copy(update={"session": per_symbol[s]}) if s in per_symbol else fs)
                        for s, fs in features.items()}

        for fs in features.values():
            log.info("%s %s", run_id, _feature_summary(fs))
            for gap in fs.data_gaps:
                log.info("%s %s gap[%s]: %s", run_id, fs.symbol, classify_gap(gap)[0], gap)

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
        state: dict[str, Any] = {
            "run_id": run_id,
            "features_by_symbol": {s: features[s] for s in graph_symbols},
            "allowed_strategies": strategies,
        }
        if sess is not None:
            state["session"] = sess
        if journal_text:
            state["day_journal"] = journal_text
        if self.macro is not None:
            try:
                macro_text = await asyncio.to_thread(self.macro.context_block, now_ms)
            except Exception:  # noqa: BLE001 - el contexto macro nunca frena el ciclo (queda en el log)
                log.exception("%s: no se pudo armar el bloque macro", run_id)
                macro_text = "Macro y eventos: no disponible en este ciclo (error al leer la base macro)."
            if macro_text:
                state["macro_context"] = macro_text
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

    # ---------------------------------------------------------------- sesiones

    def due_sessions(self, now_ms: int, kind: str = "llm") -> list[tuple[SessionDef, int, int]]:
        """Sesiones disparadas (cierre de su vela + gracia) en las ultimas 2 h que todavia no estan en el diario."""
        out = []
        for d, o, t in triggers_between(self.sessions, now_ms - SESSION_CATCHUP_MS, now_ms - SESSION_GRACE_MS):
            key = (day_str(o), d.name)
            if key in self._sessions_done:
                continue
            if self.journal is not None and self.journal.has(key[0], d.name, kind):
                continue
            out.append((d, o, t))
        return out

    async def run_session(self, d: SessionDef, open_ms: int, *, with_graph: bool = True) -> CycleResult | None:
        """Analisis por codigo de la sesion -> diario -> grafo con TODOS los simbolos -> resumen al diario.

        `with_graph=False` (modo interval): solo el analisis por codigo, para que el diario exista igual.
        """
        day = day_str(open_ms)
        trigger = open_ms + HOUR_MS
        now_ms = self._clock_ms()
        self._sessions_done.add((day, d.name))
        have_code = self.journal is not None and self.journal.has(day, d.name, "code")
        if not have_code:
            try:
                if self._session_source is not None:
                    analysis = await asyncio.to_thread(self._session_source.analyze, self.sessions, d, open_ms,
                                                       self._symbols, now_ms)
                else:
                    analysis = build_analysis(d, open_ms, {
                        s: analyze_symbol(d, s, open_ms, None, None, None, None,
                                          ["sesion: sin fuente de datos historicos configurada"])
                        for s in self._symbols})
            except Exception as exc:  # noqa: BLE001
                log.exception("sesion %s %s: fallo el analisis por codigo", d.name, day)
                analysis = build_analysis(d, open_ms, {
                    s: analyze_symbol(d, s, open_ms, None, None, None, None, [f"sesion: error {exc!r}"])
                    for s in self._symbols})
            statuses = {a.status for a in analysis.symbols.values()}
            status = "ok" if statuses == {"ok"} else ("missing" if statuses == {"missing"} else "partial")
            if self.journal is not None:
                self.journal.append(day=day, session=d.name, kind="code", label=d.label, open_ms=open_ms,
                                    trigger_ms=trigger, status=status,
                                    summary="\n".join(a.summary for a in analysis.symbols.values()),
                                    data=analysis.model_dump(mode="json"))
            log.info("sesion %s %s: analisis por codigo guardado (%s)", d.name, day, status)
        if not with_graph:
            return None
        sess = {"name": d.name, "label": d.label, "day": day, "trigger_ms": trigger}
        result = await self._run(list(self._symbols), reason=f"sesion {d.name} {day}", session=sess)
        if result.status == "ran" and result.report and (result.report.get("summary") or result.report.get("title")):
            rep = result.report
            sections = [f"{sym}: {v.get('report')}" for sym, v in (rep.get("symbols") or {}).items() if v.get("report")]
            llm_status, summary = "con_llm", "\n".join(x for x in [rep.get("title"), rep.get("summary"), *sections] if x)
        elif result.status == "error":
            llm_status, summary = "error", f"el grafo fallo: {result.detail}"
        else:
            llm_status, summary = "sin_llm", f"sin LLM en esta sesion ({result.status}): {result.detail}"
        if self.journal is not None:
            self.journal.append(day=day, session=d.name, kind="llm", label=d.label, open_ms=open_ms, trigger_ms=trigger,
                                status=llm_status, llm_status=llm_status, run_id=result.run_id, summary=summary,
                                data={"cycle_status": result.status, "n_hypotheses": result.n_hypotheses,
                                      "decisions": result.decisions})
        return result

    async def _run_sessions_forever(self, stop: asyncio.Event, with_graph: bool = True) -> None:
        log.info("sesiones: %s (%s)", ", ".join(d.spec() for d in self.sessions),
                 "grafo anclado a sesiones, todas las criptos por ciclo" if with_graph else "solo analisis por codigo")
        while not stop.is_set():
            for d, o, _ in self.due_sessions(self._clock_ms(), "llm" if with_graph else "code"):
                if stop.is_set():
                    return
                try:
                    await self.run_session(d, o, with_graph=with_graph)
                except Exception:  # noqa: BLE001 - una sesion fallida no apaga el reloj de sesiones
                    log.exception("sesion %s: fallo", d.name)
            now = self._clock_ms()
            nd, _, nt = next_trigger(self.sessions, now)
            wait_s = min(3600.0, max(1.0, (nt + SESSION_GRACE_MS - now) / 1000))
            log.info("proxima sesion: %s en %.0f s", nd.name, wait_s)
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=wait_s)

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
        """`interval`: un ciclo cada `interval_s` (el primero tras un intervalo). `sessions`: ver docstring."""
        if self.mode == "sessions":
            await self._run_sessions_forever(stop)
            return
        watcher = None
        if self.sessions:  # el diario por codigo existe tambien en modo interval (sin LLM extra)
            watcher = asyncio.create_task(self._run_sessions_forever(stop, with_graph=False), name="sessions")
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
        if watcher is not None:
            watcher.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await watcher
