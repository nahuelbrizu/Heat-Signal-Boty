"""
Etapa 2 del meta-labeling (plan-entrenamiento.md): features de cada evento de `events_v1.parquet`.

    python -m heatsignal.lab.meta.features build --grupos basicos [--out ~/heatsignal-lab/meta]

Version `features-meta-v1`. Cada grupo escribe SU parquet (`features_<grupo>_v1.parquet`, clave `evento_id`) y su
resumen JSON; `load_all()` une los eventos con todos los parquet de features que existan. La subtarea 2b agrego
`derivados_mercado`; la 2c (macro/lateralidad) agrega su entrada a `GRUPOS` (archivo, lista de features y funcion
`build(events, loader) -> DataFrame` indexado como `events`) sin tocar lo de este grupo.

Disponibilidad (`lab/clock.py`): toda feature usa SOLO datos conocidos en o antes de `t_decision`.
  - Velas 1h y 1D armadas desde 1m con `resample_bars` (fechadas al CIERRE, solo bloques completos). La vela que
    cierra justo en `t_decision` SI se conoce; la que abre en `t_decision` no.
  - Union con `asof` (ultima vela cerrada <= t_decision) con tolerancia: 1h para la serie de 1h y 1 dia para la
    diaria. Si falta la vela (hueco de datos mas viejo que eso) la feature queda NaN, no se arrastra.
  - Todo rolling es trailing; los percentiles de 1 anio son el rango de la vela actual dentro de las ultimas N
    (incluida ella, nunca posteriores).

Decisiones donde el plan no alcanzaba (grupo `basicos`):
  - `evento_id` = "primaria|symbol|t_decision ISO UTC" (unico en events_v1; estable entre corridas).
  - EMA = `ewm(span=n, adjust=False)` con `min_periods=n`; ATR/ADX/DMI/RSI de Wilder (`lab/features.py`).
    Indicadores sobre la secuencia de velas (un hueco se saltea, como `features/htf.py`); retornos, vol realizada
    y ROC sobre la grilla regular de tiempo (un hueco da NaN, no un retorno mas largo).
  - Pendiente EMA200 1h = (EMA200_t - EMA200_{t-24 velas}) / ATR14 1h.
  - Percentil 1 anio: 1h = 8760 velas (minimo 4380), diario = 365 velas (minimo 180); `rank(pct, method=max)`.
  - Vol realizada 1/7/30 d = desvio de los retornos log de 1h en 24/168/720 horas (minimo 90 % de la ventana),
    expresado en unidades diarias (x sqrt(24)).
  - Velas: body_pct = |C-O|/(H-L), close_pos = (C-L)/(H-L) (rango 0 -> 0 y 0.5), mechas / ATR14 del mismo marco.
  - `daily_trend` = `features/htf.build_htf_context` (htf-trend-v1, se importa: no se reimplementa) con las velas
    de 1h completas de la ventana del vivo (`history/hourly.py`: 35 dias, primer dia UTC entero).
  - Sesion = la ultima sesion de `features/sessions.py` (DEFAULT_SESSIONS_SPEC, DST con zoneinfo) cuya APERTURA es
    <= t_decision; `cierre` (23:00 UTC) -> "fuera". Dia de la semana 0 = lunes.
  - De la primaria: `lado`, `tamaño`, `tipo` tal cual estan en el evento.
  - `daily_trend_dia_anterior` (aprobada antes de entrenar, decisions.md) = htf-trend-v1 sobre el dia UTC COMPLETO
    anterior al de `t_decision`, evaluada al cierre de ese dia (now = 00:00 UTC de hoy, `day_start_ms` = ayer, ventana
    del vivo en ese instante). No usa ninguna vela de hoy.

Decisiones del grupo `derivados_mercado` (subtarea 2b, `features_derivados_v1.parquet`):
  - Fuentes (`Fuentes`, por defecto `lab/store.py` hasta el ultimo inicio de holdout); si un dataset no existe para
    el simbolo, sus columnas quedan NaN. Reloj: metrics disponible en ts + 5 min (`available_metrics`), tolerancia
    15 min (como la ingesta: > 15 min = hueco); funding = ultima liquidacion <= t_decision (la de t mismo cuenta),
    tolerancia 9 h; premium index 1m disponible al cierre.
  - funding: tasa liquidada tal cual (sin normalizar el intervalo; 99,8 % son de 8 h); z-score contra las ultimas
    90 liquidaciones incluida la actual (minimo 60, desvio muestral).
  - ΔOI 1h/4h/24h = OI en CONTRATOS disponible en t / el disponible en t - k - 1 (asof en ambos extremos). Snapshots
    con OI o long/short <= 0 o vacios se descartan (basura del proveedor); vale el ultimo valido dentro de 15 min.
    `oi_rel_media_30d` = OI / media trailing de 30 dias de los snapshots (minimo 50 % de los 8640).
  - `cuadrante_oi_funding` = "oi_sube|oi_baja" x "funding_pos|funding_neg" con ΔOI 24h y el funding actual (0 cuenta
    como baja/neg); NaN si falta alguno.
  - `taker_buy_sell_1h` = volumen taker comprador / vendedor de la ultima vela de 1h cerrada, desde klines (exacto;
    el `taker_ratio` de metrics es por periodo de 5 min y promediar ratios sesga).
  - `ls_top_cuentas` = `count_toptrader_long_short_ratio` (cuentas de top traders) del ultimo snapshot (Binance no
    lo publica en ~87 % de 2022: NaN ahi).
  - `premium_1h` = media del premium index 1m de la ultima hora cerrada (60 velas completas).
  - Mercado: BTC `dist_ema50_1h_atr` y `vol_real_7d` con las mismas formulas que `basicos`; `btc_daily_trend` =
    htf-trend-v1 de BTC en t (misma regla que `daily_trend`). `corr_btc_30d` = correlacion de 720 retornos log de
    1h sobre la grilla regular (minimo 90 % de pares); para BTC vale 1 (no NaN: NaN queda reservado a "falta dato").
    `n_sobre_ema200_1d` = monedas de `strategies.SEVEN` con el cierre diario > EMA200 diaria (velas diarias cerradas);
    `ret_7d_canasta` = media igual-ponderada de close(t)/close(t-168h) - 1 con velas de 1h. Si una de las 7 no tiene
    dato, las dos quedan NaN (no se cuenta sobre menos monedas).
NO es un intento: no escribe en `results/` ni en `trials.sqlite`, no entrena nada.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import sys
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from ...features.htf import DAY_MS, HOUR_MS, build_htf_context
from ...features.sessions import opens_for_utc_day, parse_sessions
from ...history.hourly import DEFAULT_LOOKBACK_H
from ...schema.features import Candle1h
from .. import features as F
from ..clock import asof, available_metrics, resample_bars
from ..strategies import SEVEN
from . import dataset as D

log = logging.getLogger("heatsignal.lab.meta.features")

VERSION = "features-meta-v1"
KEY = "evento_id"
ID_COLS = ["primaria", "symbol", "t_decision"]
DEFAULT_DIR = Path.home() / "heatsignal-lab" / "meta"
EVENTS_FILE = f"{D.VERSION}.parquet"
# columnas del evento que son RESULTADO (etiqueta o posteriores a la decision): nunca features
OUTCOME_COLS = frozenset({"t_entrada", "precio_entrada", "t_fin_regla", "nivel_sl", "nivel_tp", "t_limite", "barrera",
                          "t_decision_salida", "t_salida", "precio_salida", "pnl_bruto", "costos", "funding", "pnl_neto",
                          "etiqueta", "mae", "mfe", "ventana_completa", "peso_unicidad"})

H1 = pd.Timedelta(hours=1)
D1 = pd.Timedelta(days=1)
PCT_1Y_H, PCT_1Y_H_MIN = 365 * 24, 365 * 12
PCT_1Y_D, PCT_1Y_D_MIN = 365, 180
RV_WINDOWS = {"1d": 24, "7d": 24 * 7, "30d": 24 * 30}
RV_MIN_FRAC = 0.9
RET_BARS = (1, 4, 24, 72)
EMAS = (20, 50, 200)
SLOPE_BARS = 24
ROC_DAYS = 10

_TEND_1H = [f"dist_ema{n}_1h_atr" for n in EMAS] + ["pend_ema200_1h_atr", "adx_1h", "di_dif_1h"]
_TEND_1D = [f"dist_ema{n}_1d_atr" for n in EMAS] + ["adx_1d", "di_dif_1d"]
_VOL_1H = ["atr_pct_1h", "atr_pct_1h_pctl_1a", "bb_ancho_1h", "bb_ancho_1h_pctl_1a"] + [f"vol_real_{k}" for k in RV_WINDOWS]
_VOL_1D = ["atr_pct_1d", "atr_pct_1d_pctl_1a"]
_MOM_1H = [f"ret_log_{k}h" for k in RET_BARS] + ["rsi_1h"]
_MOM_1D = ["rsi_1d", "roc_10d"]
_VELA = ["body_pct", "close_pos", "mecha_sup_atr", "mecha_inf_atr"]
COLS_1H = _TEND_1H + _VOL_1H + _MOM_1H + [f"{c}_1h" for c in _VELA]
COLS_1D = _TEND_1D + _VOL_1D + _MOM_1D + [f"{c}_1d" for c in _VELA]
FEATURES_BASICAS: list[str] = (COLS_1H + COLS_1D + ["daily_trend", "daily_trend_dia_anterior", "hora_utc", "sesion",
                                                    "dia_semana", "lado", "tamaño", "tipo"])
CATEGORICAS_BASICAS = ["daily_trend", "daily_trend_dia_anterior", "sesion", "tipo"]

Loader = Callable[[str], pd.DataFrame]  # symbol -> velas 1m (indice = apertura, como store.load_klines)


# ---- clave ------------------------------------------------------------------------------------------------


def event_ids(events: pd.DataFrame) -> pd.Series:
    t = pd.to_datetime(events["t_decision"], utc=True).dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    return events["primaria"].astype(str) + "|" + events["symbol"].astype(str) + "|" + t


# ---- indicadores por serie (puros, trailing) ----------------------------------------------------------------


def pct_rank_trailing(x: pd.Series, window: int, min_periods: int) -> pd.Series:
    """Fraccion de las ultimas `window` observaciones (incluida la actual) que son <= la actual. Solo hacia atras."""
    return x.rolling(window, min_periods=min_periods).rank(pct=True, method="max")


def candle_shape(bars: pd.DataFrame, atr_s: pd.Series, suffix: str) -> pd.DataFrame:
    o, h, l, c = (bars[k] for k in ("open", "high", "low", "close"))
    rng = h - l
    pos = rng > 0
    return pd.DataFrame({
        f"body_pct_{suffix}": ((c - o).abs() / rng).where(pos, 0.0),
        f"close_pos_{suffix}": ((c - l) / rng).where(pos, 0.5),
        f"mecha_sup_atr_{suffix}": (h - np.maximum(o, c)) / atr_s,
        f"mecha_inf_atr_{suffix}": (np.minimum(o, c) - l) / atr_s,
    }, index=bars.index)


def _trend_block(bars: pd.DataFrame, a: pd.Series, suffix: str) -> tuple[pd.DataFrame, pd.Series]:
    c = bars["close"]
    out = pd.DataFrame(index=bars.index)
    emas = {}
    for n in EMAS:
        emas[n] = c.ewm(span=n, adjust=False, min_periods=n).mean()
        out[f"dist_ema{n}_{suffix}_atr"] = (c - emas[n]) / a
    dm = F.dmi(bars, 14)
    out[f"adx_{suffix}"] = dm["adx"]
    out[f"di_dif_{suffix}"] = dm["pdi"] - dm["mdi"]
    return out, emas[200]


def _on_grid(close: pd.Series, freq: pd.Timedelta) -> pd.Series:
    if close.empty:
        return close
    grid = pd.date_range(close.index[0], close.index[-1], freq=freq)
    return close.reindex(grid)


def hourly_features(h: pd.DataFrame) -> pd.DataFrame:
    """Features de 1h indexadas por el CIERRE de cada vela de 1h completa."""
    c = h["close"]
    a = F.atr(h, 14)
    out, ema200 = _trend_block(h, a, "1h")
    out["pend_ema200_1h_atr"] = (ema200 - ema200.shift(SLOPE_BARS)) / a
    out["atr_pct_1h"] = a / c
    out["atr_pct_1h_pctl_1a"] = pct_rank_trailing(out["atr_pct_1h"], PCT_1Y_H, PCT_1Y_H_MIN)
    out["bb_ancho_1h"] = F.bb_width(c, 20, 2.0)
    out["bb_ancho_1h_pctl_1a"] = pct_rank_trailing(out["bb_ancho_1h"], PCT_1Y_H, PCT_1Y_H_MIN)
    g = _on_grid(c, H1)
    lr = np.log(g).diff()
    for k, w in RV_WINDOWS.items():
        out[f"vol_real_{k}"] = (lr.rolling(w, min_periods=int(np.ceil(w * RV_MIN_FRAC))).std() * np.sqrt(24)).reindex(h.index)
    for k in RET_BARS:
        out[f"ret_log_{k}h"] = np.log(g / g.shift(k)).reindex(h.index)
    out["rsi_1h"] = F.rsi(c, 14)
    out = out.join(candle_shape(h, a, "1h"))
    return out[COLS_1H]


def daily_features(d: pd.DataFrame) -> pd.DataFrame:
    """Features diarias indexadas por el CIERRE del dia UTC (00:00 del dia siguiente)."""
    c = d["close"]
    a = F.atr(d, 14)
    out, _ = _trend_block(d, a, "1d")
    out["atr_pct_1d"] = a / c
    out["atr_pct_1d_pctl_1a"] = pct_rank_trailing(out["atr_pct_1d"], PCT_1Y_D, PCT_1Y_D_MIN)
    out["rsi_1d"] = F.rsi(c, 14)
    g = _on_grid(c, D1)
    out["roc_10d"] = (g / g.shift(ROC_DAYS) - 1).reindex(d.index)
    out = out.join(candle_shape(d, a, "1d"))
    return out[COLS_1D]


# ---- htf-trend-v1 y contexto ------------------------------------------------------------------------------------


def hourly_candles(h: pd.DataFrame) -> tuple[np.ndarray, list[Candle1h]]:
    """Velas de 1h completas como `Candle1h` (open_ms = cierre - 1h), igual que `history/hourly.aggregate_minutes`."""
    open_ms = ((h.index - H1).as_unit("ms").asi8).astype(np.int64)
    cs = [Candle1h.model_construct(open_ms=int(t), open=float(o), high=float(hi), low=float(lo), close=float(cl),
                                   source="klines", minutes=60, vwap_pv=None, vwap_vol=None)
          for t, o, hi, lo, cl in zip(open_ms, h["open"].to_numpy(), h["high"].to_numpy(), h["low"].to_numpy(),
                                      h["close"].to_numpy())]
    return open_ms, cs


def htf_window(now_ms: int, lookback_h: int = DEFAULT_LOOKBACK_H) -> tuple[int, int]:
    """[first, last] (open_ms) de las velas que pide `HourlyHistory.candles` en `now_ms`."""
    last = now_ms - now_ms % HOUR_MS - HOUR_MS
    first = last - (lookback_h - 1) * HOUR_MS
    return first - first % DAY_MS, last


def daily_trend_at(symbol: str, open_ms: np.ndarray, cs: list[Candle1h], times: Iterable[pd.Timestamp]) -> list[str]:
    out = []
    for t in times:
        now = int(pd.Timestamp(t).value // 1_000_000)
        first, last = htf_window(now)
        i0, i1 = np.searchsorted(open_ms, first, "left"), np.searchsorted(open_ms, last, "right")
        out.append(build_htf_context(symbol, cs[i0:i1], now).daily_trend)
    return out


def daily_trend_prev_day_at(symbol: str, open_ms: np.ndarray, cs: list[Candle1h],
                            times: Iterable[pd.Timestamp]) -> list[str]:
    """htf-trend-v1 sobre el dia UTC COMPLETO anterior al de cada `t`, evaluada al cierre de ese dia (00:00 UTC del
    dia de `t`, con la ventana del vivo en ese instante y `day_start_ms` = el dia anterior). No mira nada de hoy."""
    cache: dict[int, str] = {}
    out = []
    for t in times:
        ms = int(pd.Timestamp(t).value // 1_000_000)
        now = ms - ms % DAY_MS
        if now not in cache:
            first, last = htf_window(now)
            i0, i1 = np.searchsorted(open_ms, first, "left"), np.searchsorted(open_ms, last, "right")
            cache[now] = build_htf_context(symbol, cs[i0:i1], now, day_start_ms=now - DAY_MS).daily_trend
        out.append(cache[now])
    return out


_SESSIONS = parse_sessions()


def session_name(t: pd.Timestamp) -> str:
    ms = int(pd.Timestamp(t).value // 1_000_000)
    day = ms - ms % DAY_MS
    opens = opens_for_utc_day(_SESSIONS, day - DAY_MS) + opens_for_utc_day(_SESSIONS, day)
    past = [d for d, o in opens if o <= ms]
    if not past:
        return "fuera"
    name = past[-1].name
    return "fuera" if past[-1].role == "close" else name


# ---- grupo basicos ----------------------------------------------------------------------------------------------


def symbol_basicos(symbol: str, klines: pd.DataFrame, times: pd.DatetimeIndex) -> pd.DataFrame:
    """Features de mercado (1h, 1D, htf) de un simbolo en cada instante de `times` (sin duplicados importa poco)."""
    k = klines[["open", "high", "low", "close", "volume"]]
    h = resample_bars(k, "1h")
    d = resample_bars(k, "1D")
    fh = asof(times, hourly_features(h), tolerance=H1)
    fd = asof(times, daily_features(d), tolerance=D1)
    open_ms, cs = hourly_candles(h)
    out = pd.concat([fh, fd], axis=1)
    out["daily_trend"] = daily_trend_at(symbol, open_ms, cs, times)
    out["daily_trend_dia_anterior"] = daily_trend_prev_day_at(symbol, open_ms, cs, times)
    return out


def _check_holdout(ev: pd.DataFrame, t_dec: pd.Series) -> None:
    for p, g in ev.groupby("primaria"):  # el holdout de cada primaria ni se mira
        hs = D.HOLDOUT_START.get(p)
        if hs is not None and (t_dec[g.index] >= hs).any():
            raise RuntimeError(f"{p}: hay eventos en el holdout")


def build_basicos(events: pd.DataFrame, loader: Loader) -> pd.DataFrame:
    ev = events.reset_index(drop=True)
    t_dec = pd.to_datetime(ev["t_decision"], utc=True)
    _check_holdout(ev, t_dec)
    out = pd.DataFrame({KEY: event_ids(ev), "primaria": ev["primaria"], "symbol": ev["symbol"], "t_decision": t_dec})
    mkt = pd.DataFrame(index=ev.index, columns=COLS_1H + COLS_1D + ["daily_trend", "daily_trend_dia_anterior"],
                       dtype=object)
    parts = []
    for sym, g in ev.groupby("symbol"):
        log.info("%s: %d eventos", sym, len(g))
        times = pd.DatetimeIndex(sorted(set(t_dec[g.index])))
        f = symbol_basicos(sym, loader(sym), times)
        parts.append(f.reindex(pd.DatetimeIndex(t_dec[g.index])).set_axis(g.index))
    if parts:
        mkt = pd.concat(parts).reindex(ev.index)
    out = pd.concat([out, mkt], axis=1)
    out["hora_utc"] = t_dec.dt.hour.astype("int64")
    out["dia_semana"] = t_dec.dt.dayofweek.astype("int64")
    cache: dict[pd.Timestamp, str] = {}
    out["sesion"] = [cache.setdefault(t, session_name(t)) for t in t_dec]
    out["lado"] = ev["lado"].astype("int64")
    out["tamaño"] = ev["tamaño"].astype(float)
    out["tipo"] = ev["tipo"].astype(str)
    num = [c for c in COLS_1H + COLS_1D]
    out[num] = out[num].astype(float)
    out["daily_trend"] = out["daily_trend"].astype(object)
    out["daily_trend_dia_anterior"] = out["daily_trend_dia_anterior"].astype(object)
    return out[[KEY, *ID_COLS, *FEATURES_BASICAS]]


# ---- grupo derivados_mercado (subtarea 2b) ----------------------------------------------------------------------

BTC = "BTCUSDT"
UNIVERSO: tuple[str, ...] = tuple(SEVEN)
METRICS_TOL = pd.Timedelta(minutes=15)  # snapshot disponible hace mas de 15 min -> NaN (la ingesta: > 15 min = hueco)
FUNDING_TOL = pd.Timedelta(hours=9)  # liquidacion cada 8 h (+1 h de margen)
FUNDING_Z_N, FUNDING_Z_MIN = 90, 60  # 30 dias de liquidaciones de 8 h
OI_DELTAS = {"1h": H1, "4h": 4 * H1, "24h": 24 * H1}
OI_MEAN_WINDOW, OI_MEAN_MIN = pd.Timedelta(days=30), 30 * 288 // 2  # >= 50 % de los snapshots de 5 min
CORR_BARS, CORR_MIN = 24 * 30, int(24 * 30 * RV_MIN_FRAC)
RET_CANASTA = pd.Timedelta(days=7)
EMA_REGIMEN = 200

_DERIV = (["funding_actual", "funding_z_30d"] + [f"delta_oi_{k}" for k in OI_DELTAS]
          + ["oi_rel_media_30d", "cuadrante_oi_funding", "taker_buy_sell_1h", "ls_top_cuentas", "premium_1h"])
_MERC = ["btc_dist_ema50_1h_atr", "btc_vol_real_7d", "btc_daily_trend", "corr_btc_30d", "n_sobre_ema200_1d",
         "ret_7d_canasta"]
FEATURES_DERIVADOS: list[str] = _DERIV + _MERC
CATEGORICAS_DERIVADOS = ["cuadrante_oi_funding", "btc_daily_trend"]


@dataclass(frozen=True)
class Fuentes:
    """Datos de derivados por simbolo, con los indices de `lab/store.py` (el reloj se aplica aca, no en el loader)."""
    metrics: Callable[[str], pd.DataFrame]  # indice = ts del snapshot de 5 min
    funding: Callable[[str], pd.Series]  # indice = liquidacion
    premium: Callable[[str], pd.Series]  # premium index 1m, indice = apertura
    universo: tuple[str, ...] = UNIVERSO


def default_fuentes() -> Fuentes:
    from .. import store as S

    end = max(D.HOLDOUT_START.values())
    return Fuentes(lambda s: S.load_metrics(s, None, end), lambda s: S.load_funding(s, None, end),
                   lambda s: S.load_premium(s, None, end))


def _try_load(fn: Callable[[str], Any], sym: str, what: str) -> Any:
    try:
        return fn(sym)
    except FileNotFoundError:
        log.warning("%s: sin %s (columnas NaN)", sym, what)
        return None


def funding_features(rates: pd.Series | None, times: pd.DatetimeIndex) -> pd.DataFrame:
    """Ultima tasa LIQUIDADA en o antes de t (la de t mismo cuenta) y su z-score contra las ultimas 90 liquidaciones
    (incluida ella)."""
    cols = ["funding_actual", "funding_z_30d"]
    if rates is None or rates.empty:
        return pd.DataFrame(np.nan, index=times, columns=cols)
    r = rates.sort_index().astype(float)
    z = F.zscore(r, FUNDING_Z_N, FUNDING_Z_MIN).replace([np.inf, -np.inf], np.nan)
    return asof(times, pd.DataFrame({cols[0]: r, cols[1]: z}), tolerance=FUNDING_TOL)


def metrics_features(metrics: pd.DataFrame | None, times: pd.DatetimeIndex) -> pd.DataFrame:
    """OI en CONTRATOS y long/short de top traders, con metrics disponible en ts + 5 min (`available_metrics`)."""
    cols = [f"delta_oi_{k}" for k in OI_DELTAS] + ["oi_rel_media_30d", "ls_top_cuentas"]
    if metrics is None or metrics.empty:
        return pd.DataFrame(np.nan, index=times, columns=cols)
    m = available_metrics(metrics[["oi_contracts", "ls_top_accounts"]].astype(float)).sort_index()
    # snapshots con valor <= 0 o vacio son basura del proveedor (OI = 0 en ~0,1 %): se descartan por columna, asi el
    # asof toma el ultimo snapshot VALIDO dentro de la tolerancia
    oi = m["oi_contracts"].where(m["oi_contracts"] > 0).dropna()
    ls = m["ls_top_accounts"].where(m["ls_top_accounts"] > 0).dropna()
    oi_now = asof(times, oi, tolerance=METRICS_TOL).to_numpy()
    out = pd.DataFrame(index=times)
    for k, dt in OI_DELTAS.items():
        out[f"delta_oi_{k}"] = oi_now / asof(times - dt, oi, tolerance=METRICS_TOL).to_numpy() - 1
    mean = oi.rolling(OI_MEAN_WINDOW, min_periods=OI_MEAN_MIN).mean().rename("m")
    out["oi_rel_media_30d"] = oi_now / asof(times, mean, tolerance=METRICS_TOL).to_numpy()
    out["ls_top_cuentas"] = asof(times, ls, tolerance=METRICS_TOL).to_numpy()
    return out[cols]


def cuadrante(delta_oi: pd.Series, funding: pd.Series) -> pd.Series:
    """signo(ΔOI 24h) x signo(funding): 'oi_sube|oi_baja' + '_' + 'funding_pos|funding_neg' (0 cuenta como baja/neg);
    NaN si falta alguno."""
    vals = [None if (pd.isna(o) or pd.isna(f)) else f"{'oi_sube' if o > 0 else 'oi_baja'}_"
            f"{'funding_pos' if f > 0 else 'funding_neg'}" for o, f in zip(delta_oi, funding)]
    return pd.Series(vals, index=delta_oi.index, dtype=object)


def premium_features(premium: pd.Series | None, times: pd.DatetimeIndex) -> pd.Series:
    """Media del premium index de la ultima hora cerrada (60 velas 1m completas), fechada al cierre de la hora."""
    if premium is None or premium.empty:
        return pd.Series(np.nan, index=times, name="premium_1h")
    g = premium.astype(float).resample("1h", label="right", closed="left")
    ph = g.mean()[g.count() == 60].rename("premium_1h")
    return asof(times, ph, tolerance=H1)


def taker_ratio_1h(h: pd.DataFrame) -> pd.Series:
    """Volumen taker comprador / vendedor de la vela de 1h (desde klines: exacto, no promedio de ratios de 5 min)."""
    if "taker_buy_volume" not in h.columns:
        return pd.Series(np.nan, index=h.index, name="taker_buy_sell_1h")
    sell = h["volume"] - h["taker_buy_volume"]
    return (h["taker_buy_volume"] / sell.where(sell > 0)).rename("taker_buy_sell_1h")


def corr_with_btc(close: pd.Series, btc_close: pd.Series) -> pd.Series:
    """Correlacion trailing de 720 retornos log de 1h (grilla regular; >= 90 % de pares validos)."""
    a, b = np.log(_on_grid(close, H1)).diff(), np.log(_on_grid(btc_close, H1)).diff()
    df = pd.concat([a.rename("a"), b.rename("b")], axis=1, sort=True)
    if df.empty:
        return pd.Series(dtype=float, name="corr")
    df = df.reindex(pd.date_range(df.index[0], df.index[-1], freq=H1))
    return df["a"].rolling(CORR_BARS, min_periods=CORR_MIN).corr(df["b"]).rename("corr")


def market_context(hours: dict[str, pd.DataFrame], days: dict[str, pd.DataFrame], times: pd.DatetimeIndex,
                   universo: Iterable[str]) -> pd.DataFrame:
    """Features comunes a todas las monedas en cada instante (BTC, amplitud y canasta del universo)."""
    out = pd.DataFrame(index=times)
    hb = hours[BTC]
    fb = asof(times, hourly_features(hb)[["dist_ema50_1h_atr", "vol_real_7d"]], tolerance=H1)
    out["btc_dist_ema50_1h_atr"] = fb["dist_ema50_1h_atr"]
    out["btc_vol_real_7d"] = fb["vol_real_7d"]
    open_ms, cs = hourly_candles(hb)
    out["btc_daily_trend"] = daily_trend_at(BTC, open_ms, cs, times)
    above, rets = {}, {}
    for s in universo:
        c = days[s]["close"]
        ema = c.ewm(span=EMA_REGIMEN, adjust=False, min_periods=EMA_REGIMEN).mean()
        above[s] = asof(times, (c > ema).astype(float).where(ema.notna()).rename("x"), tolerance=D1).to_numpy()
        ch = hours[s]["close"].rename("c")
        rets[s] = (asof(times, ch, tolerance=H1).to_numpy()
                   / asof(times - RET_CANASTA, ch, tolerance=H1).to_numpy() - 1)
    out["n_sobre_ema200_1d"] = pd.DataFrame(above, index=times).sum(axis=1, skipna=False)  # NaN si falta una moneda
    out["ret_7d_canasta"] = pd.DataFrame(rets, index=times).mean(axis=1, skipna=False)
    return out


def symbol_derivados(symbol: str, h: pd.DataFrame, h_btc: pd.DataFrame, times: pd.DatetimeIndex,
                     metrics: pd.DataFrame | None, funding: pd.Series | None,
                     premium: pd.Series | None) -> pd.DataFrame:
    out = pd.concat([funding_features(funding, times), metrics_features(metrics, times)], axis=1)
    out["cuadrante_oi_funding"] = cuadrante(out["delta_oi_24h"], out["funding_actual"])
    out["taker_buy_sell_1h"] = asof(times, taker_ratio_1h(h), tolerance=H1)
    out["premium_1h"] = premium_features(premium, times)
    out["corr_btc_30d"] = 1.0 if symbol == BTC else asof(times, corr_with_btc(h["close"], h_btc["close"]),
                                                          tolerance=H1)
    return out


def build_derivados(events: pd.DataFrame, loader: Loader, fuentes: Fuentes | None = None) -> pd.DataFrame:
    fu = fuentes or default_fuentes()
    ev = events.reset_index(drop=True)
    t_dec = pd.to_datetime(ev["t_decision"], utc=True)
    _check_holdout(ev, t_dec)
    out = pd.DataFrame({KEY: event_ids(ev), "primaria": ev["primaria"], "symbol": ev["symbol"], "t_decision": t_dec})
    syms = list(dict.fromkeys([BTC, *fu.universo, *sorted(ev["symbol"].unique())]))
    hours, days = {}, {}
    for s in syms:  # 1m -> 1h y 1D una sola vez por simbolo (las 1m no se guardan: RAM)
        k = loader(s)
        k = k[[c for c in ("open", "high", "low", "close", "volume", "taker_buy_volume") if c in k.columns]]
        hours[s], days[s] = resample_bars(k, "1h"), resample_bars(k, "1D")
        del k
    all_times = pd.DatetimeIndex(sorted(set(t_dec)))
    mkt = market_context(hours, days, all_times, fu.universo)
    parts = []
    for sym, g in ev.groupby("symbol"):
        log.info("%s: %d eventos (derivados)", sym, len(g))
        times = pd.DatetimeIndex(sorted(set(t_dec[g.index])))
        f = symbol_derivados(sym, hours[sym], hours[BTC], times, _try_load(fu.metrics, sym, "metrics"),
                             _try_load(fu.funding, sym, "funding"), _try_load(fu.premium, sym, "premium"))
        parts.append(f.reindex(pd.DatetimeIndex(t_dec[g.index])).set_axis(g.index))
    der = pd.concat(parts).reindex(ev.index)
    out = pd.concat([out, der, mkt.reindex(pd.DatetimeIndex(t_dec)).set_axis(ev.index)], axis=1)
    num = [c for c in FEATURES_DERIVADOS if c not in CATEGORICAS_DERIVADOS]
    out[num] = out[num].astype(float)
    for c in CATEGORICAS_DERIVADOS:
        out[c] = out[c].astype(object)
    return out[[KEY, *ID_COLS, *FEATURES_DERIVADOS]]


# ---- registro de grupos, resumen y union ------------------------------------------------------------------------


@dataclass(frozen=True)
class Grupo:
    archivo: str
    features: list[str]
    build: Callable[[pd.DataFrame, Loader], pd.DataFrame]


GRUPOS: dict[str, Grupo] = {
    "basicos": Grupo("features_basicas_v1.parquet", FEATURES_BASICAS, build_basicos),
    "derivados_mercado": Grupo("features_derivados_v1.parquet", FEATURES_DERIVADOS, build_derivados),
    # 2c: "macro", "lateralidad" -> misma clave KEY, su propio parquet
}


def summarize(df: pd.DataFrame, grupo: str, features: list[str], extra: dict[str, Any] | None = None) -> dict[str, Any]:
    nan = {c: round(float(df[c].isna().mean()) * 100, 2) for c in features}
    out: dict[str, Any] = {
        "version": VERSION, "grupo": grupo, "clave": KEY, "n": int(len(df)),
        "t_decision_min": str(df["t_decision"].min()) if len(df) else None,
        "t_decision_max": str(df["t_decision"].max()) if len(df) else None,
        "por_primaria": {k: int(v) for k, v in df["primaria"].value_counts().sort_index().items()},
        "pct_nan": dict(sorted(nan.items(), key=lambda kv: -kv[1])),
    }
    if extra:
        out.update(extra)
    return out


def _default_loader(end: pd.Timestamp) -> Loader:
    from ..store import load_klines

    return lambda sym: load_klines(sym, None, end)


def build(grupos: Iterable[str], out_dir: Path = DEFAULT_DIR, events: pd.DataFrame | None = None,
          loader: Loader | None = None) -> dict[str, dict[str, Any]]:
    ev_path = out_dir / EVENTS_FILE
    ev_hash = None
    if events is None:
        events = pd.read_parquet(ev_path)
        ev_hash = hashlib.sha256(ev_path.read_bytes()).hexdigest()
    loader = loader or _default_loader(max(D.HOLDOUT_START.values()))
    out = {}
    for g in grupos:
        spec = GRUPOS[g]
        t0 = time.time()
        df = spec.build(events, loader)
        if df[KEY].duplicated().any():
            raise RuntimeError(f"{g}: {KEY} duplicado")
        stem = spec.archivo.removesuffix(".parquet")
        summary = summarize(df, g, spec.features, {"eventos_sha256": ev_hash, "segundos": round(time.time() - t0, 1)})
        out_dir.mkdir(parents=True, exist_ok=True)
        df.to_parquet(out_dir / spec.archivo, index=False)
        (out_dir / f"{stem}_resumen.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False))
        out[g] = summary
    return out


def load_all(out_dir: Path = DEFAULT_DIR, grupos: Iterable[str] | None = None) -> pd.DataFrame:
    """Eventos + todas las features de los grupos con parquet (union 1 a 1 por `evento_id`)."""
    ev = pd.read_parquet(out_dir / EVENTS_FILE)
    ev.insert(0, KEY, event_ids(ev))
    names = list(grupos) if grupos is not None else [g for g, s in GRUPOS.items() if (out_dir / s.archivo).exists()]
    for g in names:
        f = pd.read_parquet(out_dir / GRUPOS[g].archivo)
        f = f.drop(columns=[c for c in f.columns if c != KEY and c in ev.columns])
        ev = ev.merge(f, on=KEY, how="left", validate="one_to_one")
    return ev


def main(argv: Iterable[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m heatsignal.lab.meta.features")
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build", help="features de los eventos de events_v1 (no es un intento, no entrena)")
    b.add_argument("--grupos", default="basicos", help=f"separados por coma: {','.join(GRUPOS)}")
    b.add_argument("--out", type=Path, default=DEFAULT_DIR)
    args = ap.parse_args(list(argv) if argv is not None else None)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    grupos = [g.strip() for g in args.grupos.split(",") if g.strip()]
    unknown = [g for g in grupos if g not in GRUPOS]
    if unknown:
        ap.error(f"grupos desconocidos: {unknown}")
    summary = build(grupos, args.out.expanduser())
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
