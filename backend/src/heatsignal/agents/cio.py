"""
CIO (graph-v2-cio): el tercero superior que escucha el debate y decide. Jerarquia:
analistas (barato) -> Estrategas Alcista/Bajista (fuerte) -> **CIO (fuerte, decide)** -> CRO (codigo, riesgo) -> registro.

El CIO recibe el resumen de los analistas, la transcripcion del debate y, por simbolo, la lista de CANDIDATOS
VERIFICADOS POR CODIGO: (estrategia pre-registrada activa, direccion) cuya entrada `evaluation/timeframes.py` da por
cumplida en la vela actual. Devuelve, en UNA llamada batcheada para todos los simbolos, por simbolo:
    postura_ganadora   alcista | bajista | empate
    peso               0-1, cuanto domina esa postura (se evalua como probabilidad de acierto, ver cio_verdicts.py)
    decision           "tomar" un candidato (strategy_id + direccion) o "no_operar"
    razones            2-4 frases en lenguaje historico

Reglas duras en CODIGO (`validate_verdicts`; el CIO no inventa setups):
  - "tomar" con un strategy_id/direccion que NO esta en los candidatos de ese simbolo -> rechazo, queda no_operar con
    el motivo (incluye dar vuelta la direccion de un candidato).
  - "tomar" con postura empate o con direccion contraria a la postura (alcista<->LONG, bajista<->SHORT) -> rechazo.
  - peso fuera de [0, 1] -> rechazo (peso queda NULL: no se evalua).
  - simbolo omitido -> empate, no_operar, "omitido por el modelo".
Este nodo reemplaza la llamada LLM del Sintetizador: las llamadas por ciclo no cambian.

Candidatos: solo estrategias con `exit_conditions.timeframe = "1h"` y `entry_conditions.htf_setup` cuya entrada
cumple `check_htf_entry` y cuyo horizonte entra (`hourly_horizon`). Compatibilidad: una estrategia SIN `htf_setup`
(las de prueba previas a 1h; no hay ninguna activa en produccion) no exige setup, asi que el codigo la da por cumplida
en las dos direcciones; queda marcada `setup="sin_setup"`.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Callable, Literal

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, Field

from ..evaluation.strategy_registry import StrategyHypothesis
from ..evaluation.timeframes import check_htf_entry, hourly_horizon, is_hourly
from ..schema.features import FeatureSet
from ._common import HISTORICAL_LANGUAGE_RULE
from .debate import render_analyst_digest, render_transcript

if TYPE_CHECKING:
    from .state import GraphState

logger = logging.getLogger(__name__)

GRAPH_VERSION = "graph-v2-cio"
POSTURE_DIRECTION = {"alcista": "LONG", "bajista": "SHORT"}

_SYSTEM_PROMPT = (
    "Sos el CIO de HeatSignal: el tercero superior que escucho a los analistas y el debate entre el Estratega-Alcista "
    "y el Estratega-Bajista. Para CADA simbolo: (1) decidi que postura gano el debate (alcista, bajista o empate) y "
    "con que peso en [0, 1] (0,5 = parejo; cerca de 1 = una postura domina con evidencia clara); (2) decidi si se "
    "toma UNO de los candidatos verificados por codigo de ESE simbolo (copiando su strategy_id y su direccion "
    "EXACTOS) o 'no_operar'; (3) da 2 a 4 razones breves. Reglas: solo podes tomar un candidato listado para ese "
    "simbolo y en la direccion listada; no inventes setups ni des vuelta direcciones; si no hay candidatos, la "
    "decision es 'no_operar'. Tomar exige que la postura ganadora coincida con la direccion (alcista=LONG, "
    "bajista=SHORT); con empate, 'no_operar'. Los huecos de datos declarados pesan en contra de operar; "
    "las notas de cobertura no son huecos ni fallas severas. Despues de "
    "vos el CRO (reglas fijas) puede vetar por riesgo. " + HISTORICAL_LANGUAGE_RULE
)


class CioCandidate(BaseModel):
    strategy_id: str
    direction: Literal["LONG", "SHORT"]
    setup: str
    family: str | None = None
    description: str = ""


class CioSymbolVerdict(BaseModel):
    """Lo que devuelve el LLM por simbolo (sin restricciones duras: se validan en codigo)."""

    symbol: str
    postura_ganadora: Literal["alcista", "bajista", "empate"]
    peso: float = Field(description="Cuanto domina la postura ganadora, en [0, 1]")
    decision: Literal["tomar", "no_operar"]
    strategy_id: str | None = Field(default=None, description="strategy_id EXACTO de un candidato de ese simbolo")
    direction: Literal["LONG", "SHORT"] | None = Field(default=None, description="direccion EXACTA del candidato")
    razones: list[str] = Field(default_factory=list, description="2 a 4 frases en lenguaje historico")


class CioBatch(BaseModel):
    verdicts: list[CioSymbolVerdict]


class CioDecision(BaseModel):
    """Veredicto por simbolo tras la validacion en codigo (lo que leen el CRO, el registro y el panel)."""

    symbol: str
    postura: Literal["alcista", "bajista", "empate"]
    peso: float | None
    decision: Literal["tomar", "no_operar"]
    strategy_id: str | None = None
    direction: Literal["LONG", "SHORT"] | None = None
    razones: list[str] = Field(default_factory=list)
    candidates: list[CioCandidate] = Field(default_factory=list)
    rejection: str | None = None  # motivo si el codigo rechazo la decision del CIO

    @property
    def takes(self) -> bool:
        return self.decision == "tomar" and self.rejection is None


# ---------------------------------------------------------------- candidatos (codigo)


def code_candidates(fs: FeatureSet, strategies: list[StrategyHypothesis], *, session: dict | None = None,
                    now: datetime | None = None) -> list[CioCandidate]:
    """(estrategia, direccion) cuya entrada pre-registrada se cumple en la vela actual, segun `timeframes.py`."""
    created = (now or datetime.now(timezone.utc)).replace(microsecond=0)
    now_ms = int(created.timestamp() * 1000)
    run_session = (session or {}).get("name")
    out: list[CioCandidate] = []
    for s in strategies:
        entry = s.entry_conditions or {}
        setup = entry.get("htf_setup")
        hourly = is_hourly(s.exit_conditions)
        if setup is not None and not hourly:
            continue  # un setup de 1h sin horizonte de 1h no es verificable: no se ofrece
        if hourly and isinstance(hourly_horizon(s.exit_conditions, created), str):
            continue
        for direction in ("LONG", "SHORT"):
            if setup is not None and check_htf_entry(entry, fs.htf, direction, fs.session, now_ms, run_session):
                continue
            out.append(CioCandidate(strategy_id=str(s.strategy_id).lower(), direction=direction,
                                    setup=str(setup or "sin_setup"), family=entry.get("family"),
                                    description=s.description))
    return out


def render_candidates(by_symbol: dict[str, list[CioCandidate]]) -> str:
    lines = []
    for sym, cands in by_symbol.items():
        if not cands:
            lines.append(f"- {sym}: (ninguno: el codigo no da por cumplido ningun setup en esta vela)")
            continue
        for c in cands:
            lines.append(f"- {sym}: strategy_id={c.strategy_id} direction={c.direction} setup={c.setup}"
                         + (f" familia={c.family}" if c.family else "") + f" | {c.description[:140]}")
    return "\n".join(lines) or "(sin simbolos)"


# ---------------------------------------------------------------- validacion (codigo)


def validate_verdicts(verdicts: list[CioSymbolVerdict], candidates: dict[str, list[CioCandidate]]) -> list[CioDecision]:
    """Reglas duras del CIO. Un simbolo por decision, en el orden de `candidates`."""
    first: dict[str, CioSymbolVerdict] = {}
    for v in verdicts:
        if v.symbol not in candidates:
            logger.warning("cio: veredicto para simbolo no solicitado %r descartado", v.symbol)
            continue
        if v.symbol in first:
            logger.warning("cio: veredicto duplicado para %s: se usa el primero", v.symbol)
            continue
        first[v.symbol] = v
    out: list[CioDecision] = []
    for sym, cands in candidates.items():
        v = first.get(sym)
        if v is None:
            out.append(CioDecision(symbol=sym, postura="empate", peso=None, decision="no_operar",
                                   razones=["omitido por el modelo (sin veredicto)"], candidates=cands))
            continue
        peso_ok = isinstance(v.peso, (int, float)) and 0.0 <= float(v.peso) <= 1.0
        d = CioDecision(symbol=sym, postura=v.postura_ganadora, peso=float(v.peso) if peso_ok else None,
                        decision=v.decision, razones=[r.strip() for r in v.razones if r and r.strip()][:4],
                        candidates=cands)
        if v.decision == "tomar":
            d.strategy_id = (v.strategy_id or "").strip().lower() or None
            d.direction = v.direction
            d.rejection = _take_problem(v, d, cands, peso_ok)
            if d.rejection:
                logger.warning("cio: decision RECHAZADA para %s: %s", sym, d.rejection)
                d.decision = "no_operar"
        elif not peso_ok:
            d.rejection = f"peso fuera de [0, 1]: {v.peso!r}"
        out.append(d)
    return out


def _take_problem(v: CioSymbolVerdict, d: CioDecision, cands: list[CioCandidate], peso_ok: bool) -> str | None:
    if not peso_ok:
        return f"peso fuera de [0, 1]: {v.peso!r}"
    if not cands:
        return "no hay candidatos verificados por codigo para este simbolo: el CIO no puede inventar un setup"
    same = [c for c in cands if c.strategy_id == d.strategy_id]
    if not same:
        return f"strategy_id {v.strategy_id!r} no esta entre los candidatos verificados por codigo de {v.symbol}"
    if d.direction not in {c.direction for c in same}:
        allowed = "/".join(sorted({c.direction for c in same}))
        return f"direccion {d.direction} distinta de la del candidato ({allowed}) para {d.strategy_id}"
    want = POSTURE_DIRECTION.get(v.postura_ganadora)
    if want is None:
        return "postura empate: no se toma ningun candidato"
    if want != d.direction:
        return f"direccion {d.direction} contraria a la postura ganadora {v.postura_ganadora}"
    return None


# ---------------------------------------------------------------- nodo


def make_cio_node(llm: BaseChatModel, *, hypothesis_store: Any = None,
                  clock_ms: Callable[[], int] | None = None) -> Callable[["GraphState"], Any]:
    """`hypothesis_store.open_positions()` (si existe) saca de los candidatos lo que ya tiene hipotesis abierta."""
    structured_llm = llm.with_structured_output(CioBatch)
    clock = clock_ms or (lambda: int(time.time() * 1000))

    async def _node(state: "GraphState") -> dict[str, Any]:
        features: dict[str, FeatureSet] = state.get("features_by_symbol") or {}
        allowed: list[StrategyHypothesis] = list(state.get("allowed_strategies") or [])
        if not features:
            return {"cio_decisions": []}
        if not allowed:
            # Sin estrategias pre-registradas no hay nada que decidir: no se llama al LLM (igual que antes).
            return {"cio_decisions": [CioDecision(symbol=s, postura="empate", peso=None, decision="no_operar",
                                                  razones=["sin estrategias pre-registradas habilitadas"])
                                      for s in features]}
        now = datetime.fromtimestamp(clock() / 1000, tz=timezone.utc)
        cands = {s: code_candidates(fs, allowed, session=state.get("session"), now=now) for s, fs in features.items()}
        open_fn = getattr(hypothesis_store, "open_positions", None)
        if callable(open_fn):  # una hipotesis abierta por (estrategia, simbolo): no se ofrece lo que se rechazaria
            busy = open_fn()
            cands = {s: [c for c in cs if (c.strategy_id, s) not in busy] for s, cs in cands.items()}
        prompt = (
            f"# Candidatos verificados por codigo (unicos que se pueden tomar)\n{render_candidates(cands)}\n\n"
            f"# Salidas de los analistas\n{render_analyst_digest(state)}\n\n"
            f"# Debate\n{render_transcript(state.get('debate_transcript') or [])}"
        )
        result: CioBatch = await structured_llm.ainvoke(
            [SystemMessage(content=_SYSTEM_PROMPT), HumanMessage(content=prompt)])
        return {"cio_decisions": validate_verdicts(result.verdicts, cands), "cio_ts_ms": int(now.timestamp() * 1000)}

    return _node
