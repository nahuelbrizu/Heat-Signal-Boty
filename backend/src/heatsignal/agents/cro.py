"""
CRO (graph-v2-cio): control de riesgo SIN LLM, reglas fijas. Revisa cada decision "tomar" del CIO y la veta si se
cumple alguna de estas condiciones (en este orden; la primera que aplica queda como motivo):

  1. hueco_real     el simbolo tiene un hueco de datos REAL (`features/gaps.py`, clase "real"; las notas de cobertura
                    estructural o de respaldo no cuentan).
  2. macro          hay un evento macro FOMC, CPI o NFP (`macro/calendar.py`) en las proximas 2 h o en curso (hasta 1 h
                    despues de la publicacion). Si el calendario no se puede leer, se veta (no se verifica = no se
                    opera). Sin calendario configurado (tests) la regla no aplica.
  3. tope_sesion    se supera el tope por moneda y sesion de la familia (`entry_conditions.max_per_symbol_session`, ver
                    `synthesizer.session_cap`), contando lo ya registrado en la sesion (base) mas lo aprobado en este
                    ciclo. Sin sesion de corrida no se puede contar -> veto.
  4. correlacion    exposicion correlacionada: en altcoins (todo lo que no es BTC) no puede haber MAS DE 2 hipotesis en la
                    misma direccion contando las abiertas (sin resolver) y las ya emitidas en la sesion, mas las nuevas
                    aprobadas en este ciclo. Las nuevas se aprueban en orden de `peso` del CIO (mayor primero, luego
                    simbolo) y el resto se veta con "exposicion correlacionada".

Cada veto queda con su motivo en `cro_reviews` y en `cio_verdicts.cro_veto`. El CRO nunca aprueba algo que el CIO no
tomo ni cambia direccion o estrategia: solo puede decir que no.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Callable

from pydantic import BaseModel

from ..evaluation.strategy_registry import StrategyHypothesis
from ..features.gaps import real_gaps
from ..schema.features import FeatureSet
from .cio import CioDecision
from .synthesizer import session_cap

if TYPE_CHECKING:
    from .state import GraphState

logger = logging.getLogger(__name__)

MACRO_KINDS = ("FOMC", "CPI", "NFP")
MACRO_BEFORE_MS = 2 * 3_600_000   # evento en las proximas 2 h
MACRO_AFTER_MS = 1 * 3_600_000    # o publicado hace menos de 1 h (en curso)
MAX_SAME_DIRECTION_ALTS = 2
BTC_BASES = {"BTC"}


class CroReview(BaseModel):
    symbol: str
    approved: bool
    veto: str | None = None
    rule: str | None = None  # hueco_real | macro | tope_sesion | correlacion


def is_altcoin(symbol: str) -> bool:
    s = symbol.upper().replace("/", "-")
    base = s.split("-")[0] if "-" in s else next((s[: -len(q)] for q in ("USDT", "USDC", "USD") if s.endswith(q)), s)
    return base not in BTC_BASES


def macro_veto(calendar: Any, now_ms: int) -> str | None:
    for e in getattr(calendar, "events", ()):
        if e.kind in MACRO_KINDS and now_ms - MACRO_AFTER_MS <= e.ts_ms <= now_ms + MACRO_BEFORE_MS:
            at = datetime.fromtimestamp(e.ts_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
            when = "en curso" if e.ts_ms <= now_ms else "en las proximas 2 h"
            return f"evento macro {e.kind} ({e.label}) {at}, {when}"
    return None


def review(decisions: list[CioDecision], features: dict[str, FeatureSet], allowed: list[StrategyHypothesis], *,
           now_ms: int, calendar_fn: Callable[[], Any] | None = None, session: dict | None = None,
           session_taken: set[tuple[str, str]] | None = None,
           exposure: list[tuple[str, str, str]] | None = None) -> list[CroReview]:
    """Reglas puras (sin I/O salvo `calendar_fn`). Devuelve una revision por decision, en el mismo orden.

    `session_taken`: pares (strategy_id en minusculas, simbolo) ya emitidos en la sesion (base).
    `exposure`: (hypothesis_id, simbolo, direccion) abiertas o ya emitidas en la sesion (base).
    """
    by_id = {str(s.strategy_id).lower(): s for s in allowed}
    macro: str | None = None
    if calendar_fn is not None and any(d.takes for d in decisions):
        try:
            macro = macro_veto(calendar_fn(), now_ms)
        except Exception as exc:  # noqa: BLE001 - sin calendario no se puede verificar: se veta
            macro = f"no se pudo leer el calendario macro ({type(exc).__name__}): no se verifica, no se opera"
    cap_count: dict[tuple[str, str], int] = {}
    for sid, sym in session_taken or ():
        cap = session_cap(by_id[sid]) if sid in by_id else None
        if cap:
            cap_count[(cap[0], sym)] = cap_count.get((cap[0], sym), 0) + 1
    alt_count = {"LONG": 0, "SHORT": 0}
    for _hid, sym, direction in {(h, s, d) for h, s, d in exposure or ()}:
        if is_altcoin(sym) and direction in alt_count:
            alt_count[direction] += 1

    reviews: dict[str, CroReview] = {}
    order = sorted((d for d in decisions if d.takes), key=lambda d: (-(d.peso or 0.0), d.symbol))
    for d in order:
        fs = features.get(d.symbol)
        gaps = real_gaps(fs.data_gaps) if fs is not None else ["sin features"]
        if gaps:
            reviews[d.symbol] = CroReview(symbol=d.symbol, approved=False, rule="hueco_real",
                                          veto="hueco real de datos: " + "; ".join(gaps)[:300])
            continue
        if macro:
            reviews[d.symbol] = CroReview(symbol=d.symbol, approved=False, rule="macro", veto=macro)
            continue
        strat = by_id.get(d.strategy_id or "")
        cap = session_cap(strat) if strat is not None else None
        if cap is not None:
            if not (session or {}).get("name") or not (session or {}).get("day"):
                reviews[d.symbol] = CroReview(symbol=d.symbol, approved=False, rule="tope_sesion",
                                              veto="sin sesion de la corrida: no se puede contar el tope por sesion")
                continue
            if cap_count.get((cap[0], d.symbol), 0) >= cap[1]:
                reviews[d.symbol] = CroReview(
                    symbol=d.symbol, approved=False, rule="tope_sesion",
                    veto=f"tope por sesion: ya hay {cap[1]} de la familia {cap[0]} en {d.symbol} en esta sesion")
                continue
        if is_altcoin(d.symbol) and d.direction in alt_count:
            if alt_count[d.direction] >= MAX_SAME_DIRECTION_ALTS:
                reviews[d.symbol] = CroReview(
                    symbol=d.symbol, approved=False, rule="correlacion",
                    veto=(f"exposicion correlacionada: ya hay {alt_count[d.direction]} {d.direction} en altcoins "
                          f"(abiertas + sesion + este ciclo); tope {MAX_SAME_DIRECTION_ALTS}"))
                continue
            alt_count[d.direction] += 1
        if cap is not None:
            cap_count[(cap[0], d.symbol)] = cap_count.get((cap[0], d.symbol), 0) + 1
        reviews[d.symbol] = CroReview(symbol=d.symbol, approved=True)
    out = []
    for d in decisions:
        r = reviews.get(d.symbol) or CroReview(symbol=d.symbol, approved=False)  # no tomo: nada que aprobar
        if r.veto:
            logger.info("cro: VETO %s %s: %s", d.symbol, d.strategy_id, r.veto)
        out.append(r)
    return out


def make_cro_node(*, hypothesis_store: Any = None, calendar_fn: Callable[[], Any] | None = None,
                  clock_ms: Callable[[], int] | None = None) -> Callable[["GraphState"], Any]:
    clock = clock_ms or (lambda: int(time.time() * 1000))

    async def _node(state: "GraphState") -> dict[str, Any]:
        decisions: list[CioDecision] = list(state.get("cio_decisions") or [])
        if not any(d.takes for d in decisions):
            return {"cro_reviews": [CroReview(symbol=d.symbol, approved=False) for d in decisions]}
        allowed = list(state.get("allowed_strategies") or [])
        sess = state.get("session") or {}
        taken: set[tuple[str, str]] = set()
        taken_fn = getattr(hypothesis_store, "session_hypotheses", None)
        if callable(taken_fn) and sess.get("day") and sess.get("name"):
            taken = taken_fn(sess["day"], sess["name"])
        exp_fn = getattr(hypothesis_store, "exposure", None)
        exposure = exp_fn(sess.get("day"), sess.get("name")) if callable(exp_fn) else []
        return {"cro_reviews": review(decisions, state.get("features_by_symbol") or {}, allowed,
                                      now_ms=int(state.get("cio_ts_ms") or clock()), calendar_fn=calendar_fn,
                                      session=sess, session_taken=taken, exposure=exposure)}

    return _node
