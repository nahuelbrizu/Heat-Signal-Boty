"""
Constructor de estrategias por BLOQUES (fase 1 del plan "estrategias generadas + alertas", 2026-11-21).

Una estrategia generada es un JSON chico que combina piezas fijas que el laboratorio sabe ejecutar. Nadie (ni el
agente de investigacion, en la fase 2) escribe codigo: solo elige bloques y numeros dentro de rangos cerrados, asi
que no puede colar errores ni mirar el futuro.

    {"name": "rsi-tendencia", "timeframe": "1h", "direction": "long",
     "symbols": ["BTCUSDT", ...],                       # opcional (default: las 7)
     "entry":   {"type": "rsi", "period": 2, "low": 10, "high": 90},
     "filters": [{"type": "daily_trend", "ema": 50}],
     "exit":    {"bars": 24, "stop_atr": 2.0, "target_atr": null}}

Bloques
  timeframe  5min | 15min | 1h (velas armadas con las 1m, indexadas al CIERRE: se decide cuando la vela se conoce)
  entry      breakout(n)                 cierre >= maximo de n cierres -> long; <= minimo -> short
             rsi(period, low, high)      RSI < low -> long; > high -> short (reversion)
             ema_cross(fast, slow)       cruce de la EMA rapida hacia arriba -> long; hacia abajo -> short
             vwap_dev(k)                 cierre < VWAP del dia - k*ATR -> long; > VWAP + k*ATR -> short (reversion)
             opening_range(session, minutes)  rango de los primeros `minutes` de la sesion (asia 09:00 Tokio,
                                         londres 08:00 Londres, ny 09:30 Nueva York, con horario de verano); el
                                         primer cierre fuera del rango ese dia -> long/short
  filters    daily_trend(ema)            long solo si el ultimo cierre diario > EMA diaria; short solo si <
             atr_pct(min, max)           ATR(14) de la temporalidad en % del precio dentro del rango
             session(name)               solo entradas dentro de la sesion (asia 00-08, londres 07-16, ny 13-21 UTC)
  exit       bars (obligatorio)          salida por tiempo despues de `bars` velas
             stop_atr / target_atr       stop y objetivo en multiplos del ATR(14) al entrar (evaluados en 1m;
                                         si en la misma vela 1m se tocan los dos se asume el stop)
  direction  long | short | both

Pre-registro: el `strategy_id` es `gen-<name>-<hash8>`, con el hash del JSON normalizado: cambiar un numero es otra
estrategia (otra version) y cuenta como otro intento para el Deflated Sharpe (grilla vacia = 1 combinacion). El
registro `data/lab_generated.jsonl` es append-only (solo se agregan lineas; una linea ilegible se ignora y se avisa).

    python -m heatsignal.lab.builder validar spec.json
    python -m heatsignal.lab.builder agregar spec.json [--origen manual|agente] [--nota "..."] [--encolar]
    python -m heatsignal.lab.builder listar
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import re
import sys
import time
from collections.abc import Iterable
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

log = logging.getLogger("heatsignal.lab.builder")

BUILDER_ID = "builder-v1"
SEVEN = ["BTCUSDT", "SOLUSDT", "XRPUSDT", "ETHUSDT", "XLMUSDT", "BNBUSDT", "TRXUSDT"]
TIMEFRAMES = {"5min": 5, "15min": 15, "1h": 60}
DIRECTIONS = ("long", "short", "both")
SESSIONS_OPEN = {"asia": ("Asia/Tokyo", 9, 0), "londres": ("Europe/London", 8, 0), "ny": ("America/New_York", 9, 30)}
SESSIONS_UTC = {"asia": (0, 8), "londres": (7, 16), "ny": (13, 21)}
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{2,40}$")
MAX_HOLD_MIN = 7 * 24 * 60  # una semana como tope de tenencia
ORIGINS = ("manual", "agente")


class SpecError(ValueError):
    """Spec invalida: el mensaje dice que campo y por que (para el panel y para el agente)."""


def _num(d: dict[str, Any], key: str, lo: float, hi: float, *, integer: bool = False, default: Any = ...) -> Any:
    if key not in d or d[key] is None:
        if default is ...:
            raise SpecError(f"falta '{key}'")
        return default
    v = d[key]
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        raise SpecError(f"'{key}' tiene que ser un numero")
    if integer and int(v) != v:
        raise SpecError(f"'{key}' tiene que ser entero")
    if not lo <= v <= hi:
        raise SpecError(f"'{key}'={v} fuera de rango [{lo}, {hi}]")
    return int(v) if integer else float(v)


def _only(d: dict[str, Any], allowed: set[str], where: str) -> None:
    extra = set(d) - allowed
    if extra:
        raise SpecError(f"{where}: campos desconocidos {sorted(extra)}")


def _entry(e: Any, tf_min: int) -> dict[str, Any]:
    if not isinstance(e, dict) or "type" not in e:
        raise SpecError("'entry' tiene que ser un objeto con 'type'")
    t = e["type"]
    if t == "breakout":
        _only(e, {"type", "n"}, "entry")
        return {"type": t, "n": _num(e, "n", 5, 500, integer=True)}
    if t == "rsi":
        _only(e, {"type", "period", "low", "high"}, "entry")
        return {"type": t, "period": _num(e, "period", 2, 50, integer=True), "low": _num(e, "low", 1, 49),
                "high": _num(e, "high", 51, 99)}
    if t == "ema_cross":
        _only(e, {"type", "fast", "slow"}, "entry")
        fast, slow = _num(e, "fast", 2, 100, integer=True), _num(e, "slow", 3, 400, integer=True)
        if fast >= slow:
            raise SpecError("ema_cross: 'fast' tiene que ser menor que 'slow'")
        return {"type": t, "fast": fast, "slow": slow}
    if t == "vwap_dev":
        _only(e, {"type", "k"}, "entry")
        return {"type": t, "k": _num(e, "k", 0.25, 10)}
    if t == "opening_range":
        _only(e, {"type", "session", "minutes"}, "entry")
        s = e.get("session")
        if s not in SESSIONS_OPEN:
            raise SpecError(f"opening_range: 'session' tiene que ser una de {sorted(SESSIONS_OPEN)}")
        m = _num(e, "minutes", tf_min, 240, integer=True)
        if m % tf_min:
            raise SpecError(f"opening_range: 'minutes' tiene que ser multiplo de la temporalidad ({tf_min} min)")
        return {"type": t, "session": s, "minutes": m}
    raise SpecError(f"entry: tipo '{t}' desconocido (breakout, rsi, ema_cross, vwap_dev, opening_range)")


def _filter(f: Any) -> dict[str, Any]:
    if not isinstance(f, dict) or "type" not in f:
        raise SpecError("cada filtro tiene que ser un objeto con 'type'")
    t = f["type"]
    if t == "daily_trend":
        _only(f, {"type", "ema"}, "filtro daily_trend")
        return {"type": t, "ema": _num(f, "ema", 5, 200, integer=True)}
    if t == "atr_pct":
        _only(f, {"type", "min", "max"}, "filtro atr_pct")
        lo, hi = _num(f, "min", 0.0, 20.0, default=0.0), _num(f, "max", 0.0, 50.0, default=50.0)
        if lo >= hi:
            raise SpecError("atr_pct: 'min' tiene que ser menor que 'max'")
        return {"type": t, "min": lo, "max": hi}
    if t == "session":
        _only(f, {"type", "name"}, "filtro session")
        if f.get("name") not in SESSIONS_UTC:
            raise SpecError(f"filtro session: 'name' tiene que ser una de {sorted(SESSIONS_UTC)}")
        return {"type": t, "name": f["name"]}
    raise SpecError(f"filtro '{t}' desconocido (daily_trend, atr_pct, session)")


def validate(raw: Any) -> dict[str, Any]:
    """Spec normalizada (orden y tipos fijos) o SpecError. La normalizacion es la base del hash."""
    if not isinstance(raw, dict):
        raise SpecError("la spec tiene que ser un objeto JSON")
    _only(raw, {"name", "timeframe", "direction", "symbols", "entry", "filters", "exit", "description"}, "spec")
    name = raw.get("name")
    if not isinstance(name, str) or not NAME_RE.match(name):
        raise SpecError("'name': 3-41 caracteres, minusculas, numeros y guiones")
    tf = raw.get("timeframe")
    if tf not in TIMEFRAMES:
        raise SpecError(f"'timeframe' tiene que ser uno de {sorted(TIMEFRAMES)}")
    direction = raw.get("direction")
    if direction not in DIRECTIONS:
        raise SpecError(f"'direction' tiene que ser uno de {list(DIRECTIONS)}")
    syms = raw.get("symbols") or list(SEVEN)
    if not isinstance(syms, list) or not syms or any(s not in SEVEN for s in syms):
        raise SpecError(f"'symbols': lista no vacia de {SEVEN}")
    syms = [s for s in SEVEN if s in set(syms)]
    entry = _entry(raw.get("entry"), TIMEFRAMES[tf])
    filters_raw = raw.get("filters") or []
    if not isinstance(filters_raw, list) or len(filters_raw) > 3:
        raise SpecError("'filters': lista de hasta 3 filtros")
    filters = [_filter(f) for f in filters_raw]
    if len({f["type"] for f in filters}) != len(filters):
        raise SpecError("'filters': cada tipo de filtro una sola vez")
    ex = raw.get("exit")
    if not isinstance(ex, dict):
        raise SpecError("'exit' tiene que ser un objeto con 'bars'")
    _only(ex, {"bars", "stop_atr", "target_atr"}, "exit")
    bars = _num(ex, "bars", 1, MAX_HOLD_MIN // TIMEFRAMES[tf], integer=True)
    exit_ = {"bars": bars, "stop_atr": _num(ex, "stop_atr", 0.25, 20, default=None),
             "target_atr": _num(ex, "target_atr", 0.25, 40, default=None)}
    desc = raw.get("description") or ""
    if not isinstance(desc, str) or len(desc) > 400:
        raise SpecError("'description': texto de hasta 400 caracteres")
    return {"name": name, "timeframe": tf, "direction": direction, "symbols": syms, "entry": entry,
            "filters": sorted(filters, key=lambda f: f["type"]), "exit": exit_, "description": desc}


def spec_digest(spec: dict[str, Any]) -> str:
    """Hash del JSON normalizado SIN la descripcion (texto libre: no cambia la regla)."""
    core = {k: v for k, v in spec.items() if k != "description"}
    return hashlib.sha256(json.dumps(core, sort_keys=True).encode()).hexdigest()


def strategy_id_for(spec: dict[str, Any]) -> str:
    return f"gen-{spec['name']}-{spec_digest(spec)[:8]}"


def describe(spec: dict[str, Any]) -> str:
    """Descripcion en castellano armada desde los bloques (la del autor va aparte)."""
    e, x = spec["entry"], spec["exit"]
    ent = {"breakout": lambda: f"ruptura de {e['n']} velas",
           "rsi": lambda: f"RSI({e['period']}) < {e['low']:g} long / > {e['high']:g} short",
           "ema_cross": lambda: f"cruce EMA{e['fast']}/EMA{e['slow']}",
           "vwap_dev": lambda: f"cierre a mas de {e['k']:g} ATR del VWAP del dia (reversion)",
           "opening_range": lambda: f"ruptura del rango de los primeros {e['minutes']} min de {e['session']}"}[e["type"]]()
    fil = []
    for f in spec["filters"]:
        fil.append({"daily_trend": lambda: f"tendencia diaria (EMA{f['ema']})",
                    "atr_pct": lambda: f"ATR entre {f['min']:g} % y {f['max']:g} %",
                    "session": lambda: f"solo sesion {f['name']}"}[f["type"]]())
    sal = f"sale a las {x['bars']} velas"
    if x["stop_atr"]:
        sal += f", stop {x['stop_atr']:g} ATR"
    if x["target_atr"]:
        sal += f", objetivo {x['target_atr']:g} ATR"
    dir_ = {"long": "solo long", "short": "solo short", "both": "long y short"}[spec["direction"]]
    return f"Velas {spec['timeframe']}, {dir_}: {ent}" + (f"; filtros: {', '.join(fil)}" if fil else "") + f"; {sal}."


# ---- compilacion a regla del laboratorio ------------------------------------------------------------------


def entry_signals(spec: dict[str, Any], md: Any):
    """(entradas +1/-1/0 indexadas al CIERRE de cada vela, ATR(14), velas). Tambien lo usan las alertas (fase 3)."""
    import numpy as np
    import pandas as pd

    from . import features as F
    from .clock import asof, resample_bars
    from .strategies import _vwap_day

    tf = spec["timeframe"]
    tf_td = pd.Timedelta(minutes=TIMEFRAMES[tf])
    bars = resample_bars(md.klines, tf)
    close = bars["close"]
    e = pd.Series(0.0, index=bars.index)
    en = spec["entry"]
    if en["type"] == "breakout":
        n = en["n"]
        hi = close >= close.rolling(n, min_periods=n).max()
        lo = close <= close.rolling(n, min_periods=n).min()
        e[hi & ~lo] = 1.0
        e[lo & ~hi] = -1.0
    elif en["type"] == "rsi":
        r = F.rsi(close, en["period"])
        e[r < en["low"]] = 1.0
        e[r > en["high"]] = -1.0
    elif en["type"] == "ema_cross":
        f = close.ewm(span=en["fast"], adjust=False).mean()
        s = close.ewm(span=en["slow"], adjust=False).mean()
        cross = (f > s).astype(int).diff()
        e[cross > 0] = 1.0
        e[cross < 0] = -1.0
        e.iloc[: en["slow"]] = 0.0  # sin EMA lenta asentada no hay cruce
    elif en["type"] == "vwap_dev":
        vw = asof(bars.index, _vwap_day(md.klines).rename("vwap"))
        a = F.atr(bars, 14)
        e[close < vw - en["k"] * a] = 1.0
        e[close > vw + en["k"] * a] = -1.0
    elif en["type"] == "opening_range":
        e = _opening_range(bars, en["session"], en["minutes"], tf_td)
    a14 = F.atr(bars, 14)
    for flt in spec["filters"]:
        if flt["type"] == "daily_trend":
            d = resample_bars(md.klines, "1D")["close"]
            ema = d.ewm(span=flt["ema"], adjust=False).mean()
            ok = pd.Series(np.arange(len(d)) >= flt["ema"], index=d.index)  # EMA asentada
            up = asof(bars.index, (d > ema).astype(float).where(ok).rename("up"))
            e[(e > 0) & ~(up == 1.0)] = 0.0  # sin historia (NaN) bloquea
            e[(e < 0) & ~(up == 0.0)] = 0.0
        elif flt["type"] == "atr_pct":
            pct = a14 / close * 100
            e[~((pct >= flt["min"]) & (pct <= flt["max"]))] = 0.0
        elif flt["type"] == "session":
            h0, h1 = SESSIONS_UTC[flt["name"]]
            hour = (bars.index - tf_td).hour  # hora de APERTURA de la vela
            e[~((hour >= h0) & (hour < h1))] = 0.0
    if spec["direction"] == "long":
        e[e < 0] = 0.0
    elif spec["direction"] == "short":
        e[e > 0] = 0.0
    return e.fillna(0.0), a14, bars


def compile_rule(spec: dict[str, Any]):  # -> Rule
    """Regla con el contrato de `lab/sim.py` (ver strategies.py): dict simbolo -> Series de cambios de posicion
    indexada por el instante de DECISION (cierre de la vela). Una posicion por vez por simbolo."""
    import numpy as np
    import pandas as pd

    from .strategies import _changes, exits_with_levels, hold_events

    tf_td = pd.Timedelta(minutes=TIMEFRAMES[spec["timeframe"]])

    def rule(data: dict[str, Any], p: dict[str, Any]) -> dict[str, pd.Series]:
        out: dict[str, pd.Series] = {}
        horizon = tf_td * spec["exit"]["bars"]
        for sym, md in data.items():
            if md.klines is None or md.klines.empty:
                out[sym] = _changes({})
                continue
            e, a14, bars = entry_signals(spec, md)
            e = e[e != 0]
            if bars.empty or e.empty:
                out[sym] = _changes({})
                continue
            sl_k, tp_k = spec["exit"]["stop_atr"], spec["exit"]["target_atr"]
            if sl_k or tp_k:
                c, a = bars["close"].reindex(e.index), a14.reindex(e.index)
                sgn = np.sign(e)
                sl = (c - sgn * sl_k * a) if sl_k else None
                tp = (c + sgn * tp_k * a) if tp_k else None
                out[sym] = exits_with_levels(e, md.klines, horizon, tp=tp, sl=sl)
            else:
                out[sym] = hold_events(e, horizon, bars.index)
        return out

    return rule


def _opening_range(bars, session: str, minutes: int, tf_td):
    """+1/-1 en el primer cierre fuera del rango de apertura de cada dia (una senal por dia y sesion, dentro de las
    8 h siguientes a la apertura). Rango incompleto (faltan velas) -> ese dia no opera."""
    import pandas as pd

    tz, hh, mm = SESSIONS_OPEN[session]
    zone = ZoneInfo(tz)
    e = pd.Series(0.0, index=bars.index)
    if bars.empty:
        return e
    opens = bars.index - tf_td  # apertura de cada vela (UTC)
    days = sorted({ts.tz_convert(zone).date() for ts in opens})
    for day in days:
        local = pd.Timestamp(year=day.year, month=day.month, day=day.day, hour=hh, minute=mm, tz=zone)
        if local.weekday() >= 5 and session != "asia":
            continue  # Londres y Nueva York: dias habiles
        start = local.tz_convert("UTC")
        rng_end = start + pd.Timedelta(minutes=minutes)
        in_rng = (opens >= start) & (opens < rng_end)
        if in_rng.sum() * tf_td < pd.Timedelta(minutes=minutes):
            continue
        hi, lo = bars.loc[in_rng, "high"].max(), bars.loc[in_rng, "low"].min()
        after = bars[(opens >= rng_end) & (opens < start + pd.Timedelta(hours=8))]
        hit = after[(after["close"] > hi) | (after["close"] < lo)]
        if not hit.empty:
            e[hit.index[0]] = 1.0 if hit["close"].iloc[0] > hi else -1.0
    return e


# ---- registro append-only -----------------------------------------------------------------------------------


def registry_path() -> Path:
    env = os.environ.get("HEATSIGNAL_GENERATED_PATH")
    if env:
        return Path(env).expanduser()
    eval_db = os.environ.get("HEATSIGNAL_EVAL_DB")
    base = Path(eval_db).expanduser().parent if eval_db else Path("~/heatsignal/data").expanduser()
    return base / "lab_generated.jsonl"


def make_entry(spec: dict[str, Any], *, origin: str = "manual", note: str = "") -> dict[str, Any]:
    if origin not in ORIGINS:
        raise SpecError(f"origen tiene que ser uno de {ORIGINS}")
    norm = validate(spec)
    return {"strategy_id": strategy_id_for(norm), "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "origin": origin, "note": str(note)[:300], "builder": BUILDER_ID, "spec": norm, "hash": spec_digest(norm)}


def load_entries(path: Path | None = None) -> tuple[list[dict[str, Any]], list[str]]:
    """(entradas validas en orden, avisos). Duplicados: vale la primera (append-only: no se pisa)."""
    path = path or registry_path()
    if not path.is_file():
        return [], []
    out, warns, seen = [], [], set()
    for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            rec = json.loads(line)
            norm = validate(rec["spec"])
            if spec_digest(norm) != rec.get("hash") or strategy_id_for(norm) != rec.get("strategy_id"):
                raise SpecError("el hash o el id no coinciden con la spec (linea modificada)")
        except (ValueError, KeyError, TypeError) as exc:
            warns.append(f"{path.name}:{i}: ignorada ({exc})")
            continue
        if rec["strategy_id"] in seen:
            continue
        seen.add(rec["strategy_id"])
        out.append({**rec, "spec": norm})
    return out, warns


def append(entry: dict[str, Any], path: Path | None = None) -> bool:
    """Agrega la linea si el id no esta. False = ya existia (misma regla registrada antes)."""
    path = path or registry_path()
    existing, _ = load_entries(path)
    if any(e["strategy_id"] == entry["strategy_id"] for e in existing):
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False, sort_keys=True) + "\n")
    return True


def generated_catalog(path: Path | None = None) -> dict[str, tuple[Any, Any]]:
    """strategy_id -> (Spec, Rule) para sumar al CATALOG. Nunca levanta: un registro roto queda en el log."""
    from .strategies import Spec

    try:
        entries, warns = load_entries(path)
    except OSError as exc:
        log.warning("estrategias generadas: no se pudo leer el registro (%s)", exc)
        return {}
    for w in warns:
        log.warning("estrategias generadas: %s", w)
    out = {}
    for rec in entries:
        s = rec["spec"]
        spec = Spec(rec["strategy_id"], "generada", (s["description"] + " " if s["description"] else "") + describe(s),
                    s["timeframe"], s["direction"], f"{s['exit']['bars']} velas de {s['timeframe']}",
                    {"symbols": s["symbols"], "builder": s}, grid={}, needs=("klines",),
                    source=f"{BUILDER_ID}; origen {rec['origin']}; {rec['created_at']}")
        out[rec["strategy_id"]] = (spec, compile_rule(s))
    return out


# ---- CLI ----------------------------------------------------------------------------------------------------


def _read(path: str) -> Any:
    return json.loads(sys.stdin.read() if path == "-" else Path(path).read_text(encoding="utf-8"))


def enqueue(entry: dict[str, Any], origin: str) -> Any:
    """Pide la corrida al laboratorio (misma cola que "Mandar al laboratorio" del panel)."""
    from ..evaluation.lab_requests import LabRequestStore

    eval_db = os.environ.get("HEATSIGNAL_EVAL_DB") or str(Path("~/heatsignal/data/heatsignal_eval.sqlite").expanduser())
    store = LabRequestStore(eval_db)
    try:
        req = store.create([entry["strategy_id"]], entry["spec"]["symbols"],
                           note=f"generada ({origin}): {describe(entry['spec'])}"[:500], origin="builder")
    finally:
        close = getattr(store, "close", None)
        if callable(close):
            close()
    return req.get("request_id") if isinstance(req, dict) else req


def main(argv: Iterable[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m heatsignal.lab.builder", description=__doc__.split("\n\n")[0])
    sub = p.add_subparsers(dest="cmd", required=True)
    v = sub.add_parser("validar")
    v.add_argument("spec")
    a = sub.add_parser("agregar")
    a.add_argument("spec")
    a.add_argument("--origen", default="manual", choices=ORIGINS)
    a.add_argument("--nota", default="")
    a.add_argument("--encolar", action="store_true", help="pedir la corrida al laboratorio (cola lab_requests)")
    sub.add_parser("listar")
    args = p.parse_args(list(argv) if argv is not None else None)
    try:
        if args.cmd == "validar":
            norm = validate(_read(args.spec))
            print(json.dumps({"ok": True, "strategy_id": strategy_id_for(norm), "describe": describe(norm),
                              "spec": norm}, ensure_ascii=False, indent=2))
        elif args.cmd == "agregar":
            entry = make_entry(_read(args.spec), origin=args.origen, note=args.nota)
            added = append(entry)
            res: dict[str, Any] = {"ok": True, "strategy_id": entry["strategy_id"], "added": added,
                                   "describe": describe(entry["spec"]), "registro": str(registry_path())}
            if args.encolar:
                res["lab_request"] = enqueue(entry, args.origen)
            print(json.dumps(res, ensure_ascii=False, indent=2))
        else:
            entries, warns = load_entries()
            print(json.dumps({"registro": str(registry_path()), "avisos": warns,
                              "estrategias": [{"strategy_id": e["strategy_id"], "origin": e["origin"],
                                               "created_at": e["created_at"], "describe": describe(e["spec"])}
                                              for e in entries]}, ensure_ascii=False, indent=2))
    except SpecError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
