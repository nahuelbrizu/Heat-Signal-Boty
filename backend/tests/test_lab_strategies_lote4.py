"""Lote 4 (propuesta del usuario 2026-10-18): cruce de EMAs 9/21 + EMA200 + volumen, stop/objetivo por ATR y
tamano por riesgo; y la version con filtros ADX/DMI, RSI y ancho de Bollinger. Specs congeladas, reglas con datos
sinteticos e indicadores sin look-ahead (el test de look-ahead de las reglas recorre todo el CATALOG)."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from test_lab_strategies import _klines
from test_lab_strategies_lote2 import LOTE1_HASHES, LOTE2_HASHES
from test_lab_strategies_lote3 import LOTE3_HASHES, _hourly_bars

from heatsignal.lab import features as F
from heatsignal.lab.clock import assert_no_lookahead, resample_bars
from heatsignal.lab.strategies import CATALOG, LOTE1, LOTE2, LOTE3, LOTE4, MarketData, _risk_weight

# PROPUESTA (pendiente de aprobacion): si se cambia una spec antes de aprobarla, actualizar aca y en
# estrategias-lote4.md. Despues de aprobada, cualquier cambio es una -v2.
LOTE4_HASHES = {"ema-cruce-v1": "a950979de8e216af", "ema-cruce-filtrado-v1": "04d0e50a506fde17"}


def test_previous_lots_unchanged_and_lote4_frozen() -> None:
    assert {s: CATALOG[s][0].spec_hash() for s in LOTE1} == LOTE1_HASHES
    assert {s: CATALOG[s][0].spec_hash() for s in LOTE2} == LOTE2_HASHES
    assert {s: CATALOG[s][0].spec_hash() for s in LOTE3} == LOTE3_HASHES
    assert {s: CATALOG[s][0].spec_hash() for s in LOTE4} == LOTE4_HASHES
    total = 0
    for sid in LOTE4:
        spec = CATALOG[sid][0]
        assert spec.backtestable and spec.direction == "both" and spec.description and spec.source
        assert spec.params["timeframe"] in spec.grid["timeframe"]
        total += len(spec.grid["timeframe"])
    assert total == 4  # intentos del lote 4: el programa pasa de K = 44 a K = 48


def test_dmi_matches_adx_and_indicators_have_no_lookahead() -> None:
    k = _klines(60 * 24 * 12, seed=3, drift=0.00005)
    bars = resample_bars(k, "1h")
    pd.testing.assert_series_equal(F.dmi(bars, 14)["adx"], F.adx(bars, 14), check_names=False)
    assert_no_lookahead(lambda b: F.dmi(b, 14), bars)
    assert_no_lookahead(lambda b: F.rsi(b["close"], 14), bars)
    assert_no_lookahead(lambda b: F.bb_width(b["close"], 20), bars)


def test_rsi_bounds_and_extremes() -> None:
    idx = pd.date_range("2026-01-01", periods=60, freq="1h", tz="UTC")
    up = pd.Series(np.linspace(100, 160, 60), index=idx)
    assert F.rsi(up, 14).dropna().eq(100.0).all()  # sin perdidas
    assert F.rsi(up[::-1].set_axis(idx), 14).dropna().eq(0.0).all()  # sin ganancias
    r = F.rsi(_klines(60 * 24 * 5, seed=1).resample("1h").last()["close"], 14).dropna()
    assert ((r >= 0) & (r <= 100)).all() and r.between(30, 70).mean() > 0.5


def test_risk_weight_formula_and_cap() -> None:
    close = pd.Series([100.0, 100.0, 100.0])
    dist = pd.Series([4.0, 0.5, 0.0])  # stop a 4% -> 0,25 del capital; a 0,5% -> 2 (tope 1); 0 -> sin dato
    w = _risk_weight(close, dist, 0.01, 1.0)
    assert w.iloc[0] == pytest.approx(0.25) and w.iloc[1] == 1.0 and np.isnan(w.iloc[2])


def _cross_series(spike: bool) -> tuple[pd.DataFrame, pd.Timestamp]:
    """300 h de suba suave (precio muy sobre la EMA200), retroceso de 12 h (EMA9 < EMA21) y suba fuerte: la EMA9
    vuelve a cruzar arriba a la EMA21. Con `spike`, el volumen de la hora del cruce se triplica."""
    r = np.r_[np.full(300, 0.002), np.full(12, -0.004), np.full(30, 0.006)]
    k = _hourly_bars(r)
    bars = resample_bars(k, "1h")
    c = bars["close"]
    d = c.ewm(span=9, adjust=False, min_periods=9).mean() - c.ewm(span=21, adjust=False, min_periods=21).mean()
    t = d.index[(d > 0) & (d.shift(1) <= 0)][-1]
    if spike:
        k.loc[(k.index >= t - pd.Timedelta("1h")) & (k.index < t), "volume"] *= 3
    return k, t


def test_ema_cross_enters_at_cross_bar_with_volume_and_risk_size() -> None:
    spec, rule = CATALOG["ema-cruce-v1"]
    k, t = _cross_series(spike=True)
    s = rule({"BTCUSDT": MarketData(k)}, spec.params)["BTCUSDT"]
    entries = s[s != 0]
    assert t in entries.index and entries[t] > 0
    bars = resample_bars(k, "1h")
    w = min(1.0, 0.01 * bars["close"][t] / (1.5 * F.atr(bars, 14)[t]))
    assert entries[t] == pytest.approx(w)


def test_ema_cross_needs_volume_confirmation() -> None:
    spec, rule = CATALOG["ema-cruce-v1"]
    k, t = _cross_series(spike=False)  # volumen en base cae mientras sube el precio: < su media
    s = rule({"BTCUSDT": MarketData(k)}, spec.params)["BTCUSDT"]
    assert t not in s[s != 0].index


@pytest.mark.parametrize("tf", ["1h", "4h"])
def test_filtered_is_subset_of_base_and_respects_every_filter(tf) -> None:
    base_spec, rule = CATALOG["ema-cruce-v1"]
    filt_spec, _ = CATALOG["ema-cruce-filtrado-v1"]
    k = _klines(60 * 24 * (60 if tf == "1h" else 200), seed=11, drift=0.00002)
    data = {"ETHUSDT": MarketData(k)}
    base = rule(data, {**base_spec.params, "timeframe": tf})["ETHUSDT"]
    filt = rule(data, {**filt_spec.params, "timeframe": tf})["ETHUSDT"]
    eb, ef = base[base != 0], filt[filt != 0]
    assert len(eb) > 0
    bars = resample_bars(k, tf)
    dm, r = F.dmi(bars, 14), F.rsi(bars["close"], 14)
    w = F.bb_width(bars["close"], 20)
    wm = w.rolling(20, min_periods=20).mean()
    for t, d in ef.items():
        if t not in bars.index:
            continue  # tras una salida intrabar puede re-entrar recien en el cierre siguiente
        assert dm["adx"][t] >= 25 and w[t] > wm[t]
        assert (dm["pdi"][t] > dm["mdi"][t] and 50 < r[t] < 70) if d > 0 else (dm["mdi"][t] > dm["pdi"][t] and 30 < r[t] < 50)
    # cada posicion abierta se cierra (stop, objetivo o tope de seguridad) antes de volver a abrir
    for s in (base, filt):
        nz = (s != 0).astype(int)
        assert not ((nz == 1) & (nz.shift(1) == 1) & (np.sign(s) == np.sign(s.shift(1)))).any()


def test_stop_and_target_are_1_5_and_3_atr() -> None:
    spec, rule = CATALOG["ema-cruce-v1"]
    k, t = _cross_series(spike=True)
    bars = resample_bars(k, "1h")
    entry, a = bars["close"][t], F.atr(bars, 14)[t]
    for level, label in ((entry + 3.0 * a * 1.001, "tp"), (entry - 1.5 * a * 1.001, "sl")):
        k2 = k[k.index < t + pd.Timedelta("2h")].copy()
        after = k2.index >= t + pd.Timedelta("30min")
        k2.loc[after, ["open", "high", "low", "close"]] = level  # a la media hora toca el nivel
        k2.loc[(k2.index >= t) & ~after, ["open", "high", "low", "close"]] = entry
        s = rule({"BTCUSDT": MarketData(k2)}, spec.params)["BTCUSDT"]
        exit_t = s.index[s.index > t][0]
        assert s[exit_t] == 0.0 and exit_t == t + pd.Timedelta("31min"), label
