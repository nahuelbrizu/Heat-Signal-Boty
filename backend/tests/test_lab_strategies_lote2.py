"""Lote 2 del laboratorio: specs congeladas, reglas correctas con datos sinteticos y SIN look-ahead.

Ademas fija los spec_hash del lote 1 (aprobado el 2026-10-17, decisions.md): agregar el lote 2 no puede
cambiarlos.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest
from test_lab_strategies import _funding, _klines, _metrics, _truncate

from heatsignal.lab.strategies import CATALOG, LOTE1, LOTE2, MarketData

T0 = pd.Timestamp("2026-01-01", tz="UTC")  # jueves
MIN = pd.Timedelta("1min")
H = pd.Timedelta("1h")

LOTE1_HASHES = {
    "donchian-ensemble-v1": "86061813c7d1691c",
    "flujo-apertura-hora-v1": "676f9a08f9af3828",
    "reversion-flujo-extremo-15m-v1": "cd15e9623abe8bfa",
    "capitulacion-long-v1": "3717fbb833f28a96",
    "squeeze-short-v1": "f146c3d219bc056b",
    "funding-crowding-xs-v1": "4aa52e735fc55460",
    "funding-crowding-short-v1": "38869de0fc4bc760",
    "funding-spread-hl-cex-v1": "09cb4efe470c9fef",
    "absorcion-flujo-v1": "a3768ae5a4adaadd",
}

# PROPUESTA (pendiente de aprobacion): si se cambia una spec antes de aprobarla, actualizar aca y en
# estrategias-lote2.md. Despues de aprobada, cualquier cambio es una -v2.
LOTE2_HASHES = {
    "compresion-breakout-v1": "a25d6babd6964b30",
    "momentum-intradia-v1": "4776269a35a6edde",
    "oi-precio-continuacion-v1": "df0fd44676eb37c7",
    "carry-xs-funding-v1": "118bdbb71f36906a",
    "lunes-asia-v1": "73cb513730c5c018",
}


# ---- specs -----------------------------------------------------------------------------------------------


def test_lote1_hashes_unchanged() -> None:
    assert set(LOTE1) == set(LOTE1_HASHES)
    assert {sid: CATALOG[sid][0].spec_hash() for sid in LOTE1} == LOTE1_HASHES


def test_lote2_specs_frozen_and_complete() -> None:
    assert set(LOTE2) == set(LOTE2_HASHES)
    assert {sid: CATALOG[sid][0].spec_hash() for sid in LOTE2} == LOTE2_HASHES
    for sid in LOTE2:
        spec, _ = CATALOG[sid]
        assert spec.strategy_id == sid and spec.description and spec.source and spec.timeframe
        assert spec.direction in {"long", "short", "both", "neutral"}
        assert spec.backtestable
        n = 1
        for k, vals in spec.grid.items():
            assert spec.params[k] in vals  # el valor congelado esta dentro de la grilla
            n *= len(vals)
        assert n <= 4
    assert "metrics" in CATALOG["oi-precio-continuacion-v1"][0].needs
    assert "funding" in CATALOG["carry-xs-funding-v1"][0].needs
    assert CATALOG["lunes-asia-v1"][0].params["modo"] == "observacion"


# ---- constructor de velas con control fino ------------------------------------------------------------------


def _bars(r: np.ndarray, buy_share: np.ndarray, start: pd.Timestamp = T0, p0: float = 100.0) -> pd.DataFrame:
    close = p0 * np.exp(np.cumsum(r))
    open_ = np.r_[p0, close[:-1]]
    qv = np.full(len(r), 1e5)
    buy = qv * buy_share
    return pd.DataFrame({
        "open": open_, "high": np.maximum(open_, close), "low": np.minimum(open_, close), "close": close,
        "volume": qv / close, "quote_volume": qv, "count": 10, "taker_buy_volume": buy / close,
        "taker_buy_quote_volume": buy,
    }, index=pd.date_range(start, periods=len(r), freq="1min"))


# ---- compresion-breakout-v1 ---------------------------------------------------------------------------------


def _compression_case(direction: int, with_flow: bool, compress: bool = True):
    rng = np.random.default_rng(5)
    normal, comp, after = 96 * 60, 30 * 60, 15 * 60
    sd = np.full(normal + comp + 60 + after, 0.001)
    if compress:  # la volatilidad se achica hora a hora: el ancho de Bollinger cae por debajo de su p10
        sd[normal:normal + comp] = 0.001 * 0.8 ** (np.arange(comp) // 60 + 1)
    r = rng.normal(0, 1, len(sd)) * sd
    share = rng.uniform(0.4, 0.6, len(sd))
    b0 = normal + comp
    r[b0:b0 + 60] = direction * 0.0002  # ruptura de ~1.2% en una hora
    share[b0:b0 + 60] = 0.9 if (direction > 0) == with_flow else 0.1
    k = _bars(r, share)
    breakout_close = k.index[b0 + 59] + MIN
    return k, breakout_close


_COMP_P = {"width_window": 48}


@pytest.mark.parametrize("direction", [1, -1])
def test_compresion_breakout_fires_with_flow(direction) -> None:
    spec, rule = CATALOG["compresion-breakout-v1"]
    k, t = _compression_case(direction, with_flow=True)
    sig = rule({"X": MarketData(k)}, {**spec.params, **_COMP_P})["X"]
    assert sig.get(t) == float(direction)
    # sale por stop (lado opuesto del rango) o a las 12 h, nunca despues
    exit_t = sig.index[sig.index > t][0]
    assert sig[exit_t] == 0.0 and exit_t <= t + pd.Timedelta(hours=12)


@pytest.mark.parametrize("direction", [1, -1])
def test_compresion_breakout_no_entry_against_flow(direction) -> None:
    spec, rule = CATALOG["compresion-breakout-v1"]
    k, t = _compression_case(direction, with_flow=False)
    sig = rule({"X": MarketData(k)}, {**spec.params, **_COMP_P})["X"]
    assert t not in sig.index


def test_compresion_breakout_needs_compression() -> None:
    spec, rule = CATALOG["compresion-breakout-v1"]
    k, t = _compression_case(1, with_flow=True, compress=False)
    sig = rule({"X": MarketData(k)}, {**spec.params, **_COMP_P})["X"]
    assert t not in sig.index


def test_compresion_breakout_stop_is_opposite_side_of_range() -> None:
    spec, rule = CATALOG["compresion-breakout-v1"]
    k, t = _compression_case(1, with_flow=True)
    # despues de la ruptura, el precio se desploma: tiene que salir por stop antes de 12 h
    k = k.copy()
    after = k.index >= t + 2 * H
    k.loc[after, ["open", "high", "low", "close"]] *= 0.9
    sig = rule({"X": MarketData(k)}, {**spec.params, **_COMP_P})["X"]
    assert sig.get(t) == 1.0
    exit_t = sig.index[sig.index > t][0]
    assert exit_t == t + 2 * H + MIN  # cierre de la primera vela 1m que perfora el minimo del rango


# ---- momentum-intradia-v1 -----------------------------------------------------------------------------------


def _momentum_data():
    days = 20
    rng = np.random.default_rng(11)
    r = rng.normal(0, 0.0008, days * 1440)
    share = np.full(len(r), 0.5)
    for d, move in ((15, 0.001), (16, 0.0), (17, -0.001)):
        r[d * 1440:d * 1440 + 30] = move  # +3% / 0 / -3% en la primera media hora del dia d
    k = _bars(r, share)
    return {"BTCUSDT": MarketData(k), "SOLUSDT": MarketData(k.copy())}


def test_momentum_intradia_trades_last_half_hour_in_signal_direction() -> None:
    spec, rule = CATALOG["momentum-intradia-v1"]
    sig = rule(_momentum_data(), {**spec.params, "pct_window_days": 10})
    btc = sig["BTCUSDT"]
    d15 = T0 + pd.Timedelta(days=15)
    d17 = T0 + pd.Timedelta(days=17)
    assert btc.get(d15 + pd.Timedelta("23h30min")) == 1.0
    assert btc.get(d15 + pd.Timedelta(days=1)) == 0.0
    assert btc.get(d17 + pd.Timedelta("23h30min")) == -1.0
    assert btc.get(d17 + pd.Timedelta(days=1)) == 0.0
    assert (d15 + pd.Timedelta(days=1, hours=23, minutes=30)) not in btc.index  # dia 16: senal 0, no opera
    # solo entre 23:30 y 00:00
    assert all((t.hour, t.minute) in {(23, 30), (0, 0)} for t in btc.index)
    assert len(sig["SOLUSDT"]) == 0  # simbolo fuera del universo: sin senal


def test_momentum_intradia_skips_incomplete_first_half_hour() -> None:
    spec, rule = CATALOG["momentum-intradia-v1"]
    data = _momentum_data()
    k = data["BTCUSDT"].klines
    d15 = T0 + pd.Timedelta(days=15)
    k = k.drop(k.index[(k.index >= d15 + pd.Timedelta("10min")) & (k.index < d15 + pd.Timedelta("11min"))])
    sig = rule({"BTCUSDT": MarketData(k)}, {**spec.params, "pct_window_days": 10})["BTCUSDT"]
    assert (d15 + pd.Timedelta("23h30min")) not in sig.index


# ---- oi-precio-continuacion-v1 ------------------------------------------------------------------------------


def _oi_case(direction: int, oi_move: float, flow: float):
    rng = np.random.default_rng(21)
    base, move, after = 5 * 1440, 240, 12 * 60
    r = rng.normal(0, 0.0008, base + move + after)
    share = rng.uniform(0.4, 0.6, len(r))
    r[base:base + move] = direction * 0.0001  # 2.4% en 4 h
    share[base:base + move] = flow
    k = _bars(r, share)
    idx = pd.date_range(k.index[0], k.index[-1], freq="5min")
    oi = np.cumsum(rng.normal(0, 0.002, len(idx)))
    t_move = k.index[base]
    in_move = (idx >= t_move) & (idx < t_move + 4 * H)
    oi[in_move] += np.linspace(0, oi_move, in_move.sum())
    oi[idx >= t_move + 4 * H] += oi_move
    m = pd.DataFrame({"oi_contracts": 1e6 * np.exp(oi), "oi_usd": 1e8}, index=idx)
    return MarketData(k, metrics=m), t_move + 4 * H


_OI_P = {"sigma_window": 48, "oi_window_hours": 48}


@pytest.mark.parametrize("direction", [1, -1])
def test_oi_continuacion_fires_with_price_oi_and_flow(direction) -> None:
    spec, rule = CATALOG["oi-precio-continuacion-v1"]
    md, t = _oi_case(direction, oi_move=0.10, flow=0.8 if direction > 0 else 0.2)
    sig = rule({"X": md}, {**spec.params, **_OI_P})["X"]
    near = sig[(sig.index > t - 4 * H) & (sig.index <= t + H)]  # durante la suba de 4 h
    assert (near == float(direction)).any()
    entry = near.index[near == float(direction)][0]
    assert sig.get(entry + pd.Timedelta(hours=8)) == 0.0  # salida a las 8 h


@pytest.mark.parametrize("oi_move,flow", [(-0.10, 0.8), (0.10, 0.2)])
def test_oi_continuacion_needs_oi_up_and_flow_in_favor(oi_move, flow) -> None:
    spec, rule = CATALOG["oi-precio-continuacion-v1"]
    md, t = _oi_case(1, oi_move=oi_move, flow=flow)
    sig = rule({"X": md}, {**spec.params, **_OI_P})["X"]
    near = sig[(sig.index > t - 4 * H) & (sig.index <= t + H)]  # durante la suba de 4 h
    assert not (near == 1.0).any()


def test_oi_continuacion_without_metrics_has_no_signal() -> None:
    spec, rule = CATALOG["oi-precio-continuacion-v1"]
    md, _ = _oi_case(1, oi_move=0.10, flow=0.8)
    sig = rule({"X": MarketData(md.klines)}, {**spec.params, **_OI_P})["X"]
    assert len(sig) == 0


# ---- carry-xs-funding-v1 ------------------------------------------------------------------------------------


def _const_funding(values: list[float], start: pd.Timestamp = T0) -> pd.Series:
    return pd.Series(values, index=pd.date_range(start, periods=len(values), freq="8h"))


def test_carry_xs_long_two_lowest_short_two_highest_and_holds() -> None:
    spec, rule = CATALOG["carry-xs-funding-v1"]
    k = _klines(10)
    data = {f"S{i}": MarketData(k, funding=_const_funding([0.0001 * i] * 30)) for i in range(6)}
    sig = rule(data, spec.params)
    t3 = T0 + pd.Timedelta(hours=16)  # tercera liquidacion: primer promedio de 3
    assert {s: v.get(t3, 0.0) for s, v in sig.items()} == {"S0": 1.0, "S1": 1.0, "S2": 0.0, "S3": 0.0,
                                                          "S4": -1.0, "S5": -1.0}
    # composicion constante: un solo cambio por simbolo (no se rebalancea en cada liquidacion)
    assert all(len(v) <= 1 for v in sig.values())


def test_carry_xs_rebalances_only_on_composition_change() -> None:
    spec, rule = CATALOG["carry-xs-funding-v1"]
    k = _klines(10)
    data = {f"S{i}": MarketData(k, funding=_const_funding([0.0001 * i] * 30)) for i in range(1, 6)}
    data["S0"] = MarketData(k, funding=_const_funding([0.0] * 15 + [0.01] * 15))  # pasa a ser el mas caro
    sig = rule(data, spec.params)
    final = {s: float(v.iloc[-1]) if len(v) else 0.0 for s, v in sig.items()}
    assert final == {"S0": -1.0, "S1": 1.0, "S2": 1.0, "S3": 0.0, "S4": 0.0, "S5": -1.0}
    assert len(sig["S1"]) == 1 and len(sig["S5"]) == 1  # no cambiaron de lado: no se tocan
    t_switch = sig["S0"].index[-1]
    assert t_switch == T0 + pd.Timedelta(hours=8 * 15)  # promedio de (0, 0, 0.01) ya es el mayor


def test_carry_xs_needs_min_symbols_with_fresh_data() -> None:
    spec, rule = CATALOG["carry-xs-funding-v1"]
    k = _klines(10)
    four = {f"S{i}": MarketData(k, funding=_const_funding([0.0001 * i] * 30)) for i in range(4)}
    assert all(len(v) == 0 for v in rule(four, spec.params).values())
    # un quinto simbolo con dato viejo (> 9 h) no cuenta
    stale = dict(four, S9=MarketData(k, funding=_const_funding([0.0005] * 3, start=T0 - pd.Timedelta(days=5))))
    assert all(len(v) == 0 for v in rule(stale, spec.params).values())


# ---- lunes-asia-v1 ------------------------------------------------------------------------------------------


def test_lunes_asia_long_btc_sunday_23_to_monday_23() -> None:
    spec, rule = CATALOG["lunes-asia-v1"]
    k = _klines(60 * 24 * 15)  # 2026-01-01 (jueves) a 2026-01-15
    sig = rule({"BTCUSDT": MarketData(k), "ETHUSDT": MarketData(k)}, spec.params)
    sun1, sun2 = pd.Timestamp("2026-01-04 23:00", tz="UTC"), pd.Timestamp("2026-01-11 23:00", tz="UTC")
    assert list(sig["BTCUSDT"].items()) == [(sun1, 1.0), (sun1 + pd.Timedelta(days=1), 0.0),
                                            (sun2, 1.0), (sun2 + pd.Timedelta(days=1), 0.0)]
    assert len(sig["ETHUSDT"]) == 0


# ---- anti look-ahead con ventanas cortas (para que las reglas efectivamente disparen) -----------------------

NAMES = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "BNBUSDT"]
SHORT = {
    "compresion-breakout-v1": {"width_window": 72},
    "momentum-intradia-v1": {"pct_window_days": 10},
    "oi-precio-continuacion-v1": {"sigma_window": 72, "oi_window_hours": 72},
    "carry-xs-funding-v1": {},
    "lunes-asia-v1": {},
}


def _data2(n_days: int = 40) -> dict[str, MarketData]:
    out = {}
    for i, name in enumerate(NAMES):
        n = 60 * 24 * n_days
        k = _klines(n, seed=i, drift=0.00002 * (i - 2), shock_at=n // 2 + i * 777, shock=(-0.08 if i % 2 else 0.08))
        out[name] = MarketData(k, funding=_funding(n_days, 10 + i, base=0.00002 * (i - 2)), metrics=_metrics(k, 20 + i))
    return out


@pytest.mark.parametrize("sid", list(LOTE2))
def test_lote2_rules_have_no_lookahead(sid) -> None:
    assert set(SHORT) == set(LOTE2)
    spec, rule = CATALOG[sid]
    params = {**spec.params, **SHORT[sid]}
    data = _data2()
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
