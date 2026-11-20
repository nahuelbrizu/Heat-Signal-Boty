"""
Velas de 5m y 15m en vivo (live-5m15m-v1, 2026-11-19): apoyo y monitoreo de las conclusiones del grafo.

Pedido del usuario: "analizar mas velas de menos duracion para apoyar y monitorear las conclusiones y tratar de
seguir en vivo al mercado". Reglas:
  - Solo CODIGO, sin LLM. Fuente: `bars_1m` propios de Binance (trades del WS, los escribe el grabador cada minuto;
    es la tabla que se ve en "Datos guardados").
  - Solo velas CERRADAS y completas: una vela de N minutos cuenta si cerro (open + N min <= ahora) y tiene al menos
    `MIN_FILL` de sus minutos con precio; las otras se descartan y se declaran en `data_gaps`.
  - NO abren hipotesis ni cambian decisiones: ninguna estrategia esta pre-registrada en 5m/15m (abrir por esto seria
    una estrategia nueva sin pre-registro). El grafo las recibe como contexto y el panel las muestra en "En vivo".
  - El monitoreo de una hipotesis abierta (`monitor_hypothesis`) es descriptivo: la resolucion sigue siendo la del
    evaluador al vencimiento (evaluation/resolver.py), nunca un cierre anticipado.
Sin pandas (corre en el proceso de ingesta y en el panel).
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from typing import Any

from ..schema.features import Candle1h, LiveContext, LiveTf
from .htf import DAY_MS, ema_series, wilder_atr

MIN_MS = 60_000
TIMEFRAMES = {"5m": 5, "15m": 15}
MIN_FILL = 0.8  # fraccion minima de minutos con precio para que una vela cuente
EMA_N, ATR_N = 20, 14
KEEP_CANDLES = 12
STALE_MS = 3 * MIN_MS  # sin minutos nuevos hace mas de esto -> el WS o el grabador estan atrasados
ALERT_ATR = 1.0  # movimiento a favor/en contra que se marca, en ATR14 de 15m

Row = Sequence[Any]  # (minute_ms, open, high, low, close[, volumen USD])


def aggregate(rows: Sequence[Row], minutes: int, now_ms: int, gaps: list[str] | None = None) -> list[Candle1h]:
    """1m ordenadas -> velas de `minutes` alineadas a UTC, solo cerradas y con >= MIN_FILL de minutos con precio."""
    span = minutes * MIN_MS
    groups: dict[int, list[Row]] = {}
    for r in rows:
        if r[1] is None or r[2] is None or r[3] is None or r[4] is None:
            continue
        t = int(r[0])
        groups.setdefault(t - t % span, []).append(r)
    need = math.ceil(minutes * MIN_FILL)
    out, dropped = [], 0
    for start in sorted(groups):
        if start + span > now_ms:
            continue  # en curso
        g = sorted(groups[start], key=lambda r: r[0])
        if len(g) < need:
            dropped += 1
            continue
        out.append(Candle1h(open_ms=start, open=float(g[0][1]), high=max(float(r[2]) for r in g),
                            low=min(float(r[3]) for r in g), close=float(g[-1][4]), source="bars_1m",
                            minutes=min(60, len(g))))
    if dropped and gaps is not None:
        gaps.append(f"live: {dropped} vela(s) de {minutes}m descartadas por tener < {need} minutos con precio")
    return out


def _tf_context(tf: str, bars: list[Candle1h], now_ms: int) -> LiveTf:
    minutes = TIMEFRAMES[tf]
    if not bars:
        return LiveTf(timeframe=tf, status="missing", reason=f"sin velas de {tf} cerradas")
    last = bars[-1]
    expected = now_ms - now_ms % (minutes * MIN_MS) - minutes * MIN_MS
    closes = [b.close for b in bars]
    ema = ema_series(closes, EMA_N)
    atr = wilder_atr(bars, ATR_N)
    status, reason = "ok", ""
    if last.open_ms != expected:
        status, reason = "partial", f"la ultima vela de {tf} cerrada no es la esperada (falta la mas reciente)"
    elif not ema or atr is None:
        status, reason = "partial", f"faltan velas para EMA{EMA_N}/ATR{ATR_N} ({len(bars)} de {EMA_N})"
    e = ema[-1] if ema else None
    dist = (last.close - e) / atr if e is not None and atr else None
    slope = (ema[-1] - ema[-4]) / atr if len(ema) >= 4 and atr else None
    ret3 = (last.close / bars[-4].close - 1) * 100 if len(bars) >= 4 and bars[-4].close else None
    return LiveTf(timeframe=tf, status=status, reason=reason, last_closed_open_ms=last.open_ms, close=last.close,
                  ema20=e, atr14=atr, dist_ema20_atr=dist, ema20_slope_atr=slope, ret_3bars_pct=ret3, bars=len(bars),
                  candles=[(b.open_ms, b.open, b.high, b.low, b.close) for b in bars[-KEEP_CANDLES:]])


def _vwap(rows: Sequence[Row], day_start: int) -> float | None:
    """VWAP del dia UTC: precio tipico x volumen base (bars_1m trae volumen en USD: base = USD / precio tipico)."""
    pv = vol = 0.0
    for r in rows:
        if int(r[0]) < day_start or len(r) < 6 or r[5] is None or r[2] is None:
            continue
        tp = (float(r[2]) + float(r[3]) + float(r[4])) / 3
        if tp <= 0:
            continue
        v = float(r[5]) / tp
        pv += tp * v
        vol += v
    return pv / vol if vol > 0 else None


def build_live_context(symbol: str, rows: Sequence[Row], now_ms: int, gaps: Sequence[str] = ()) -> LiveContext:
    g = list(gaps)
    priced = [r for r in rows if r[4] is not None]
    if not priced:
        return LiveContext(symbol=symbol, ts_ms=now_ms, status="missing",
                           data_gaps=[*g, "live: sin bars_1m recientes (grabador apagado o WS caido)"])
    last = max(priced, key=lambda r: r[0])
    tfs = {tf: _tf_context(tf, aggregate(rows, m, now_ms, g), now_ms) for tf, m in TIMEFRAMES.items()}
    last_ms = int(last[0])
    if now_ms - (last_ms + MIN_MS) > STALE_MS:
        g.append(f"live: el ultimo minuto con precio es de hace {(now_ms - last_ms) // MIN_MS} min")
    vwap = _vwap(rows, now_ms - now_ms % DAY_MS)
    atr15 = tfs["15m"].atr14
    price = float(last[4])
    status = ("missing" if all(t.status == "missing" for t in tfs.values())
              else "ok" if all(t.status == "ok" for t in tfs.values()) and now_ms - (last_ms + MIN_MS) <= STALE_MS
              else "partial")
    return LiveContext(symbol=symbol, ts_ms=now_ms, status=status, last_minute_ms=last_ms, last_price=price,
                       vwap_day=vwap, dist_vwap_atr15=(price - vwap) / atr15 if vwap and atr15 else None, tfs=tfs,
                       data_gaps=g)


def missing_live(symbol: str, now_ms: int, reason: str) -> LiveContext:
    return LiveContext(symbol=symbol, ts_ms=now_ms, status="missing", data_gaps=[f"live: {reason}"])


# ---- monitoreo de una hipotesis abierta ---------------------------------------------------------------

def monitor_hypothesis(h: dict[str, Any], rows: Sequence[Row], live: LiveContext, now_ms: int) -> dict[str, Any]:
    """Como va una hipotesis abierta con los minutos desde que se creo. Descriptivo: no la resuelve ni la cierra.

    `h`: {hypothesis_id, symbol, direction, entry_price, created_ms, due_ms, ...}. Devuelve pnl, maximo a favor (MFE)
    y en contra (MAE) en %, y alertas fijas (reglas de este modulo, no del LLM)."""
    sign = 1.0 if h["direction"] == "LONG" else -1.0
    entry = float(h["entry_price"])
    start = h["created_ms"] - h["created_ms"] % MIN_MS
    after = [r for r in rows if int(r[0]) >= start and r[2] is not None and r[3] is not None]
    out: dict[str, Any] = {**h, "minutes_to_due": max(0, (h["due_ms"] - now_ms) // MIN_MS), "alerts": []}
    price = live.last_price
    if not entry or price is None:
        out.update(status="sin_datos", pnl_pct=None, mfe_pct=None, mae_pct=None)
        out["alerts"].append("sin precio en vivo para seguirla")
        return out
    pnl = sign * (price / entry - 1) * 100
    hi = max([float(r[2]) for r in after] or [price])
    lo = min([float(r[3]) for r in after] or [price])
    mfe = (hi / entry - 1) * 100 if sign > 0 else (1 - lo / entry) * 100
    mae = (lo / entry - 1) * 100 if sign > 0 else (1 - hi / entry) * 100
    t15, t5 = live.tfs.get("15m"), live.tfs.get("5m")
    atr_pct = t15.atr14 / entry * 100 if t15 is not None and t15.atr14 else None
    out.update(status="ok", last_price=price, pnl_pct=pnl, mfe_pct=mfe, mae_pct=mae,
               pnl_atr15=pnl / atr_pct if atr_pct else None, minutes_seguida=len(after))
    if t15 is not None and t15.dist_ema20_atr is not None and sign * t15.dist_ema20_atr < 0:
        out["alerts"].append("el ultimo cierre de 15m quedo del lado contrario de la EMA20 de 15m")
    if (t5 is not None and t5.ret_3bars_pct is not None and sign * t5.ret_3bars_pct < 0 and t15 is not None
            and t15.ret_3bars_pct is not None and sign * t15.ret_3bars_pct < 0):
        out["alerts"].append("las ultimas 3 velas de 5m y de 15m van en contra")
    if atr_pct and pnl <= -ALERT_ATR * atr_pct:
        out["alerts"].append(f"va en contra mas de {ALERT_ATR:g} ATR de 15m")
    if atr_pct and pnl >= ALERT_ATR * atr_pct:
        out["alerts"].append(f"va a favor mas de {ALERT_ATR:g} ATR de 15m")
    if live.vwap_day is not None and sign * (price - live.vwap_day) < 0:
        out["alerts"].append("el precio esta del lado contrario del VWAP del dia")
    return out


def live_brief_line(live: LiveContext | None) -> str:
    """Una linea compacta para el contexto del grafo (~60 tokens)."""
    if live is None:
        return "  [5m/15m en vivo]: no calculado"
    if live.status == "missing":
        return f"  [5m/15m en vivo]: sin_datos ({'; '.join(live.data_gaps) or 'sin velas'})"

    def f(v: float | None, fmt: str) -> str:
        return "n/d" if v is None else fmt.format(v)

    parts = []
    for tf in ("15m", "5m"):
        t = live.tfs.get(tf)
        if t is None or t.status == "missing":
            parts.append(f"{tf} sin_datos")
            continue
        parts.append(f"{tf}: cierre vs EMA20 {f(t.dist_ema20_atr, '{:+.2f}')} ATR (pendiente "
                     f"{f(t.ema20_slope_atr, '{:+.2f}')}), ultimas 3 velas {f(t.ret_3bars_pct, '{:+.2f}')}%"
                     + ("" if t.status == "ok" else f" [{t.status}]"))
    return (f"  [5m/15m en vivo, codigo {live.live_model_id}, SOLO apoyo: no abre hipotesis] " + "; ".join(parts)
            + f"; precio vs VWAP del dia {f(live.dist_vwap_atr15, '{:+.2f}')} ATR15m")
