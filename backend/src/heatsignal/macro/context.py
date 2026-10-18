"""
Bloque "Macro y eventos" para el Analista de Contexto de Mercado y vista del panel.

Point-in-time al instante de la corrida (`now_ms`):
  - FRED: solo valores con `available_ms <= now_ms` (store.fred_as_of), con su fecha de publicacion.
  - Calendario: eventos en [now, now + 48 h); si no hay, el proximo.
  - Polymarket: ultima lectura <= now (si tiene mas de `POLY_STALE_MS`, no se muestra: se dice que falta) y su
    variacion contra NUESTRA lectura de hace ~24 h; si no la tenemos, la de la API (`oneDayPriceChange`), marcada.

Tope de tamano: `MAX_BLOCK_CHARS` (~1150 caracteres ~= 330-380 tokens con la estimacion conservadora de 3,2
caracteres por token, por debajo de las ~400 pedidas). Si no entra, se recortan mercados y despues series, y se
avisa en el propio bloque. Nada de lenguaje predictivo: son datos publicados y probabilidades implicitas leidas.

`MACRO_CONTEXT_ID` versiona el formato: cambiar series, tope o redaccion = version nueva.
"""

from __future__ import annotations

import logging
import math
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .calendar import Calendar, load_calendar
from .fred import SERIES, FredSeries, api_key_from_env
from .store import FredObs, MacroStore, ms_to_iso

log = logging.getLogger("heatsignal.macro.context")

MACRO_CONTEXT_ID = "macro-context-v1"
MAX_BLOCK_CHARS = 1150
CHARS_PER_TOKEN = 3.2           # conservador para espanol con numeros (sobre-estima tokens)
EVENT_WINDOW_H = 48.0
POLY_STALE_MS = 3 * 3_600_000
POLY_DAY_MS = 86_400_000
POLY_DAY_TOLERANCE_MS = 3 * 3_600_000
MAX_POLY_IN_BLOCK = 8
FRED_STALE_DAYS = 3             # sin corrida OK de FRED en 3 dias -> se avisa


def estimate_tokens(text: str) -> int:
    return math.ceil(len(text) / CHARS_PER_TOKEN)


def _d(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%d/%m")


def _hm(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%d/%m %H:%M")


def _num(v: float | None, dec: int = 2) -> str:
    return "n/d" if v is None else f"{v:,.{dec}f}".replace(",", "X").replace(".", ",").replace("X", ".")


def series_summary(s: FredSeries, obs: list[FredObs]) -> dict[str, Any] | None:
    """Ultimo valor conocido y cambios, solo con lo publicado hasta ese momento."""
    vals = [o for o in obs if o.value is not None]
    if not vals:
        return None
    last = vals[-1]
    out: dict[str, Any] = {"series_id": s.series_id, "label": s.label, "name": s.name, "units": s.units,
                           "frequency": s.frequency, "value": last.value, "obs_date": last.obs_date,
                           "realtime_start": last.realtime_start, "realtime_clipped": last.realtime_clipped,
                           "available_ms": last.available_ms, "available_at": ms_to_iso(last.available_ms),
                           "note": s.note, "points": [[o.obs_date, o.value, o.realtime_start] for o in vals]}
    if s.frequency == "mensual":
        prev = vals[-2] if len(vals) >= 2 else None
        out["change_1"] = None if prev is None or not prev.value else (last.value / prev.value - 1) * 100
        yago = next((o for o in vals if o.obs_date[:4] == str(int(last.obs_date[:4]) - 1) and o.obs_date[5:7] == last.obs_date[5:7]), None)
        out["change_12"] = None if yago is None or not yago.value else (last.value / yago.value - 1) * 100
        out["change_kind"] = "pct"
    else:
        ref = vals[-6] if len(vals) >= 6 else (vals[0] if len(vals) > 1 else None)
        out["change_5"] = None if ref is None else last.value - ref.value
        out["change_kind"] = "diff"
    return out


def _fred_text(sm: dict[str, Any]) -> str:
    unit = " %" if sm["units"] == "%" else ""
    if sm["frequency"] == "mensual":
        extra = []
        if sm.get("change_1") is not None:
            extra.append(f"{sm['change_1']:+.1f} % m/m".replace(".", ","))
        if sm.get("change_12") is not None:
            extra.append(f"{sm['change_12']:+.1f} % a/a".replace(".", ","))
        return (f"{sm['label']} {_num(sm['value'], 1)} ({sm['obs_date'][:7]}; {', '.join(extra) or 'sin variación'}; "
                f"publ. {_pub_from(sm)})")
    ch = sm.get("change_5")
    chs = "" if ch is None else f"; 5 obs: {ch:+.2f}".replace(".", ",")
    return f"{sm['label']} {_num(sm['value'])}{unit} (al {sm['obs_date'][8:10]}/{sm['obs_date'][5:7]}{chs}; publ. {_pub_from(sm)})"


def _pub_from(sm: dict[str, Any]) -> str:
    rs = sm["realtime_start"]
    return ("≤" if sm["realtime_clipped"] else "") + rs[8:10] + "/" + rs[5:7]


def poly_rows(store: MacroStore, now_ms: int, labels: dict[str, str] | None = None) -> tuple[list[dict[str, Any]], int | None]:
    """Ultima lectura de cada mercado <= now, con variacion 24 h (propia o de la API, marcada)."""
    rows = store.poly_latest(now_ms)
    if not rows:
        return [], None
    last_ms = max(r["fetched_ms"] for r in rows)
    out = []
    for r in rows:
        if last_ms - r["fetched_ms"] > POLY_STALE_MS:  # mercado que ya no se lee (salio de la lista o cerro)
            continue
        ref = store.poly_prob_at(r["market_id"], r["fetched_ms"] - POLY_DAY_MS + POLY_DAY_TOLERANCE_MS)
        own = None
        if ref is not None and abs((r["fetched_ms"] - ref[0]) - POLY_DAY_MS) <= POLY_DAY_TOLERANCE_MS:
            own = r["prob"] - ref[1]
        r = dict(r)
        r["label"] = (labels or {}).get(r["config_key"] or "", r["config_key"])
        r["change_24h"] = own if own is not None else r.get("one_day_change")
        r["change_24h_source"] = "propia" if own is not None else ("api" if r.get("one_day_change") is not None else None)
        r["fetched_at"] = ms_to_iso(r["fetched_ms"])
        out.append(r)
    return out, last_ms


_Q_TRIM = [(re.compile(p, re.I), r) for p, r in (
    (r"^will (the price of |there be |the )?", ""), (r" in fed interest rates", ""), (r" interest rates", ""),
    (r" after the \w+ \d{4} meeting", ""), (r"^(\w+) be ", r"\1 "), (r"\?$", ""))]


def short_question(q: str, n: int = 56) -> str:
    """Pregunta compacta para el bloque (el panel muestra la completa)."""
    q = (q or "").strip()
    for rx, rep in _Q_TRIM:
        q = rx.sub(rep, q)
    q = q[:1].upper() + q[1:]
    return q if len(q) <= n else q[: n - 3] + "..."


def _poly_text(r: dict[str, Any]) -> str:
    q = short_question(r.get("question") or "")
    ch = r.get("change_24h")
    chs = "" if ch is None else (f" (±0 pp" if round(ch * 100) == 0 else f" ({ch * 100:+.0f} pp") + \
        (" API)" if r.get("change_24h_source") == "api" else ")")
    return f"\"{q}\" {r['prob'] * 100:.0f} %{chs}"


def fred_summaries(store: MacroStore | None, now_ms: int) -> list[dict[str, Any]]:
    out = []
    if store is None:
        return out
    for s in SERIES:
        sm = series_summary(s, store.fred_as_of(s.series_id, now_ms, n_obs=15 if s.frequency == "mensual" else 30))
        if sm is not None:
            out.append(sm)
    return out


def source_status(store: MacroStore | None, now_ms: int, calendar: Calendar | None, cal_error: str | None
                  ) -> dict[str, dict[str, Any]]:
    """Estado de cada fuente para el bloque y el panel: key cargada, ultima corrida, ultimo error."""
    runs = store.last_runs(5) if store is not None else {}

    def last(src: str) -> tuple[dict | None, dict | None, dict | None]:
        rs = runs.get(src) or []
        ok = next((r for r in rs if r["status"] in ("ok", "partial")), None)
        err = next((r for r in rs if r["status"] == "error"), None)
        return (rs[0] if rs else None), ok, err

    fred_last, fred_ok, fred_err = last("fred")
    key = api_key_from_env() is not None
    fred_state = "apagado" if not key else ("sin_datos" if fred_ok is None else "ok")
    if key and fred_ok is not None and now_ms - fred_ok["finished_ms"] > FRED_STALE_DAYS * 86_400_000:
        fred_state = "atrasado"
    if key and fred_last is not None and fred_last["status"] == "error":
        fred_state = "error"
    poly_last, poly_ok, poly_err = last("polymarket")
    poly_state = "sin_datos" if poly_ok is None else ("ok" if now_ms - poly_ok["finished_ms"] <= POLY_STALE_MS else "atrasado")
    if poly_last is not None and poly_last["status"] == "error":
        poly_state = "error"
    cal_warn = calendar.coverage_warning(now_ms) if calendar is not None else None
    return {
        "fred": {"state": fred_state, "key_loaded": key, "last_run": fred_last, "last_ok": fred_ok, "last_error": fred_err,
                 "detail": None if key else "falta FRED_API_KEY en .env: FRED queda apagado"},
        "polymarket": {"state": poly_state, "key_loaded": None, "last_run": poly_last, "last_ok": poly_ok,
                       "last_error": poly_err},
        "calendar": {"state": "error" if cal_error else ("aviso" if cal_warn else "ok"),
                     "calendar_id": calendar.calendar_id if calendar else None,
                     "verified_at": calendar.verified_at if calendar else None,
                     "detail": cal_error or cal_warn},
    }


def _load_cal() -> tuple[Calendar | None, str | None]:
    try:
        return load_calendar(), None
    except Exception as exc:  # noqa: BLE001 - un calendario roto se informa, no tumba el ciclo
        log.exception("calendario macro ilegible")
        return None, f"calendario ilegible: {exc}"


def build_block(store: MacroStore | None, now_ms: int, *, calendar: Calendar | None = None,
                cal_error: str | None = None, labels: dict[str, str] | None = None,
                max_chars: int = MAX_BLOCK_CHARS) -> str:
    """Texto del bloque. Siempre devuelve algo (aunque sea 'sin datos'), y entra en `max_chars`."""
    if calendar is None and cal_error is None:
        calendar, cal_error = _load_cal()
    st = source_status(store, now_ms, calendar, cal_error)
    fred = fred_summaries(store, now_ms)
    polys, poly_ms = poly_rows(store, now_ms, labels) if store is not None else ([], None)
    if poly_ms is not None and now_ms - poly_ms > POLY_STALE_MS:
        polys = []
    head = (f"Macro y eventos ({MACRO_CONTEXT_ID}; datos PUBLICADOS hasta {_hm(now_ms)} UTC, point-in-time; "
            "contexto histórico, no pronóstico):")

    fred_parts = [_fred_text(s) for s in fred]
    if st["fred"]["state"] == "apagado":
        fred_line = "- FRED: apagado (falta FRED_API_KEY); sin series macro."
        fred_parts = []
    else:
        fred_line = "- FRED: sin datos publicados guardados todavía."

    if calendar is None:
        ev_line = f"- Próximas 48 h: calendario no disponible ({cal_error})."
    else:
        evs = calendar.upcoming(now_ms, EVENT_WINDOW_H)
        if evs:
            ev_line = "- Próximas 48 h: " + "; ".join(f"{e.label} {_hm(e.ts_ms)} UTC" for e in evs) + "."
        else:
            nxt = calendar.next_events(now_ms, 1)
            ev_line = "- Próximas 48 h: ninguno" + (f" (próximo: {nxt[0].label} {_hm(nxt[0].ts_ms)} UTC)." if nxt else
                                                    " y el calendario no tiene fechas futuras.")

    poly_labels: list[str] = []
    if not polys:
        poly_line = ("- Polymarket: sin lectura en las últimas 3 h." if poly_ms is not None
                     else "- Polymarket: sin lecturas guardadas todavía.")
        poly_items: list[str] = []
    else:
        order = list((labels or {}).keys())
        polys = sorted(polys, key=lambda r: (order.index(r["config_key"]) if r["config_key"] in order else len(order),
                                             -(r.get("volume") or 0)))[:MAX_POLY_IN_BLOCK]
        poly_items = [_poly_text(r) for r in polys]
        poly_labels = [r["label"] or r["config_key"] or "?" for r in polys]
        poly_line = ""

    def assemble(fparts: list[str], pitems: list[str], cut: bool) -> str:
        lines = [head]
        if not fred_parts:
            lines.append(fred_line)
        else:
            lines.append("- FRED: " + (" | ".join(fparts) or "(series omitidas por tamaño)")
                         + (" [FRED atrasado]" if st["fred"]["state"] in ("atrasado", "error") else ""))
        lines.append(ev_line)
        if pitems:
            groups: dict[str, list[str]] = {}
            for lab, it in zip(poly_labels, pitems):
                groups.setdefault(lab, []).append(it)
            lines.append(f"- Polymarket (prob. implícita leída {_hm(poly_ms)} UTC; var. 24 h): "
                         + "; ".join(f"{lab}: {', '.join(its)}" for lab, its in groups.items()) + ".")
        else:
            lines.append(poly_line)
        if cut:
            lines.append("(bloque recortado por tamaño)")
        return "\n".join(lines)

    fparts, pitems = list(fred_parts), list(poly_items)
    text = assemble(fparts, pitems, False)
    while len(text) > max_chars and (pitems or fparts):
        if len(pitems) > 2 or (pitems and not fparts):
            pitems.pop()
        elif fparts:
            fparts.pop()
        else:
            pitems.pop()
        text = assemble(fparts, pitems, True)
    if len(text) > max_chars:
        text = text[: max_chars - 3] + "..."
    return text


class MacroContextProvider:
    """Lo que usa el ciclo de analisis: `context_block(now_ms)` con una conexion de solo lectura por llamada."""

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)

    def context_block(self, now_ms: int) -> str:
        store = None
        try:
            if self.db_path.exists():
                store = MacroStore(self.db_path, readonly=True)
        except Exception:  # noqa: BLE001 - la base macro no frena el ciclo; el bloque lo dice
            log.exception("no se pudo abrir la base macro %s", self.db_path)
        try:
            from .polymarket import labels, load_config

            try:
                lab = labels(load_config())
            except Exception:  # noqa: BLE001
                lab = None
            return build_block(store, now_ms, labels=lab)
        finally:
            if store is not None:
                store.close()
