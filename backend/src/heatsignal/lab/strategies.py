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
from .clock import asof, available_1m, available_metrics, resample_bars

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


# =========================================================================================================
# LOTE 2 (PROPUESTA: pendiente de aprobacion antes de correr). Las reglas del lote 1 de arriba NO cambian.
# =========================================================================================================

DAY = pd.Timedelta(days=1)


# ---- 2.3 compresion-breakout-v1 ------------------------------------------------------------------------


def compresion_breakout(data: dict[str, MarketData], p: dict[str, Any]) -> Signals:
    """Velas 1h. Compresion = ancho de Bollinger(bb_n, bb_k) < su percentil `width_pct` trailing de
    `width_window` velas, durante >= `min_bars` velas seguidas. Entrada al cierre de la vela que rompe el
    rango (max high / min low) de esa racha comprimida con el close, si el desbalance taker de la vela de
    ruptura va a favor y |imb| supera su percentil `imb_pct` trailing. Stop = lado opuesto del rango;
    salida por tiempo a `hold_hours`.
    """
    out: Signals = {}
    for sym, md in data.items():
        bars = resample_bars(md.klines, "1h")
        close = bars["close"]
        mid = close.rolling(p["bb_n"], min_periods=p["bb_n"]).mean()
        sd = close.rolling(p["bb_n"], min_periods=p["bb_n"]).std()
        width = 2 * p["bb_k"] * sd / mid
        thr = F.rolling_percentile(width, p["width_window"], p["width_pct"])
        compressed = (width < thr).to_numpy()
        imb = F.taker_imbalance(bars)
        imb_thr = F.rolling_percentile(imb.abs(), p["width_window"], p["imb_pct"]).to_numpy()
        highs, lows, closes, imbs = (bars["high"].to_numpy(), bars["low"].to_numpy(), close.to_numpy(),
                                     imb.to_numpy())
        entries = np.zeros(len(bars))
        stops = np.full(len(bars), np.nan)
        run, run_hi, run_lo = 0, -np.inf, np.inf
        for i in range(len(bars)):
            # la vela i se evalua contra la racha comprimida que termina en i-1 (rango ya conocido)
            if run >= p["min_bars"] and np.isfinite(imb_thr[i]) and np.isfinite(imbs[i]):
                if closes[i] > run_hi and imbs[i] > 0 and abs(imbs[i]) > imb_thr[i]:
                    entries[i], stops[i] = 1.0, run_lo
                elif closes[i] < run_lo and imbs[i] < 0 and abs(imbs[i]) > imb_thr[i]:
                    entries[i], stops[i] = -1.0, run_hi
            if compressed[i]:
                run += 1
                run_hi, run_lo = max(run_hi, highs[i]), min(run_lo, lows[i])
            else:
                run, run_hi, run_lo = 0, -np.inf, np.inf
        out[sym] = exits_with_levels(pd.Series(entries, index=bars.index), md.klines,
                                     pd.Timedelta(hours=p["hold_hours"]), sl=pd.Series(stops, index=bars.index))
    return out


# ---- 2.2 momentum-intradia-v1 --------------------------------------------------------------------------


def momentum_intradia(data: dict[str, MarketData], p: dict[str, Any]) -> Signals:
    """Senal del dia D = retorno open(00:00) -> close(vela 00:29) UTC (conocido a las 00:30; solo dias con
    las 30 velas 1m completas). Si |senal| > percentil `pct` trailing de las ultimas `pct_window_days`
    senales, se abre en esa direccion a las `entry` UTC del mismo dia D y se cierra a las 00:00 de D+1.
    Solo los simbolos de `symbols`; el resto queda sin senal.
    """
    out: Signals = {}
    win = pd.Timedelta(minutes=p["signal_minutes"])
    entry_off = pd.Timedelta(p["entry"])
    for sym, md in data.items():
        if sym not in p["symbols"]:
            out[sym] = _changes({})
            continue
        k = md.klines
        first = k[k.index - k.index.floor("1D") < win]
        grp = first.groupby(first.index.floor("1D"))
        complete = grp["close"].count() == p["signal_minutes"]
        sig = (grp["close"].last() / grp["open"].first() - 1)[complete]
        thr = F.rolling_percentile(sig.abs(), p["pct_window_days"], p["pct"])
        entries = np.sign(sig).where(sig.abs() > thr, 0.0).fillna(0.0)
        entries.index = entries.index + entry_off  # decision a las 23:30 del dia D (senal conocida 00:30)
        out[sym] = hold_events(entries, DAY - entry_off, k.index)
    return out


# ---- 2.5 oi-precio-continuacion-v1 ---------------------------------------------------------------------


def oi_precio_continuacion(data: dict[str, MarketData], p: dict[str, Any]) -> Signals:
    """Velas 1h. ret_4h > k * sigma_4h (desvio trailing de los retornos de 4 h, `sigma_window` velas),
    cambio del OI en CONTRATOS a 4 h > percentil `oi_pct` trailing y desbalance taker de las ultimas 4 h
    > 0 -> long `hold_hours`; simetrico short (ret_4h < -k sigma, OI subiendo igual, desbalance < 0).
    metrics se toma como disponible en ts + 5m (`lab/clock.py`); un OI mas viejo que `oi_max_age` = hueco.
    """
    out: Signals = {}
    for sym, md in data.items():
        if md.metrics is None:
            out[sym] = _changes({})
            continue
        hour = resample_bars(md.klines, "1h")
        ret4 = F.log_returns(hour["close"], 4)
        sigma4 = ret4.shift(1).rolling(p["sigma_window"], min_periods=p["sigma_window"]).std()
        m = available_metrics(md.metrics)["oi_contracts"]
        oi = asof(hour.index, m, tolerance=pd.Timedelta(p["oi_max_age"]))
        d_oi = F.oi_change_contracts(oi, 4)
        oi_thr = F.rolling_percentile(d_oi, p["oi_window_hours"], p["oi_pct"])
        roll = hour[["taker_buy_quote_volume", "quote_volume"]].rolling(4, min_periods=4).sum()
        imb4 = F.taker_imbalance(roll)
        oi_up = d_oi > oi_thr
        k = p["k_sigma"]
        entries = pd.Series(0.0, index=hour.index)
        entries[(ret4 > k * sigma4) & oi_up & (imb4 > 0)] = 1.0
        entries[(ret4 < -k * sigma4) & oi_up & (imb4 < 0)] = -1.0
        out[sym] = hold_events(entries, pd.Timedelta(hours=p["hold_hours"]), md.klines.index)
    return out


# ---- 3.1 carry-xs-funding-v1 ---------------------------------------------------------------------------


def carry_xs_funding(data: dict[str, MarketData], p: dict[str, Any]) -> Signals:
    """En cada liquidacion de funding (union de las de todos los simbolos): ordenar por el promedio de las
    ultimas `lookback` tasas liquidadas de cada simbolo; long a las `n_side` de menor funding y short a las
    `n_side` de mayor, mismo nocional, neutral. Solo se opera si cambia la composicion. Un simbolo cuya
    ultima tasa tiene mas de `max_age_hours` no cuenta; con menos de `min_symbols` con dato no se cambia
    nada. Empates: orden alfabetico del simbolo (determinista).
    """
    avg = {s: md.funding.sort_index().rolling(p["lookback"], min_periods=p["lookback"]).mean().dropna()
           for s, md in data.items() if md.funding is not None and len(md.funding)}
    out: Signals = {s: _changes({}) for s in data}
    if not avg:
        return out
    times = sorted(set().union(*(a.index for a in avg.values())))
    idx = pd.DatetimeIndex(times)
    tol = pd.Timedelta(hours=p["max_age_hours"])
    frame = pd.DataFrame({s: asof(idx, a.rename(s), tolerance=tol) for s, a in avg.items()}, index=idx)
    syms = sorted(frame.columns)
    pos = {s: 0.0 for s in syms}
    rows: dict[str, dict[pd.Timestamp, float]] = {s: {} for s in syms}
    n = p["n_side"]
    for t, row in frame[syms].iterrows():
        row = row.dropna()
        if len(row) < max(p["min_symbols"], 2 * n):
            continue
        ranked = sorted(row.index, key=lambda s: (row[s], s))
        target = {s: 0.0 for s in syms}
        for s in ranked[:n]:
            target[s] = 1.0
        for s in ranked[-n:]:
            target[s] = -1.0
        for s in syms:
            if target[s] != pos[s]:
                rows[s][t] = target[s]
                pos[s] = target[s]
    for s in syms:
        out[s] = _changes(rows[s])
    return out


# ---- 6.1 lunes-asia-v1 (observacion) -------------------------------------------------------------------


def lunes_asia(data: dict[str, MarketData], p: dict[str, Any]) -> Signals:
    """Long `symbols` desde el domingo `entry` UTC durante `hold_hours`. Regla de calendario: la decision no
    usa precios; solo se emiten entradas dentro del rango de datos disponible."""
    out: Signals = {}
    off = pd.Timedelta(p["entry"])
    for sym, md in data.items():
        k = md.klines
        if sym not in p["symbols"] or not len(k):
            out[sym] = _changes({})
            continue
        lo, hi = k.index[0], k.index[-1] + MIN  # hi = cierre de la ultima vela conocida
        days = pd.date_range(lo.floor("1D") - DAY * 7, hi.floor("1D"), freq="1D")
        starts = [d + off for d in days if d.dayofweek == 6 and lo <= d + off <= hi]
        entries = pd.Series(1.0, index=pd.DatetimeIndex(starts, tz="UTC") if starts and starts[0].tzinfo is None
                            else pd.DatetimeIndex(starts), dtype=float)
        out[sym] = hold_events(entries, pd.Timedelta(hours=p["hold_hours"]), k.index)
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

# Lote 1 aprobado el 2026-10-17 (decisions.md): sus specs quedan congeladas por hash.
LOTE1 = tuple(CATALOG)

# ---- lote 2: PROPUESTA, pendiente de aprobacion antes de correr ------------------------------------------

CATALOG.update({
    "compresion-breakout-v1": (Spec(
        "compresion-breakout-v1", "continuacion",
        "Velas 1h. Compresion: ancho de Bollinger(20, 2) bajo su p10 trailing de 30 dias durante >= 6 velas "
        "seguidas. Entrada al cierre de la vela cuyo close rompe el rango (max/min) de esa racha, si el "
        "desbalance taker de la vela de ruptura va a favor y |imb| supera su p75 trailing de 30 dias. Stop en "
        "el lado opuesto del rango; salida a las 12 h. Long y short.",
        "1h", "both", "12h",
        {"bb_n": 20, "bb_k": 2.0, "width_pct": 0.10, "width_window": 720, "min_bars": 6, "imb_pct": 0.75,
         "hold_hours": 12},
        grid={"width_pct": [0.10, 0.20], "min_bars": [6, 12]},
        source="catalogo-hipotesis.md 2.3 (compresion-breakout-v1: Bollinger comprimido + ruptura con flujo a "
               "favor, 12 h; riesgo MEDIO-ALTO); sin paper academico citado"), compresion_breakout),
    "momentum-intradia-v1": (Spec(
        "momentum-intradia-v1", "continuacion",
        "Senal = retorno 00:00 -> 00:30 UTC (open de la vela 00:00 al close de la 00:29, dia completo). Si "
        "|senal| > p60 trailing de las ultimas 90 senales diarias, abrir en esa direccion a las 23:30 UTC del "
        "mismo dia y cerrar a las 00:00. Solo BTC y ETH (los demas sin senal). Sin filtro de volumen.",
        "1m", "both", "30min",
        {"symbols": ["BTCUSDT", "ETHUSDT"], "signal_minutes": 30, "pct": 0.60, "pct_window_days": 90,
         "entry": "23h30min"},
        source="catalogo-hipotesis.md 2.2 (momentum-intradia-volumen-v1: la primera media hora UTC predice la "
               "ultima; riesgo MEDIO-ALTO); esta version no usa el filtro de volumen"), momentum_intradia),
    "oi-precio-continuacion-v1": (Spec(
        "oi-precio-continuacion-v1", "continuacion",
        "Velas 1h. ret_4h > 1 sigma_4h (desvio trailing de 30 dias), cambio del OI en CONTRATOS a 4 h > p80 "
        "trailing (90 dias) y desbalance taker de las ultimas 4 h > 0 -> long 8 h. Simetrico: ret_4h < -1 "
        "sigma, OI subiendo igual y desbalance < 0 -> short 8 h. metrics disponible en ts + 5m.",
        "1h", "both", "8h",
        {"k_sigma": 1.0, "sigma_window": 720, "oi_pct": 0.80, "oi_window_hours": 2160, "oi_max_age": "30min",
         "hold_hours": 8},
        grid={"k_sigma": [1.0, 1.5]}, needs=("klines", "metrics"),
        source="catalogo-hipotesis.md 2.5 (oi-precio-continuacion-v1: precio, OI y compras subiendo -> a favor "
               "8 h; riesgo ALTO); ML4T H6 (OI en contratos, 4 h -> 8 h; alli en version contraria)"),
        oi_precio_continuacion),
    "carry-xs-funding-v1": (Spec(
        "carry-xs-funding-v1", "carry",
        "En cada liquidacion de funding: ordenar los simbolos por el promedio de sus ultimas 3 tasas "
        "liquidadas; long a las 2 de menor funding y short a las 2 de mayor, mismo nocional, neutral. Se "
        "rebalancea solo si cambia la composicion. Minimo 5 simbolos con dato (tasa de hace <= 9 h); si no, "
        "no se cambia nada. Distinta de funding-crowding-xs-v1 (aca se cobra el carry, no se apuesta a la "
        "reversion).",
        "8h", "neutral", "hasta cambio de composicion",
        {"lookback": 3, "n_side": 2, "min_symbols": 5, "max_age_hours": 9},
        needs=("klines", "funding"),
        source="catalogo-hipotesis.md 3.1 (carry-xs-funding-v1: long las 2 de menor funding, short las 2 de "
               "mayor; riesgo MEDIO); BIS WP 1087 (Schmeling, Schrimpf, Todorov)"), carry_xs_funding),
    "lunes-asia-v1": (Spec(
        "lunes-asia-v1", "estacionalidad",
        "MODO OBSERVACION (sin capital aunque pase los criterios): long BTC desde el domingo 23:00 UTC hasta "
        "el lunes 23:00 UTC, todas las semanas.",
        "calendario", "long", "24h",
        {"symbols": ["BTCUSDT"], "entry": "23h", "hold_hours": 24, "modo": "observacion"},
        source="catalogo-hipotesis.md 6.1 (lunes-asia-v1, estacionalidad; riesgo ALTO); Quantpedia "
               "(calendario). Efectos de calendario: debiles o desaparecidos tras costos"), lunes_asia),
})
LOTE2 = tuple(k for k in CATALOG if k not in LOTE1)


# =========================================================================================================
# LOTE 3 (PROPUESTA: pendiente de aprobacion antes de correr). Las reglas de los lotes 1 y 2 NO cambian.
#
# Convencion de pesos (vale para todo el lote, y en especial para las de 2 patas): el valor de la senal es la
# fraccion del capital ASIGNADO AL SIMBOLO (`lab/sim.py`, allocation="equal": capital / n_simbolos). Como
# todos los simbolos reciben la misma asignacion, la relacion entre patas se conserva con cualquier universo:
# una pata Y con peso -w y una pata X con peso +beta*w tienen nocionales en relacion 1 : beta (beta-neutral).
# Con mas simbolos en --symbols el retorno se diluye (el Sharpe no cambia).
# =========================================================================================================


def _hourly_close(k: pd.DataFrame) -> pd.Series:
    """Close de velas 1h (indice = cierre) en una grilla horaria COMPLETA: hora sin vela completa = NaN.
    Asi un `shift(n)` es exactamente n horas y un hueco no se salta en silencio."""
    bars = resample_bars(k, "1h")
    if bars.empty:
        return pd.Series(dtype=float)
    idx = pd.date_range(bars.index[0], bars.index[-1], freq="1h")
    return bars["close"].reindex(idx)


def _xs_changes(targets: dict[pd.Timestamp, dict[str, float]], syms: list[str]) -> dict[str, dict[pd.Timestamp, float]]:
    """Objetivos por instante -> cambios por simbolo (solo se emite cuando cambia el objetivo)."""
    pos = {s: 0.0 for s in syms}
    rows: dict[str, dict[pd.Timestamp, float]] = {s: {} for s in syms}
    for t in sorted(targets):
        for s in syms:
            v = targets[t].get(s, 0.0)
            if v != pos[s]:
                rows[s][t] = v
                pos[s] = v
    return rows


# ---- 3.1 tsmom-diario-v1 (ML4T H7) ---------------------------------------------------------------------


def tsmom_diario(data: dict[str, MarketData], p: dict[str, Any]) -> Signals:
    """Decision diaria a las 00:00 UTC (cierre de la vela 1h 23:00-00:00). mom = log(close[t - skip] /
    close[t - skip - lookback]) con velas 1h; vol = desvio de los retornos 1h de las ultimas `vol_window_hours`
    velas (hasta t) anualizado con sqrt(24*365). Peso = sign(mom) * min(target_vol / vol, max_leverage).
    Rebalanceo solo si cambia el signo, si se parte de 0 o si |w_nuevo - w_actual| > `rebalance_threshold` *
    |w_actual| (cambio RELATIVO). Si falta el dato del dia (hueco, vol no definida) se mantiene la posicion.
    """
    out: Signals = {}
    ann = np.sqrt(24 * 365)
    for sym, md in data.items():
        c = _hourly_close(md.klines) if sym in p["symbols"] else pd.Series(dtype=float)
        if c.empty:
            out[sym] = _changes({})
            continue
        lr = np.log(c).diff()
        vw = p["vol_window_hours"]
        vol = lr.rolling(vw, min_periods=int(vw * p["vol_min_frac"])).std() * ann
        ref = c.shift(p["skip_hours"])
        mom = np.log(ref / ref.shift(p["lookback_hours"]))
        rows: dict[pd.Timestamp, float] = {}
        pos = 0.0
        for t in c.index[(c.index.hour == 0) & (c.index.minute == 0)]:
            m, v = mom[t], vol[t]
            if not (np.isfinite(m) and np.isfinite(v)) or v <= 0 or m == 0:
                continue
            w = float(np.sign(m) * min(p["target_vol"] / v, p["max_leverage"]))
            if pos == 0 or np.sign(w) != np.sign(pos) or abs(w - pos) > p["rebalance_threshold"] * abs(pos):
                rows[t] = w
                pos = w
        out[sym] = _changes(rows)
    return out


# ---- 3.2 low-vol-xs-v1 (ML4T H2) -----------------------------------------------------------------------


def low_vol_xs(data: dict[str, MarketData], p: dict[str, Any]) -> Signals:
    """Diario a las 00:00 UTC: RV = desvio de los retornos 1h de las ultimas `rv_hours` velas (hasta t). Long
    las `n_side` de menor RV y short las `n_side` de mayor, peso +-1 (dolar-neutral). Tenencia 24 h: se re-arma
    cada dia y solo se opera lo que cambia. Con menos de `min_symbols` simbolos con RV ese dia -> todo plano.
    Empates: orden alfabetico (determinista)."""
    syms = sorted(data)
    n = p["rv_hours"]
    rv = {}
    for s in syms:
        c = _hourly_close(data[s].klines)
        if not c.empty:
            rv[s] = np.log(c).diff().rolling(n, min_periods=int(n * p["rv_min_frac"])).std()
    out: Signals = {s: _changes({}) for s in syms}
    if not rv:
        return out
    frame = pd.DataFrame(rv)
    k = p["n_side"]
    targets: dict[pd.Timestamp, dict[str, float]] = {}
    for t in frame.index[(frame.index.hour == 0) & (frame.index.minute == 0)]:
        row = frame.loc[t].dropna()
        target: dict[str, float] = {}
        if len(row) >= max(p["min_symbols"], 2 * k):
            ranked = sorted(row.index, key=lambda s: (row[s], s))
            target.update({s: 1.0 for s in ranked[:k]})
            target.update({s: -1.0 for s in ranked[-k:]})
        targets[t] = target
    for s, r in _xs_changes(targets, syms).items():
        out[s] = _changes(r)
    return out


# ---- 3.3 pares-eth-btc-v1 ------------------------------------------------------------------------------


def pair_zscore(y: pd.Series, x: pd.Series, window: int) -> pd.DataFrame:
    """Spread y - beta*x con beta OLS (con intercepto) de las `window` velas ANTERIORES a t (no incluye t).
    z_t = (y_t - beta*x_t - media del spread en la ventana) / desvio del residuo en la ventana, todo con la
    misma beta. Solo filas con ambos datos. Devuelve columnas beta, z."""
    df = pd.concat({"y": y, "x": x}, axis=1).dropna()
    roll_y, roll_x = df["y"].rolling(window, min_periods=window), df["x"].rolling(window, min_periods=window)
    m_y, m_x = roll_y.mean().shift(1), roll_x.mean().shift(1)
    v_y, v_x = roll_y.var().shift(1), roll_x.var().shift(1)
    c_yx = roll_y.cov(df["x"]).shift(1)
    beta = c_yx / v_x.where(v_x > 0)
    resid_var = v_y - beta * c_yx
    z = (df["y"] - beta * df["x"] - (m_y - beta * m_x)) / np.sqrt(resid_var.where(resid_var > 0))
    return pd.DataFrame({"beta": beta, "z": z})


def pares_eth_btc(data: dict[str, MarketData], p: dict[str, Any]) -> Signals:
    """Velas 1h. spread = log(Y) - beta*log(X) (Y = ETH, X = BTC), beta y z-score trailing de `window_hours`
    velas (`pair_zscore`). z > +entry_z -> short spread: Y -w, X +beta*w; z < -entry_z -> long spread: Y +w,
    X -beta*w; w = 1/max(1, beta) (ninguna pata pasa de 1). beta se fija al entrar (sin rebalanceo) y tiene
    que ser > 0. Salida: |z| < exit_z, stop |z| > stop_z, o a las `max_hold_hours` (exacto). No se entra con
    |z| > stop_z. Despues de un stop o de una salida por tiempo no se re-entra hasta que |z| < entry_z
    (re-armado). Los demas simbolos quedan sin senal."""
    ys, xs = p["leg_y"], p["leg_x"]
    out: Signals = {s: _changes({}) for s in data}
    if ys not in data or xs not in data:
        return out
    cy, cx = _hourly_close(data[ys].klines), _hourly_close(data[xs].klines)
    if cy.empty or cx.empty:
        return out
    zb = pair_zscore(np.log(cy), np.log(cx), p["window_hours"])
    max_hold = pd.Timedelta(hours=p["max_hold_hours"])
    ry: dict[pd.Timestamp, float] = {}
    rx: dict[pd.Timestamp, float] = {}
    pos, t_in, armed = 0, None, True
    for t, beta, z in zip(zb.index, zb["beta"].to_numpy(), zb["z"].to_numpy()):
        if pos != 0 and t >= t_in + max_hold:
            t_out = t_in + max_hold
            ry[t_out], rx[t_out] = 0.0, 0.0
            pos, armed = 0, False
        if pos != 0:  # todavia dentro del horizonte
            if np.isfinite(z) and abs(z) < p["exit_z"]:
                ry[t], rx[t] = 0.0, 0.0
                pos, armed = 0, True
            elif np.isfinite(z) and abs(z) > p["stop_z"]:
                ry[t], rx[t] = 0.0, 0.0
                pos, armed = 0, False
            continue  # una salida en t no re-entra en la misma vela
        if not np.isfinite(z):
            continue
        if not armed:
            armed = abs(z) < p["entry_z"]
            continue
        if p["entry_z"] < abs(z) <= p["stop_z"] and np.isfinite(beta) and beta > 0:
            d = -1 if z > 0 else 1  # +1 = long spread (long Y, short X)
            w = 1.0 / max(1.0, float(beta))
            ry[t], rx[t] = d * w, -d * float(beta) * w
            pos, t_in = d, t
    out[ys], out[xs] = _changes(ry), _changes(rx)
    return out


# ---- 3.4 rango-reversion-v1 ----------------------------------------------------------------------------


def rango_reversion(data: dict[str, MarketData], p: dict[str, Any]) -> Signals:
    """Velas 1h, solo `symbols`. Regimen "rango" = ADX(adx_n) < adx_max (ADX de Wilder, `features.adx`).
    En rango: close < SMA(bb_n) - bb_k*sd -> long; close > SMA + bb_k*sd -> short. Salida: take profit en la
    EMA(ema_n) del momento de la entrada (nivel fijo; la entrada exige que la EMA este del lado del objetivo),
    stop a sl_atr * ATR(atr_n) del close de entrada, o a las `hold_hours`. TP/SL evaluados en velas 1m
    (`exits_with_levels`: empate -> stop)."""
    out: Signals = {}
    for sym, md in data.items():
        bars = resample_bars(md.klines, "1h") if sym in p["symbols"] else pd.DataFrame()
        if bars.empty:
            out[sym] = _changes({})
            continue
        close = bars["close"]
        mid = close.rolling(p["bb_n"], min_periods=p["bb_n"]).mean()
        sd = close.rolling(p["bb_n"], min_periods=p["bb_n"]).std()
        ema = close.ewm(span=p["ema_n"], adjust=False, min_periods=p["ema_n"]).mean()
        rango = F.adx(bars, p["adx_n"]) < p["adx_max"]
        atr = F.atr(bars, p["atr_n"])
        entries = pd.Series(0.0, index=bars.index)
        entries[rango & (close < mid - p["bb_k"] * sd) & (ema > close)] = 1.0
        entries[rango & (close > mid + p["bb_k"] * sd) & (ema < close)] = -1.0
        sl = close - entries * p["sl_atr"] * atr
        out[sym] = exits_with_levels(entries, md.klines, pd.Timedelta(hours=p["hold_hours"]), tp=ema, sl=sl)
    return out


# ---- 3.5 reversion-xs-1h-v1 (catalogo 1.2) ---------------------------------------------------------------


def reversion_xs_1h(data: dict[str, MarketData], p: dict[str, Any]) -> Signals:
    """Velas 1h. Para cada alt: r = retorno log 1h; beta = cov(r_alt, r_btc)/var(r_btc) de las `window_hours`
    velas ANTERIORES (con ambos datos); e_t = r_alt,t - beta * r_btc,t; sigma = desvio de e de las
    `window_hours` velas anteriores. e < -k*sigma -> long alt (w = 1/max(1, beta)) y short BTC beta*w;
    e > +k*sigma -> al reves. beta > 0. Salida a las `hold_hours`; una posicion por alt a la vez. La pata BTC
    es la SUMA de las coberturas abiertas de todas las alts (puede pasar de 1: hasta ~6 con las 6 alts)."""
    btc = p["btc"]
    out: Signals = {s: _changes({}) for s in data}
    if btc not in data:
        return out
    cb = _hourly_close(data[btc].klines)
    if cb.empty:
        return out
    rb = np.log(cb).diff()
    w_n, k = p["window_hours"], p["k_sigma"]
    hold = pd.Timedelta(hours=p["hold_hours"])
    hedge: dict[pd.Timestamp, float] = {}
    for alt in p["symbols"]:
        if alt not in data or alt == btc:
            continue
        ca = _hourly_close(data[alt].klines)
        if ca.empty:
            continue
        df = pd.concat({"a": np.log(ca).diff(), "b": rb}, axis=1).dropna()
        beta = (df["a"].rolling(w_n, min_periods=w_n).cov(df["b"]) /
                df["b"].rolling(w_n, min_periods=w_n).var()).shift(1)
        e = df["a"] - beta * df["b"]
        sigma = e.shift(1).rolling(w_n, min_periods=w_n).std()
        rows: dict[pd.Timestamp, float] = {}
        busy_until = pd.Timestamp.min.tz_localize("UTC")
        for t, et, st, bt in zip(df.index, e.to_numpy(), sigma.to_numpy(), beta.to_numpy()):
            if t < busy_until or not (np.isfinite(et) and np.isfinite(st) and np.isfinite(bt)) or bt <= 0 or st <= 0:
                continue
            if et < -k * st:
                d = 1.0
            elif et > k * st:
                d = -1.0
            else:
                continue
            w = 1.0 / max(1.0, float(bt))
            rows[t], rows[t + hold] = d * w, 0.0
            hedge[t] = hedge.get(t, 0.0) - d * float(bt) * w
            hedge[t + hold] = hedge.get(t + hold, 0.0) + d * float(bt) * w
            busy_until = t + hold
        out[alt] = _changes(rows)
    b_rows: dict[pd.Timestamp, float] = {}
    level, last = 0.0, 0.0
    for t in sorted(hedge):
        level += hedge[t]
        v = round(level, 12) + 0.0  # sin ruido de coma flotante (y sin -0.0)
        if v != last:
            b_rows[t] = v
            last = v
    out[btc] = _changes(b_rows)
    return out


# ---- 3.6 funding-prepago-v1 (catalogo 3.3) -------------------------------------------------------------


def funding_prepago(data: dict[str, MarketData], p: dict[str, Any]) -> Signals:
    """Alrededor de cada liquidacion T (00/08/16 UTC). En la decision d = T - `pre_minutes` se usa la ULTIMA
    tasa YA LIQUIDADA conocida en d (normalmente la de T - 8 h; la de T todavia no existe: usarla seria
    look-ahead). Si esa tasa > percentil `pct_hi` de las `pct_window` liquidaciones anteriores (trailing, sin
    incluirla) -> short de d a T y long de T a T + `post_minutes`; si < percentil `pct_lo` -> al reves. Una tasa
    mas vieja que `max_age_hours` = hueco (sin senal). Solo decisiones dentro del rango de velas disponible."""
    out: Signals = {}
    pre, post = pd.Timedelta(minutes=p["pre_minutes"]), pd.Timedelta(minutes=p["post_minutes"])
    for sym, md in data.items():
        k = md.klines
        if sym not in p["symbols"] or md.funding is None or not len(md.funding) or not len(k):
            out[sym] = _changes({})
            continue
        f = md.funding.sort_index()
        hi_thr = F.rolling_percentile(f, p["pct_window"], p["pct_hi"])
        lo_thr = F.rolling_percentile(f, p["pct_window"], p["pct_lo"])
        state = pd.Series(0.0, index=f.index)
        state[f > hi_thr] = 1.0
        state[f < lo_thr] = -1.0
        lo, hi = k.index[0], k.index[-1] + MIN
        ts = pd.date_range(lo.floor("1D"), hi.floor("1D") + DAY, freq="1D")
        settle = [d + pd.Timedelta(hours=h) for d in ts for h in p["settle_hours"]]
        decisions = [T - pre for T in settle if lo <= T - pre <= hi]
        rows: dict[pd.Timestamp, float] = {}
        if decisions:
            idx = pd.DatetimeIndex(decisions)
            known = asof(idx, state.rename("s"), tolerance=pd.Timedelta(hours=p["max_age_hours"]))
            for d, s in zip(idx, known.to_numpy()):
                if not np.isfinite(s) or s == 0:
                    continue
                T = d + pre
                rows[d], rows[T], rows[T + post] = -s, s, 0.0
        out[sym] = _changes(rows)
    return out


ALTS = [s for s in SEVEN if s != "BTCUSDT"]

CATALOG.update({
    "tsmom-diario-v1": (Spec(
        "tsmom-diario-v1", "continuacion",
        "Momentum de serie de tiempo. Decision diaria a las 00:00 UTC con velas 1h: posicion = signo del "
        "retorno de 7 dias (de t-169h a t-1h: se salta la ultima hora) * (vol objetivo / vol realizada de 30 "
        "dias con retornos 1h, anualizada), tope 1x. Solo se rebalancea si cambia el signo o si el peso cambia "
        "mas de 20% relativo al actual. Long y short, los 7 simbolos.",
        "1h", "both", "dias",
        {"symbols": list(SEVEN), "lookback_hours": 168, "skip_hours": 1, "vol_window_hours": 720,
         "vol_min_frac": 0.9, "target_vol": 0.15, "max_leverage": 1.0, "rebalance_threshold": 0.20},
        grid={"target_vol": [0.15, 0.25]},
        source="ML4T H7 (research/ml4t-metodologia.md: signo del retorno 7d x sigma objetivo / RV 30d, diario; "
               "riesgo MEDIO); Moskowitz, Ooi & Pedersen 2012 (JFE, Time series momentum); "
               "catalogo-hipotesis.md: momentum de serie temporal con respaldo academico"), tsmom_diario),
    "low-vol-xs-v1": (Spec(
        "low-vol-xs-v1", "cross-sectional",
        "Diario a las 00:00 UTC: volatilidad realizada de 7 dias (retornos 1h) de cada simbolo; long las 2 de "
        "menor volatilidad y short las 2 de mayor, mismo nocional (dolar-neutral, NO beta-neutral). Tenencia "
        "24 h: se re-arma cada dia y solo se opera lo que cambia. Minimo 5 simbolos con dato; si no, plano.",
        "1h", "neutral", "24h",
        {"rv_hours": 168, "rv_min_frac": 0.9, "n_side": 2, "min_symbols": 5},
        source="ML4T H2 (research/ml4t-metodologia.md: long las 2 de menor RV 7d, short las 2 de mayor, 24 h; "
               "riesgo MEDIO-ALTO, puede ser beta disfrazada); anomalia de baja volatilidad: Ang, Hodrick, Xing & "
               "Zhang 2006 (JF, acciones)"), low_vol_xs),
    "pares-eth-btc-v1": (Spec(
        "pares-eth-btc-v1", "reversion",
        "Pares ETH/BTC con velas 1h: spread = log(ETH) - beta*log(BTC), beta por OLS de las 720 velas anteriores "
        "(30 dias), z-score del spread con la misma ventana. z > +2 -> short ETH / long BTC por beta; z < -2 "
        "-> long ETH / short BTC; beta fija al entrar, ninguna pata > 1x. Salida con |z| < 0.5, stop con |z| > "
        "4 o a las 72 h. Tras stop o salida por tiempo, no re-entra hasta |z| < umbral. Solo ETH y BTC.",
        "1h", "neutral", "<=72h",
        {"leg_y": "ETHUSDT", "leg_x": "BTCUSDT", "window_hours": 720, "entry_z": 2.0, "exit_z": 0.5,
         "stop_z": 4.0, "max_hold_hours": 72},
        grid={"entry_z": [2.0, 2.5]},
        source="propuesta del usuario (lote 3), no esta en catalogo-hipotesis.md; pairs trading: Gatev, "
               "Goetzmann & Rouwenhorst 2006 (RFS); version con hedge OLS rolling y z-score del residuo"),
        pares_eth_btc),
    "rango-reversion-v1": (Spec(
        "rango-reversion-v1", "reversion",
        "Velas 1h, solo BNB, TRX y XLM. Regimen rango = ADX(14) < 20. En rango: close bajo la banda inferior "
        "de Bollinger(20, 2) -> long; sobre la superior -> short. Take profit en la EMA20 del momento de "
        "entrada (nivel fijo), stop a 1.5 ATR(14) del close de entrada, salida a las 12 h.",
        "1h", "both", "<=12h",
        {"symbols": ["BNBUSDT", "TRXUSDT", "XLMUSDT"], "adx_n": 14, "adx_max": 20.0, "bb_n": 20, "bb_k": 2.0,
         "ema_n": 20, "atr_n": 14, "sl_atr": 1.5, "hold_hours": 12},
        source="propuesta del usuario (lote 3), no esta en catalogo-hipotesis.md; Bollinger (bandas 20, 2) y "
               "Wilder 1978 (ADX, ATR); sin paper academico"), rango_reversion),
    "reversion-xs-1h-v1": (Spec(
        "reversion-xs-1h-v1", "reversion",
        "Velas 1h, las 6 alts contra BTC. Residual e = ret_1h(alt) - beta*ret_1h(BTC), beta rolling de las 720 "
        "velas anteriores (30 dias). Si e < -2 sigma(e) (sigma trailing 30 dias) -> long alt y short BTC por "
        "beta; si e > +2 sigma -> al reves. Salida a las 4 h. La pata BTC suma las coberturas abiertas.",
        "1h", "neutral", "4h",
        {"symbols": list(ALTS), "btc": "BTCUSDT", "window_hours": 720, "k_sigma": 2.0, "hold_hours": 4},
        source="catalogo-hipotesis.md 1.2 (reversion-xs-1h-v1: residual de la alt contra BTC > 2 sigma -> "
               "revierte en 4 h; riesgo ALTO, la evidencia en monedas grandes va en contra)"), reversion_xs_1h),
    "funding-prepago-v1": (Spec(
        "funding-prepago-v1", "carry",
        "Alrededor de cada liquidacion T = 00/08/16 UTC. En T-60 min se mira la ultima tasa YA liquidada "
        "(la de T-8 h; la de T todavia no se conoce). Si > p90 de las 270 liquidaciones anteriores (90 dias): "
        "short de T-60 a T y long de T a T+60. Si < p10: long de T-60 a T y short de T a T+60. Los 7 simbolos.",
        "5min", "both", "2h",
        {"symbols": list(SEVEN), "settle_hours": [0, 8, 16], "pre_minutes": 60, "post_minutes": 60,
         "pct_hi": 0.90, "pct_lo": 0.10, "pct_window": 270, "max_age_hours": 9},
        needs=("klines", "funding"),
        source="catalogo-hipotesis.md 3.3 (funding-prepago-v1: posicionarse alrededor de la hora de "
               "liquidacion del funding; riesgo ALTO, especulativa); sin paper academico"), funding_prepago),
})
LOTE3 = tuple(k for k in CATALOG if k not in LOTE1 and k not in LOTE2)


# =====================================================================================================
# Lote 4 (propuesta del usuario 2026-10-18): cruce de EMAs 9/21 con filtro EMA200 + volumen, stop/objetivo
# por ATR y tamano por riesgo; y la misma con filtros antirruido (ADX/DMI, RSI, ancho de Bollinger).
# =====================================================================================================


def _risk_weight(close: pd.Series, stop_dist: pd.Series, risk: float, cap: float) -> pd.Series:
    """Fraccion del capital del simbolo tal que tocar el stop cueste `risk` de ese capital:
    Q = capital*risk/|entrada - stop| -> w = Q*precio/capital = risk*precio/|entrada - stop|. Tope `cap` (1 = sin
    apalancamiento). El precio de entrada para el calculo es el close de la vela de la senal."""
    return (risk * close / stop_dist.where(stop_dist > 0)).clip(upper=cap)


def ema_cruce(data: dict[str, MarketData], p: dict[str, Any]) -> Signals:
    """Velas `timeframe`, los `symbols`. Entrada al CIERRE de la vela en que EMA(fast) cruza a EMA(slow)
    (cruce en esa vela: antes <=, ahora >), con close del lado de la EMA(macro) y volumen > SMA(vol_n) del
    volumen. Con `filters`: ademas ADX(adx_n) >= adx_min y DI a favor (+DI > -DI long; -DI > +DI short), RSI(rsi_n)
    en (50, rsi_hi) long / (rsi_lo, 50) short, y ancho de Bollinger(bb_n, 2) > su SMA(bb_n).
    Stop a sl_atr*ATR(atr_n) del close de entrada, objetivo a rr*sl_atr*ATR. Sin otra salida que stop/objetivo
    salvo el tope de seguridad `max_hold_hours`. Una posicion por simbolo a la vez (las senales con posicion
    abierta se ignoran). Tamano por riesgo: `risk` del capital del simbolo hasta el stop, tope `max_weight`.
    TP/SL evaluados en velas 1m (`exits_with_levels`: empate -> stop)."""
    out: Signals = {}
    tf = p["timeframe"]
    for sym, md in data.items():
        bars = resample_bars(md.klines, tf) if sym in p["symbols"] else pd.DataFrame()
        if bars.empty:
            out[sym] = _changes({})
            continue
        close, vol = bars["close"], bars["volume"]
        ema = {k: close.ewm(span=p[k], adjust=False, min_periods=p[k]).mean() for k in ("ema_fast", "ema_slow", "ema_macro")}
        diff = ema["ema_fast"] - ema["ema_slow"]
        up = (diff > 0) & (diff.shift(1) <= 0)
        dn = (diff < 0) & (diff.shift(1) >= 0)
        vol_ok = vol > vol.rolling(p["vol_n"], min_periods=p["vol_n"]).mean()
        long_ok = up & (close > ema["ema_macro"]) & vol_ok
        short_ok = dn & (close < ema["ema_macro"]) & vol_ok
        if p["filters"]:
            d = F.dmi(bars, p["adx_n"])
            r = F.rsi(close, p["rsi_n"])
            w = F.bb_width(close, p["bb_n"])
            strong = (d["adx"] >= p["adx_min"]) & (w > w.rolling(p["bb_n"], min_periods=p["bb_n"]).mean())
            long_ok &= strong & (d["pdi"] > d["mdi"]) & (r > 50) & (r < p["rsi_hi"])
            short_ok &= strong & (d["mdi"] > d["pdi"]) & (r < 50) & (r > p["rsi_lo"])
        atr = F.atr(bars, p["atr_n"])
        entries = pd.Series(0.0, index=bars.index)
        entries[long_ok.fillna(False)] = 1.0
        entries[short_ok.fillna(False)] = -1.0
        entries[atr.isna()] = 0.0
        dist = p["sl_atr"] * atr
        sl = close - entries * dist
        tp = close + entries * p["rr"] * dist
        ev = exits_with_levels(entries, md.klines, pd.Timedelta(hours=p["max_hold_hours"]), tp=tp, sl=sl)
        size = _risk_weight(close, dist, p["risk"], p["max_weight"])
        out[sym] = ev * size.reindex(ev.index).fillna(1.0).where(ev != 0, 1.0)
    return out


_EMA_BASE = {"symbols": list(SEVEN), "timeframe": "1h", "ema_fast": 9, "ema_slow": 21, "ema_macro": 200,
             "vol_n": 20, "atr_n": 14, "sl_atr": 1.5, "rr": 2.0, "risk": 0.01, "max_weight": 1.0,
             "max_hold_hours": 720}
CATALOG.update({
    "ema-cruce-v1": (Spec(
        "ema-cruce-v1", "continuacion",
        "Velas 1h o 4h (grilla), los 7. Long cuando EMA9 cruza arriba a EMA21 en la vela, con close > EMA200 y "
        "volumen > media de 20; short al reves. Stop a 1,5 ATR(14) del close de entrada, objetivo a 3 ATR "
        "(R:R 1:2), sin otra salida (tope de seguridad 30 dias). Tamano: 1% del capital del simbolo en riesgo "
        "hasta el stop, sin apalancamiento.",
        "1h|4h", "both", "hasta stop/objetivo",
        {**_EMA_BASE, "filters": False},
        grid={"timeframe": ["1h", "4h"]},
        source="propuesta del usuario (lote 4, 2026-10-18): cruce EMA 9/21 + EMA200 + volumen, SL/TP por ATR, "
               "tamano por riesgo; seguimiento de tendencia clasico, sin paper academico"), ema_cruce),
    "ema-cruce-filtrado-v1": (Spec(
        "ema-cruce-filtrado-v1", "continuacion",
        "Igual que ema-cruce-v1 y ademas, en la vela del cruce: ADX(14) >= 25 con +DI > -DI (long) o -DI > +DI "
        "(short), RSI(14) entre 50 y 70 (long) o entre 30 y 50 (short), y ancho de Bollinger(20, 2) mayor que "
        "su media de 20 velas.",
        "1h|4h", "both", "hasta stop/objetivo",
        {**_EMA_BASE, "filters": True, "adx_n": 14, "adx_min": 25.0, "rsi_n": 14, "rsi_lo": 30.0, "rsi_hi": 70.0,
         "bb_n": 20},
        grid={"timeframe": ["1h", "4h"]},
        source="propuesta del usuario (lote 4, 2026-10-18): filtros antirruido sobre ema-cruce-v1 (Wilder 1978 "
               "ADX/DMI y RSI; Bollinger); sin paper academico"), ema_cruce),
})
LOTE4 = tuple(k for k in CATALOG if k not in LOTE1 and k not in LOTE2 and k not in LOTE3)


# =====================================================================================================
# Lote 5 (2026-10-18): de la base de conocimiento que paso el usuario, lo que se acordo usar.
# =====================================================================================================


def _combine(signals: list[Signals], weights: list[float]) -> Signals:
    """Suma ponderada de senales esparsas (cambios de posicion) por simbolo: cada una se lleva a la union de
    instantes con forward-fill y se suma. Sin renormalizar: con pesos que suman 1 la posicion queda en [-1, 1]."""
    out: Signals = {}
    for sym in sorted(set().union(*(s.keys() for s in signals))):
        parts = [s.get(sym, _changes({})) for s in signals]
        idx = pd.DatetimeIndex(sorted(set().union(*(p.index for p in parts))))
        if idx.empty:
            out[sym] = _changes({})
            continue
        total = sum(w * p.reindex(idx.union(p.index)).ffill().reindex(idx).fillna(0.0) for p, w in zip(parts, weights))
        out[sym] = total[total.diff().fillna(total) != 0]
    return out


def cartera_tendencia(data: dict[str, MarketData], p: dict[str, Any]) -> Signals:
    """Cartera de las 3 de tendencia mejor rankeadas del programa (donchian-ensemble-v1, tsmom-diario-v1,
    ema-cruce-v1), cada una con sus parametros congelados POR DEFECTO de la spec (no los que eligio el
    walk-forward: elegirlos mirando resultados seria ajustar) y un tercio del capital de cada simbolo."""
    sigs, ws = [], []
    for sid, w in p["components"].items():
        spec, rule = CATALOG[sid]
        sigs.append(rule(data, dict(spec.params)))
        ws.append(w)
    return _combine(sigs, ws)


def max_ruptura_10d(data: dict[str, MarketData], p: dict[str, Any]) -> Signals:
    """Velas diarias (cierre 00:00 UTC). Si el close del dia es el maximo de los ultimos `lookback_days`
    closes (incluido el de hoy) -> long por `hold_days`. Solo long. Una posicion por simbolo a la vez."""
    out: Signals = {}
    for sym, md in data.items():
        bars = resample_bars(md.klines, "1D")
        if bars.empty:
            out[sym] = _changes({})
            continue
        c = bars["close"]
        is_max = c >= c.rolling(p["lookback_days"], min_periods=p["lookback_days"]).max()
        entries = pd.Series(np.where(is_max, 1.0, 0.0), index=bars.index)
        out[sym] = hold_events(entries, pd.Timedelta(days=p["hold_days"]), bars.index)
    return out


def _vwap_day(k1m: pd.DataFrame) -> pd.Series:
    """VWAP anclado a las 00:00 UTC, con precio tipico (h+l+c)/3 de las velas 1m, indexado al CIERRE de cada vela
    1m (cuando se conoce). Se reinicia cada dia UTC."""
    tp = (k1m["high"] + k1m["low"] + k1m["close"]) / 3
    v = k1m["volume"]
    day = k1m.index.floor("D")
    vwap = (tp * v).groupby(day).cumsum() / v.groupby(day).cumsum().where(lambda x: x > 0)
    vwap.index = k1m.index + MIN
    return vwap


def vwap_reversion(data: dict[str, MarketData], p: dict[str, Any]) -> Signals:
    """Velas 15m. d = (close - VWAP del dia UTC) / ATR(atr_n) de 15m. d < -k -> long; d > +k -> short; solo
    desde `min_minutes` despues de las 00:00 UTC (VWAP con pocas velas no es referencia). Objetivo: el VWAP del
    momento de entrada (nivel fijo); stop a sl_atr*ATR mas alla del close; salida a las `hold_hours`.
    TP/SL en velas 1m (`exits_with_levels`: empate -> stop)."""
    out: Signals = {}
    for sym, md in data.items():
        bars = resample_bars(md.klines, "15min") if sym in p["symbols"] else pd.DataFrame()
        if bars.empty:
            out[sym] = _changes({})
            continue
        close = bars["close"]
        vwap = _vwap_day(md.klines).reindex(bars.index)
        atr = F.atr(bars, p["atr_n"])
        d = (close - vwap) / atr
        mins = (bars.index - (bars.index - MIN).floor("D")) / MIN  # minutos desde el ancla del dia de la vela
        ok = (mins >= p["min_minutes"]) & d.notna()
        entries = pd.Series(0.0, index=bars.index)
        entries[ok & (d < -p["k_atr"])] = 1.0
        entries[ok & (d > p["k_atr"])] = -1.0
        sl = close - entries * p["sl_atr"] * atr
        out[sym] = exits_with_levels(entries, md.klines, pd.Timedelta(hours=p["hold_hours"]), tp=vwap, sl=sl)
    return out


CATALOG.update({
    "cartera-tendencia-v1": (Spec(
        "cartera-tendencia-v1", "cartera",
        "Cartera de las 3 de tendencia: donchian-ensemble-v1 (4h, target_vol 0,20), tsmom-diario-v1 (target_vol "
        "0,15) y ema-cruce-v1 (1h), con los parametros por defecto de cada spec congelada y un tercio del capital "
        "de cada simbolo para cada una. Los 7 simbolos.",
        "1h|4h", "both", "lo de cada componente",
        {"components": {"donchian-ensemble-v1": 1 / 3, "tsmom-diario-v1": 1 / 3, "ema-cruce-v1": 1 / 3}},
        source="base de conocimiento del usuario (2026-10-18): combinar estrategias poco correlacionadas suma "
               "muestra sin bajar calidad (estudio Rulyfi, DSR 0,88 -> 0,95); diversificacion entre reglas de "
               "tendencia"), cartera_tendencia),
    "max-ruptura-10d-v1": (Spec(
        "max-ruptura-10d-v1", "continuacion",
        "Velas diarias. Si el cierre del dia es el maximo de los ultimos 10 cierres, long por 1 dia. Solo long, "
        "los 7 simbolos.",
        "1D", "long", "1 dia",
        {"lookback_days": 10, "hold_days": 1},
        source="base de conocimiento del usuario (2026-10-18): Quantpedia, 'Revisiting Trend-following and "
               "Mean-reversion Strategies in Bitcoin' (pata MAX)"), max_ruptura_10d),
    "vwap-reversion-v1": (Spec(
        "vwap-reversion-v1", "reversion",
        "Velas 15m, los 7. Distancia al VWAP del dia UTC en ATR(14) de 15m: < -2,5 -> long, > +2,5 -> short, desde "
        "la 01:00 UTC. Objetivo en el VWAP del momento de entrada, stop a 1,5 ATR, salida a las 4 h.",
        "15m", "both", "<=4h",
        {"symbols": list(SEVEN), "k_atr": 2.5, "atr_n": 14, "sl_atr": 1.5, "hold_hours": 4, "min_minutes": 60},
        source="base de conocimiento del usuario (2026-10-18): mean reversion a VWAP de sesion (sin evidencia "
               "publicada citada: validar propio)"), vwap_reversion),
})
LOTE5 = tuple(k for k in CATALOG if k not in LOTE1 + LOTE2 + LOTE3 + LOTE4)


# =====================================================================================================
# Lote 6 (2026-10-20): exploracion masiva, 60 variantes con parametros FIJOS (sin grilla: 1 intento cada una).
# Especificacion exacta: .claude/context/estrategias-lote6-exploratorio.md. Las reglas y specs anteriores NO
# cambian. Reloj (`lab/clock.py`): velas diarias fechadas al cierre (00:00 UTC) y decision en ese cierre; metrics
# disponibles en ts + 5 min; funding en la liquidacion; ratio taker = klines de la ultima hora CERRADA; sesiones
# con DST via zoneinfo. Cada variante opera los simbolos que reciba (por defecto los 7 de `run.py`).
# =====================================================================================================

from datetime import datetime as _dt, time as _time, timedelta as _td  # noqa: E402
from zoneinfo import ZoneInfo  # noqa: E402

_L6_SOURCE = "lote 6 exploratorio (estrategias-lote6-exploratorio.md, aprobado por el usuario 2026-10-20)"
_SIDE_DIR = {"long": "long", "ambos": "both", "short": "short"}


def _daily(md: MarketData) -> pd.DataFrame:
    return resample_bars(md.klines, "1D")


def _breakout_entries(close: pd.Series, n: int, side: str) -> pd.Series:
    """Cierre = maximo de los ultimos n cierres (incluido el de hoy) -> +1; con side 'ambos', cierre = minimo -> -1.
    Si se cumplen las dos (n cierres iguales) no hay senal."""
    hi = close >= close.rolling(n, min_periods=n).max()
    lo = (close <= close.rolling(n, min_periods=n).min()) if side == "ambos" else pd.Series(False, index=close.index)
    e = pd.Series(0.0, index=close.index)
    e[hi & ~lo] = 1.0
    e[lo & ~hi] = -1.0
    return e


def l6_ruptura(data: dict[str, MarketData], p: dict[str, Any]) -> Signals:
    """Velas diarias. Ruptura del maximo (y del minimo en 'ambos') de `lookback_days` cierres -> `hold_days` dias."""
    out: Signals = {}
    for sym, md in data.items():
        bars = _daily(md)
        if bars.empty:
            out[sym] = _changes({})
            continue
        e = _breakout_entries(bars["close"], p["lookback_days"], p["side"])
        out[sym] = hold_events(e, pd.Timedelta(days=p["hold_days"]), bars.index)
    return out


def l6_tsmom(data: dict[str, MarketData], p: dict[str, Any]) -> Signals:
    """Decision diaria al cierre de la vela diaria (00:00 UTC): signo de log(close_t / close_{t-L}). Tamano 'fijo'
    = 1; 'vol' = min(target_vol / vol, max_leverage) con vol = desvio de los retornos diarios de `vol_window_days`
    dias anualizado con sqrt(365). En 'long' el signo negativo da posicion 0. Se mantiene hasta la proxima
    decision (se emite un cambio solo si el objetivo cambia). Sin dato del dia (hueco, vol indefinida): se
    mantiene la posicion."""
    out: Signals = {}
    for sym, md in data.items():
        bars = _daily(md)
        if bars.empty:
            out[sym] = _changes({})
            continue
        c = bars["close"]
        mom = np.log(c / c.shift(p["lookback_days"]))
        if p["sizing"] == "vol":
            vol = F.realized_vol(c, p["vol_window_days"]) * np.sqrt(365)
            size = np.minimum(p["target_vol"] / vol.where(vol > 0), p["max_leverage"])
        else:
            size = pd.Series(1.0, index=c.index)
        rows: dict[pd.Timestamp, float] = {}
        pos = 0.0
        for t in c.index:
            m, s = mom[t], size[t]
            if not (np.isfinite(m) and np.isfinite(s)):
                continue
            w = float(np.sign(m) * s)
            if p["side"] == "long":
                w = max(w, 0.0)
            if abs(w - pos) > 1e-12:  # ruido de punto flotante no es un cambio de objetivo
                rows[t] = w
                pos = w
        out[sym] = _changes(rows)
    return out


def _calendar_starts(k: pd.DataFrame, p: dict[str, Any]) -> pd.Series:
    """Instantes UTC de entrada dentro del rango de datos [primera apertura, ultimo cierre]. Con `weekday` (0 = lunes):
    las 00:00 UTC de ese dia. Con `tz`/`local_time`/`weekdays`: la hora local de cada dia habil local, pasada a UTC
    con zoneinfo (DST incluido)."""
    lo, hi = k.index[0], k.index[-1] + MIN
    d0, d1 = (lo - DAY * 2).date(), (hi + DAY).date()
    starts = []
    d = d0
    while d <= d1:
        if "weekday" in p:
            t = pd.Timestamp(d, tz="UTC") if d.weekday() == p["weekday"] else None
        elif d.weekday() in p["weekdays"]:
            hh, mm = map(int, p["local_time"].split(":"))
            t = pd.Timestamp(_dt.combine(d, _time(hh, mm), tzinfo=ZoneInfo(p["tz"]))).tz_convert("UTC")
        else:
            t = None
        if t is not None and lo <= t <= hi:
            starts.append(t)
        d += _td(days=1)
    return pd.Series(1.0, index=pd.DatetimeIndex(starts, tz="UTC") if not starts else pd.DatetimeIndex(starts), dtype=float)


def l6_calendario(data: dict[str, MarketData], p: dict[str, Any]) -> Signals:
    """Long `hold_hours` desde cada instante de calendario (no usa precios)."""
    out: Signals = {}
    for sym, md in data.items():
        if not len(md.klines):
            out[sym] = _changes({})
            continue
        out[sym] = hold_events(_calendar_starts(md.klines, p), pd.Timedelta(hours=p["hold_hours"]), md.klines.index)
    return out


def _funding_z_time(f: pd.Series, window: str) -> pd.Series:
    """z-score de cada tasa liquidada contra las liquidadas en la ventana temporal (t - window, t], incluida la
    actual. Solo definido cuando ya hay `window` completo de historia (desde la primera liquidacion conocida)."""
    f = f.dropna().sort_index()
    if f.empty:
        return f
    roll = f.rolling(window, min_periods=3)
    z = (f - roll.mean()) / roll.std().where(lambda s: s > 0)
    return z[f.index >= f.index[0] + pd.Timedelta(window)]


def l6_der_funding_z(data: dict[str, MarketData], p: dict[str, Any]) -> Signals:
    """En cada liquidacion: z de funding (30 d) > +z -> short; < -z -> long; mantener `hold_hours`."""
    out: Signals = {}
    for sym, md in data.items():
        if md.funding is None or not len(md.funding):
            out[sym] = _changes({})
            continue
        z = _funding_z_time(md.funding, p["z_window"])
        e = pd.Series(0.0, index=z.index)
        e[z > p["z"]] = -1.0
        e[z < -p["z"]] = 1.0
        out[sym] = hold_events(e, pd.Timedelta(hours=p["hold_hours"]), md.klines.index)
    return out


def l6_der_oi_funding(data: dict[str, MarketData], p: dict[str, Any]) -> Signals:
    """Decision diaria a las 00:00 UTC (cierre de la vela diaria). OI en CONTRATOS ahora vs hace 24 h (metrics
    disponibles en ts + 5 min; un dato mas viejo que `oi_max_age` = hueco, sin senal) < 0 y suma de las tasas de
    funding liquidadas en (t - 24 h, t] < 0 -> long `hold_days`."""
    out: Signals = {}
    for sym, md in data.items():
        bars = _daily(md)
        if md.metrics is None or md.funding is None or bars.empty or not len(md.metrics):
            out[sym] = _changes({})
            continue
        idx = bars.index
        m = available_metrics(md.metrics)["oi_contracts"].dropna()
        tol = pd.Timedelta(p["oi_max_age"])
        win = pd.Timedelta(hours=p["window_hours"])
        oi_now = asof(idx, m, tolerance=tol)
        oi_then = pd.Series(asof(idx - win, m, tolerance=tol).to_numpy(), index=idx)
        d_oi = oi_now / oi_then - 1
        f = md.funding.dropna().sort_index()
        cum = f.cumsum()
        c_now = asof(idx, cum.rename("c")).fillna(0.0)
        c_then = pd.Series(asof(idx - win, cum.rename("c")).fillna(0.0).to_numpy(), index=idx)
        n_now = asof(idx, pd.Series(np.arange(1, len(f) + 1), index=f.index, name="n")).fillna(0)
        n_then = pd.Series(asof(idx - win, pd.Series(np.arange(1, len(f) + 1), index=f.index, name="n")).fillna(0).to_numpy(),
                           index=idx)
        f_sum = (c_now - c_then).where(n_now - n_then > 0)  # sin liquidaciones en la ventana -> sin dato
        e = pd.Series(0.0, index=idx)
        e[(d_oi < 0) & (f_sum < 0)] = 1.0
        out[sym] = hold_events(e, pd.Timedelta(days=p["hold_days"]), idx)
    return out


def l6_der_taker(data: dict[str, MarketData], p: dict[str, Any]) -> Signals:
    """Velas 1h (fechadas al cierre). Desbalance taker de la ultima hora cerrada (klines) >= percentil `pct_hi` de
    las `pct_window_hours` horas anteriores -> short; <= percentil `pct_lo` -> long; mantener `hold_hours`."""
    out: Signals = {}
    for sym, md in data.items():
        hour = resample_bars(md.klines, "1h")
        if hour.empty:
            out[sym] = _changes({})
            continue
        imb = F.taker_imbalance(hour)
        w = p["pct_window_hours"]
        hi = F.rolling_percentile(imb, w, p["pct_hi"])
        lo = F.rolling_percentile(imb, w, p["pct_lo"])
        e = pd.Series(0.0, index=hour.index)
        e[imb >= hi] = -1.0
        e[(imb <= lo) & ~(imb >= hi)] = 1.0
        out[sym] = hold_events(e, pd.Timedelta(hours=p["hold_hours"]), hour.index)
    return out


def l6_rsi2(data: dict[str, MarketData], p: dict[str, Any]) -> Signals:
    """Velas diarias. RSI(2) de Wilder < `lo` -> long (side long) / > `hi` -> short (side short); `hold_days`."""
    out: Signals = {}
    for sym, md in data.items():
        bars = _daily(md)
        if bars.empty:
            out[sym] = _changes({})
            continue
        r = F.rsi(bars["close"], p["rsi_n"])
        e = pd.Series(0.0, index=bars.index)
        if p["side"] == "long":
            e[r < p["lo"]] = 1.0
        else:
            e[r > p["hi"]] = -1.0
        out[sym] = hold_events(e, pd.Timedelta(days=p["hold_days"]), bars.index)
    return out


def l6_regimen(data: dict[str, MarketData], p: dict[str, Any]) -> Signals:
    """Ruptura de `lookback_days` cierres mantenida `hold_days`, solo si el filtro de regimen del mismo cierre pasa:
    'adx': ADX(adx_n) diario > adx_min; 'atrp': ATR(atr_n)/close < mediana (percentil `atrp_pct`) de los
    `atrp_window_days` dias ANTERIORES."""
    out: Signals = {}
    for sym, md in data.items():
        bars = _daily(md)
        if bars.empty:
            out[sym] = _changes({})
            continue
        e = _breakout_entries(bars["close"], p["lookback_days"], p["side"])
        if p["filter"] == "adx":
            ok = F.adx(bars, p["adx_n"]) > p["adx_min"]
        else:
            atrp = F.atr(bars, p["atr_n"]) / bars["close"]
            ok = atrp < F.rolling_percentile(atrp, p["atrp_window_days"], p["atrp_pct"])
        e = e.where(ok, 0.0)
        out[sym] = hold_events(e, pd.Timedelta(days=p["hold_days"]), bars.index)
    return out


def _l6_specs() -> dict[str, tuple[Spec, Rule]]:
    specs: dict[str, tuple[Spec, Rule]] = {}

    def add(sid: str, family: str, desc: str, tf: str, direction: str, horizon: str, params: dict[str, Any],
            rule: Rule, needs: tuple[str, ...] = ("klines",)) -> None:
        specs[sid] = (Spec(sid, family, desc, tf, direction, horizon, params, grid={}, needs=needs,
                           source=_L6_SOURCE), rule)

    for n in (5, 10, 20, 55):
        for h in (1, 3, 5):
            for side in ("long", "ambos"):
                add(f"l6-ruptura-n{n}-h{h}-{side}-v1", "ruptura-n",
                    f"Velas diarias: cierre = maximo de {n} dias -> long {h} d"
                    + (f"; cierre = minimo de {n} dias -> short {h} d." if side == "ambos" else "."),
                    "1D", _SIDE_DIR[side], f"{h} d", {"lookback_days": n, "hold_days": h, "side": side}, l6_ruptura)
    for lb in (7, 28, 84):
        for sizing in ("fijo", "vol"):
            for side in ("long", "ambos"):
                params: dict[str, Any] = {"lookback_days": lb, "sizing": sizing, "side": side}
                if sizing == "vol":
                    params.update({"target_vol": 0.20, "vol_window_days": 30, "max_leverage": 1.0})
                add(f"l6-tsmom-l{lb}-{sizing}-{side}-v1", "tsmom-n",
                    f"Signo del retorno de {lb} d, decision diaria 00:00 UTC, "
                    + ("tamano fijo 1x" if sizing == "fijo" else "vol-target 20 % anual (vol 30 d), tope 1x")
                    + (", solo long." if side == "long" else ", long y short."),
                    "1D", _SIDE_DIR[side], "hasta la proxima decision", params, l6_tsmom)
    for wd, name in enumerate(("lun", "mar", "mie", "jue", "vie", "sab", "dom")):
        add(f"l6-cal-{name}-v1", "calendario", f"Long 24 h desde las 00:00 UTC de cada {name}.",
            "calendario", "long", "24 h", {"weekday": wd, "hold_hours": 24}, l6_calendario)
    add("l6-cal-finde-v1", "calendario", "Long del viernes 00:00 al domingo 24:00 UTC (72 h).",
        "calendario", "long", "72 h", {"weekday": 4, "hold_hours": 72}, l6_calendario)
    for name, tz, hhmm in (("asia", "Asia/Tokyo", "09:00"), ("londres", "Europe/London", "08:00"),
                           ("ny", "America/New_York", "09:30")):
        add(f"l6-cal-{name}-v1", "calendario",
            f"Long 4 h desde la apertura {name} ({tz} {hhmm}, con DST), dias habiles locales (lun-vie).",
            "calendario", "long", "4 h", {"tz": tz, "local_time": hhmm, "weekdays": [0, 1, 2, 3, 4], "hold_hours": 4},
            l6_calendario)
    for h in (24, 72):
        add(f"l6-der-funding-z2-h{h}-v1", "derivados",
            f"En cada liquidacion: z-score del funding (30 d) > +2 -> short, < -2 -> long, mantener {h} h.",
            "liquidacion", "both", f"{h} h", {"z_window": "30D", "z": 2.0, "hold_hours": h}, l6_der_funding_z,
            needs=("klines", "funding"))
    for d in (3, 7):
        add(f"l6-der-oibaja-fneg-d{d}-v1", "derivados",
            f"Diario 00:00 UTC: OI en contratos 24 h < 0 y funding liquidado 24 h < 0 -> long {d} d.",
            "1D", "long", f"{d} d", {"window_hours": 24, "oi_max_age": "30min", "hold_days": d}, l6_der_oi_funding,
            needs=("klines", "funding", "metrics"))
    add("l6-der-taker-p95-h4-v1", "derivados",
        "Desbalance taker (klines) de la ultima hora cerrada >= p95 de 30 d -> short; <= p5 -> long; mantener 4 h.",
        "1h", "both", "4 h", {"pct_window_hours": 720, "pct_hi": 0.95, "pct_lo": 0.05, "hold_hours": 4}, l6_der_taker)
    for side in ("long", "short"):
        for h in (1, 3):
            add(f"l6-rsi2-{side}-h{h}-v1", "reversion-diaria",
                f"RSI(2) diario {'< 10 -> long' if side == 'long' else '> 90 -> short'}, mantener {h} d.",
                "1D", side, f"{h} d", {"rsi_n": 2, "lo": 10, "hi": 90, "side": side, "hold_days": h}, l6_rsi2)
    for filt, tag in (("adx", "adx25"), ("atrp", "atrp50")):
        for side in ("long", "ambos"):
            params = {"lookback_days": 20, "hold_days": 5, "side": side, "filter": filt}
            params.update({"adx_n": 14, "adx_min": 25} if filt == "adx" else
                          {"atr_n": 14, "atrp_window_days": 365, "atrp_pct": 0.50})
            add(f"l6-reg-{tag}-{side}-v1", "regimen",
                "Ruptura de 20 d mantenida 5 d"
                + (" con ADX(14) diario > 25" if filt == "adx" else " con ATR(14)/precio < percentil 50 de 1 anio")
                + (", solo long." if side == "long" else ", long y short."),
                "1D", _SIDE_DIR[side], "5 d", params, l6_regimen)
    return specs


CATALOG.update(_l6_specs())
LOTE6 = tuple(k for k in CATALOG if k.startswith("l6-"))
