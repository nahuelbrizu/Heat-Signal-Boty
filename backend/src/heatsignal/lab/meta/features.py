"""
Etapa 2 del meta-labeling (plan-entrenamiento.md): features de cada evento de `events_v1.parquet`.

    python -m heatsignal.lab.meta.features build --grupos basicos [--out ~/heatsignal-lab/meta]

Version `features-meta-v1`. Cada grupo escribe SU parquet (`features_<grupo>_v1.parquet`, clave `evento_id`) y su
resumen JSON; `load_all()` une los eventos con todos los parquet de features que existan. Las subtareas 2b
(derivados/mercado) y 2c (macro/lateralidad) agregan su entrada a `GRUPOS` (archivo, lista de features y funcion
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
from ..clock import asof, resample_bars
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
FEATURES_BASICAS: list[str] = (COLS_1H + COLS_1D + ["daily_trend", "hora_utc", "sesion", "dia_semana",
                                                    "lado", "tamaño", "tipo"])
CATEGORICAS_BASICAS = ["daily_trend", "sesion", "tipo"]

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
    return out


def build_basicos(events: pd.DataFrame, loader: Loader) -> pd.DataFrame:
    ev = events.reset_index(drop=True)
    t_dec = pd.to_datetime(ev["t_decision"], utc=True)
    for p, g in ev.groupby("primaria"):  # el holdout de cada primaria ni se mira
        hs = D.HOLDOUT_START.get(p)
        if hs is not None and (t_dec[g.index] >= hs).any():
            raise RuntimeError(f"{p}: hay eventos en el holdout")
    out = pd.DataFrame({KEY: event_ids(ev), "primaria": ev["primaria"], "symbol": ev["symbol"], "t_decision": t_dec})
    mkt = pd.DataFrame(index=ev.index, columns=COLS_1H + COLS_1D + ["daily_trend"], dtype=object)
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
    return out[[KEY, *ID_COLS, *FEATURES_BASICAS]]


# ---- registro de grupos, resumen y union ------------------------------------------------------------------------


@dataclass(frozen=True)
class Grupo:
    archivo: str
    features: list[str]
    build: Callable[[pd.DataFrame, Loader], pd.DataFrame]


GRUPOS: dict[str, Grupo] = {
    "basicos": Grupo("features_basicas_v1.parquet", FEATURES_BASICAS, build_basicos),
    # 2b: "derivados", "mercado"; 2c: "macro", "lateralidad" -> misma clave KEY, su propio parquet
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
