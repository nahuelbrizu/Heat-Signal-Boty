"""Lote 5 (2026-10-18, de la base de conocimiento del usuario): cartera de tendencia, ruptura del maximo de 10
dias y reversion al VWAP del dia. Specs congeladas y reglas con datos sinteticos (el look-ahead de las reglas lo
cubre el test general sobre todo el CATALOG)."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from test_lab_strategies import _klines
from test_lab_strategies_lote2 import LOTE1_HASHES, LOTE2_HASHES
from test_lab_strategies_lote3 import LOTE3_HASHES, _hourly_bars
from test_lab_strategies_lote4 import LOTE4_HASHES

from heatsignal.lab import features as F
from heatsignal.lab.clock import resample_bars
from heatsignal.lab.strategies import (
    CATALOG, LOTE1, LOTE2, LOTE3, LOTE4, LOTE5, MarketData, _vwap_day,
)

# PROPUESTA (pendiente de aprobacion). Despues de aprobada, cualquier cambio es una -v2.
LOTE5_HASHES = {"cartera-tendencia-v1": "3d7b32a429f494fc", "max-ruptura-10d-v1": "39cb9dee6d3e469a",
                "vwap-reversion-v1": "724e2b1e207828df"}


def _pos(s: pd.Series, t: pd.Timestamp) -> float:
    s = s[s.index <= t]
    return float(s.iloc[-1]) if len(s) else 0.0


def test_previous_lots_unchanged_and_lote5_frozen() -> None:
    for lot, hashes in ((LOTE1, LOTE1_HASHES), (LOTE2, LOTE2_HASHES), (LOTE3, LOTE3_HASHES), (LOTE4, LOTE4_HASHES),
                        (LOTE5, LOTE5_HASHES)):
        assert {s: CATALOG[s][0].spec_hash() for s in lot} == hashes
    assert all(not CATALOG[s][0].grid for s in LOTE5)  # 3 intentos: K del programa 48 -> 51


def test_cartera_is_one_third_of_each_component_at_any_time() -> None:
    spec, rule = CATALOG["cartera-tendencia-v1"]
    data = {"BTCUSDT": MarketData(_klines(60 * 24 * 90, seed=5, drift=0.00003)),
            "ETHUSDT": MarketData(_klines(60 * 24 * 90, seed=6, drift=-0.00002))}
    total = rule(data, spec.params)
    parts = {sid: CATALOG[sid][1](data, dict(CATALOG[sid][0].params)) for sid in spec.params["components"]}
    rng = np.random.default_rng(0)
    idx = data["BTCUSDT"].klines.index
    for t in idx[rng.integers(len(idx) // 2, len(idx), 40)]:
        for sym in data:
            want = sum(_pos(parts[sid][sym], t) / 3 for sid in parts)
            assert _pos(total[sym], t) == pytest.approx(want, abs=1e-12)
    assert all(total[s].abs().max() <= 1 + 1e-9 for s in data)


def test_max_breakout_long_one_day_only_on_10_day_high() -> None:
    spec, rule = CATALOG["max-ruptura-10d-v1"]
    r = np.r_[np.full(24 * 12, -0.0005), np.full(24, 0.004), np.full(24 * 6, -0.001)]  # cae, un dia fuerte, cae
    k = _hourly_bars(r)
    s = rule({"BTCUSDT": MarketData(k)}, spec.params)["BTCUSDT"]
    entries = s[s > 0]
    bars = resample_bars(k, "1D")
    c = bars["close"]
    assert (s >= 0).all() and len(entries) >= 1
    for t in entries.index:
        assert c[t] >= c[c.index <= t].tail(10).max() - 1e-12
        assert s[s.index > t].iloc[0] == 0.0 and s.index[s.index > t][0] == t + pd.Timedelta("1D")


def test_vwap_day_resets_and_is_known_at_candle_close() -> None:
    k = _klines(60 * 48, seed=2)
    v = _vwap_day(k)
    day2 = k.index >= pd.Timestamp("2026-01-02", tz="UTC") if k.index[0].year == 2026 else None
    first = k.index[0]
    tp = (k["high"] + k["low"] + k["close"]) / 3
    # primera vela del dia: VWAP = su precio tipico, conocido al cierre (open + 1m)
    assert v[first + pd.Timedelta("1min")] == pytest.approx(tp.iloc[0])
    nxt = first.floor("D") + pd.Timedelta("1D")
    assert v[nxt + pd.Timedelta("1min")] == pytest.approx(tp[nxt])  # se reinicia a las 00:00 UTC
    del day2


def test_vwap_reversion_entries_threshold_window_and_target() -> None:
    spec, rule = CATALOG["vwap-reversion-v1"]
    k = _klines(60 * 24 * 20, seed=9)
    s = rule({"SOLUSDT": MarketData(k)}, spec.params)["SOLUSDT"]
    bars = resample_bars(k, "15min")
    vwap = _vwap_day(k).reindex(bars.index)
    d = (bars["close"] - vwap) / F.atr(bars, 14)
    entries = s[(s != 0) & s.index.isin(bars.index)]
    assert len(entries) > 0
    for t, side in entries.items():
        assert ((t - pd.Timedelta("1min")).floor("D") + pd.Timedelta("60min")) <= t
        assert d[t] < -2.5 if side > 0 else d[t] > 2.5
        exit_t = s.index[s.index > t][0]
        assert exit_t <= t + pd.Timedelta("4h")
        # sale a 0, o sale y en ese mismo cierre de vela 15m entra una senal nueva (misma direccion o contraria)
        assert s[exit_t] == 0.0 or exit_t in bars.index
