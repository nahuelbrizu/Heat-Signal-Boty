"""
Lote 7: grilla long con stop por compra (idea de infinity-grid, research/infinity-grid.md; reimplementada, sin codigo
del proyecto). Pre-registro aprobado por el usuario el 2026-10-26 (stop por compra, opcion a) ANTES de cualquier
resultado; ver decisions.md, "Lote 7: grilla".

Regla por simbolo, sobre velas 1m (decision al CIERRE de la vela del evento; el simulador ejecuta al open siguiente):
  - Escalera anclada en A: n_lots escalones L_j = A / (1 + interval)^j, j = 1..n_lots. Cada escalon tiene a lo sumo
    UN lote de 1/n_lots del capital del simbolo (exposicion maxima 1x, sin apalancamiento).
  - Compra: un escalon libre se llena cuando el minimo de una vela 1m queda ESTRICTAMENTE debajo de L_j (tocar no
    alcanza) y la vela ANTERIOR cerro por encima de L_j (la orden limite esperaba debajo del precio: una limite por
    encima del precio se llenaria al instante a precio de mercado, no en el escalon; asi un escalon que el precio ya
    dejo arriba —despues de un stop o mientras la variante lateral no compraba— no "compra en el escalon"). En la variante lateral, ademas la tendencia del dia (`htf-trend-v1`, ultimo cierre de 1h conocido) tiene
    que ser 'lateral'; si no, no se compra (los lotes abiertos siguen con su objetivo y su stop).
  - Objetivo (take profit) del lote: L_j * (1 + interval) (el escalon de arriba): maximo de una vela >= nivel.
  - Stop del lote (como las -v2, lab/sltp.py): L_j * (1 - f), f = clip(3 * ATR(14) 1h / L_j, 8 %, 20 %), con el ATR de
    la ultima vela de 1h CERRADA al momento de la compra. Si en la misma vela se tocan objetivo y stop -> stop. El
    lote se evalua desde la vela SIGUIENTE a la de su compra. Al cerrar, el escalon queda libre desde la vela siguiente.
  - Corrimiento hacia arriba (infinity-grid): sin lotes abiertos, si el maximo de una vela supera A * (1 + interval)
    (= el escalon de arriba del primero), A pasa a ser el cierre de esa vela. Con lotes abiertos la escalera queda fija.
  - Orden dentro de una vela: cierres (stop/objetivo) -> compras -> corrimiento.
  - Inicio: A = cierre de la primera vela con ATR(14) 1h disponible.
  - Guardia de liquidacion: `sltp.check_stop` con la exposicion total del simbolo (<= 1x: siempre cumple).

Diferencias con infinity-grid (declaradas): futuros a 1x en vez de spot; stop por compra; las ordenes se simulan con la
convencion del laboratorio (decision al cierre de la vela que cruza, fill al open siguiente con costo taker 5 + 2 bps)
en vez de limite al precio exacto con costo maker: es mas conservador en costos y no depende de verificar tarifas maker;
con lotes abiertos la escalera no se corre (infinity-grid corre las compras pendientes igual).
Sesgo del llenado (medido, no corregido): el simulador llena la compra al open de la vela siguiente al cruce; una
limite real llena en el escalon. Si el precio sigue bajando, el simulador compra MAS BARATO que la realidad (sobre todo
en un derrumbe que cruza varios escalones en un minuto); en la venta por objetivo, al reves. `fill_bias` lo mide lote por
lote (en fraccion del capital del simbolo) para juzgar el resultado con y sin el sesgo.
"""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from . import sltp as SL

MIN = pd.Timedelta(minutes=1)


def _first(mask_fn, start: int, n: int, chunk: int = 4096) -> int | None:
    """Primer indice >= start donde `mask_fn(a, b)` (bool[a:b]) es True, buscando en bloques crecientes."""
    s = start
    while s < n:
        e = min(n, s + chunk)
        m = mask_fn(s, e)
        if m.any():
            return s + int(m.argmax())
        s, chunk = e, chunk * 2
    return None


def grid_symbol(k: pd.DataFrame, atr_1h: pd.Series, allowed: np.ndarray | None, p: dict[str, Any],
                symbol: str = "") -> tuple[pd.Series, list[dict[str, Any]]]:
    """Posicion objetivo (decision -> peso) y lotes (con motivo de cierre) de un simbolo."""
    n = len(k)
    if n == 0:
        return pd.Series(dtype=float), []
    op, hi, lo, cl = (k[c].to_numpy(dtype=float) for c in ("open", "high", "low", "close"))
    cl_prev = np.r_[np.inf, cl[:-1]]  # cierre de la vela anterior (la orden limite estaba DEBAJO del precio)
    idx = k.index
    step, nl = float(p["interval"]), int(p["n_lots"])
    sp = {kk: p[kk] for kk in SL.PARAMS if kk in p}
    sp = {**SL.PARAMS, **sp}
    # ATR de 1h conocido al CIERRE de cada vela 1m (vela de 1h cerrada <= ese instante)
    atr_ix = atr_1h.index.searchsorted(idx + MIN, side="right") - 1
    atr_v = np.where(atr_ix >= 0, atr_1h.to_numpy()[np.clip(atr_ix, 0, None)], np.nan) if len(atr_1h) else np.full(n, np.nan)
    ok_atr = np.flatnonzero(np.isfinite(atr_v) & (atr_v > 0))
    if not len(ok_atr):
        return pd.Series(dtype=float), []
    i = int(ok_atr[0])
    anchor = cl[i]
    i += 1
    lots: dict[int, dict[str, Any]] = {}       # escalon -> lote abierto
    free_from = {j: i for j in range(1, nl + 1)}  # escalon libre desde la vela
    changes: dict[pd.Timestamp, float] = {}
    trades: list[dict[str, Any]] = []
    can_buy = (lambda a, b: np.ones(b - a, dtype=bool)) if allowed is None else (lambda a, b: allowed[a:b])

    def level(j: int) -> float:
        return anchor / (1 + step) ** j

    def sim_fill(bar: int | None) -> float | None:  # open de la vela siguiente (convencion del simulador)
        return None if bar is None or bar + 1 >= n else float(op[bar + 1])

    while i < n:
        # proximo evento: cierre de un lote, compra de un escalon libre, o corrimiento
        cand = [lt["exit_bar"] for lt in lots.values() if lt["exit_bar"] is not None]
        for j in (j for j in range(1, nl + 1) if j not in lots):
            L = level(j)
            b = _first(lambda a, e, L=L: (lo[a:e] < L) & (cl_prev[a:e] > L) & can_buy(a, e), max(i, free_from[j]), n)
            if b is not None:
                cand.append(b)
        if not lots:
            th = anchor * (1 + step)
            b = _first(lambda a, e: hi[a:e] > th, i, n)
            if b is not None:
                cand.append(b)
        if not cand:
            break
        b = min(cand)
        t = idx[b] + MIN  # decision al cierre de la vela
        before = len(lots)
        # 1) cierres: el escalon queda libre desde la vela siguiente
        for j in [j for j, lt in lots.items() if lt["exit_bar"] == b]:
            lt = lots.pop(j)
            trades.append({**lt, "exit": t})
            free_from[j] = b + 1
        # 2) compras (todas las que cruza la vela, si se puede comprar)
        if allowed is None or allowed[b]:
            for j in range(1, nl + 1):
                L = level(j)
                if j in lots or free_from[j] > b or not (lo[b] < L < cl_prev[b]):
                    continue
                f = SL.sl_fraction(L, float(atr_v[b]), sp)
                if f is None:
                    continue
                sl, tp = L * (1 - f), L * (1 + step)
                SL.check_stop(1.0, L, sl, (len(lots) + 1) / nl, sp)
                # misma regla que sltp.first_hit (empate -> stop), buscando por bloques
                xb = _first(lambda a, e, sl=sl, tp=tp: (lo[a:e] <= sl) | (hi[a:e] >= tp), b + 1, n)
                motivo = None if xb is None else ("stop" if lo[xb] <= sl else "objetivo")
                fb, fx = sim_fill(b), sim_fill(xb) if motivo == "objetivo" else None
                # sesgo del llenado (positivo = el simulador llena mejor que una orden limite real en el nivel)
                bias = (0.0 if fb is None else (L - fb) / L) + (0.0 if fx is None else (fx - tp) / tp)
                lots[j] = {"symbol": symbol, "level": j, "entry": t, "price": L, "sl": sl, "tp": tp, "sl_frac": f,
                           "exit_bar": xb, "motivo": motivo, "sesgo_llenado": bias, "peso": 1 / nl}
        # 3) corrimiento hacia arriba (solo sin lotes)
        if not lots and hi[b] > anchor * (1 + step):
            anchor = cl[b]
            free_from = {j: b + 1 for j in range(1, nl + 1)}
        if len(lots) != before or t in changes:
            changes[t] = len(lots) / nl
        i = b + 1
    for lt in lots.values():
        trades.append({**lt, "exit": None, "motivo": None})
    s = pd.Series(changes, dtype=float).sort_index()
    if len(s):
        s = s[s.diff().fillna(s) != 0]
    return s, trades


def grilla(data: dict[str, Any], p: dict[str, Any]) -> dict[str, pd.Series]:
    from .regime import daily_trend_1h
    from .strategies import _changes

    atrs = SL.AtrCache(data, int(p.get("atr_n", SL.ATR_N)))
    out: dict[str, pd.Series] = {}
    for sym, md in data.items():
        k = md.klines
        if sym not in p["symbols"] or k is None or not len(k):
            out[sym] = _changes({})
            continue
        allowed = None
        if p["lateral"]:
            tr = daily_trend_1h(k)
            j = tr.index.searchsorted(k.index, side="right") - 1  # ultimo cierre de 1h <= apertura de la vela
            vals = tr.to_numpy()
            allowed = np.where(j >= 0, vals[np.clip(j, 0, None)] == "lateral", False) if len(tr) else np.zeros(len(k), bool)
        s, _ = grid_symbol(k, atrs.series(sym, "1h"), allowed, p, sym)
        out[sym] = s if len(s) else _changes({})
    return out


def fill_bias(data: dict[str, Any], p: dict[str, Any]) -> dict[str, Any]:
    """Diagnostico del sesgo del llenado: por simbolo, suma de (sesgo de cada lote x su peso) = cuanto MEJOR (> 0) da el
    simulador que ordenes limite en el nivel, en fraccion del capital del simbolo; y cuantos lotes cerraron por motivo."""
    from .regime import daily_trend_1h

    atrs = SL.AtrCache(data, int(p.get("atr_n", SL.ATR_N)))
    out: dict[str, Any] = {}
    for sym, md in data.items():
        k = md.klines
        if sym not in p["symbols"] or k is None or not len(k):
            continue
        allowed = None
        if p["lateral"]:
            tr = daily_trend_1h(k)
            j = tr.index.searchsorted(k.index, side="right") - 1
            allowed = np.where(j >= 0, tr.to_numpy()[np.clip(j, 0, None)] == "lateral", False) if len(tr) else None
        _, trades = grid_symbol(k, atrs.series(sym, "1h"), allowed, p, sym)
        closed = [t for t in trades if t["motivo"]]
        out[sym] = {"lotes": len(trades), "stop": sum(t["motivo"] == "stop" for t in closed),
                    "objetivo": sum(t["motivo"] == "objetivo" for t in closed),
                    "sesgo_capital": float(sum(t["sesgo_llenado"] * t["peso"] for t in trades))}
    return out
