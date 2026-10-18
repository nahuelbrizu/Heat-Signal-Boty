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
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from ..features.htf import trend_alignment
from ..schema.features import HtfContext

HOUR = timedelta(hours=1)
SESSION_SETUPS = ("apertura_continuacion_a_favor", "apertura_reversion_en_contra")
SETUPS = ("pullback_ema20_1h_a_favor", "continuacion_a_favor", "extremo_rango_diario_en_contra", *SESSION_SETUPS)


def is_hourly(exit_conditions: dict[str, Any]) -> bool:
    return exit_conditions.get("timeframe") == "1h"


def ceil_hour(ts: datetime) -> datetime:
    floor = ts.replace(minute=0, second=0, microsecond=0)
    return floor if floor == ts else floor + HOUR


def hourly_horizon(exit_conditions: dict[str, Any], created_at: datetime) -> tuple[int, int] | str:
    """(horizon_s, velas) o el motivo de rechazo. `created_at` timezone-aware, sin microsegundos."""
    created = created_at.astimezone(timezone.utc)
    start = ceil_hour(created)
    if exit_conditions.get("horizon") == "end_of_day_utc":
        due = created.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
        bars = int((due - start) / HOUR)
        min_bars = int(exit_conditions.get("min_bars", 1))
        if bars < max(1, min_bars):
            return f"quedan {bars} velas de 1h completas hasta el fin del dia UTC (< {max(1, min_bars)} pre-registradas)"
    else:
        raw = exit_conditions.get("horizon_bars")
        if not isinstance(raw, int) or isinstance(raw, bool) or raw < 1:
            return f"estrategia 1h sin horizon_bars entero >= 1 ni horizon=end_of_day_utc: {raw!r}"
        bars = raw
        due = start + bars * HOUR
    return int((due - created).total_seconds()), bars


def check_htf_entry(entry_conditions: dict[str, Any], htf: HtfContext | None, direction: str,
                    session: Any = None, now_ms: int | None = None) -> str | None:
    """None si la propuesta cumple las condiciones pre-registradas de 1h; si no, el motivo de rechazo."""
    setup = entry_conditions.get("htf_setup")
    if setup is None:
        return None  # la estrategia no exige setup de 1h
    if setup not in SETUPS:
        return f"htf_setup desconocido en la estrategia: {setup!r}"
    if htf is None or htf.status == "missing":
        return "sin contexto de 1h (velas cerradas) para verificar la entrada"
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
    if setup == "extremo_rango_diario_en_contra":
        want = "maximo" if htf.daily_trend == "alcista" else "minimo"
        if htf.setups.extremo_rango_diario != want:
            return f"el cierre no esta en el extremo {want} del rango diario (codigo: {htf.setups.extremo_rango_diario})"
    elif not getattr(htf.setups, setup):
        return f"el setup {setup} no se cumple en el contexto de 1h (calculado por codigo)"
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
