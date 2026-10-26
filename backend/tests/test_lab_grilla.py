"""Lote 7, grilla long con stop por compra (lab/grilla.py): escalones, objetivo +1 escalon, stop por compra (piso 8 %),
empate -> stop, corrimiento solo sin lotes, filtro lateral, sin mirar el futuro."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from heatsignal.lab import sltp as SL
from heatsignal.lab.grilla import grid_symbol, grilla
from heatsignal.lab.strategies import CATALOG, LOTE7, MarketData

P = {"symbols": ["BTCUSDT"], "n_lots": 5, "interval": 0.04, "lateral": False, **SL.PARAMS}


def _k(path):
    """path: lista de (low, high, close) por vela 1m; open = close anterior."""
    lo, hi, cl = (np.array([x[i] for x in path], dtype=float) for i in range(3))
    op = np.r_[cl[0], cl[:-1]]
    idx = pd.date_range("2026-01-01", periods=len(path), freq="min", tz="UTC")
    return pd.DataFrame({"open": op, "high": hi, "low": lo, "close": cl, "volume": 1.0}, index=idx)


def _atr(k, v=0.5):  # ATR 1h constante y chico: stop en el piso de 8 %
    return pd.Series(v, index=k.index[:1])


def flat(n, p=100.0):
    return [(p * 0.9999, p * 1.0001, p)] * n


def test_buy_on_cross_then_target_one_step_up():
    L1 = 100 / 1.04
    k = _k(flat(5) + [(L1 * 0.999, 100.0, L1)] + flat(3, L1) + [(L1, 100.0, 99.9)] + flat(3, 99.9))
    s, trades = grid_symbol(k, _atr(k), None, P)
    t_buy, t_sell = k.index[5] + pd.Timedelta(minutes=1), k.index[9] + pd.Timedelta(minutes=1)
    assert list(s.items()) == [(t_buy, 0.2), (t_sell, 0.0)]
    assert trades[0]["price"] == pytest.approx(L1) and trades[0]["motivo"] == "objetivo"
    assert trades[0]["tp"] == pytest.approx(100.0) and trades[0]["sl"] == pytest.approx(L1 * 0.92)


def test_touch_is_not_a_fill_and_crash_hits_each_stop():
    L1, L2 = 100 / 1.04, 100 / 1.04 ** 2
    k = _k(flat(3) + [(L1, 99.0, 98.0)]          # toca L1 sin cruzarlo: no compra
           + [(L2 * 0.999, 98.0, L2)]            # cruza L1 y L2 en la misma vela: dos compras
           + flat(2, L2) + [(L2 * 0.80, L2, L2 * 0.81)] + flat(2, L2 * 0.81))
    s, trades = grid_symbol(k, _atr(k), None, P)
    # el derrumbe cierra L1 y L2 por stop y en la misma vela llena L3..L5 (cierres -> compras), que caen la siguiente;
    # L1 y L2 NO se recompran con el precio debajo (una limite no va por encima del precio)
    assert [round(v, 3) for v in s.to_numpy()] == [0.4, 0.6, 0.0]
    assert [t["motivo"] for t in trades] == ["stop"] * 5 and len(trades) == 5
    assert all(t["sl_frac"] == pytest.approx(0.08) for t in trades)
    # sesgo medido: en el derrumbe el simulador compra L3..L5 al open siguiente, mucho mas barato que el escalon
    crash = [t for t in trades if t["level"] >= 3]
    assert all(t["sesgo_llenado"] > 0.05 for t in crash)


def test_freed_level_rearms_only_after_close_above():
    L1 = 100 / 1.04
    k = _k(flat(3) + [(L1 * 0.999, 99.0, L1)] + [(L1 * 0.90, L1, L1 * 0.91)]   # compra L1 y stop
           + flat(3, L1 * 0.91) + [(L1 * 0.91, L1 * 1.01, L1 * 1.005)]        # vuelve arriba: se rearma
           + [(L1 * 0.998, L1 * 1.0, L1 * 0.999)] + flat(2, L1 * 0.999))       # cruza otra vez: compra
    s, trades = grid_symbol(k, _atr(k), None, P)
    # (la caida tambien llena L2 y L3: con escalones de 4 % y stop de 8 %, el stop de L1 queda debajo de L3)
    l1 = [t for t in trades if t["level"] == 1]
    assert [t["motivo"] for t in l1] == ["stop", None]
    assert l1[1]["entry"] == k.index[9] + pd.Timedelta(minutes=1)  # recompra recien despues del cierre por encima


def test_tie_is_stop_and_stop_uses_atr_with_cap():
    L1 = 100 / 1.04
    k = _k(flat(3) + [(L1 * 0.999, 99.0, L1)] + [(L1 * 0.5, 120.0, L1)] + flat(2, L1))
    s, trades = grid_symbol(k, _atr(k, v=30.0), None, P)     # 3 ATR = 90 % -> techo 20 %
    assert trades[0]["motivo"] == "stop" and trades[0]["sl_frac"] == pytest.approx(0.20)


def test_shift_up_only_without_open_lots():
    k = _k(flat(3) + [(100.0, 104.5, 104.0)] + flat(3, 104.0) + [(104 / 1.04 * 0.999, 104.0, 100.0)] + flat(2, 100.0))
    s, trades = grid_symbol(k, _atr(k), None, P)
    assert trades and trades[0]["price"] == pytest.approx(104.0 / 1.04)  # la escalera se corrio al cierre 104


def test_lateral_filter_blocks_new_buys():
    L1 = 100 / 1.04
    k = _k(flat(3) + [(L1 * 0.999, 100.0, L1)] + flat(3, L1))
    allowed = np.zeros(len(k), dtype=bool)
    s, trades = grid_symbol(k, _atr(k), allowed, P)
    assert len(s) == 0 and trades == []


def test_catalog_and_exposure_never_above_1x():
    for sid in LOTE7:
        spec = CATALOG[sid][0]
        assert spec.grid == {"interval": [0.02, 0.04]} and spec.params["n_lots"] == 5 and spec.backtestable
    rng = np.random.default_rng(1)
    n = 6 * 1440
    c = 100 * np.exp(np.cumsum(rng.normal(0, 0.002, n)))
    k = pd.DataFrame({"open": np.r_[100, c[:-1]], "high": c * 1.001, "low": c * 0.999, "close": c, "volume": 1.0},
                     index=pd.date_range("2026-01-01", periods=n, freq="min", tz="UTC"))
    out = grilla({"BTCUSDT": MarketData(k)}, {**CATALOG["grilla-long-v1"][0].params, "interval": 0.02})
    s = out["BTCUSDT"]
    assert len(s) > 5 and s.max() <= 1.0 + 1e-12 and s.min() >= 0.0
    # causal: con menos datos, las decisiones hasta el corte son las mismas
    cut = k.index[n // 2]
    part = grilla({"BTCUSDT": MarketData(k.loc[:cut])}, {**CATALOG["grilla-long-v1"][0].params, "interval": 0.02})["BTCUSDT"]
    a = s[s.index <= cut]
    assert a.equals(part[part.index <= cut])
