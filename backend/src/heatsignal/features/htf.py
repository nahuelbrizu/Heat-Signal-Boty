"""
Contexto multi-temporalidad (`htf-trend-v1`): velas de 1h CERRADAS + tendencia del dia UTC.

Puro y deterministico (sin I/O ni reloj): recibe las velas de 1h ya armadas por `history/hourly.py`
y el `now_ms` del caller. Solo se usan velas con `open_ms + 1h <= now_ms` (sin look-ahead): una vela
abierta o futura que llegue por error se descarta y se declara.

Calculos (todo sobre cierres de velas de 1h cerradas):
  - ATR(14) de Wilder en 1h; EMA20 y SMA50 de 1h; pendiente de la EMA20 = (EMA20 ahora - EMA20 hace 4 velas) / ATR.
  - Dia UTC en curso: apertura = open de la vela 00:00 UTC; max/min/ultimo cierre de las velas cerradas del dia;
    cambio desde la apertura en % y en ATR(1h); posicion del cierre en el rango del dia (0 = minimo, 1 = maximo).
  - Velas diarias = dias UTC anteriores COMPLETOS (24 velas de 1h); EMA10 y SMA20 diarias de sus cierres.

Regla `daily_trend` (fija; cambiarla = `htf-trend-v2`, nunca pisar esta):
    prerequisitos: >= 1 vela de 1h cerrada del dia, ATR(14) 1h, EMA20 1h con 4 velas de pendiente y
                   >= 10 dias completos para la EMA10 diaria. Si falta alguno -> "sin_datos" (con motivo).
    alcista  si  cambio_desde_apertura >= +1.5 ATR(1h)  Y  ultimo_cierre > EMA10 diaria  Y  pendiente EMA20 1h > 0
    bajista  si  cambio_desde_apertura <= -1.5 ATR(1h)  Y  ultimo_cierre < EMA10 diaria  Y  pendiente EMA20 1h < 0
    lateral  en cualquier otro caso.

Setups (banderas por codigo; las estrategias 1h pre-registradas los exigen):
    pullback_ema20_1h_a_favor: tendencia alcista (bajista) y la ultima vela toco la EMA20 1h desde el lado de la
        tendencia (low <= EMA20 + 0.25 ATR; high >= EMA20 - 0.25 ATR) cerrando a <= 0.5 ATR de ella y del lado de
        la tendencia (o sobre la EMA).
    extremo_rango_diario: rango del dia >= 2 ATR y el cierre en el 15% superior ("maximo") o inferior ("minimo").
    continuacion_a_favor: tendencia alcista (bajista), cierre del lado de la tendencia de la EMA20 1h a <= 1.5 ATR
        y fuera del 15% extremo del rango diario en la direccion de la tendencia.
"""

from __future__ import annotations

from collections.abc import Sequence

from ..schema.features import Candle1h, DailyTrend, HtfContext, HtfSetups, TrendAlignment

HOUR_MS = 3_600_000
DAY_MS = 86_400_000
OUT_CANDLES = 48
ATR_PERIOD = 14
EMA_1H = 20
SMA_1H = 50
SLOPE_BARS = 4
EMA_1D = 10
SMA_1D = 20
TREND_MIN_ATR = 1.5
EXTREME_FRAC = 0.15
MIN_RANGE_ATR = 2.0
PULLBACK_TOUCH_ATR = 0.25
PULLBACK_MAX_ATR = 0.5
CONT_MAX_ATR = 1.5


def ema_series(values: Sequence[float], period: int) -> list[float]:
    """EMA sembrada con la SMA de los primeros `period` valores; [] si no alcanzan."""
    if len(values) < period:
        return []
    k = 2.0 / (period + 1)
    e = sum(values[:period]) / period
    out = [e]
    for v in values[period:]:
        e = v * k + e * (1 - k)
        out.append(e)
    return out


def wilder_atr(candles: Sequence[Candle1h], period: int = ATR_PERIOD) -> float | None:
    if len(candles) < period + 1:
        return None
    trs = [max(c.high - c.low, abs(c.high - p.close), abs(c.low - p.close)) for p, c in zip(candles, candles[1:])]
    atr = sum(trs[:period]) / period
    for tr in trs[period:]:
        atr = (atr * (period - 1) + tr) / period
    return atr


def trend_alignment(direction: str | None, daily_trend: str | None) -> TrendAlignment:
    """LONG en alcista / SHORT en bajista = a favor; al reves = en contra."""
    if daily_trend in (None, "sin_datos") or direction not in ("LONG", "SHORT"):
        return "sin_datos"
    if daily_trend == "lateral":
        return "lateral"
    up = daily_trend == "alcista"
    return "a_favor" if (direction == "LONG") == up else "en_contra"


def classify_trend(change_atr: float | None, last_close: float | None, ema_1d: float | None,
                   slope_atr: float | None) -> tuple[DailyTrend, str]:
    """La regla `htf-trend-v1` aislada (testeable con numeros sinteticos)."""
    missing = [n for n, v in (("vela de 1h cerrada del dia", change_atr), ("ultimo cierre", last_close),
                              ("EMA10 diaria", ema_1d), ("pendiente EMA20 1h", slope_atr)) if v is None]
    if missing:
        return "sin_datos", "faltan: " + ", ".join(missing)
    assert change_atr is not None and last_close is not None and ema_1d is not None and slope_atr is not None
    detail = (f"cambio desde la apertura {change_atr:+.2f} ATR(1h), cierre {'>' if last_close > ema_1d else '<='} "
              f"EMA10 diaria, pendiente EMA20 1h {slope_atr:+.2f} ATR")
    if change_atr >= TREND_MIN_ATR and last_close > ema_1d and slope_atr > 0:
        return "alcista", detail
    if change_atr <= -TREND_MIN_ATR and last_close < ema_1d and slope_atr < 0:
        return "bajista", detail
    return "lateral", detail


def _daily(candles: Sequence[Candle1h], day_start_ms: int) -> tuple[list[dict[str, float]], int]:
    """Velas diarias de los dias ANTERIORES completos (24 velas). Devuelve (dias, dias incompletos)."""
    days: dict[int, list[Candle1h]] = {}
    for c in candles:
        d = c.open_ms - c.open_ms % DAY_MS
        if d < day_start_ms:
            days.setdefault(d, []).append(c)
    out, incomplete = [], 0
    for d in sorted(days):
        cs = days[d]
        if len(cs) != 24:
            incomplete += 1
            continue
        out.append({"open_ms": d, "open": cs[0].open, "high": max(c.high for c in cs),
                    "low": min(c.low for c in cs), "close": cs[-1].close})
    return out, incomplete


def build_htf_context(symbol: str, candles: Sequence[Candle1h], now_ms: int,
                      gaps: Sequence[str] = (), day_start_ms: int | None = None) -> HtfContext:
    """Arma el contexto desde velas de 1h (cualquier orden). `gaps`: huecos que ya declaro la fuente.

    `day_start_ms` (opcional) fija el dia UTC a describir; por defecto el dia de `now_ms`. Lo usa el analisis de
    cierre (a las 00:00 UTC describe el dia que termino, con sus 24 velas ya cerradas).
    """
    data_gaps = list(gaps)
    day_start = now_ms - now_ms % DAY_MS if day_start_ms is None else day_start_ms - day_start_ms % DAY_MS
    uniq: dict[int, Candle1h] = {}
    future = 0
    for c in candles:
        if c.open_ms % HOUR_MS:
            data_gaps.append(f"htf: vela desalineada descartada (open_ms={c.open_ms})")
            continue
        if c.open_ms + HOUR_MS > now_ms:
            future += 1
            continue
        uniq[c.open_ms] = c
    if future:
        data_gaps.append(f"htf: {future} vela(s) abiertas o futuras descartadas (solo velas cerradas)")
    cs = [uniq[k] for k in sorted(uniq)]
    if not cs:
        data_gaps.append("htf: sin velas de 1h cerradas: tendencia diaria sin_datos")
        return HtfContext(symbol=symbol, ts_ms=now_ms, status="missing", day_start_ms=day_start,
                          trend_reason="sin velas de 1h", data_gaps=data_gaps)

    holes = sum((b.open_ms - a.open_ms) // HOUR_MS - 1 for a, b in zip(cs, cs[1:]))
    if holes:
        data_gaps.append(f"htf: {holes} hora(s) sin vela dentro de la serie de 1h (EMA/ATR las saltean)")
    partial = [c for c in cs[-OUT_CANDLES:] if c.minutes < 60]
    if partial:
        data_gaps.append(f"htf: {len(partial)} vela(s) de 1h armadas con minutos faltantes "
                         f"(minimo {min(c.minutes for c in partial)}/60)")

    cs = [c for c in cs if c.open_ms < day_start + DAY_MS]  # nada posterior al dia descripto
    if not cs:
        return HtfContext(symbol=symbol, ts_ms=now_ms, status="missing", day_start_ms=day_start,
                          trend_reason="sin velas de 1h hasta el dia pedido", data_gaps=data_gaps)
    closes = [c.close for c in cs]
    atr = wilder_atr(cs)
    ema1h = ema_series(closes, EMA_1H)
    ema_now = ema1h[-1] if ema1h else None
    slope = None
    if atr and len(ema1h) > SLOPE_BARS:
        slope = (ema1h[-1] - ema1h[-1 - SLOPE_BARS]) / atr
    sma50 = sum(closes[-SMA_1H:]) / SMA_1H if len(closes) >= SMA_1H else None
    last = cs[-1]
    expected_last = min(now_ms - now_ms % HOUR_MS, day_start + DAY_MS) - HOUR_MS
    if last.open_ms < expected_last:
        data_gaps.append(f"htf: la ultima vela de 1h cerrada disponible tiene "
                         f"{(expected_last - last.open_ms) // HOUR_MS} hora(s) de atraso")

    today = [c for c in cs if day_start <= c.open_ms < day_start + DAY_MS]
    day_open = day_high = day_low = None
    chg_pct = chg_atr = pos = range_atr = None
    if today and today[0].open_ms == day_start:
        day_open = today[0].open
        day_high, day_low = max(c.high for c in today), min(c.low for c in today)
        chg_pct = (last.close / day_open - 1) * 100
        chg_atr = (last.close - day_open) / atr if atr else None
        pos = (last.close - day_low) / (day_high - day_low) if day_high > day_low else 0.5
        range_atr = (day_high - day_low) / atr if atr else None
    elif today:
        data_gaps.append("htf: falta la vela de 00:00 UTC: sin apertura del dia")

    days, incomplete = _daily(cs, day_start)
    if incomplete:
        data_gaps.append(f"htf: {incomplete} dia(s) UTC incompletos excluidos de las medias diarias")
    dcloses = [d["close"] for d in days]
    ema1d = ema_series(dcloses, EMA_1D)
    ema1d_now = ema1d[-1] if ema1d else None
    sma20d = sum(dcloses[-SMA_1D:]) / SMA_1D if len(dcloses) >= SMA_1D else None
    if ema1d_now is None:
        data_gaps.append(f"htf: {len(dcloses)} dias completos < {EMA_1D} para la EMA diaria")

    trend, reason = classify_trend(chg_atr, last.close, ema1d_now, slope)
    dist = (last.close - ema_now) / atr if atr and ema_now is not None else None

    setups = HtfSetups()
    if pos is not None and range_atr is not None and range_atr >= MIN_RANGE_ATR:
        if pos >= 1 - EXTREME_FRAC:
            setups.extremo_rango_diario = "maximo"
        elif pos <= EXTREME_FRAC:
            setups.extremo_rango_diario = "minimo"
    if trend in ("alcista", "bajista") and atr and ema_now is not None and dist is not None:
        up = trend == "alcista"
        touched = (last.low <= ema_now + PULLBACK_TOUCH_ATR * atr) if up else (last.high >= ema_now - PULLBACK_TOUCH_ATR * atr)
        side_ok = dist >= -PULLBACK_MAX_ATR if up else dist <= PULLBACK_MAX_ATR
        setups.pullback_ema20_1h_a_favor = bool(touched and side_ok and abs(dist) <= PULLBACK_MAX_ATR)
        at_trend_extreme = setups.extremo_rango_diario == ("maximo" if up else "minimo")
        on_side = dist > 0 if up else dist < 0
        setups.continuacion_a_favor = bool(on_side and abs(dist) <= CONT_MAX_ATR and not at_trend_extreme)

    prev = days[-1] if days and days[-1]["open_ms"] == day_start - DAY_MS else None
    sources: dict[str, int] = {}
    for c in cs[-OUT_CANDLES:]:
        sources[c.source] = sources.get(c.source, 0) + 1
    status = "ok" if trend != "sin_datos" and not holes and not partial else "partial"
    return HtfContext(
        symbol=symbol, ts_ms=now_ms, status=status, candles_1h=cs[-OUT_CANDLES:], last_closed_open_ms=last.open_ms,
        day_start_ms=day_start, day_hours_closed=len(today), day_open=day_open, day_high=day_high, day_low=day_low,
        last_close=last.close, change_from_open_pct=chg_pct, change_from_open_atr=chg_atr, pos_in_day_range=pos,
        day_range_atr=range_atr,
        prev_day=None if prev is None else {k: prev[k] for k in ("open", "high", "low", "close")},
        ema20_1h=ema_now, sma50_1h=sma50, dist_ema20_1h_atr=dist, ema20_1h_slope_atr=slope,
        ema10_1d=ema1d_now, sma20_1d=sma20d,
        close_vs_ema10_1d_pct=None if ema1d_now is None else (last.close / ema1d_now - 1) * 100,
        daily_closes=len(dcloses), atr14_1h=atr, atr14_1h_pct=None if atr is None else atr / last.close * 100,
        daily_trend=trend, trend_reason=reason, setups=setups, sources=sources, data_gaps=data_gaps,
    )


def missing_context(symbol: str, now_ms: int, reason: str) -> HtfContext:
    """Contexto vacio y declarado (fallo de la fuente): nunca se omite en silencio."""
    return HtfContext(symbol=symbol, ts_ms=now_ms, status="missing", day_start_ms=now_ms - now_ms % DAY_MS,
                      trend_reason=reason, data_gaps=[f"htf: {reason}"])
