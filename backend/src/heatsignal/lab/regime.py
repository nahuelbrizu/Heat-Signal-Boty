"""
`htf-trend-v1` (features/htf.py) vectorizado para el laboratorio: la tendencia del dia UTC en cada cierre de vela de 1h.

La regla es la MISMA que usa el grafo en vivo (alcista / bajista / lateral / sin_datos; ver el docstring de
`features/htf.py`) y no se cambia aca: este modulo solo la calcula para toda la historia de una vez, en vez de una
llamada a `build_htf_context` por hora. Un test la compara contra `build_htf_context` instante por instante.

Diferencia declarada: el laboratorio arma las velas de 1h solo con horas COMPLETAS (`clock.resample_bars`); en vivo una
hora con minutos faltantes entra igual (marcada `partial`). Con datos completos dan lo mismo.

Indice del resultado = instante de DECISION = cierre de la vela de 1h (la regla en vivo corre con now_ms = ese cierre).
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from ..features.htf import ATR_PERIOD, EMA_1D, EMA_1H, SLOPE_BARS, TREND_MIN_ATR
from .clock import resample_bars

HOUR = pd.Timedelta(hours=1)
DAY_MS = 86_400_000
HOUR_MS = 3_600_000


def _ema_seeded(x: np.ndarray, period: int) -> np.ndarray:
    """EMA sembrada con la SMA de los primeros `period` valores (como `htf.ema_series`); NaN antes."""
    out = np.full(len(x), np.nan)
    if len(x) < period:
        return out
    k = 2.0 / (period + 1)
    e = float(np.mean(x[:period]))
    out[period - 1] = e
    for i in range(period, len(x)):
        e = x[i] * k + e * (1 - k)
        out[i] = e
    return out


def _wilder_atr(h: np.ndarray, lo: np.ndarray, c: np.ndarray, period: int) -> np.ndarray:
    """ATR de Wilder como `htf.wilder_atr` sobre las velas hasta i (inclusive); NaN si hay <= period velas."""
    out = np.full(len(c), np.nan)
    if len(c) < period + 1:
        return out
    tr = np.maximum.reduce([h[1:] - lo[1:], np.abs(h[1:] - c[:-1]), np.abs(lo[1:] - c[:-1])])
    a = float(np.mean(tr[:period]))
    out[period] = a
    for j in range(period, len(tr)):
        a = (a * (period - 1) + tr[j]) / period
        out[j + 1] = a
    return out


def daily_trend_1h(k1m: pd.DataFrame) -> pd.Series:
    """Tendencia del dia (`htf-trend-v1`) en cada cierre de vela de 1h, indexada por ese cierre."""
    bars = resample_bars(k1m, "1h") if len(k1m) else pd.DataFrame()
    if bars.empty:
        return pd.Series(dtype=object)
    close_ms = pd.DatetimeIndex(bars.index).as_unit("ns").asi8 // 1_000_000
    open_ms = close_ms - HOUR_MS
    o, h, lo, c = (bars[k].to_numpy(dtype=float) for k in ("open", "high", "low", "close"))
    atr = _wilder_atr(h, lo, c, ATR_PERIOD)
    ema = _ema_seeded(c, EMA_1H)
    slope = np.full(len(c), np.nan)
    ok = np.arange(len(c)) >= EMA_1H - 1 + SLOPE_BARS
    with np.errstate(invalid="ignore", divide="ignore"):
        slope[ok] = (ema[ok] - ema[np.flatnonzero(ok) - SLOPE_BARS]) / atr[ok]
    slope[~(atr > 0)] = np.nan

    day = open_ms - open_ms % DAY_MS
    df = pd.DataFrame({"day": day, "open_ms": open_ms, "open": o, "close": c})
    g = df.groupby("day", sort=True)
    daily = pd.DataFrame({"n": g.size(), "close": g["close"].last(), "first_open_ms": g["open_ms"].first(),
                          "first_open": g["open"].first()})
    full = daily[daily["n"] == 24]
    ema_d = _ema_seeded(full["close"].to_numpy(), EMA_1D)
    ema_days = full.index.to_numpy()

    out = np.empty(len(c), dtype=object)
    for i in range(len(c)):
        t = int(close_ms[i])
        day_start = t - t % DAY_MS
        if t == day_start or day_start not in daily.index or daily.at[day_start, "first_open_ms"] != day_start:
            out[i] = "sin_datos"  # sin vela de 1h cerrada del dia, o falta la de 00:00 UTC
            continue
        j = int(np.searchsorted(ema_days, day_start, side="left")) - 1  # ultimo dia completo ANTERIOR
        e1d = float(ema_d[j]) if j >= 0 else np.nan
        a, s = atr[i], slope[i]
        if not (a > 0) or np.isnan(e1d) or np.isnan(s):
            out[i] = "sin_datos"
            continue
        chg = (c[i] - daily.at[day_start, "first_open"]) / a
        if chg >= TREND_MIN_ATR and c[i] > e1d and s > 0:
            out[i] = "alcista"
        elif chg <= -TREND_MIN_ATR and c[i] < e1d and s < 0:
            out[i] = "bajista"
        else:
            out[i] = "lateral"
    return pd.Series(out, index=bars.index, name="daily_trend")
