"""
Sesiones de mercado (`session-v1`): horarios con DST correcto (zoneinfo) y analisis por codigo de la vela de apertura.

Sesiones por defecto (`HEATSIGNAL_SESSIONS`, formato `nombre=Zona/IANA@HH:MM`, separadas por coma):
    asia    Asia/Tokyo        09:00  (= 00:00 UTC; Japon no tiene horario de verano)
    europa  Europe/London     08:00  (07:00 UTC con BST, 08:00 UTC en invierno)
    eeuu    America/New_York  09:30  (apertura de acciones: 13:30 UTC con EDT, 14:30 UTC con EST)
    cierre  UTC               23:00  (rol "close": la ultima vela del dia UTC; el analisis resume el dia que termina)
El nombre `cierre` tiene rol "close"; el resto, "open".

Disparo: cuando CIERRA la primera vela de 1h de la sesion -> trigger = apertura + 1h (solo velas cerradas; en el cierre,
23:00 + 1h = 00:00 UTC). El dia del diario es el dia UTC de la apertura de la vela, asi el orden dentro de un dia es
Asia -> Europa -> EE.UU. -> cierre.

La vela de apertura se arma desde 1m sobre [apertura, apertura + 1h): la apertura de Nueva York (13:30 UTC) no
coincide con las velas de 1h alineadas a la hora, por eso no se usa la vela de 1h de la base.

Banderas (por codigo; las exigen las estrategias por sesion):
    apertura_continuacion_a_favor: daily_trend alcista/bajista y |cuerpo| >= 50% del rango en la direccion de la tendencia.
    apertura_reversion_en_contra:  alcista: max de la vela > max de la sesion anterior y cierre <= ese max;
                                   bajista: min < min de la sesion anterior y cierre >= ese min.
`sesion anterior` = desde la apertura de la sesion "open" previa hasta la apertura de esta (1m).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Literal
from zoneinfo import ZoneInfo

from ..schema.features import HtfContext
from .gaps import real_gaps
from ..schema.sessions import (
    DayOwnData,
    OpeningCandle,
    SessionAnalysis,
    SessionFlags,
    SessionSymbolAnalysis,
)

HOUR_MS = 3_600_000
DAY_MS = 86_400_000
DEFAULT_SESSIONS_SPEC = "asia=Asia/Tokyo@09:00,europa=Europe/London@08:00,eeuu=America/New_York@09:30,cierre=UTC@23:00"
LABELS = {"asia": "Asia (Tokio)", "europa": "Europa (Londres)", "eeuu": "EE.UU. (Nueva York)", "cierre": "Cierre del día UTC"}
BODY_FRAC_MIN = 0.5
VOL_DAYS = 20


@dataclass(frozen=True)
class SessionDef:
    name: str
    tz: str
    hour: int
    minute: int
    role: Literal["open", "close"] = "open"

    @property
    def label(self) -> str:
        return LABELS.get(self.name, self.name)

    def open_utc_ms(self, local_day: date) -> int:
        """Apertura de la sesion en el dia LOCAL `local_day`, en epoch ms UTC (zoneinfo resuelve el DST)."""
        local = datetime(local_day.year, local_day.month, local_day.day, self.hour, self.minute, tzinfo=ZoneInfo(self.tz))
        return int(local.astimezone(timezone.utc).timestamp() * 1000)

    def spec(self) -> str:
        return f"{self.name}={self.tz}@{self.hour:02d}:{self.minute:02d}"


def parse_sessions(spec: str | None = None) -> list[SessionDef]:
    out: list[SessionDef] = []
    for part in (spec or DEFAULT_SESSIONS_SPEC).split(","):
        part = part.strip()
        if not part:
            continue
        try:
            name, rest = part.split("=", 1)
            tz, hhmm = rest.split("@", 1)
            hh, mm = (int(x) for x in hhmm.split(":", 1))
            ZoneInfo(tz.strip())
        except Exception as exc:  # noqa: BLE001
            raise ValueError(f"sesion mal escrita {part!r} (formato nombre=Zona/IANA@HH:MM): {exc}") from exc
        if not (0 <= hh < 24 and 0 <= mm < 60):
            raise ValueError(f"hora invalida en {part!r}")
        name = name.strip().lower()
        out.append(SessionDef(name, tz.strip(), hh, mm, "close" if name == "cierre" else "open"))
    if len({d.name for d in out}) != len(out):
        raise ValueError("nombres de sesion repetidos")
    return out


def _utc_date(ms: int) -> date:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).date()


def day_str(ms: int) -> str:
    return _utc_date(ms).isoformat()


def opens_for_utc_day(defs: Sequence[SessionDef], day_start_ms: int) -> list[tuple[SessionDef, int]]:
    """Sesiones cuya vela de apertura empieza en el dia UTC [day_start, day_start + 1 dia), por hora."""
    d0 = _utc_date(day_start_ms)
    out = []
    for d in defs:
        for k in (-1, 0, 1):
            o = d.open_utc_ms(d0 + timedelta(days=k))
            if day_start_ms <= o < day_start_ms + DAY_MS:
                out.append((d, o))
    return sorted(out, key=lambda x: x[1])


def triggers_between(defs: Sequence[SessionDef], start_ms: int, end_ms: int) -> list[tuple[SessionDef, int, int]]:
    """(sesion, apertura, disparo) con start < disparo <= end, ordenados por disparo."""
    out = []
    day = start_ms - start_ms % DAY_MS - DAY_MS
    while day <= end_ms:
        for d, o in opens_for_utc_day(defs, day):
            t = o + HOUR_MS
            if start_ms < t <= end_ms:
                out.append((d, o, t))
        day += DAY_MS
    return sorted(out, key=lambda x: x[2])


def next_trigger(defs: Sequence[SessionDef], now_ms: int) -> tuple[SessionDef, int, int]:
    found = triggers_between(defs, now_ms, now_ms + 3 * DAY_MS)
    if not found:
        raise ValueError("no hay sesiones configuradas")
    return found[0]


def session_at(defs: Sequence[SessionDef], ts_ms: int) -> tuple[str, str] | None:
    """(sesion, dia del diario) del ultimo analisis de sesion disparado <= ts: de que sesion "sale" una hipotesis."""
    found = triggers_between(defs, ts_ms - 2 * DAY_MS, ts_ms)
    if not found:
        return None
    d, o, _ = found[-1]
    return d.name, day_str(o)


def prev_open(defs: Sequence[SessionDef], open_ms: int) -> tuple[SessionDef, int] | None:
    """Sesion de rol 'open' inmediatamente anterior a `open_ms` (puede ser del dia UTC anterior)."""
    cands = []
    day = open_ms - open_ms % DAY_MS
    for k in (0, 1, 2):
        cands += [(d, o) for d, o in opens_for_utc_day(defs, day - k * DAY_MS) if d.role == "open" and o < open_ms]
    return max(cands, key=lambda x: x[1]) if cands else None


# ---- analisis por codigo ------------------------------------------------------------------------

def opening_candle(minutes: Sequence[tuple], open_ms: int, atr: float | None, vol_history: Sequence[float],
                   sources: dict[str, int] | None = None) -> OpeningCandle | None:
    """minutes: (minute_ms, o, h, l, c, quote_vol|None) dentro de [open_ms, open_ms + 1h)."""
    ms = sorted((m for m in minutes if open_ms <= m[0] < open_ms + HOUR_MS and m[1] is not None), key=lambda m: m[0])
    if not ms:
        return None
    o, c = float(ms[0][1]), float(ms[-1][4])
    h, low = max(float(m[2]) for m in ms), min(float(m[3]) for m in ms)
    rng = h - low
    vols = [m[5] for m in ms if len(m) > 5 and m[5] is not None]
    qv = sum(vols) if len(vols) == len(ms) else None
    avg = sum(vol_history) / len(vol_history) if vol_history else None
    return OpeningCandle(
        open_ms=open_ms, open=o, high=h, low=low, close=c, minutes=len(ms), sources=dict(sources or {}),
        quote_volume=qv, range=rng, body=c - o, upper_wick=h - max(o, c), lower_wick=min(o, c) - low,
        body_frac=abs(c - o) / rng if rng > 0 else 0.0, range_atr=rng / atr if atr else None,
        vol_avg_20d=avg, vol_days=len(vol_history), vol_ratio=qv / avg if qv is not None and avg else None,
    )


def analyze_symbol(
    d: SessionDef,
    symbol: str,
    open_ms: int,
    opening: OpeningCandle | None,
    htf: HtfContext | None,
    prev: tuple[str, float, float] | None,
    own: DayOwnData | None,
    gaps: Sequence[str] = (),
) -> SessionSymbolAnalysis:
    data_gaps = list(gaps)
    trigger = open_ms + HOUR_MS
    if opening is None:
        data_gaps.append("sesion: sin minutos de precio para la vela de apertura")
    elif opening.minutes < 60:
        data_gaps.append(f"sesion: vela de apertura armada con {opening.minutes}/60 minutos")
    if opening is not None and opening.vol_days < VOL_DAYS:
        data_gaps.append(f"sesion: volumen medio con {opening.vol_days}/{VOL_DAYS} dias")
    if htf is None:
        data_gaps.append("sesion: sin contexto de 1h (tendencia diaria)")
    trend = htf.daily_trend if htf is not None else "sin_datos"
    body_dir = None
    if opening is not None and opening.body_frac >= BODY_FRAC_MIN and opening.body != 0:
        body_dir = "alcista" if opening.body > 0 else "bajista"
    broke = None
    flags = SessionFlags()
    if opening is not None and prev is not None and d.role == "open":
        _, ph, pl = prev
        up, dn = opening.high > ph, opening.low < pl
        broke = "ambos" if up and dn else "arriba" if up else "abajo" if dn else "no"
        if trend == "alcista":
            flags.apertura_reversion_en_contra = bool(up and opening.close <= ph)
        elif trend == "bajista":
            flags.apertura_reversion_en_contra = bool(dn and opening.close >= pl)
    elif d.role == "open":
        data_gaps.append("sesion: sin rango de la sesion anterior")
    if trend in ("alcista", "bajista") and body_dir == trend and d.role == "open":
        flags.apertura_continuacion_a_favor = True
    day_open = htf.day_open if htf is not None else None
    ref_close = opening.close if opening is not None else (htf.last_close if htf is not None else None)
    status = "missing" if opening is None else ("ok" if opening.minutes == 60 and htf is not None
                                               and htf.status == "ok" else "partial")
    a = SessionSymbolAnalysis(
        symbol=symbol, session=d.name, day=day_str(open_ms), role=d.role, open_ms=open_ms, trigger_ms=trigger,
        status=status, opening=opening, opening_body_dir=body_dir, day_open=day_open,
        day_high=None if htf is None else htf.day_high, day_low=None if htf is None else htf.day_low,
        change_from_day_open_pct=None if day_open is None or ref_close is None else (ref_close / day_open - 1) * 100,
        daily_trend=trend, trend_reason="" if htf is None else htf.trend_reason,
        prev_session=None if prev is None else prev[0], prev_range_high=None if prev is None else prev[1],
        prev_range_low=None if prev is None else prev[2], broke_prev_range=broke, own=own, flags=flags,
        data_gaps=data_gaps,
    )
    a.summary = summarize(a, d.label)
    return a


def _f(v: float | None, fmt: str) -> str:
    return "n/d" if v is None else fmt.format(v)


def summarize(a: SessionSymbolAnalysis, label: str) -> str:
    """Resumen por codigo, en lenguaje historico (lo que paso), para el diario y los prompts."""
    parts = [f"{a.symbol} {label}:"]
    o = a.opening
    if a.role == "close":
        parts.append(f"dia UTC {a.day}: apertura {_f(a.day_open, '{:.6g}')}, max {_f(a.day_high, '{:.6g}')}, "
                     f"min {_f(a.day_low, '{:.6g}')}, cierre {_f(o.close if o else None, '{:.6g}')} "
                     f"({_f(a.change_from_day_open_pct, '{:+.2f}')}%); tendencia del dia {a.daily_trend}.")
    elif o is not None:
        parts.append(f"vela de apertura {'verde' if o.body > 0 else 'roja' if o.body < 0 else 'neutra'} "
                     f"(cuerpo {o.body_frac:.0%} del rango, rango {_f(o.range_atr, '{:.2f}')} ATR1h, volumen "
                     f"{_f(o.vol_ratio, '{:.2f}')}x el promedio de esa hora en {o.vol_days} dias); "
                     f"{_f(a.change_from_day_open_pct, '{:+.2f}')}% desde la apertura UTC; tendencia del dia {a.daily_trend}; "
                     f"rango de la sesion anterior ({a.prev_session or 'n/d'}): roto {a.broke_prev_range or 'n/d'}.")
    else:
        parts.append("sin vela de apertura (hueco de datos).")
    if a.own is not None and a.own.minutes:
        w = a.own
        parts.append(f"Datos propios del dia: CVD ${w.cvd_usd:+,.0f}, liquidaciones longs ${w.liq_long_usd:,.0f} / "
                     f"shorts ${w.liq_short_usd:,.0f}, funding medio {_f(w.funding_8h_mean, '{:+.5%}')}, "
                     f"OI {_f(w.oi_change_pct, '{:+.2f}')}% en el dia.")
    flags = [k for k, v in a.flags.model_dump().items() if v]
    if flags:
        parts.append("Banderas: " + ", ".join(flags) + ".")
    if n_real := len(real_gaps(a.data_gaps)):  # lo estructural/respaldo no es un hueco (features/gaps.py)
        parts.append(f"Huecos: {n_real}.")
    return " ".join(parts)


def build_analysis(d: SessionDef, open_ms: int, symbols: dict[str, SessionSymbolAnalysis]) -> SessionAnalysis:
    return SessionAnalysis(day=day_str(open_ms), session=d.name, label=d.label, role=d.role, open_ms=open_ms,
                           trigger_ms=open_ms + HOUR_MS, symbols=symbols)


def render_journal(entries: Sequence[dict], max_chars: int = 6000) -> str:
    """Diario del dia para los prompts: por sesion (en orden), el resumen por codigo y, si hubo, el del LLM.

    entries: filas de `SessionJournal.day()` (dicts con session, label, kind, summary, ...). Se recorta por el final
    viejo si supera `max_chars` (presupuesto de tokens), declarandolo.
    """
    if not entries:
        return "(sin analisis de sesiones previos hoy)"
    blocks = []
    for e in entries:
        tag = "codigo" if e["kind"] == "code" else f"LLM ({e.get('llm_status')})"
        blocks.append(f"### {e.get('label') or e['session']} — {tag}\n{e.get('summary') or ''}".strip())
    text = "\n".join(blocks)
    if len(text) > max_chars:
        text = "[diario recortado: se omite lo mas viejo]\n" + text[-max_chars:]
    return text
