"""
Familias de hipotesis de velas de 1h con tendencia diaria: horizonte alineado y condiciones de entrada por codigo.

Una estrategia es "de 1h" si `exit_conditions["timeframe"] == "1h"`. Su horizonte se pre-registra como:
    {"timeframe": "1h", "horizon_bars": N}                         N velas de 1h completas (N >= 1)
    {"timeframe": "1h", "horizon": "end_of_day_utc", "min_bars": M}  hasta las 00:00 UTC siguientes (>= M velas)
La ventana va desde la emision hasta el CIERRE de la N-esima vela de 1h completa posterior:
    due_at = ceil_hora(created_at) + N h     (fin de dia: due_at = proximas 00:00 UTC)
asi `horizon_s = due_at - created_at` (cae entre N h y N+1 h) y `due_at` siempre es un cierre de vela de 1h, que
el Resolver usa para resolver con la vela de 1h (ver `price_source.BinanceHourlyClosePriceSource`).

Condiciones de entrada que el CODIGO verifica (el LLM no puede saltearlas) en `entry_conditions`:
    "htf_setup":       "pullback_ema20_1h_a_favor" | "continuacion_a_favor" | "extremo_rango_diario_en_contra"
    "daily_trend_in":  ["alcista", "bajista"]   tendencias en las que aplica
    "alignment":       "a_favor" | "en_contra"  direccion respecto de la tendencia del dia
    "htf_model_id":    version de la regla de tendencia con la que se pre-registro (debe coincidir)
Familias por sesion (`features/sessions.py`), ademas:
    "htf_setup":       "apertura_continuacion_a_favor" | "apertura_reversion_en_contra"
    "sessions":        ["europa", "eeuu"]       sesiones en las que aplica
    "max_minutes_after_trigger": 60             la hipotesis sale a lo sumo N min despues del cierre de la vela
                                                de apertura (en modo sessions el grafo corre ~1-2 min despues)
Familia de dias laterales (`features/htf.py`, `htf-levels-v1`; decisions.md 2026-10-19), ademas:
    "htf_setup":       "falsa_ruptura_asia" | "rechazo_dia_anterior" | "extremo_vwap"
    "daily_trend_in":  ["lateral"]              y "alignment": "lateral"
    "levels_model_id": "htf-levels-v1"          version de los niveles (debe coincidir)
    "sessions":        ["europa", "eeuu"]       (solo falsa ruptura) sesion de la CORRIDA en la que aplica
    La bandera la calcula el codigo sobre la ULTIMA vela cerrada: el contexto tiene que ser de la vela que acaba de
    cerrar (open = hora actual - 1h; un contexto atrasado se rechaza) y la direccion propuesta tiene que coincidir con
    la de la bandera. El tope "una por moneda y por sesion" (`max_per_symbol_session`) lo aplica el Sintetizador
    contra la base (`agents/synthesizer.py`).

Otras temporalidades (2026-11-19): `timeframe` puede ser "1h", "2h" o "4h" (`TIMEFRAME_HOURS`). Con "2h"/"4h" las
velas son de N h alineadas a UTC (`features/htf.py`, `FeatureSet.htf_tf`), `horizon_bars` cuenta velas de N h
(due_at = ceil a N h de la emision + N h x barras; fin de dia igual que en 1h) y la entrada se verifica sobre la vela
de N h que ACABA de cerrar: si la ultima vela cerrada del contexto no es la de [piso(N h) - N h, piso(N h)), no hay
entrada (una corrida a mitad de una vela de N h no reusa la señal vieja). Los setups de apertura de sesion son de 1h
por definicion (la vela de apertura) y no se aceptan en 2h/4h.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from ..features.htf import TIMEFRAME_HOURS, trend_alignment
from ..schema.features import HtfContext

HOUR = timedelta(hours=1)
HOUR_MS = 3_600_000
SESSION_SETUPS = ("apertura_continuacion_a_favor", "apertura_reversion_en_contra")
LATERAL_SETUPS = ("falsa_ruptura_asia", "rechazo_dia_anterior", "extremo_vwap")
SETUPS = ("pullback_ema20_1h_a_favor", "continuacion_a_favor", "extremo_rango_diario_en_contra", *SESSION_SETUPS,
          *LATERAL_SETUPS)


def is_hourly(exit_conditions: dict[str, Any]) -> bool:
    """True si la estrategia es de velas (1h, 2h o 4h) con horizonte alineado al cierre de vela."""
    return exit_conditions.get("timeframe") in TIMEFRAME_HOURS


def bar_hours(exit_conditions: dict[str, Any]) -> int:
    return TIMEFRAME_HOURS.get(str(exit_conditions.get("timeframe")), 1)


def htf_for(fs: Any, exit_conditions: dict[str, Any]) -> HtfContext | None:
    """Contexto de la temporalidad de la estrategia: `fs.htf` (1h) o `fs.htf_tf["2h"/"4h"]`."""
    tf = exit_conditions.get("timeframe")
    if tf in (None, "1h"):
        return getattr(fs, "htf", None)
    return (getattr(fs, "htf_tf", None) or {}).get(tf)


def ceil_hour(ts: datetime, hours: int = 1) -> datetime:
    """Techo de `ts` a un multiplo de `hours` h desde las 00:00 UTC."""
    ts = ts.astimezone(timezone.utc)
    floor = ts.replace(minute=0, second=0, microsecond=0)
    floor -= timedelta(hours=floor.hour % hours)
    return floor if floor == ts else floor + hours * HOUR


def hourly_horizon(exit_conditions: dict[str, Any], created_at: datetime) -> tuple[int, int] | str:
    """(horizon_s, velas) o el motivo de rechazo. `created_at` timezone-aware, sin microsegundos."""
    created = created_at.astimezone(timezone.utc)
    n = bar_hours(exit_conditions)
    bar = n * HOUR
    tf = f"{n}h"
    start = ceil_hour(created, n)
    if exit_conditions.get("horizon") == "end_of_day_utc":
        due = created.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
        bars = int((due - start) / bar)
        min_bars = int(exit_conditions.get("min_bars", 1))
        if bars < max(1, min_bars):
            return f"quedan {bars} velas de {tf} completas hasta el fin del dia UTC (< {max(1, min_bars)} pre-registradas)"
    else:
        raw = exit_conditions.get("horizon_bars")
        if not isinstance(raw, int) or isinstance(raw, bool) or raw < 1:
            return f"estrategia {tf} sin horizon_bars entero >= 1 ni horizon=end_of_day_utc: {raw!r}"
        bars = raw
        due = start + bars * bar
    return int((due - created).total_seconds()), bars


def check_htf_entry(entry_conditions: dict[str, Any], htf: HtfContext | None, direction: str,
                    session: Any = None, now_ms: int | None = None, run_session: str | None = None) -> str | None:
    """None si la propuesta cumple las condiciones pre-registradas de 1h; si no, el motivo de rechazo."""
    setup = entry_conditions.get("htf_setup")
    if setup is None:
        return None  # la estrategia no exige setup de 1h
    if setup not in SETUPS:
        return f"htf_setup desconocido en la estrategia: {setup!r}"
    tf = getattr(htf, "timeframe", "1h") if htf is not None else str(entry_conditions.get("timeframe") or "1h")
    if htf is None or htf.status == "missing":
        return f"sin contexto de {tf} (velas cerradas) para verificar la entrada"
    n = TIMEFRAME_HOURS.get(tf, 1)
    if n > 1:
        if setup in SESSION_SETUPS:
            return f"el setup {setup} es de la vela de apertura de 1h: no aplica en {tf}"
        if now_ms is not None:
            bar_ms = n * HOUR_MS
            if now_ms % bar_ms >= HOUR_MS or htf.last_closed_open_ms != now_ms - now_ms % bar_ms - bar_ms:
                return f"no cerro una vela de {tf} en esta hora: la entrada solo se verifica al cierre de la vela de {tf}"
    want_model = entry_conditions.get("htf_model_id")
    if want_model and want_model != htf.htf_model_id:
        return f"la estrategia se pre-registro con {want_model} y el contexto es {htf.htf_model_id}"
    allowed = entry_conditions.get("daily_trend_in")
    if allowed and htf.daily_trend not in allowed:
        return f"daily_trend={htf.daily_trend} no esta en {allowed}"
    align = trend_alignment(direction, htf.daily_trend)
    want_align = entry_conditions.get("alignment")
    if want_align and align != want_align:
        return f"direccion {direction} queda {align} de la tendencia {htf.daily_trend}; la estrategia exige {want_align}"
    if setup in SESSION_SETUPS:
        return _check_session(entry_conditions, setup, session, now_ms)
    if setup in LATERAL_SETUPS:
        return _check_lateral(entry_conditions, setup, htf, direction, now_ms, run_session)
    if setup == "extremo_rango_diario_en_contra":
        want = "maximo" if htf.daily_trend == "alcista" else "minimo"
        if htf.setups.extremo_rango_diario != want:
            return f"el cierre no esta en el extremo {want} del rango diario (codigo: {htf.setups.extremo_rango_diario})"
    elif not getattr(htf.setups, setup):
        return f"el setup {setup} no se cumple en el contexto de {tf} (calculado por codigo)"
    return None


def _check_session(entry_conditions: dict[str, Any], setup: str, session: Any, now_ms: int | None) -> str | None:
    """Setups por sesion: la bandera la calcula `features/sessions.py` al cerrar la vela de apertura."""
    if session is None:
        return "sin analisis de sesion del dia para este simbolo"
    allowed = entry_conditions.get("sessions")
    if allowed and session.session not in allowed:
        return f"la ultima sesion es {session.session}; la estrategia aplica a {allowed}"
    max_min = entry_conditions.get("max_minutes_after_trigger", 60)
    if now_ms is not None and now_ms - session.trigger_ms > float(max_min) * 60_000:
        return f"pasaron mas de {max_min} min desde el cierre de la vela de apertura de {session.session}"
    if not getattr(session.flags, setup, False):
        return f"el setup {setup} no se cumple en la vela de apertura de {session.session} (codigo)"
    return None


def _check_lateral(entry_conditions: dict[str, Any], setup: str, htf: HtfContext, direction: str,
                   now_ms: int | None, run_session: str | None) -> str | None:
    """Setups de dias laterales: bandera de `HtfContext.levels` (codigo) sobre la ultima vela 1h cerrada."""
    if htf.daily_trend != "lateral":  # tambien lo exige daily_trend_in, pero no depende del JSON
        return f"daily_trend={htf.daily_trend}: las estrategias de lateralidad solo aplican con daily_trend=lateral"
    lv = htf.levels
    if lv is None:
        return "sin niveles de 1h (htf-levels) para verificar la entrada"
    want_model = entry_conditions.get("levels_model_id")
    if want_model and want_model != lv.levels_model_id:
        return f"la estrategia se pre-registro con {want_model} y los niveles son {lv.levels_model_id}"
    allowed = entry_conditions.get("sessions")
    if allowed and run_session not in allowed:
        return f"la sesion de la corrida es {run_session or 'ninguna'}; la estrategia aplica a {allowed}"
    if now_ms is not None:
        bar_ms = TIMEFRAME_HOURS.get(getattr(htf, "timeframe", "1h"), 1) * HOUR_MS
        expected = now_ms - now_ms % bar_ms - bar_ms
        if htf.last_closed_open_ms != expected:
            return "el contexto no es de la ultima vela cerrada (atrasado o adelantado): no se verifica la entrada"
    flag = getattr(lv, setup)
    if flag is None:
        why = "; ".join(lv.sin_datos)
        return f"el setup {setup} no se cumple en la ultima vela 1h cerrada (codigo)" + (f" [sin_datos: {why}]" if why else "")
    if flag != direction:
        return f"el setup {setup} da {flag} (codigo) y la propuesta es {direction}"
    return None
