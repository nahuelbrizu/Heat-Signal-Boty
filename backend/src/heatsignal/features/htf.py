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

Niveles para dias laterales (`htf-levels-v1`, `HtfContext.levels`; aprobados 2026-10-19, ver decisions.md):
    rango de Asia: max/min de las velas 1h de 00:00-08:00 UTC del dia (las 8 deben estar cerradas, si no sin_datos).
    extremos de ayer: max/min del dia UTC anterior completo (24 velas 1h).
    VWAP del dia UTC: sum(precio tipico (H+L+C)/3 x volumen base) / sum(volumen) de las velas 1m desde las 00:00 UTC
        hasta el cierre de la ultima vela 1h cerrada (via `Candle1h.vwap_pv/vwap_vol`; una hora sin 1m -> sin_datos).
    ADX14 1h (Wilder); mecha superior = high - max(O, C), inferior = min(O, C) - low, rango = high - low.
    Banderas (solo con daily_trend = lateral; su valor es la direccion):
      falsa_ruptura_asia:   ultima vela (posterior a Asia) high > maxAsia + 0.5 ATR y close < maxAsia -> SHORT;
                            low < minAsia - 0.5 ATR y close > minAsia -> LONG. (La sesion europa/eeuu la verifica
                            `evaluation/timeframes.py`.)
      rechazo_dia_anterior: high >= maxAyer - 0.25 ATR, mecha sup >= 50% del rango y close < maxAyer -> SHORT;
                            low <= minAyer + 0.25 ATR, mecha inf >= 50% y close > minAyer -> LONG.
      extremo_vwap:         |close - VWAP| >= 2 ATR y ADX14 1h < 20 -> hacia el VWAP (close > VWAP -> SHORT).
    Si se cumplen las dos direcciones a la vez (vela gigante), no hay bandera (ambigua, se declara).

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

from ..schema.features import Candle1h, DailyTrend, HtfContext, HtfLevels, HtfSetups, TrendAlignment

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
# htf-levels-v1 (fijos; cambiarlos = -v2 de las estrategias de lateralidad)
ASIA_END_H = 8
ADX_PERIOD = 14
FALSE_BREAK_ATR = 0.5
PREV_TOUCH_ATR = 0.25
WICK_MIN_FRAC = 0.5
VWAP_DIST_ATR = 2.0
ADX_MAX = 20.0


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


def wilder_adx(candles: Sequence[Candle1h], period: int = ADX_PERIOD) -> float | None:
    """ADX de Wilder: TR/+DM/-DM suavizados (suma inicial de `period`), DX, ADX = media de los primeros `period` DX
    y despues suavizado de Wilder. Necesita 2*period + 1 velas; None si no alcanzan."""
    if len(candles) < 2 * period + 1:
        return None
    trs, pdm, mdm = [], [], []
    for p, c in zip(candles, candles[1:]):
        up, down = c.high - p.high, p.low - c.low
        pdm.append(up if up > down and up > 0 else 0.0)
        mdm.append(down if down > up and down > 0 else 0.0)
        trs.append(max(c.high - c.low, abs(c.high - p.close), abs(c.low - p.close)))
    tr_s, p_s, m_s = sum(trs[:period]), sum(pdm[:period]), sum(mdm[:period])
    dxs: list[float] = []

    def _dx() -> float:
        if tr_s <= 0:
            return 0.0
        pdi, mdi = 100 * p_s / tr_s, 100 * m_s / tr_s
        return 0.0 if pdi + mdi == 0 else 100 * abs(pdi - mdi) / (pdi + mdi)

    dxs.append(_dx())
    for i in range(period, len(trs)):
        tr_s = tr_s - tr_s / period + trs[i]
        p_s = p_s - p_s / period + pdm[i]
        m_s = m_s - m_s / period + mdm[i]
        dxs.append(_dx())
    adx = sum(dxs[:period]) / period
    for dx in dxs[period:]:
        adx = (adx * (period - 1) + dx) / period
    return adx


def _one_side(short: bool, long: bool, name: str, gaps: list[str]) -> str | None:
    if short and long:
        gaps.append(f"{name}: se cumplen LONG y SHORT a la vez en la misma vela (ambigua, sin bandera)")
        return None
    return "SHORT" if short else "LONG" if long else None


def build_levels(cs: Sequence[Candle1h], today: Sequence[Candle1h], day_start: int, prev: dict | None,
                 atr: float | None, trend: str) -> HtfLevels:
    """Niveles `htf-levels-v1` desde velas 1h CERRADAS (ya filtradas por el caller). Puro."""
    lv = HtfLevels()
    miss = lv.sin_datos
    last = today[-1] if today else None
    # rango de Asia
    asia = [c for c in today if c.open_ms < day_start + ASIA_END_H * HOUR_MS]
    lv.asia_hours = len(asia)
    if len(asia) == ASIA_END_H:
        lv.asia_high, lv.asia_low = max(c.high for c in asia), min(c.low for c in asia)
    elif last is not None and last.open_ms < day_start + (ASIA_END_H - 1) * HOUR_MS:
        miss.append(f"rango de Asia: en curso ({len(asia)}/{ASIA_END_H} velas cerradas)")
    else:
        miss.append(f"rango de Asia: faltan velas de 00-08 UTC ({len(asia)}/{ASIA_END_H})")
    # extremos de ayer
    if prev is not None:
        lv.prev_day_high, lv.prev_day_low = prev["high"], prev["low"]
    else:
        miss.append("extremos de ayer: el dia UTC anterior no tiene sus 24 velas de 1h")
    # VWAP del dia (velas 1m)
    if not today:
        miss.append("VWAP: todavia no cerro ninguna vela de 1h del dia")
    else:
        hours = {c.open_ms for c in today}
        holes = [h for h in range(day_start, today[-1].open_ms + HOUR_MS, HOUR_MS) if h not in hours]
        no_1m = [c for c in today if c.vwap_pv is None or c.vwap_vol is None]
        vol = sum(c.vwap_vol or 0.0 for c in today)
        if holes:
            miss.append(f"VWAP: faltan {len(holes)} hora(s) del dia")
        elif no_1m:
            miss.append(f"VWAP: {len(no_1m)} hora(s) del dia sin velas 1m en la base (solo REST 1h)")
        elif vol <= 0:
            miss.append("VWAP: volumen 0 en las velas 1m del dia")
        else:
            lv.vwap = sum(c.vwap_pv or 0.0 for c in today) / vol
            lv.vwap_minutes = sum(c.minutes for c in today)
    lv.adx14_1h = wilder_adx(cs)
    if lv.adx14_1h is None:
        miss.append(f"ADX14 1h: menos de {2 * ADX_PERIOD + 1} velas de 1h")
    if atr is None:
        miss.append("ATR14 1h: sin datos")
    if last is None:
        miss.append("ultima vela 1h del dia: ninguna cerrada")
        return lv
    rng = last.high - last.low
    lv.last_range = rng
    lv.last_upper_wick = last.high - max(last.open, last.close)
    lv.last_lower_wick = min(last.open, last.close) - last.low
    if rng > 0:
        lv.upper_wick_frac = lv.last_upper_wick / rng
        lv.lower_wick_frac = lv.last_lower_wick / rng
    if atr and lv.vwap is not None:
        lv.dist_vwap_atr = (last.close - lv.vwap) / atr
    if trend != "lateral" or not atr:
        return lv
    # banderas (solo dia lateral)
    if lv.asia_high is not None and lv.asia_low is not None and last.open_ms >= day_start + ASIA_END_H * HOUR_MS:
        lv.falsa_ruptura_asia = _one_side(  # type: ignore[assignment]
            last.high > lv.asia_high + FALSE_BREAK_ATR * atr and last.close < lv.asia_high,
            last.low < lv.asia_low - FALSE_BREAK_ATR * atr and last.close > lv.asia_low, "falsa_ruptura_asia", miss)
    if lv.prev_day_high is not None and lv.prev_day_low is not None and rng > 0:
        lv.rechazo_dia_anterior = _one_side(  # type: ignore[assignment]
            last.high >= lv.prev_day_high - PREV_TOUCH_ATR * atr and lv.last_upper_wick >= WICK_MIN_FRAC * rng
            and last.close < lv.prev_day_high,
            last.low <= lv.prev_day_low + PREV_TOUCH_ATR * atr and lv.last_lower_wick >= WICK_MIN_FRAC * rng
            and last.close > lv.prev_day_low, "rechazo_dia_anterior", miss)
    if lv.vwap is not None and lv.adx14_1h is not None:
        if abs(last.close - lv.vwap) >= VWAP_DIST_ATR * atr and lv.adx14_1h < ADX_MAX:
            lv.extremo_vwap = "SHORT" if last.close > lv.vwap else "LONG"
    return lv


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
    levels = build_levels(cs, today, day_start, prev, atr, trend)
    data_gaps.extend(f"htf-levels: {m}" for m in levels.sin_datos  # lo "en curso" no es un hueco de datos
                     if "en curso" not in m and "todavia no cerro" not in m and "ambigua" not in m)
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
        daily_trend=trend, trend_reason=reason, setups=setups, levels=levels, sources=sources, data_gaps=data_gaps,
    )


def missing_context(symbol: str, now_ms: int, reason: str) -> HtfContext:
    """Contexto vacio y declarado (fallo de la fuente): nunca se omite en silencio."""
    return HtfContext(symbol=symbol, ts_ms=now_ms, status="missing", day_start_ms=now_ms - now_ms % DAY_MS,
                      trend_reason=reason, data_gaps=[f"htf: {reason}"])
