"""
Estrategias del primer lote como reglas deterministas (plan-laboratorio.md). PROPUESTA: no estan
registradas; se registran (pre-registro) recien cuando el usuario apruebe cada spec.

Contrato (el mismo que `lab/sim.py`):
  - entrada: `MarketData` por simbolo (velas 1m indexadas por APERTURA, funding liquidado, metrics);
  - salida: dict simbolo -> Series de posicion objetivo indexada por el instante de DECISION
    (cuando los datos usados ya estaban disponibles; ver `lab/clock.py`). El simulador ejecuta al open de
    la vela 1m siguiente.
Cada estrategia declara sus parametros congelados y una grilla de calibracion chica (`grid`) fijada ANTES
de ver resultados; el walk-forward solo puede elegir dentro de esa grilla, y cada combinacion cuenta como
un intento para el Deflated Sharpe.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from typing import Any

import numpy as np
import pandas as pd

from . import features as F
from .clock import available_1m, available_metrics, resample_bars

H = pd.Timedelta(hours=1)
MIN = pd.Timedelta(minutes=1)


@dataclass
class MarketData:
    klines: pd.DataFrame                    # 1m, indice = apertura (store.load_klines)
    funding: pd.Series | None = None        # tasa liquidada, indice = liquidacion
    metrics: pd.DataFrame | None = None     # snapshots 5m, indice = ts (store.load_metrics)


@dataclass(frozen=True)
class Spec:
    strategy_id: str
    family: str
    description: str
    timeframe: str
    direction: str                          # long | short | both | neutral
    horizon: str
    params: dict[str, Any]
    grid: dict[str, list[Any]] = field(default_factory=dict)
    needs: tuple[str, ...] = ("klines",)
    backtestable: bool = True
    source: str = ""

    def spec_hash(self) -> str:
        return hashlib.sha256(json.dumps(asdict(self), sort_keys=True, default=str).encode()).hexdigest()[:16]


Signals = dict[str, pd.Series]
Rule = Callable[[dict[str, MarketData], dict[str, Any]], Signals]


# ---- utilidades de salida por eventos -----------------------------------------------------------------


def _changes(out: dict[pd.Timestamp, float]) -> pd.Series:
    """dict instante -> posicion como Series con DatetimeIndex UTC (tambien si no hubo eventos)."""
    keys = sorted(out)
    return pd.Series([out[k] for k in keys], index=pd.DatetimeIndex(keys, tz="UTC") if not keys or keys[0].tzinfo is None
                     else pd.DatetimeIndex(keys), dtype=float)


def hold_events(entries: pd.Series, horizon: pd.Timedelta, index: pd.DatetimeIndex) -> pd.Series:
    """Convierte eventos (instante de decision -> direccion +1/-1) en posicion que dura `horizon`.

    Una posicion por vez: los eventos que llegan con una posicion abierta se ignoran (sin piramidar).
    Devuelve una Series de cambios de posicion (decision -> objetivo), lista para el simulador.
    """
    out: dict[pd.Timestamp, float] = {}
    busy_until = pd.Timestamp.min.tz_localize("UTC")
    for t, d in entries[entries != 0].items():
        if t < busy_until:
            continue
        out[t] = float(np.sign(d))
        busy_until = t + horizon
        out[busy_until] = 0.0
    return _changes(out)


def exits_with_levels(entries: pd.Series, k1m: pd.DataFrame, horizon: pd.Timedelta,
                      tp: pd.Series | None = None, sl: pd.Series | None = None) -> pd.Series:
    """Eventos con take-profit / stop-loss en precio y salida por tiempo, evaluados en velas 1m.

    Conservador: si en la misma vela 1m se tocan TP y SL, se asume SL. El nivel se evalua desde la vela
    siguiente a la decision. La salida se DECIDE al cierre de la vela que toca el nivel (el simulador la
    ejecuta al open siguiente: no se asume fill en el nivel exacto).
    """
    out: dict[pd.Timestamp, float] = {}
    busy_until = pd.Timestamp.min.tz_localize("UTC")
    highs, lows = k1m["high"], k1m["low"]
    for t, d in entries[entries != 0].items():
        if t < busy_until:
            continue
        d = float(np.sign(d))
        end = t + horizon
        window = slice(t, end - MIN)  # velas con apertura en [t, end): la primera se abre en t (post-decision)
        hi, lo = highs.loc[window], lows.loc[window]
        exit_t = end
        tp_lvl = None if tp is None else tp.get(t)
        sl_lvl = None if sl is None else sl.get(t)
        hit_sl = hit_tp = None
        if sl_lvl is not None and np.isfinite(sl_lvl):
            m = (lo <= sl_lvl) if d > 0 else (hi >= sl_lvl)
            hit_sl = m.idxmax() if m.any() else None
        if tp_lvl is not None and np.isfinite(tp_lvl):
            m = (hi >= tp_lvl) if d > 0 else (lo <= tp_lvl)
            hit_tp = m.idxmax() if m.any() else None
        hits = [h for h in (hit_sl, hit_tp) if h is not None]
        if hits:
            first = min(hits)  # empate en la misma vela -> SL (va primero en la lista y min conserva)
            exit_t = first + MIN  # decision al cierre de esa vela
        out[t] = d
        out[exit_t] = 0.0
        busy_until = exit_t
    return _changes(out)


# ---- 1. donchian-ensemble-v1 ------------------------------------------------------------------------


def donchian_ensemble(data: dict[str, MarketData], p: dict[str, Any]) -> Signals:
    out: Signals = {}
    for sym, md in data.items():
        bars = resample_bars(md.klines, p["timeframe"])
        states = []
        for n in p["channels"]:
            ch = F.donchian(bars, n)
            exit_ch = F.donchian(bars, max(2, n // 2))
            st = np.zeros(len(bars))
            on = False
            for i, (c, up, lo) in enumerate(zip(bars["close"].to_numpy(), ch["upper"].to_numpy(), exit_ch["lower"].to_numpy())):
                if not on and np.isfinite(up) and c > up:
                    on = True
                elif on and np.isfinite(lo) and c < lo:
                    on = False
                st[i] = 1.0 if on else 0.0
            states.append(st)
        raw = pd.Series(np.mean(states, axis=0), index=bars.index)
        bars_per_year = pd.Timedelta("365D") / pd.Timedelta(p["timeframe"])
        vol = F.realized_vol(bars["close"], p["vol_window"]) * np.sqrt(bars_per_year)
        scale = (p["target_vol"] / vol).clip(upper=p["max_leverage"])
        out[sym] = (raw * scale).fillna(0.0)
    return out


# ---- 2. flujo-apertura-hora-v1 ------------------------------------------------------------------------


def flujo_apertura_hora(data: dict[str, MarketData], p: dict[str, Any]) -> Signals:
    out: Signals = {}
    for sym, md in data.items():
        k = md.klines
        opening = k[k.index.minute < p["open_minutes"]]
        grp = opening.groupby(opening.index.floor("1h"))
        buy = grp["taker_buy_quote_volume"].sum()
        tot = grp["quote_volume"].sum()
        complete = grp["close"].count() == p["open_minutes"]
        imb = ((2 * buy - tot) / tot.where(tot > 0))[complete]
        imb.index = imb.index + pd.Timedelta(minutes=p["open_minutes"])  # disponible al cierre del ultimo minuto
        thr = F.rolling_percentile(imb.abs(), p["pct_window_hours"], p["pct"])
        entries = np.sign(imb).where(imb.abs() > thr, 0.0).fillna(0.0)
        out[sym] = hold_events(entries, pd.Timedelta(hours=p["hold_hours"]), k.index)
    return out


# ---- 3. reversion-flujo-extremo-15m-v1 ----------------------------------------------------------------


def reversion_flujo_extremo(data: dict[str, MarketData], p: dict[str, Any]) -> Signals:
    out: Signals = {}
    for sym, md in data.items():
        bars = resample_bars(md.klines, p["timeframe"])
        ret = F.log_returns(bars["close"])
        sigma = ret.shift(1).rolling(p["vol_window"], min_periods=p["vol_window"]).std()
        imb = F.taker_imbalance(bars)
        thr = F.rolling_percentile(imb.abs(), p["vol_window"], p["imb_pct"])
        cond = (ret.abs() > p["k_sigma"] * sigma) & (np.sign(imb) == np.sign(ret)) & (imb.abs() > thr)
        entries = (-np.sign(ret)).where(cond, 0.0).fillna(0.0)
        out[sym] = hold_events(entries, pd.Timedelta(p["hold"]), md.klines.index)
    return out


# ---- 4/5. capitulacion-long-v1 / squeeze-short-v1 (proxy con klines) ----------------------------------


def _cascade(data: dict[str, MarketData], p: dict[str, Any], direction: int) -> Signals:
    """Proxy de cascada con klines: movimiento > k sigma en 15m, volumen extremo y flujo taker extremo
    en el sentido del movimiento; entrada en contra (rebote). TP = retroceso de `tp_retrace` del movimiento
    de 60m; SL = extremo de la cascada -/+ `sl_sigma` sigma60; tiempo maximo `max_hold`."""
    out: Signals = {}
    for sym, md in data.items():
        bars = resample_bars(md.klines, "15min")
        ret = F.log_returns(bars["close"])
        sigma = ret.shift(1).rolling(p["vol_window"], min_periods=p["vol_window"]).std()
        vol_thr = F.rolling_percentile(bars["quote_volume"], p["vol_window"], p["volume_pct"])
        imb = F.taker_imbalance(bars)
        if direction > 0:  # capitulacion de longs: caida + venta taker extrema -> long
            imb_thr = F.rolling_percentile(imb, p["vol_window"], 1 - p["imb_pct"])
            cond = (ret < -p["k_sigma"] * sigma) & (imb < imb_thr)
        else:              # squeeze de shorts: suba + compra taker extrema -> short
            imb_thr = F.rolling_percentile(imb, p["vol_window"], p["imb_pct"])
            cond = (ret > p["k_sigma"] * sigma) & (imb > imb_thr)
        cond &= bars["quote_volume"] > vol_thr
        entries = pd.Series(float(direction), index=bars.index).where(cond, 0.0).fillna(0.0)
        # niveles con la vela de 60m previa a la decision
        hour = resample_bars(md.klines, "1h")
        prev_close = bars["close"].shift(4)
        move = bars["close"] - prev_close
        sigma60 = F.realized_vol(hour["close"], p["vol_window"] // 4).reindex(bars.index, method="ffill") * bars["close"]
        extreme = bars["low"] if direction > 0 else bars["high"]
        tp = bars["close"] - p["tp_retrace"] * move
        sl = extreme - direction * p["sl_sigma"] * sigma60
        out[sym] = exits_with_levels(entries, md.klines, pd.Timedelta(p["max_hold"]), tp=tp, sl=sl)
    return out


def capitulacion_long(data, p):
    return _cascade(data, p, +1)


def squeeze_short(data, p):
    return _cascade(data, p, -1)


# ---- 6. funding-crowding-xs-v1 -------------------------------------------------------------------------


def funding_crowding_xs(data: dict[str, MarketData], p: dict[str, Any]) -> Signals:
    """En cada liquidacion: long al simbolo de menor z-score de funding, short al de mayor; mismo nocional."""
    z = pd.DataFrame({s: F.funding_zscore(md.funding, p["z_window"]) for s, md in data.items() if md.funding is not None})
    z = z.dropna(how="all")
    out: Signals = {}
    hold = pd.Timedelta(hours=p["hold_hours"])
    last_change = pd.Timestamp.min.tz_localize("UTC")
    pos = {s: 0.0 for s in z.columns}
    rows = {s: {} for s in z.columns}
    for t, row in z.iterrows():
        row = row.dropna()
        if len(row) < p["min_symbols"] or t < last_change + hold:
            continue
        lo, hi = row.idxmin(), row.idxmax()
        target = {s: 0.0 for s in z.columns}
        target[lo], target[hi] = 1.0, -1.0
        for s in z.columns:
            if target[s] != pos[s]:
                rows[s][t] = target[s]
                pos[s] = target[s]
        last_change = t
    for s in z.columns:
        out[s] = _changes(rows[s])
    return out


# ---- 7. funding-crowding-short-v1 ----------------------------------------------------------------------


def funding_crowding_short(data: dict[str, MarketData], p: dict[str, Any]) -> Signals:
    out: Signals = {}
    for sym, md in data.items():
        if md.funding is None or md.metrics is None:
            out[sym] = _changes({})
            continue
        hour = resample_bars(md.klines, "1h")
        f_thr = F.rolling_percentile(md.funding, p["funding_window"], p["funding_pct"])
        f_hot = (md.funding > f_thr).astype(float)
        f_hot = f_hot.reindex(hour.index, method="ffill").fillna(0.0)  # liquidado -> vigente hasta la proxima
        m = available_metrics(md.metrics)
        oi = m["oi_contracts"].reindex(hour.index, method="ffill")
        d_oi = F.oi_change_contracts(oi, 24)
        oi_thr = F.rolling_percentile(d_oi, p["oi_window_hours"], p["oi_pct"])
        breakdown = hour["close"] < hour["low"].shift(1).rolling(3, min_periods=3).min()
        cond = (f_hot > 0) & (d_oi > oi_thr) & breakdown
        entries = pd.Series(-1.0, index=hour.index).where(cond, 0.0).fillna(0.0)
        sigma4 = F.realized_vol(resample_bars(md.klines, "4h")["close"], 30).reindex(hour.index, method="ffill")
        sl = hour["close"] * (1 + p["sl_sigma"] * sigma4)
        out[sym] = exits_with_levels(entries, md.klines, pd.Timedelta(hours=p["hold_hours"]), sl=sl)
    return out


# ---- control: absorcion-flujo-v1 (se espera que falle por costos) ---------------------------------------


def absorcion_flujo(data: dict[str, MarketData], p: dict[str, Any]) -> Signals:
    out: Signals = {}
    for sym, md in data.items():
        bars = resample_bars(md.klines, "5min")
        roll = bars[["taker_buy_quote_volume", "quote_volume"]].rolling(12, min_periods=12).sum()
        ofi = (2 * roll["taker_buy_quote_volume"] - roll["quote_volume"]) / roll["quote_volume"]
        ret = bars["close"] / bars["close"].shift(12) - 1
        entries = pd.Series(0.0, index=bars.index)
        entries[(ofi > p["ofi"]) & (ret < 0)] = -1.0
        entries[(ofi < -p["ofi"]) & (ret > 0)] = 1.0
        out[sym] = hold_events(entries, H, md.klines.index)
    return out


# ---- catalogo ----------------------------------------------------------------------------------------------

SEVEN = ["BTCUSDT", "SOLUSDT", "XRPUSDT", "ETHUSDT", "XLMUSDT", "BNBUSDT", "TRXUSDT"]

CATALOG: dict[str, tuple[Spec, Rule]] = {
    "donchian-ensemble-v1": (Spec(
        "donchian-ensemble-v1", "continuacion", "Ensemble de canales Donchian en 4h con tamano por volatilidad; long.",
        "4h", "long", "dias", {"timeframe": "4h", "channels": [20, 40, 80, 160], "vol_window": 180,
                               "target_vol": 0.20, "max_leverage": 1.0},
        grid={"target_vol": [0.15, 0.20, 0.30]},
        source="Zarattini, Pagani & Barbon 2025 (SFI RP 25-80)"), donchian_ensemble),
    "flujo-apertura-hora-v1": (Spec(
        "flujo-apertura-hora-v1", "continuacion", "Desbalance taker de los minutos :00-:01 > p95 trailing -> a favor 4 h.",
        "1m", "both", "4h", {"open_minutes": 2, "pct": 0.95, "pct_window_hours": 720, "hold_hours": 4},
        grid={"pct": [0.95, 0.99]}, source="Kim & Hansen 2026 (arXiv:2607.09426)"), flujo_apertura_hora),
    "reversion-flujo-extremo-15m-v1": (Spec(
        "reversion-flujo-extremo-15m-v1", "reversion", "Vela 15m > k sigma con flujo extremo a favor -> en contra 1 h.",
        "15min", "both", "1h", {"timeframe": "15min", "vol_window": 2880, "k_sigma": 2.5, "imb_pct": 0.97, "hold": "1h"},
        grid={"k_sigma": [2.0, 2.5, 3.0], "imb_pct": [0.95, 0.97, 0.99]},
        source="Kitron & Wengrowicz 2026 (arXiv:2608.21888): bruto ~1.3 bp, se espera que muera por costos"),
        reversion_flujo_extremo),
    "capitulacion-long-v1": (Spec(
        "capitulacion-long-v1", "liquidaciones", "Proxy de cascada de longs (caida > 3 sigma, volumen y venta taker extremos) -> long.",
        "15min", "long", "<=4h", {"vol_window": 2880, "k_sigma": 3.0, "volume_pct": 0.99, "imb_pct": 0.98,
                                  "tp_retrace": 0.5, "sl_sigma": 0.5, "max_hold": "4h"},
        source="He, Wang & Zhang (SSRN 6185958); evidencia debil (arXiv:2607.27070)"), capitulacion_long),
    "squeeze-short-v1": (Spec(
        "squeeze-short-v1", "liquidaciones", "Espejo: suba > 3 sigma con compra taker y volumen extremos -> short.",
        "15min", "short", "<=4h", {"vol_window": 2880, "k_sigma": 3.0, "volume_pct": 0.99, "imb_pct": 0.98,
                                   "tp_retrace": 0.5, "sl_sigma": 0.5, "max_hold": "4h"},
        source="espejo de capitulacion-long-v1"), squeeze_short),
    "funding-crowding-xs-v1": (Spec(
        "funding-crowding-xs-v1", "carry", "En cada liquidacion: long al menor z-score de funding (14 d), short al mayor.",
        "8h", "neutral", "24h", {"z_window": 42, "hold_hours": 24, "min_symbols": 5},
        grid={"hold_hours": [8, 24]}, needs=("klines", "funding"),
        source="ML4T caso crypto perps (IC -0.035); BIS WP 1087"), funding_crowding_xs),
    "funding-crowding-short-v1": (Spec(
        "funding-crowding-short-v1", "carry", "Funding > p95 (90 d) + OI en contratos 24 h > p80 + quiebre de minimos -> short 24 h.",
        "1h", "short", "24h", {"funding_window": 270, "funding_pct": 0.95, "oi_window_hours": 2160, "oi_pct": 0.80,
                               "hold_hours": 24, "sl_sigma": 2.0},
        needs=("klines", "funding", "metrics"),
        source="BIS WP 1087 (carry y riesgo de caida); BitMEX: funding extremo solo no predice"), funding_crowding_short),
    "funding-spread-hl-cex-v1": (Spec(
        "funding-spread-hl-cex-v1", "cross-exchange", "Funding de Hyperliquid vs mediana CEX > p95 -> delta-neutral hasta 72 h.",
        "1h", "neutral", "<=72h", {"pct": 0.95, "exit_pct": 0.50, "max_hold_hours": 72},
        needs=("bars_1m multi-exchange (grabacion propia)",), backtestable=False,
        source="BitMEX blog (Hyperliquid funding); sin paper academico"), lambda data, p: {}),
    "absorcion-flujo-v1": (Spec(
        "absorcion-flujo-v1", "control", "CONTROL: OFI 1h > 0.25 con precio cayendo -> short 1 h (y simetrico). Se espera que FALLE neto de costos.",
        "5min", "both", "1h", {"ofi": 0.25}, source="ML4T cap. 18 (cost cliff)"), absorcion_flujo),
}
