"""
Diario de lecciones: una leccion corta (2-4 oraciones) por hipotesis RESUELTA. Solo para que la lea el usuario.

REGLA DURA: estas lecciones NO entran a ningun prompt del grafo ni a ninguna decision (ni al Sintetizador, ni al
debate, ni al Redactor, ni al bot de paper). Reinyectarlas cambiaria el sistema sin pre-registro ni conteo de
intentos (anti-p-hacking). Lo verifica `tests/test_lessons.py` (ningun modulo de agents/, pipeline/, paperbot/ ni
paper/ importa este modulo, y un grafo con fakes no ve el texto de una leccion guardada).

Inspirado en TradingAgents (`tradingagents/memory/reflection.py`, TauricResearch, Apache License 2.0): una llamada
al modelo rapido con el resultado ya conocido; nombrar la ventana porque acota lo que el resultado puede mostrar
("a thesis written for months is not disproved by a week"). Diferencia de fondo: alla la reflexion se re-inyecta
en corridas futuras; aca NO, a proposito.

Contenido (los NUMEROS y el veredicto de la ventana los pone el CODIGO; el LLM solo redacta):
  1. que mostro el resultado frente a la tesis (PnL neto, movimiento real, contra mantener);
  2. si la ventana alcanza para juzgar: movimiento real vs el ruido esperable en el horizonte
     (ATR14 de 1h al emitir / precio x raiz(horas)); siempre es UNA hipotesis (n = 1);
  3. que condicion de contexto estaba presente (tendencia del dia, alineacion, sesion, temporalidad y el texto del
     analista de contexto del ciclo, que incluye funding/OI).

Generacion: `write_missing()` desde el job de evaluacion, una llamada al LLM barato (`llm_factory`, `cheap`) por
hipotesis. Sin LLM: version por codigo, `source='codigo'`. Si el LLM falla, se reintenta en la proxima corrida;
pasadas `LLM_RETRY_HOURS` se guarda la version por codigo con el error anotado (nunca queda sin leccion en silencio).

Persistencia: tabla `hypothesis_lessons` en `heatsignal_eval.sqlite`, append-only (triggers), UNA por hipotesis
(PRIMARY KEY): idempotente, re-correr el job no duplica ni pisa.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import re
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from ..agents.guardrails import find_predictive_phrases

log = logging.getLogger("heatsignal.evaluation.lessons")

LESSON_MODEL_ID = "lesson-v1"
MAX_PER_RUN = 20
LLM_RETRY_HOURS = 6.0
MAX_TEXT_CHARS = 900

SCHEMA = """
CREATE TABLE IF NOT EXISTS hypothesis_lessons (
    hypothesis_id TEXT PRIMARY KEY,
    strategy_id TEXT NOT NULL,
    symbol TEXT NOT NULL,
    created_ms INTEGER NOT NULL,
    hypothesis_created_ms INTEGER,
    resolved_ms INTEGER,
    outcome TEXT NOT NULL,
    direction TEXT,
    pnl_pct REAL,
    real_move_pct REAL,
    alpha_pct REAL,
    daily_trend TEXT,
    trend_alignment TEXT,
    session TEXT,
    timeframe TEXT,
    window_verdict TEXT NOT NULL,
    source TEXT NOT NULL,
    model TEXT,
    llm_error TEXT,
    lesson_model_id TEXT NOT NULL,
    text TEXT NOT NULL,
    facts TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_lessons_strategy ON hypothesis_lessons(strategy_id, created_ms);
CREATE TRIGGER IF NOT EXISTS trg_lessons_no_update BEFORE UPDATE ON hypothesis_lessons
BEGIN SELECT RAISE(ABORT, 'hypothesis_lessons es append-only'); END;
CREATE TRIGGER IF NOT EXISTS trg_lessons_no_delete BEFORE DELETE ON hypothesis_lessons
BEGIN SELECT RAISE(ABORT, 'hypothesis_lessons es append-only'); END;
"""

COLUMNS = ["hypothesis_id", "strategy_id", "symbol", "created_ms", "hypothesis_created_ms", "resolved_ms", "outcome",
           "direction", "pnl_pct", "real_move_pct", "alpha_pct", "daily_trend", "trend_alignment", "session",
           "timeframe", "window_verdict", "source", "model", "llm_error", "lesson_model_id", "text", "facts"]

WINDOW_TEXT = {
    "alcanza": "el movimiento real supero el ruido esperable de la ventana",
    "dudoso": "el movimiento real quedo cerca del ruido esperable de la ventana",
    "no_alcanza": "el movimiento real quedo dentro del ruido esperable: la ventana no alcanza para juzgar la tesis",
    "sin_dato": "sin ATR de 1h al emitir: no se puede medir si la ventana alcanza",
}


def _ms(dt: datetime | None) -> int | None:
    return None if dt is None else int(dt.timestamp() * 1000)


def window_verdict(real_move_pct: float | None, atr_pct_1h: float | None, horizon_s: int) -> tuple[str, float | None]:
    """(veredicto, ruido esperable en %). Ruido = ATR14(1h)% x raiz(horas del horizonte), regla fija de lesson-v1."""
    if real_move_pct is None or not atr_pct_1h or atr_pct_1h <= 0:
        return "sin_dato", None
    noise = atr_pct_1h * math.sqrt(max(horizon_s / 3600.0, 1 / 60))
    ratio = abs(real_move_pct) / noise
    return ("no_alcanza" if ratio < 0.5 else "dudoso" if ratio < 1.0 else "alcanza"), noise


@dataclass
class LessonFacts:
    hypothesis_id: str
    strategy_id: str
    strategy: str
    symbol: str
    direction: str
    confidence: float
    created_ms: int | None
    resolved_ms: int | None
    horizon_s: int
    outcome: str                 # acierto | fallo
    pnl_pct: float | None
    gross_pnl_pct: float | None
    real_move_pct: float | None
    benchmark_pnl_pct: float | None
    alpha_pct: float | None
    window_verdict: str
    noise_pct: float | None
    daily_trend: str | None
    trend_alignment: str | None
    session: str | None
    timeframe: str | None
    rationale: str
    cycle_context: str | None    # texto del Analista de Contexto del ciclo (funding / OI / regimen)

    def as_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


def facts_from_record(rec: Any, strategy_desc: str = "", cycle_context: str | None = None) -> LessonFacts:
    h, res, met = rec.hypothesis, rec.resolution, rec.metrics
    real_move = None
    if res is not None and res.exit_price is not None:
        real_move = float((res.exit_price / h.entry_reference_price - 1) * 100)
    atr_pct = None
    snap = h.htf_snapshot or {}
    if snap.get("atr14_1h") and snap.get("last_close"):
        try:
            atr_pct = float(snap["atr14_1h"]) / float(snap["last_close"]) * 100
        except (TypeError, ValueError, ZeroDivisionError):
            atr_pct = None
    verdict, noise = window_verdict(real_move, atr_pct, h.horizon_s)
    return LessonFacts(
        hypothesis_id=str(h.hypothesis_id), strategy_id=str(h.strategy_id), strategy=strategy_desc or str(h.strategy_id),
        symbol=h.symbol, direction=h.direction.value, confidence=h.confidence_score, created_ms=_ms(h.created_at),
        resolved_ms=_ms(res.resolved_at) if res else None, horizon_s=h.horizon_s,
        outcome="acierto" if res is not None and res.win else "fallo",
        pnl_pct=None if res is None else res.pnl_pct, gross_pnl_pct=None if met is None else met.gross_pnl_pct,
        real_move_pct=real_move, benchmark_pnl_pct=None if met is None else met.benchmark_pnl_pct,
        alpha_pct=None if met is None else met.alpha_pct, window_verdict=verdict, noise_pct=noise,
        daily_trend=h.daily_trend, trend_alignment=h.trend_alignment, session=h.session, timeframe=h.timeframe,
        rationale=(h.rationale or "")[:600], cycle_context=(cycle_context or None) and cycle_context[:400],
    )


def _p(v: float | None) -> str:
    return "n/d" if v is None else f"{v:+.2f} %".replace(".", ",")


def _horizon(s: int) -> str:
    return f"{s / 3600:g} h" if s >= 3600 else f"{s / 60:g} min"


def code_lesson(f: LessonFacts) -> str:
    """Version por CODIGO (sin LLM), siempre en lenguaje historico."""
    dir_es = {"long": "largo", "short": "corto"}.get(f.direction, f.direction)
    s1 = (f"La hipotesis {dir_es} en {f.symbol} termino en {f.outcome}: PnL neto {_p(f.pnl_pct)} con un movimiento real "
          f"del precio de {_p(f.real_move_pct)} en {_horizon(f.horizon_s)} (mantener rindio {_p(f.benchmark_pnl_pct)}).")
    noise = "" if f.noise_pct is None else f" (ruido esperable ~{f.noise_pct:.2f} %)".replace(".", ",")
    s2 = f"Sobre la ventana: {WINDOW_TEXT[f.window_verdict]}{noise}; es una sola hipotesis (n = 1), no juzga la estrategia."
    ctx = [f"tendencia del dia {f.daily_trend or 'sin registro'}",
           f"direccion {'a favor' if f.trend_alignment == 'a_favor' else 'en contra' if f.trend_alignment == 'en_contra' else (f.trend_alignment or 'sin registro')} de la tendencia",
           f"sesion {f.session or 'sin registro'}"]
    if f.timeframe:
        ctx.append(f"temporalidad {f.timeframe}")
    s3 = "Contexto al emitir: " + ", ".join(ctx) + "."
    return " ".join((s1, s2, s3))


class LessonText(BaseModel):
    text: str = Field(description="2 a 4 oraciones en espanol, lenguaje historico, sin markdown ni vinetas")


SYSTEM_PROMPT = (
    "Sos el redactor del diario de lecciones de HeatSignal. Una hipotesis ya se resolvio y tenes los NUMEROS "
    "calculados por codigo. Escribi entre 2 y 4 oraciones en espanol rioplatense, prosa simple, sin vinetas ni "
    "markdown, en este orden: (1) que mostro el resultado frente a la tesis, citando el PnL neto y el movimiento "
    "real; (2) si la ventana alcanza para juzgar, usando EXACTAMENTE el veredicto de ventana dado y recordando que es "
    "una sola hipotesis; (3) que condicion de contexto estaba presente (tendencia del dia, alineacion, sesion, "
    "funding u OI si aparecen en el contexto del ciclo). Lenguaje HISTORICO: describi lo que paso; nada de "
    "pronosticos, consejos de operacion ni frases como 'va a', 'subira', 'conviene'. No inventes numeros. "
    "Este texto es solo para que lo lea una persona: no se usa para decidir nada."
)


def human_prompt(f: LessonFacts) -> str:
    return "\n".join([
        f"Estrategia: {f.strategy}",
        f"Simbolo: {f.symbol} | direccion: {f.direction} | confidence: {f.confidence:.2f} | horizonte: {_horizon(f.horizon_s)}",
        f"Resultado: {f.outcome} | PnL neto {_p(f.pnl_pct)} | bruto {_p(f.gross_pnl_pct)} | movimiento real {_p(f.real_move_pct)} | "
        f"mantener {_p(f.benchmark_pnl_pct)} | alpha {_p(f.alpha_pct)}",
        f"Veredicto de ventana (codigo): {f.window_verdict} — {WINDOW_TEXT[f.window_verdict]}"
        + ("" if f.noise_pct is None else f" (ruido esperable {f.noise_pct:.2f} %)"),
        f"Contexto al emitir: tendencia {f.daily_trend or 'sin registro'}, alineacion {f.trend_alignment or 'sin registro'}, "
        f"sesion {f.session or 'sin registro'}, temporalidad {f.timeframe or 'n/d'}",
        f"Contexto del ciclo (analista): {f.cycle_context or 'no disponible'}",
        f"Tesis (justificacion al emitir): {f.rationale or 'n/d'}",
    ])


def _clean(text: str) -> str:
    t = re.sub(r"[*#`>_]+", "", text or "").strip()
    t = re.sub(r"^\s*[-•]\s*", "", t, flags=re.M)
    t = re.sub(r"\s+", " ", t)
    return t[:MAX_TEXT_CHARS]


def _sentences(t: str) -> int:
    return len([s for s in re.split(r"(?<=[.!?])\s+", t.strip()) if s])


def validate_llm_text(t: str) -> str | None:
    """None si sirve; si no, el motivo (entonces se usa la version por codigo)."""
    if not t:
        return "texto vacio"
    n = _sentences(t)
    if n < 2 or n > 5:
        return f"{n} oraciones (se piden 2-4)"
    bad = find_predictive_phrases(t)
    if bad:
        return "lenguaje predictivo: " + ", ".join(bad[:3])
    return None


class LessonStore:
    def __init__(self, db_path: str | Path) -> None:
        self.path = Path(db_path)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self.path, timeout=30.0, check_same_thread=False)
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    def add(self, row: dict[str, Any]) -> bool:
        """True si la agrego; False si esa hipotesis ya tenia leccion (idempotente)."""
        with self._lock, self._conn:
            cur = self._conn.execute(
                f"INSERT OR IGNORE INTO hypothesis_lessons ({', '.join(COLUMNS)}) VALUES ({', '.join('?' * len(COLUMNS))})",
                [row.get(c) for c in COLUMNS])
        return cur.rowcount == 1

    def has(self, hypothesis_id: str) -> bool:
        with self._lock:
            return self._conn.execute("SELECT 1 FROM hypothesis_lessons WHERE hypothesis_id=?",
                                      (hypothesis_id,)).fetchone() is not None

    def missing(self, limit: int = MAX_PER_RUN) -> list[tuple[str, float]]:
        """(hypothesis_id, resolved_ts) de hipotesis RESUELTAS (con resultado) sin leccion, las mas viejas primero."""
        with self._lock:
            try:
                rows = self._conn.execute(
                    "SELECT r.hypothesis_id, r.resolved_at FROM hypothesis_resolutions r "
                    "LEFT JOIN hypothesis_lessons l ON l.hypothesis_id = r.hypothesis_id "
                    "WHERE r.status = 'resolved' AND l.hypothesis_id IS NULL ORDER BY r.id LIMIT ?",
                    (int(limit),)).fetchall()
            except sqlite3.OperationalError:  # base sin tablas de hipotesis todavia
                return []
        out = []
        for hid, at in rows:
            try:
                ts = datetime.fromisoformat(str(at).replace("Z", "+00:00")).timestamp()
            except ValueError:
                ts = 0.0
            out.append((hid, ts))
        return out


def lesson_row(f: LessonFacts, text: str, *, source: str, model: str | None, llm_error: str | None,
               now_ms: int) -> dict[str, Any]:
    return {
        "hypothesis_id": f.hypothesis_id, "strategy_id": f.strategy_id, "symbol": f.symbol, "created_ms": now_ms,
        "hypothesis_created_ms": f.created_ms, "resolved_ms": f.resolved_ms, "outcome": f.outcome,
        "direction": f.direction, "pnl_pct": f.pnl_pct, "real_move_pct": f.real_move_pct, "alpha_pct": f.alpha_pct,
        "daily_trend": f.daily_trend, "trend_alignment": f.trend_alignment, "session": f.session,
        "timeframe": f.timeframe, "window_verdict": f.window_verdict, "source": source, "model": model,
        "llm_error": llm_error, "lesson_model_id": LESSON_MODEL_ID, "text": text,
        "facts": json.dumps(f.as_dict(), ensure_ascii=False, default=str),
    }


async def _ask(structured: Any, f: LessonFacts) -> str:
    from langchain_core.messages import HumanMessage, SystemMessage

    out = await structured.ainvoke([SystemMessage(content=SYSTEM_PROMPT), HumanMessage(content=human_prompt(f))])
    return _clean(getattr(out, "text", "") if not isinstance(out, dict) else out.get("text", ""))


def write_missing(lessons: LessonStore, store: Any, registry: Any = None, *, llm: Any = None, model_name: str | None = None,
                  cycle_log: Any = None, now: datetime | None = None, limit: int = MAX_PER_RUN) -> dict[str, Any]:
    """Escribe las lecciones que falten. Devuelve un resumen (para el JSON del job). Nunca lanza por el LLM."""
    from uuid import UUID

    now = now or datetime.now(timezone.utc)
    now_ms = int(now.timestamp() * 1000)
    summary: dict[str, Any] = {"llm_available": llm is not None, "written": 0, "llm": 0, "codigo": 0, "deferred": 0, "errors": []}
    todo = lessons.missing(limit)
    if not todo:
        return summary
    structured = llm.with_structured_output(LessonText) if llm is not None else None
    loop = asyncio.new_event_loop() if structured is not None else None
    try:
        for hid, resolved_ts in todo:
            rec = store.get_record(UUID(hid))
            if rec is None or rec.resolution is None:
                continue
            desc = ""
            if registry is not None:
                s = registry.get(rec.hypothesis.strategy_id)
                desc = getattr(s, "description", "") if s is not None else ""
            ctx_text = None
            if cycle_log is not None and rec.hypothesis.run_id:
                try:
                    row = cycle_log.get(rec.hypothesis.run_id)
                    sym = ((row or {}).get("report") or {}).get("symbols", {}).get(rec.hypothesis.symbol) or {}
                    ctx_text = (sym.get("context") or {}).get("text")
                except Exception:  # noqa: BLE001
                    ctx_text = None
            f = facts_from_record(rec, desc, ctx_text)
            text, source, err = None, "codigo", None
            if structured is not None:
                try:
                    candidate = loop.run_until_complete(_ask(structured, f))
                    problem = validate_llm_text(candidate)
                    if problem is None:
                        text, source = candidate, "llm"
                    else:
                        err = f"texto del LLM descartado: {problem}"
                except Exception as exc:  # noqa: BLE001 - el LLM nunca tumba el job
                    err = f"{type(exc).__name__}: {str(exc)[:200]}"
                    age_h = (now.timestamp() - resolved_ts) / 3600 if resolved_ts else 0
                    if age_h < LLM_RETRY_HOURS:
                        summary["deferred"] += 1
                        summary["errors"].append(f"{hid[:8]}: {err} (se reintenta)")
                        continue
            if text is None:
                text = code_lesson(f)
            if lessons.add(lesson_row(f, text, source=source, model=model_name if source == "llm" else None,
                                      llm_error=err, now_ms=now_ms)):
                summary["written"] += 1
                summary[source] += 1
    finally:
        if loop is not None:
            loop.close()
    return summary


# ---------------------------------------------------------------- lectura (panel, conexion de solo lectura)

FILTERS = ("strategy_id", "symbol", "daily_trend", "outcome", "source", "window_verdict")


def _row_json(r: tuple) -> dict[str, Any]:
    d = dict(zip(COLUMNS, r))
    try:
        d["facts"] = json.loads(d["facts"])
    except (TypeError, ValueError):
        d["facts"] = None
    for k in ("created_ms", "resolved_ms", "hypothesis_created_ms"):
        v = d.get(k)
        d[k.replace("_ms", "_at")] = None if v is None else datetime.fromtimestamp(v / 1000, tz=timezone.utc).isoformat().replace("+00:00", "Z")
    return d


def query(conn: sqlite3.Connection, *, limit: int = 50, offset: int = 0, **filters: Any) -> dict[str, Any]:
    where, params = [], []
    for k in FILTERS:
        v = filters.get(k)
        if v in (None, ""):
            continue
        if v == "none" and k == "daily_trend":
            where.append("daily_trend IS NULL")
        else:
            where.append(f"{k} = ?")
            params.append(v)
    clause = ("WHERE " + " AND ".join(where)) if where else ""
    try:
        total = int(conn.execute(f"SELECT COUNT(*) FROM hypothesis_lessons {clause}", params).fetchone()[0])
        rows = conn.execute(f"SELECT {', '.join(COLUMNS)} FROM hypothesis_lessons {clause} "
                            "ORDER BY created_ms DESC, hypothesis_id LIMIT ? OFFSET ?", [*params, limit, offset]).fetchall()
        facets = {}
        for k in ("strategy_id", "symbol", "daily_trend", "outcome", "source"):
            facets[k] = [{"value": v, "n": n} for v, n in conn.execute(
                f"SELECT {k}, COUNT(*) FROM hypothesis_lessons GROUP BY {k} ORDER BY COUNT(*) DESC").fetchall()]
    except sqlite3.OperationalError:  # todavia no hay tabla (el job no corrio con este codigo)
        return {"total": 0, "limit": limit, "offset": offset, "items": [], "facets": {}, "available": False}
    return {"total": total, "limit": limit, "offset": offset, "items": [_row_json(r) for r in rows], "facets": facets,
            "available": True}


def get_one(conn: sqlite3.Connection, hypothesis_id: str) -> dict[str, Any] | None:
    try:
        r = conn.execute(f"SELECT {', '.join(COLUMNS)} FROM hypothesis_lessons WHERE hypothesis_id = ?",
                         (hypothesis_id,)).fetchone()
    except sqlite3.OperationalError:
        return None
    return None if r is None else _row_json(r)


def make_cheap_llm() -> tuple[Any, str | None, str | None]:
    """(llm barato, nombre, motivo si no hay). Usa `llm_factory.make_llms` sin modificarlo."""
    try:
        from ..agents.llm_factory import MissingApiKeyError, make_llms
    except Exception as exc:  # noqa: BLE001 - sin dependencias de LLM instaladas
        return None, None, f"sin LLM ({type(exc).__name__})"
    try:
        routing = make_llms()
    except MissingApiKeyError as exc:
        return None, None, str(exc)
    except Exception as exc:  # noqa: BLE001
        return None, None, f"no se pudo armar el LLM: {type(exc).__name__}: {str(exc)[:160]}"
    cheap = routing.cheap
    models = getattr(cheap, "models", None)
    name = getattr(models[0] if models else cheap, "model", None) or getattr(models[0] if models else cheap, "model_name", None)
    return cheap, (str(name) if name else "cheap") + (f" (+{len(models) - 1} respaldo)" if models and len(models) > 1 else ""), None
