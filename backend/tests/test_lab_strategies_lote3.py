"""Lote 3 del laboratorio: specs congeladas, reglas correctas con datos sinteticos y SIN look-ahead.

Ademas fija los spec_hash de los lotes 1 y 2 (aprobados, decisions.md): agregar el lote 3 no puede cambiarlos.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest
from test_lab_strategies import _funding, _klines, _metrics, _truncate
from test_lab_strategies_lote2 import LOTE1_HASHES, LOTE2_HASHES

from heatsignal.lab import features as F
from heatsignal.lab.clock import assert_no_lookahead, resample_bars
from heatsignal.lab.sim import Costs, simulate
from heatsignal.lab.strategies import CATALOG, LOTE1, LOTE2, LOTE3, SEVEN, MarketData, pair_zscore

T0 = pd.Timestamp("2026-01-01", tz="UTC")
MIN = pd.Timedelta("1min")
H = pd.Timedelta("1h")
DAY = pd.Timedelta("1D")

# PROPUESTA (pendiente de aprobacion): si se cambia una spec antes de aprobarla, actualizar aca y en
# estrategias-lote3.md. Despues de aprobada, cualquier cambio es una -v2.
LOTE3_HASHES = {
    "tsmom-diario-v1": "7515d798e5d66a55",
    "low-vol-xs-v1": "307d3572e576e4c2",
    "pares-eth-btc-v1": "c9c2243ebcef9393",
    "rango-reversion-v1": "a03dba31be20137f",
    "reversion-xs-1h-v1": "e2a331826092f698",
    "funding-prepago-v1": "3fc248f8641d0402",
}


# ---- specs -----------------------------------------------------------------------------------------------


def test_lote1_and_lote2_hashes_unchanged() -> None:
    assert set(LOTE1) == set(LOTE1_HASHES) and set(LOTE2) == set(LOTE2_HASHES)
    assert {sid: CATALOG[sid][0].spec_hash() for sid in LOTE1} == LOTE1_HASHES
    assert {sid: CATALOG[sid][0].spec_hash() for sid in LOTE2} == LOTE2_HASHES


def test_lote3_specs_frozen_and_complete() -> None:
    assert set(LOTE3) == set(LOTE3_HASHES)
    assert {sid: CATALOG[sid][0].spec_hash() for sid in LOTE3} == LOTE3_HASHES
    total = 0
    for sid in LOTE3:
        spec, _ = CATALOG[sid]
        assert spec.strategy_id == sid and spec.description and spec.source and spec.timeframe and spec.horizon
        assert spec.direction in {"long", "short", "both", "neutral"}
        assert spec.backtestable
        n = 1
        for k, vals in spec.grid.items():
            assert spec.params[k] in vals  # el valor congelado esta dentro de la grilla
            n *= len(vals)
        assert n <= 4
        total += n
    assert total == 8  # intentos del lote 3: el programa pasa de K = 29 a K = 37
    assert "funding" in CATALOG["funding-prepago-v1"][0].needs
    assert set(CATALOG["rango-reversion-v1"][0].params["symbols"]) == {"BNBUSDT", "TRXUSDT", "XLMUSDT"}
    assert set(CATALOG["reversion-xs-1h-v1"][0].params["symbols"]) == set(SEVEN) - {"BTCUSDT"}


# ---- constructor de velas 1m a partir de retornos por hora ---------------------------------------------------


def _hourly_bars(log_ret_h: np.ndarray, p0: float = 100.0, start: pd.Timestamp = T0) -> pd.DataFrame:
    """Velas 1m donde cada hora tiene todo su retorno en el primer minuto (precio plano el resto de la hora)."""
    r = np.zeros(len(log_ret_h) * 60)
    r[::60] = log_ret_h
    close = p0 * np.exp(np.cumsum(r))
    open_ = np.r_[p0, close[:-1]]
    qv = np.full(len(r), 1e5)
    return pd.DataFrame({
        "open": open_, "high": np.maximum(open_, close), "low": np.minimum(open_, close), "close": close,
        "volume": qv / close, "quote_volume": qv, "count": 10, "taker_buy_volume": qv / 2 / close,
        "taker_buy_quote_volume": qv / 2,
    }, index=pd.date_range(start, periods=len(r), freq="1min"))


# ---- tsmom-diario-v1 ----------------------------------------------------------------------------------------


_TS_P = {"vol_window_hours": 240}


@pytest.mark.parametrize("direction", [1, -1])
def test_tsmom_sign_and_vol_scaling(direction) -> None:
    spec, rule = CATALOG["tsmom-diario-v1"]
    rng = np.random.default_rng(1)
    r = rng.normal(direction * 0.0015, 0.004, 24 * 30)
    sig = rule({"BTCUSDT": MarketData(_hourly_bars(r))}, {**spec.params, **_TS_P})["BTCUSDT"]
    assert len(sig) > 0
    assert all(t.hour == 0 and t.minute == 0 for t in sig.index)  # solo decisiones diarias 00:00 UTC
    assert (np.sign(sig) == direction).all()
    # peso = target / vol anualizada, con tope 1x
    c = resample_bars(_hourly_bars(r), "1h")["close"]
    vol = np.log(c).diff().rolling(240, min_periods=216).std() * np.sqrt(24 * 365)
    t = sig.index[0]
    assert sig[t] == pytest.approx(direction * min(spec.params["target_vol"] / vol[t], 1.0))
    assert sig.abs().max() <= 1.0 + 1e-12


def test_tsmom_rebalances_only_on_relative_change_over_20pct() -> None:
    spec, rule = CATALOG["tsmom-diario-v1"]
    rng = np.random.default_rng(2)
    r = rng.normal(0.0005, 0.004, 24 * 40)
    sig = rule({"BTCUSDT": MarketData(_hourly_bars(r))}, {**spec.params, **_TS_P})["BTCUSDT"]
    prev = sig.shift(1).dropna()
    rel = ((sig.loc[prev.index] - prev).abs() / prev.abs())
    assert (rel > 0.20).all()  # cada cambio emitido supera el umbral relativo
    # con 0 de umbral habria un cambio todos los dias
    every = rule({"BTCUSDT": MarketData(_hourly_bars(r))}, {**spec.params, **_TS_P, "rebalance_threshold": 0.0})
    assert len(every["BTCUSDT"]) > len(sig)


def test_tsmom_skips_last_hour() -> None:
    """Una caida enorme en la ultima hora (23:00 -> 00:00) no entra en el retorno de 7 dias de esa decision."""
    spec, rule = CATALOG["tsmom-diario-v1"]
    r = np.full(24 * 20, 0.001)  # +2.4% por dia
    crash_day = 15
    r[crash_day * 24 - 1] = -0.5  # vela 23:00-00:00 del dia 14 -> decision del dia 15 a las 00:00
    t = T0 + crash_day * DAY
    p = {**spec.params, **_TS_P, "rebalance_threshold": 0.0}
    sig = rule({"BTCUSDT": MarketData(_hourly_bars(r))}, p)["BTCUSDT"]
    assert sig.get(t) is not None and sig[t] > 0  # sin el skip seria negativo
    noskip = rule({"BTCUSDT": MarketData(_hourly_bars(r))}, {**p, "skip_hours": 0})["BTCUSDT"]
    assert noskip[t] < 0


def test_tsmom_symbol_universe() -> None:
    spec, rule = CATALOG["tsmom-diario-v1"]
    r = np.random.default_rng(3).normal(0.0005, 0.004, 24 * 30)
    sig = rule({"FOOUSDT": MarketData(_hourly_bars(r))}, {**spec.params, **_TS_P})
    assert len(sig["FOOUSDT"]) == 0


# ---- low-vol-xs-v1 ------------------------------------------------------------------------------------------


def _vol_universe(n_sym: int, days: int = 12) -> dict[str, MarketData]:
    out = {}
    for i in range(n_sym):
        r = np.random.default_rng(10 + i).normal(0, 0.001 * (i + 1), 24 * days)
        out[f"S{i}USDT"] = MarketData(_hourly_bars(r))
    return out


def test_low_vol_long_lowest_short_highest_daily() -> None:
    spec, rule = CATALOG["low-vol-xs-v1"]
    sig = rule(_vol_universe(6), spec.params)
    first = T0 + 7 * DAY  # primer dia con 7 dias de RV (>= 90% de 168 velas)
    at = {s: v.get(first, 0.0) for s, v in sig.items()}
    assert at == {"S0USDT": 1.0, "S1USDT": 1.0, "S2USDT": 0.0, "S3USDT": 0.0, "S4USDT": -1.0, "S5USDT": -1.0}
    # composicion estable: un solo cambio por simbolo, todos a las 00:00 UTC
    assert all(len(v) <= 1 for v in sig.values())
    assert all(t.hour == 0 and t.minute == 0 for v in sig.values() for t in v.index)


def test_low_vol_flat_with_too_few_symbols() -> None:
    spec, rule = CATALOG["low-vol-xs-v1"]
    sig = rule(_vol_universe(4), spec.params)
    assert all(len(v) == 0 for v in sig.values())


def test_low_vol_goes_flat_when_universe_shrinks() -> None:
    spec, rule = CATALOG["low-vol-xs-v1"]
    data = _vol_universe(6, days=14)
    short = data["S5USDT"].klines
    data["S5USDT"] = MarketData(short[short.index < T0 + 10 * DAY])  # deja de haber datos
    data["S4USDT"] = MarketData(data["S4USDT"].klines[data["S4USDT"].klines.index < T0 + 10 * DAY])
    sig = rule(data, spec.params)
    final = {s: float(v.iloc[-1]) if len(v) else 0.0 for s, v in sig.items()}
    assert all(x == 0.0 for x in final.values())  # 4 simbolos con dato < 5: todo plano


# ---- pares-eth-btc-v1 ---------------------------------------------------------------------------------------


def _pair_case(n_hours: int = 24 * 45, shock_at: int | None = None, shock: float = 0.0, seed: int = 7,
               shock_len: int = 1):
    rng = np.random.default_rng(seed)
    lb = np.cumsum(rng.normal(0, 0.005, n_hours))
    s = rng.uniform(-0.003, 0.003, n_hours)  # residuo acotado (|z| < ~1.8): sin entradas por ruido
    if shock_at is not None:
        s[shock_at:shock_at + shock_len] = shock
    le = 0.2 + 1.2 * lb + s
    btc = _hourly_bars(np.diff(np.r_[0.0, lb]), p0=100.0)
    eth = _hourly_bars(np.diff(np.r_[0.0, le]), p0=100.0 * np.exp(0.2))
    return {"ETHUSDT": MarketData(eth), "BTCUSDT": MarketData(btc), "SOLUSDT": MarketData(btc.copy())}


_PAIR_P = {"window_hours": 240}


def test_pair_zscore_is_causal_and_recovers_beta() -> None:
    data = _pair_case()
    y = np.log(resample_bars(data["ETHUSDT"].klines, "1h")["close"])
    x = np.log(resample_bars(data["BTCUSDT"].klines, "1h")["close"])
    zb = pair_zscore(y, x, 240)
    assert zb["beta"].dropna().median() == pytest.approx(1.2, abs=0.1)
    frame = pd.concat({"y": y, "x": x}, axis=1)
    assert_no_lookahead(lambda d: pair_zscore(d["y"], d["x"], 240), frame)


@pytest.mark.parametrize("sign", [1, -1])
def test_pares_enters_beta_neutral_and_exits_on_reversion(sign) -> None:
    spec, rule = CATALOG["pares-eth-btc-v1"]
    hour = 24 * 30
    data = _pair_case(shock_at=hour, shock=sign * 0.006)  # ~3 sigma del residuo
    sig = rule(data, {**spec.params, **_PAIR_P})
    t = T0 + (hour + 1) * H  # cierre de la vela del shock
    eth, btc = sig["ETHUSDT"], sig["BTCUSDT"]
    assert np.sign(eth.get(t, 0.0)) == -sign and np.sign(btc.get(t, 0.0)) == sign
    y = np.log(resample_bars(data["ETHUSDT"].klines, "1h")["close"])
    x = np.log(resample_bars(data["BTCUSDT"].klines, "1h")["close"])
    beta = pair_zscore(y, x, 240)["beta"][t]
    assert -btc[t] / eth[t] == pytest.approx(beta)  # nocional BTC = beta * nocional ETH
    assert max(abs(eth[t]), abs(btc[t])) == pytest.approx(1.0)  # ninguna pata pasa de 1x
    exit_t = eth.index[eth.index > t][0]
    assert eth[exit_t] == 0.0 and btc[exit_t] == 0.0 and exit_t <= t + 72 * H
    assert len(sig["SOLUSDT"]) == 0


def test_pares_stop_and_rearm() -> None:
    spec, rule = CATALOG["pares-eth-btc-v1"]
    hour = 24 * 30
    data = _pair_case(shock_at=hour, shock=0.006)
    # despues de entrar, el spread se dispara (|z| > 4) y queda alto 5 horas
    k = data["ETHUSDT"].klines.copy()
    after = (k.index >= T0 + (hour + 1) * H) & (k.index < T0 + (hour + 6) * H)
    k.loc[after, ["open", "high", "low", "close"]] *= np.exp(0.03)
    data["ETHUSDT"] = MarketData(k)
    sig = rule(data, {**spec.params, **_PAIR_P})["ETHUSDT"]
    t = T0 + (hour + 1) * H
    assert sig[t] < 0
    assert sig.index[sig.index > t][0] == t + H and sig[t + H] == 0.0  # stop en la vela siguiente
    # mientras |z| sigue alto no re-entra (no hay re-armado)
    assert not ((sig.index > t + H) & (sig.index <= t + 5 * H)).any()


def test_pares_time_exit_exactly_72h() -> None:
    spec, rule = CATALOG["pares-eth-btc-v1"]
    hour = 24 * 30
    # el spread salta a ~3 sigma y se queda ahi mucho tiempo: no revierte ni toca el stop
    data = _pair_case(shock_at=hour, shock=0.006, shock_len=120)
    sig = rule(data, {**spec.params, **_PAIR_P, "stop_z": 50.0, "exit_z": 0.01})["ETHUSDT"]
    t = T0 + (hour + 1) * H
    assert sig[t] < 0
    nxt = sig.index[sig.index > t][0]
    assert nxt == t + 72 * H and sig[nxt] == 0.0  # salida por tiempo, exacta
    # re-armado: si vuelve a entrar, antes tuvo que haber una vela con |z| < entry_z despues de la salida
    later = sig[(sig.index > nxt) & (sig != 0)]
    if len(later):
        y = np.log(resample_bars(data["ETHUSDT"].klines, "1h")["close"])
        x = np.log(resample_bars(data["BTCUSDT"].klines, "1h")["close"])
        z = pair_zscore(y, x, 240)["z"]
        between = z[(z.index >= nxt) & (z.index < later.index[0])]
        assert (between.abs() < spec.params["entry_z"]).any()


def test_pares_spread_reversion_is_profitable_in_simulator() -> None:
    """Convencion de pesos de 2 patas verificada con lab/sim.py: un spread que revierte gana en precio."""
    spec, rule = CATALOG["pares-eth-btc-v1"]
    data = _pair_case(shock_at=24 * 30, shock=0.006)
    sig = rule(data, {**spec.params, **_PAIR_P})
    legs = {s: sig[s] for s in ("ETHUSDT", "BTCUSDT")}
    prices = {s: data[s].klines[["open", "high", "low", "close"]] for s in legs}
    res = simulate(prices, legs, None, Costs(fee_bps=0.0, default_slippage_bps=0.0))
    assert len(res.trades) >= 2
    assert res.breakdown["precio"] > 0
    # cobertura: la pata BTC cancela casi todo el movimiento comun (BTC sube ~ fuerte en la muestra)
    eth_alone = simulate({"ETHUSDT": prices["ETHUSDT"]}, {"ETHUSDT": legs["ETHUSDT"]}, None,
                         Costs(fee_bps=0.0, default_slippage_bps=0.0))
    assert np.std(res.daily_returns) < np.std(eth_alone.daily_returns) * 2  # no explota por la pata BTC


# ---- rango-reversion-v1 -------------------------------------------------------------------------------------


def _range_series(n_hours: int, seed: int, trend: float = 0.0) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    x = np.zeros(n_hours)
    for i in range(1, n_hours):  # OU alrededor de 0 (rango) mas tendencia opcional
        x[i] = 0.8 * x[i - 1] + rng.normal(0, 0.004)
    lp = x + trend * np.arange(n_hours)
    return _hourly_bars(np.diff(np.r_[0.0, lp]))


def test_rango_reversion_entries_respect_regime_and_bands() -> None:
    spec, rule = CATALOG["rango-reversion-v1"]
    k = _range_series(24 * 20, seed=4)
    sig = rule({"BNBUSDT": MarketData(k), "BTCUSDT": MarketData(k.copy())}, spec.params)
    s = sig["BNBUSDT"]
    assert len(sig["BTCUSDT"]) == 0  # fuera del universo
    entries = s[s != 0]
    assert (entries == 1.0).any() and (entries == -1.0).any()
    bars = resample_bars(k, "1h")
    c = bars["close"]
    mid, sd = c.rolling(20).mean(), c.rolling(20).std()
    adx = F.adx(bars, 14)
    for t, d in entries.items():
        assert adx[t] < 20
        assert (c[t] < mid[t] - 2 * sd[t]) if d > 0 else (c[t] > mid[t] + 2 * sd[t])
        exit_t = s.index[s.index > t][0]
        assert s[exit_t] == 0.0 and exit_t <= t + 12 * H


def test_rango_reversion_no_entries_in_trend() -> None:
    spec, rule = CATALOG["rango-reversion-v1"]
    k = _range_series(24 * 20, seed=4, trend=0.01)  # tendencia fuerte: ADX alto
    bars = resample_bars(k, "1h")
    assert (F.adx(bars, 14).iloc[100:] > 20).all()
    s = rule({"TRXUSDT": MarketData(k)}, spec.params)["TRXUSDT"]
    assert not (s[s.index > bars.index[100]] != 0).any()


def test_rango_reversion_stop_at_1_5_atr() -> None:
    spec, rule = CATALOG["rango-reversion-v1"]
    k = _range_series(24 * 20, seed=4)
    s0 = rule({"XLMUSDT": MarketData(k)}, spec.params)["XLMUSDT"]
    t = s0[s0 == 1.0].index[0]
    bars = resample_bars(k, "1h")
    sl = bars["close"][t] - 1.5 * F.atr(bars, 14)[t]
    k2 = k.copy()
    flat = (k2.index >= t) & (k2.index < t + 10 * MIN)  # 10 minutos quietos despues de entrar
    k2.loc[flat, ["open", "high", "low", "close"]] = bars["close"][t]
    k2.loc[k2.index >= t + 10 * MIN, ["open", "high", "low", "close"]] = sl * 0.99  # se desploma
    k2 = k2[k2.index < t + 13 * H]
    s = rule({"XLMUSDT": MarketData(k2)}, spec.params)["XLMUSDT"]
    assert s[t] == 1.0
    assert s.index[s.index > t][0] == t + 11 * MIN  # cierre de la primera vela 1m bajo el stop


def test_adx_has_no_lookahead() -> None:
    bars = resample_bars(_range_series(24 * 10, seed=9), "1h")
    assert_no_lookahead(lambda b: F.adx(b, 14), bars)


# ---- reversion-xs-1h-v1 -------------------------------------------------------------------------------------


def _xs_case(shocks: dict[str, float], hour: int = 24 * 15, n_hours: int = 24 * 20):
    rng = np.random.default_rng(31)
    rb = rng.normal(0, 0.005, n_hours)
    data = {"BTCUSDT": MarketData(_hourly_bars(rb))}
    for i, alt in enumerate(["SOLUSDT", "ETHUSDT", "XRPUSDT"]):
        ra = 1.5 * rb + rng.uniform(-0.005, 0.005, n_hours)  # residuo acotado: sin entradas por ruido
        ra[hour] = 1.5 * rb[hour] + shocks.get(alt, 0.0)
        data[alt] = MarketData(_hourly_bars(ra))
    return data, T0 + (hour + 1) * H


_XS_P = {"window_hours": 120}


def test_reversion_xs_long_alt_short_btc_on_negative_residual() -> None:
    spec, rule = CATALOG["reversion-xs-1h-v1"]
    data, t = _xs_case({"SOLUSDT": -0.02})  # ~ -6.7 sigma
    sig = rule(data, {**spec.params, **_XS_P})
    sol, btc = sig["SOLUSDT"], sig["BTCUSDT"]
    assert sol[t] > 0 and btc[t] < 0
    assert -btc[t] / sol[t] == pytest.approx(1.5, abs=0.2)  # cobertura por beta
    assert sol[t + 4 * H] == 0.0 and btc.get(t + 4 * H) == 0.0  # salida a las 4 h


def test_reversion_xs_btc_leg_sums_hedges() -> None:
    spec, rule = CATALOG["reversion-xs-1h-v1"]
    data, t = _xs_case({"SOLUSDT": -0.02, "XRPUSDT": 0.02})
    sig = rule(data, {**spec.params, **_XS_P})
    sol, xrp, btc = sig["SOLUSDT"], sig["XRPUSDT"], sig["BTCUSDT"]
    assert sol[t] > 0 and xrp[t] < 0
    p = {**spec.params, **_XS_P}
    alone_sol = rule({k: v for k, v in data.items() if k != "XRPUSDT"}, p)["BTCUSDT"][t]
    alone_xrp = rule({k: v for k, v in data.items() if k != "SOLUSDT"}, p)["BTCUSDT"][t]
    assert btc.get(t, 0.0) == pytest.approx(alone_sol + alone_xrp)  # coberturas opuestas casi se cancelan


def test_reversion_xs_without_btc_has_no_signal() -> None:
    spec, rule = CATALOG["reversion-xs-1h-v1"]
    data, _ = _xs_case({"SOLUSDT": -0.02})
    data.pop("BTCUSDT")
    assert all(len(v) == 0 for v in rule(data, {**spec.params, **_XS_P}).values())


# ---- funding-prepago-v1 -------------------------------------------------------------------------------------


def _prepago_case(value_at: dict[pd.Timestamp, float] | None = None, days: int = 100):
    rng = np.random.default_rng(41)
    idx = pd.date_range(T0, periods=days * 3, freq="8h")
    f = pd.Series(0.0001 + rng.normal(0, 0.00002, len(idx)), index=idx)
    for t, v in (value_at or {}).items():
        f[t] = v
    k = _klines(60 * 24 * 3, seed=1)
    k.index = k.index + (days - 3) * DAY  # velas de los ultimos 3 dias
    return MarketData(k, funding=f)


S0 = T0 + 98 * DAY + 8 * H  # liquidacion conocida; la siguiente es T = S0 + 8 h


@pytest.mark.parametrize("value,direction", [(0.002, -1.0), (-0.002, 1.0)])
def test_prepago_uses_previous_settlement(value, direction) -> None:
    spec, rule = CATALOG["funding-prepago-v1"]
    s = rule({"BTCUSDT": _prepago_case({S0: value})}, spec.params)["BTCUSDT"]
    T = S0 + 8 * H
    around = s[(s.index >= T - 9 * H) & (s.index <= T + H)]
    assert list(around.items()) == [(T - H, direction), (T, -direction), (T + H, 0.0)]


def test_prepago_ignores_the_rate_being_settled_at_T() -> None:
    """La tasa de T no se conoce en T-60: cambiarla no puede cambiar la decision (seria look-ahead)."""
    spec, rule = CATALOG["funding-prepago-v1"]
    T = S0 + 8 * H
    base = rule({"BTCUSDT": _prepago_case()}, spec.params)["BTCUSDT"]
    moved = rule({"BTCUSDT": _prepago_case({T: 0.002})}, spec.params)["BTCUSDT"]
    assert T - H not in base.index
    pd.testing.assert_series_equal(base[base.index <= T], moved[moved.index <= T])
    # la tasa de T recien cuenta para la decision de T + 8 h - 60 min
    assert moved.get(T + 7 * H) == -1.0


def test_prepago_stale_funding_and_universe() -> None:
    spec, rule = CATALOG["funding-prepago-v1"]
    md = _prepago_case({S0: 0.002})
    stale = MarketData(md.klines, funding=md.funding[md.funding.index <= S0 - DAY])  # dato > 9 h
    assert len(rule({"BTCUSDT": stale}, spec.params)["BTCUSDT"]) == 0
    assert len(rule({"FOOUSDT": md}, spec.params)["FOOUSDT"]) == 0
    assert len(rule({"BTCUSDT": MarketData(md.klines)}, spec.params)["BTCUSDT"]) == 0


# ---- anti look-ahead con ventanas cortas (para que las reglas efectivamente disparen) -----------------------

SHORT = {
    "tsmom-diario-v1": {"vol_window_hours": 240},
    "low-vol-xs-v1": {},
    "pares-eth-btc-v1": {"window_hours": 120, "entry_z": 1.5},
    "rango-reversion-v1": {},
    "reversion-xs-1h-v1": {"window_hours": 120},
    "funding-prepago-v1": {"pct_window": 30},
}


def _data3(n_days: int = 40) -> dict[str, MarketData]:
    out = {}
    for i, name in enumerate(SEVEN):
        n = 60 * 24 * n_days
        k = _klines(n, seed=i, drift=0.00002 * (i - 3), shock_at=n // 2 + i * 777, shock=(-0.08 if i % 2 else 0.08))
        out[name] = MarketData(k, funding=_funding(n_days, 10 + i, base=0.00002 * (i - 3)), metrics=_metrics(k, 20 + i))
    return out


@pytest.mark.parametrize("sid", list(LOTE3))
def test_lote3_rules_have_no_lookahead(sid) -> None:
    assert set(SHORT) == set(LOTE3)
    spec, rule = CATALOG[sid]
    params = {**spec.params, **SHORT[sid]}
    data = _data3()
    full = rule(data, params)
    assert sum(int((s != 0).sum()) for s in full.values()) > 0, "la regla no disparo: test vacio"
    start = next(iter(data.values())).klines.index[0]
    for frac in (0.4, 0.55, 0.75, 0.9):
        cut = start + pd.Timedelta(days=40 * frac)
        part = rule(_truncate(data, cut), params)
        for sym, s in part.items():
            p = s[s.index <= cut - MIN]
            f = full.get(sym, pd.Series(dtype=float))
            f = f[f.index <= cut - MIN]
            pd.testing.assert_series_equal(p, f, check_names=False, check_freq=False, check_index_type=False)
