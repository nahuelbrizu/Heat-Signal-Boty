"""
SL/TP obligatorio en cada operacion nueva (-v2) y guardia de liquidacion.

Pedido del usuario (2026-10-26): "cuando hace operaciones siempre tiene que poner un take profit y un stop loss ...
para asi dejarlas corriendo hasta que uno salte y cierre la operacion" + "intentando que nunca toquemos la
liquidacion". Decisiones del usuario (mismo dia, ANTES de ver un solo resultado): versiones -v2 EN PARALELO (las v1 y
la prueba hacia adelante pre-registrada no se tocan), la operacion cierra SOLO por stop o por objetivo, stop a
3 ATR(14) de la temporalidad (piso 8 %, techo 20 % del precio) y objetivo al doble de esa distancia (1:2).
Ver decisions.md, "SL/TP obligatorio: versiones -v2 (2026-10-26)".

Regla -v2 de una estrategia base (la v1 congelada, sin cambios):
  - ENTRADAS = las de la base: cada vez que su posicion objetivo pasa de 0 a long/short, o da vuelta de lado. El
    tamano es el de la base en ese instante (|objetivo|). Los ajustes de tamano, rebalanceos y salidas de la base se
    ignoran. Con una operacion abierta, las entradas nuevas se ignoran (una por vez, sin piramidar).
  - NIVELES fijos desde la entrada: precio de referencia = precio de entrada (open de la vela 1m siguiente a la
    decision); distancia del stop f = clip(3 * ATR(14) / precio, 8 %, 20 %), con el ATR de la ultima vela CERRADA de
    la temporalidad de la base (1D o 1h; 4h si la base es de 4h); stop = precio * (1 - lado*f); objetivo = precio *
    (1 + lado*2*f).
  - SALIDA: solo cuando una vela 1m toca el stop o el objetivo (si toca los dos en la misma vela -> stop, como
    `strategies.exits_with_levels`). Sin salida por tiempo, por senal ni por rebalanceo. Se decide al cierre de esa
    vela y se ejecuta al open siguiente (no se asume fill en el nivel exacto).
  - Carteras (familias, cartera de tendencia): cada componente es su propia operacion con su propio stop y objetivo;
    la posicion del simbolo es la suma ponderada de las operaciones abiertas.

Guardia de liquidacion (margen aislado; formula de Binance para una posicion aislada sin otras abiertas):
    long:  liq = entrada * (1 - 1/L) / (1 - mmr)        short: liq = entrada * (1 + 1/L) / (1 + mmr)
El stop tiene que quedar a como mucho LIQ_BUFFER (la mitad) de la distancia entre la entrada y la liquidacion. Si el
tamano pedido (= apalancamiento sobre el capital del simbolo) no lo cumple, se achica al maximo seguro
(`max_safe_leverage`); `check_stop` vuelve a verificar con la posicion agregada y, si no cumple, levanta
StopBeyondLiquidation (la corrida falla a la vista: nunca se opera con un stop detras de la liquidacion). Hoy todo
corre a <= 1x: la liquidacion de un long queda en ~0 y la de un short en ~+96 %, lejos del techo de 20 %.
MMR = 2 % es un supuesto CONSERVADOR (mayor que el del primer tramo de BTC/ETH en Binance); antes de plata real se
reemplaza por el tramo real de cada simbolo (leverage brackets del exchange, verificados contra su doc).

Estado: el bot de paper no recalcula la cadena desde cero (con la ventana movil de 64 d de velas 1m una operacion
abierta hace mas tiempo se "olvidaria" y se cerraria sin tocar ningun nivel). Cada operacion queda guardada con sus
niveles (tabla `sltp_trades` de paperbot.sqlite) y en cada corrida solo se chequean las velas nuevas (`advance`).
Como un stop/objetivo puesto en el exchange, las salidas tambien corren durante un hueco del bot; las entradas no.
El laboratorio usa la misma `advance` sobre toda la historia (`run_chain`).
"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import asdict, dataclass
from typing import Any

import numpy as np
import pandas as pd

MIN = pd.Timedelta(minutes=1)

SL_ATR = 3.0
ATR_N = 14
SL_MIN = 0.08
SL_MAX = 0.20
RR = 2.0
MMR = 0.02
LIQ_BUFFER = 0.5

PARAMS: dict[str, float] = {"sl_atr": SL_ATR, "atr_n": ATR_N, "sl_min": SL_MIN, "sl_max": SL_MAX, "rr": RR,
                            "mmr": MMR, "liq_buffer": LIQ_BUFFER}

_TF = {"1D": "1D", "1d": "1D", "4h": "4h", "1h": "1h"}


class StopBeyondLiquidation(Exception):
    """El stop quedaria detras (o demasiado cerca) del precio de liquidacion: no se opera."""


# ---- niveles y liquidacion ------------------------------------------------------------------------------


def atr_timeframe(spec_timeframe: str, params: dict[str, Any]) -> str:
    """Temporalidad del ATR: la de la base (1D / 4h / 1h); '1h|4h' -> la elegida en params; el resto (5min,
    calendario, liquidacion) -> 1h."""
    tf = spec_timeframe
    if "|" in tf:
        tf = str(params.get("timeframe", "1h"))
    return _TF.get(tf, "1h")


def sl_fraction(ref: float, atr: float, p: dict[str, Any] = PARAMS) -> float | None:
    if not (np.isfinite(ref) and ref > 0 and np.isfinite(atr) and atr > 0):
        return None
    return float(min(max(p["sl_atr"] * atr / ref, p["sl_min"]), p["sl_max"]))


def liquidation_price(side: float, entry: float, leverage: float, mmr: float = MMR) -> float | None:
    """Precio de liquidacion de una posicion aislada (None si no hay posicion). Un long a <= 1x da <= 0: no se
    liquida (el precio no puede llegar a 0)."""
    if leverage <= 0:
        return None
    if side > 0:
        return max(entry * (1 - 1 / leverage) / (1 - mmr), 0.0)
    return entry * (1 + 1 / leverage) / (1 + mmr)


def max_safe_leverage(side: float, sl_frac: float, p: dict[str, Any] = PARAMS) -> float:
    """Mayor apalancamiento con el que el stop queda a <= liq_buffer de la distancia a la liquidacion."""
    m, x = p["mmr"], sl_frac / p["liq_buffer"]
    den = 1 - (1 - m) * (1 - x) if side > 0 else (1 + m) * (1 + x) - 1
    return math.inf if den <= 0 else 1 / den


def check_stop(side: float, entry: float, sl: float, leverage: float, p: dict[str, Any] = PARAMS) -> float | None:
    """Verifica que el stop salte bastante antes que la liquidacion. Devuelve el precio de liquidacion."""
    liq = liquidation_price(side, entry, leverage, p["mmr"])
    if liq is None:
        return None
    room = abs(entry - liq)
    if (sl - liq) * side <= 0 or abs(entry - sl) > p["liq_buffer"] * room + 1e-12 * entry:
        raise StopBeyondLiquidation(
            f"stop {sl:.6g} vs liquidacion {liq:.6g} (entrada {entry:.6g}, {leverage:.3g}x, lado {side:+.0f}): el stop "
            f"tiene que estar a <= {p['liq_buffer']:.0%} de la distancia a la liquidacion")
    return liq


@dataclass
class Position:
    """Una operacion -v2 abierta (o recien cerrada) de un slot (componente) en un simbolo."""
    slot: str
    symbol: str
    side: float              # +1 long / -1 short
    size: float              # |objetivo de la base| en la entrada (ya achicado por la guardia de liquidacion)
    weight: float            # peso del slot en la cartera (1 si no es cartera)
    entry_ms: int            # instante de DECISION de la entrada
    ref_price: float         # precio de entrada (fill)
    atr: float
    atr_tf: str
    sl_frac: float
    sl: float
    tp: float
    leverage: float          # = size (fraccion del capital del simbolo en el slot)
    liq_price: float | None

    def signed(self) -> float:
        return self.side * self.size * self.weight

    def as_row(self) -> dict[str, Any]:
        return asdict(self)


def open_position(slot: str, symbol: str, side: float, size: float, weight: float, entry_ms: int, ref: float,
                  atr: float, atr_tf: str, p: dict[str, Any] = PARAMS) -> Position | None:
    """Niveles de una entrada nueva (None si no hay ATR o precio valido: la entrada no se toma)."""
    f = sl_fraction(ref, atr, p)
    if f is None or size <= 0:
        return None
    size = min(size, max_safe_leverage(side, f, p))
    sl, tp = ref * (1 - side * f), ref * (1 + side * p["rr"] * f)
    liq = check_stop(side, ref, sl, size, p)
    return Position(slot, symbol, float(side), float(size), float(weight), int(entry_ms), float(ref), float(atr), atr_tf,
                    f, float(sl), float(tp), float(size), liq)


# ---- entradas y toques ------------------------------------------------------------------------------


def new_entries(sig: pd.Series | None) -> list[tuple[pd.Timestamp, float]]:
    """(decision, objetivo con signo) cada vez que la posicion de la base pasa de 0 a un lado o da vuelta."""
    if sig is None or not len(sig):
        return []
    s = sig.dropna().sort_index(kind="stable")
    s = s[~s.index.duplicated(keep="last")]
    out, prev = [], 0.0
    for t, w in s.items():
        w = float(w)
        sg = float(np.sign(w)) if abs(w) > 1e-12 else 0.0
        if sg != 0 and sg != prev:
            out.append((t, w))
        prev = sg
    return out


def first_hit(k1m: pd.DataFrame, side: float, sl: float, tp: float, start: pd.Timestamp,
              end: pd.Timestamp) -> tuple[pd.Timestamp, str] | None:
    """Primera vela 1m con apertura en [start, end) que toca el stop o el objetivo. Empate en la vela -> stop."""
    idx = k1m.index
    i, j = idx.searchsorted(start, side="left"), idx.searchsorted(end, side="left")
    if i >= j:
        return None
    hi, lo = k1m["high"].to_numpy()[i:j], k1m["low"].to_numpy()[i:j]
    hit_sl = (lo <= sl) if side > 0 else (hi >= sl)
    hit_tp = (hi >= tp) if side > 0 else (lo <= tp)
    a = int(np.argmax(hit_sl)) if hit_sl.any() else None
    b = int(np.argmax(hit_tp)) if hit_tp.any() else None
    if a is None and b is None:
        return None
    if a is not None and (b is None or a <= b):
        return idx[i + a], "stop"
    return idx[i + b], "objetivo"  # type: ignore[operator]


def advance(pos: Position | None, entries: list[tuple[pd.Timestamp, float]], k1m: pd.DataFrame, *,
            start: pd.Timestamp, end: pd.Timestamp, entry_after: pd.Timestamp | None,
            make: Callable[[pd.Timestamp, float, float], Position | None]) -> tuple[list[dict[str, Any]], Position | None]:
    """Avanza un slot de un simbolo desde `start` hasta `end` (velas con apertura < end).

    `pos` = la operacion abierta que viene de antes (o None). Toques: velas desde max(start, entrada). Entradas: las de
    `entries` con entry_after < t <= end y sin operacion abierta en t (`make` arma los niveles; None = no se toma).
    Devuelve los eventos en orden ({'t', 'kind': open|close|skip, 'pos', 'motivo'}) y la operacion abierta al final."""
    events: list[dict[str, Any]] = []
    cursor = start
    ents = [(t, v) for t, v in entries if (entry_after is None or t > entry_after) and t <= end]
    i = 0
    while True:
        if pos is not None:
            hit = first_hit(k1m, pos.side, pos.sl, pos.tp, max(cursor, _ts(pos.entry_ms)), end)
            if hit is None:
                break
            bar, motivo = hit
            cursor = bar + MIN
            events.append({"t": cursor, "kind": "close", "pos": pos, "motivo": motivo, "bar": bar})
            pos = None
            continue
        while i < len(ents) and ents[i][0] < cursor:
            i += 1
        if i >= len(ents):
            break
        t, v = ents[i]
        i += 1
        new = make(t, float(np.sign(v)), abs(float(v)))
        if new is None:
            events.append({"t": t, "kind": "skip", "pos": None, "motivo": "sin_atr_o_precio"})
            continue
        pos, cursor = new, t
        events.append({"t": t, "kind": "open", "pos": pos, "motivo": None})
    return events, pos


def _ts(ms: int) -> pd.Timestamp:
    return pd.Timestamp(ms, unit="ms", tz="UTC")


def _ms(t: pd.Timestamp) -> int:
    return int(pd.Timestamp(t).value // 1_000_000)


# ---- ATR por temporalidad ---------------------------------------------------------------------------


class AtrCache:
    """ATR(n) de la temporalidad, indexado por el CIERRE de cada vela (cuando se conoce). Un calculo por simbolo y
    temporalidad en cada corrida (las componentes de una familia comparten la serie)."""

    def __init__(self, data: dict[str, Any], n: int = ATR_N) -> None:
        self.data, self.n, self._c = data, n, {}

    def series(self, symbol: str, tf: str) -> pd.Series:
        key = (symbol, tf)
        if key not in self._c:
            from . import features as F
            from . import strategies as S
            from .clock import resample_bars

            md = self.data[symbol]
            bars = S._daily(md) if tf == "1D" else resample_bars(md.klines, tf)
            self._c[key] = F.atr(bars, self.n).dropna() if len(bars) else pd.Series(dtype=float)
        return self._c[key]

    def at(self, symbol: str, tf: str, t: pd.Timestamp) -> float:
        s = self.series(symbol, tf)
        j = s.index.searchsorted(t, side="right") - 1
        return float(s.iloc[j]) if j >= 0 else math.nan


# ---- slots (componentes) y regla del laboratorio --------------------------------------------------------


def slots(p: dict[str, Any]) -> list[tuple[str, float, Any, Any, dict[str, Any]]]:
    """(slot, peso, spec, regla, params) de la base de una -v2: sus componentes si es cartera, si no ella misma con
    los valores de la grilla que trae `p`."""
    from .strategies import CATALOG

    spec, rule = CATALOG[p["base"]]
    comps = spec.params.get("components")
    if isinstance(comps, dict):
        return [(c, float(w), CATALOG[c][0], CATALOG[c][1], dict(CATALOG[c][0].params)) for c, w in comps.items()]
    return [(p["base"], 1.0, spec, rule, {**spec.params, **{k: p[k] for k in spec.grid if k in p}})]


def run_chain(data: dict[str, Any], p: dict[str, Any]) -> dict[str, pd.Series]:
    """Regla -v2 para el laboratorio: `advance` desde el principio de los datos. Precio de entrada = open de la
    primera vela 1m con apertura >= decision (la convencion del simulador)."""
    from .strategies import _changes, _combine

    sp = {k: p[k] for k in PARAMS}
    atrs = AtrCache(data, int(sp["atr_n"]))
    sigs, ws = [], []
    for slot, w, spec, rule, bp in slots(p):
        tf = atr_timeframe(spec.timeframe, bp)
        base = rule(data, bp)
        out: dict[str, pd.Series] = {}
        for sym, md in data.items():
            k = md.klines
            if k is None or not len(k):
                out[sym] = _changes({})
                continue
            opens = k["open"].to_numpy()

            def make(t, side, size, sym=sym, k=k, opens=opens, slot=slot, tf=tf):
                j = k.index.searchsorted(t, side="left")
                if j >= len(k):
                    return None
                return open_position(slot, sym, side, size, 1.0, _ms(t), float(opens[j]), atrs.at(sym, tf, t), tf, sp)

            evs, _ = advance(None, new_entries(base.get(sym)), k, start=k.index[0], end=k.index[-1] + MIN,
                             entry_after=None, make=make)
            ch: dict[pd.Timestamp, float] = {}
            for e in evs:
                if e["kind"] == "open":
                    ch[e["t"]] = e["pos"].side * e["pos"].size
                elif e["kind"] == "close":
                    ch[e["t"]] = 0.0
            out[sym] = _changes(ch)
        sigs.append(out)
        ws.append(w)
    return sigs[0] if len(sigs) == 1 and ws[0] == 1.0 else _combine(sigs, ws)


def is_v2(spec: Any) -> bool:
    """Una spec -v2 con SL/TP obligatorio (la arma `strategies._sltp_specs`)."""
    p = getattr(spec, "params", None) or {}
    return "base" in p and all(k in p for k in PARAMS)
