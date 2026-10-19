"""Etapa 1 del meta-labeling (lab/meta/dataset.py): eventos, triple barrier y pesos por unicidad sin mirar el futuro."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from heatsignal.lab.meta import dataset as D
from heatsignal.lab.sim import simulate
from heatsignal.lab.strategies import CATALOG, MarketData

T0 = pd.Timestamp("2026-01-01", tz="UTC")
MIN = pd.Timedelta(minutes=1)


def _md(n_days: int, seed: int, drift: float = 0.0) -> MarketData:
    n = n_days * 1440
    rng = np.random.default_rng(seed)
    # tramos con tendencia para que las reglas de tendencia disparen
    mu = np.repeat(rng.choice([-1.0, 1.0], n // (1440 * 6) + 1) * 0.00002, 1440 * 6)[:n] + drift
    c = 100 * np.exp(np.cumsum(rng.normal(0, 0.0008, n) + mu))
    o = np.r_[100.0, c[:-1]]
    qv = rng.uniform(1e5, 2e5, n)
    k = pd.DataFrame({"open": o, "high": np.maximum(o, c) * 1.0002, "low": np.minimum(o, c) * 0.9998, "close": c,
                      "volume": qv / c * rng.uniform(0.5, 1.5, n), "quote_volume": qv},
                     index=pd.date_range(T0, periods=n, freq="1min"))
    f = pd.Series(rng.normal(0.0001, 0.0001, n_days * 3), index=pd.date_range(T0, periods=n_days * 3, freq="8h"))
    return MarketData(k, f)


def _flat(n_min: int, px: float = 100.0) -> pd.DataFrame:
    idx = pd.date_range(T0, periods=n_min, freq="1min")
    return pd.DataFrame({"open": px, "high": px * 1.0001, "low": px * 0.9999, "close": px}, index=idx)


def _arrays(k: pd.DataFrame, funding: pd.Series | None = None) -> D._Arrays:
    return D._Arrays(MarketData(k, funding))


@pytest.fixture(scope="module")
def synth():
    data = {"BTCUSDT": _md(90, 1), "ETHUSDT": _md(90, 2)}
    hs = T0 + pd.Timedelta(days=75)
    return data, {p: hs for p in D.PRIMARIAS}


def test_no_event_or_label_uses_holdout_data(synth) -> None:
    data, hs = synth
    df, _ = D.build(data=data, holdout_start=hs, dev_start=T0)
    assert set(df["primaria"]) == set(D.PRIMARIAS)  # el test tiene que ejercitar las 4 primarias
    cut = hs[D.PRIMARIAS[0]]
    poisoned = {}
    for s, md in data.items():
        k = md.klines.copy()
        after = k.index >= cut
        k.loc[after, ["open", "high", "low", "close"]] *= np.linspace(0.2, 5.0, after.sum())[:, None]
        k.loc[after, "volume"] *= 50
        f = md.funding.copy()
        f[f.index >= cut] = 0.05
        poisoned[s] = MarketData(k, f)
    df2, _ = D.build(data=poisoned, holdout_start=hs, dev_start=T0)
    pd.testing.assert_frame_equal(df, df2)
    assert (df["t_salida"] <= cut).all() and (df["t_decision"] < cut).all()


def test_sigma_uses_only_days_closed_by_decision() -> None:
    md = _md(40, 3)
    k = md.klines
    for t in (T0 + pd.Timedelta(days=30), T0 + pd.Timedelta(days=30, hours=13, minutes=7)):
        got = D.daily_sigma(k, [t])[0]
        poisoned = k.copy()
        poisoned.loc[poisoned.index >= t, ["open", "high", "low", "close"]] *= 3.0
        assert D.daily_sigma(poisoned, [t])[0] == got
        closes = k["close"].resample("1D", label="right", closed="left").last()
        closes = closes[closes.index <= t].iloc[-21:]
        assert got == pytest.approx(np.log(closes).diff().dropna().std())


def test_barrier_starts_at_candle_after_decision() -> None:
    k = _flat(200)
    t = T0 + pd.Timedelta(minutes=50)
    k.loc[t - MIN, "low"] = 50.0  # la vela de la decision (abre en t-1m) toca el stop: no cuenta
    r = D.triple_barrier(_arrays(k), t, 1, sl=99.0, tp=101.0, t_limite=t + pd.Timedelta(minutes=60))
    assert r["barrera"] == "tiempo" and r["t_entrada"] == t
    assert r["t_salida"] == t + pd.Timedelta(minutes=60)
    k.loc[t, "low"] = 98.0  # la vela de entrada (abre en t) si cuenta
    r = D.triple_barrier(_arrays(k), t, 1, sl=99.0, tp=101.0, t_limite=t + pd.Timedelta(minutes=60))
    assert r["barrera"] == "sl" and r["t_decision_salida"] == t + MIN and r["t_salida"] == t + MIN


def test_tie_in_same_candle_is_stop() -> None:
    k = _flat(200)
    t = T0 + pd.Timedelta(minutes=10)
    hit = t + pd.Timedelta(minutes=5)
    k.loc[hit, ["high", "low"]] = [103.0, 97.0]
    k.loc[hit + MIN, "open"] = 98.5
    for lado, sl, tp in ((1, 99.0, 101.0), (-1, 101.0, 99.0)):
        r = D.triple_barrier(_arrays(k), t, lado, sl=sl, tp=tp, t_limite=t + pd.Timedelta(hours=2))
        assert r["barrera"] == "sl"
        assert r["t_decision_salida"] == hit + MIN and r["precio_salida"] == 98.5
    r = D.triple_barrier(_arrays(k), t, 1, sl=99.0, tp=101.0, t_limite=t + pd.Timedelta(hours=2))
    assert r["pnl_bruto"] == pytest.approx(-0.015) and r["etiqueta"] == 0
    assert r["mae"] == pytest.approx(-0.03) and r["mfe"] == pytest.approx(0.03)


def test_label_pnl_matches_simulator_with_costs_and_funding() -> None:
    md = _md(5, 4)
    t, t_lim = T0 + pd.Timedelta(hours=7, minutes=13), T0 + pd.Timedelta(days=2, hours=3)
    for lado in (1, -1):
        r = D.triple_barrier(D._Arrays(md), t, lado, sl=None, tp=None, t_limite=t_lim)
        res = simulate({"X": md.klines}, {"X": pd.Series([float(lado), 0.0], index=pd.DatetimeIndex([t, t_lim]))},
                       {"X": md.funding})
        tr = res.trades.iloc[0]
        assert r["funding"] != 0
        assert r["pnl_neto"] == pytest.approx(tr["ret_net"], rel=1e-9, abs=1e-12)


def test_uniqueness_weights_hand_case() -> None:
    h = lambda x: T0 + pd.Timedelta(hours=x)  # noqa: E731
    t0 = [h(0), h(2), h(10), h(20), h(20), h(20)]
    t1 = [h(4), h(6), h(11), h(23), h(23), h(21)]
    w = D.uniqueness_weights(t0, t1)
    # A [0,4) y B [2,6): 2 h solos + 2 h de a dos -> (2*1 + 2*0.5)/4; C solo; D,E [20,23) con F [20,21)
    third, half = 1 / 3, 1 / 2
    assert w == pytest.approx([0.75, 0.75, 1.0, (third + 2 * half) / 3, (third + 2 * half) / 3, third])


def test_signal_events_open_flip_and_rebalance() -> None:
    idx = pd.date_range(T0, periods=8, freq="1D")
    s = pd.Series([0.0, 0.5, 0.5, 0.8, -0.3, -0.3, 0.0, 0.2], index=idx)
    ev = D.signal_events(s, rebalances=True)
    assert [(e["tipo"], e["t_decision"], e["lado"], e["tamaño"]) for e in ev] == [
        ("apertura", idx[1], 1, 0.5), ("rebalanceo", idx[3], 1, 0.8), ("apertura", idx[4], -1, -0.3),
        ("apertura", idx[7], 1, 0.2)]
    assert [e["t_fin_regla"] for e in ev[:3]] == [idx[4], idx[4], idx[6]] and pd.isna(ev[3]["t_fin_regla"])
    assert [e["tipo"] for e in D.signal_events(s)] == ["apertura"] * 3


@pytest.mark.parametrize("primaria", D.PRIMARIAS)
def test_openings_match_rule_signal_changes(synth, primaria) -> None:
    data, hs = synth
    spec, rule = CATALOG[primaria]
    dev = D.development(data, hs[primaria])
    sig = rule(dev, dict(spec.params))
    df = D.build_primaria(primaria, data, hs[primaria], 2.0, dev_start=T0)
    assert len(df)
    for sym, s in sig.items():
        s = s.fillna(0.0)
        prev = s.shift(1, fill_value=0.0)
        opens = s.index[(s != 0) & ((prev == 0) | (np.sign(s) != np.sign(prev)))]
        if primaria != "ema-cruce-v1":  # sin sigma (menos de 15 dias de historia) no hay barreras: se descarta
            opens = opens[np.isfinite(D.daily_sigma(dev[sym].klines, opens))]
        got = df[(df["symbol"] == sym) & (df["tipo"] == "apertura")]
        assert list(got["t_decision"]) == list(opens)
        assert (np.sign(got["tamaño"]) == got["lado"]).all()
        assert list(got["tamaño"]) == list(s[opens])
        reb = df[(df["symbol"] == sym) & (df["tipo"] == "rebalanceo")]
        if primaria == "tsmom-diario-v1":
            same = s.index[(s != 0) & (prev != 0) & (np.sign(s) == np.sign(prev)) & (s != prev)]
            same = same[np.isfinite(D.daily_sigma(dev[sym].klines, same))]
            assert list(reb["t_decision"]) == list(same)
        else:
            assert reb.empty
    if primaria == "ema-cruce-v1":  # las barreras de la etiqueta son las de la regla: sale cuando ella sale
        done = df[df["t_fin_regla"].notna()]
        assert (done["t_decision_salida"] == done["t_fin_regla"]).all()
    if primaria == "max-ruptura-10d-v1":
        assert ((df["t_limite"] - df["t_decision"]) == pd.Timedelta(days=1)).all()
    if primaria in ("donchian-ensemble-v1", "tsmom-diario-v1"):
        assert ((df["t_limite"] - df["t_decision"]) <= pd.Timedelta(days=30)).all()
    assert df["precio_entrada"].tolist() == [data[s].klines["open"][data[s].klines.index >= t].iloc[0]
                                            for s, t in zip(df["symbol"], df["t_decision"])]


def test_rule_exit_pnl_hand_signal_matches_simulator_total() -> None:
    """Senal a mano (apertura, giro, cierre, re-apertura y cierre): la suma de pnl_regla * |tamano| = PnL total
    de simulate() (capital 1, un simbolo), con costos de 7 bps por lado y funding."""
    md = _md(6, 7)
    h = lambda x: T0 + pd.Timedelta(hours=x, minutes=7)  # noqa: E731
    s = pd.Series([0.6, -0.4, 0.0, 1.0, 0.0], index=pd.DatetimeIndex([h(3), h(30), h(50), h(70), h(120)]))
    evs = D.signal_events(s)
    a = D._Arrays(md)
    tot = sum(abs(e["tamaño"]) * D.rule_exit_pnl(a, e["t_decision"], e["lado"], e["t_fin_regla"])["pnl_regla"]
              for e in evs)
    res = simulate({"X": md.klines}, {"X": s}, {"X": md.funding})
    assert res.breakdown["funding"] != 0
    assert tot == pytest.approx(res.total_pnl, rel=1e-9, abs=1e-12)


def test_rule_exit_pnl_incomplete_uses_last_close() -> None:
    k = _flat(600)
    r = D.rule_exit_pnl(_arrays(k), T0 + pd.Timedelta(minutes=5), 1, pd.NaT)
    assert not r["ventana_completa_regla"] and r["pnl_regla"] == pytest.approx(-2 * D.COST_PER_SIDE)
    r = D.rule_exit_pnl(_arrays(k), T0 + pd.Timedelta(minutes=5), 1, T0 + pd.Timedelta(minutes=30))
    assert r["ventana_completa_regla"] and r["t_salida_regla"] == T0 + pd.Timedelta(minutes=30)


def _frozen(s: pd.Series) -> pd.Series:
    """Misma senal con el tamano de la apertura fijo durante todo el tramo (sin re-ajustes del mismo signo)."""
    s = s.sort_index().fillna(0.0)
    s = s[~s.index.duplicated(keep="last")]
    prev = s.shift(1, fill_value=0.0)
    opening = (s != 0) & ((prev == 0) | (np.sign(s) != np.sign(prev)))
    return s.where(opening | (s == 0)).ffill().fillna(0.0)


@pytest.mark.parametrize("primaria", ["donchian-ensemble-v1", "max-ruptura-10d-v1", "ema-cruce-v1"])
def test_pnl_regla_matches_simulator_trades(synth, primaria) -> None:
    """Regla real sobre datos sinteticos: para los eventos cerrados por la regla, sum(pnl_regla * |tamano|) = suma
    de ret_net de los trades de simulate() con la misma entrada. pnl_regla mantiene el nocional de la apertura;
    donchian-ensemble re-ajusta el tamano dentro del tramo (vol target) y simulate() opera esos re-ajustes, asi que
    se compara contra la senal con el tamano de apertura congelado (la diferencia es solo el re-ajuste: exposicion
    y costos sobre el delta). tsmom queda afuera: sus rebalanceos son eventos propios (ver dataset.py)."""
    data, hs = synth
    spec, rule = CATALOG[primaria]
    dev = D.development(data, hs[primaria])
    sig = {sym: _frozen(x) for sym, x in rule(dev, dict(spec.params)).items()}
    df = D.build_primaria(primaria, data, hs[primaria], 2.0, dev_start=T0)
    done = df[df["ventana_completa_regla"]]
    assert len(done)
    for sym in dev:
        res = simulate({sym: dev[sym].klines}, {sym: sig[sym]}, {sym: dev[sym].funding})
        tr = res.trades.set_index("entry_time")
        ev = done[done["symbol"] == sym]
        assert set(ev["t_entrada"]) <= set(tr.index)
        assert (ev["t_salida_regla"].to_numpy() == tr.loc[ev["t_entrada"], "exit_time"].to_numpy()).all()
        got = float((ev["pnl_regla"] * ev["tamaño"].abs()).sum())
        assert got == pytest.approx(float(tr.loc[ev["t_entrada"], "ret_net"].sum()), rel=1e-9, abs=1e-12)
