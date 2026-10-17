"""Estrategias del lote: reglas correctas y SIN look-ahead (truncar los datos no cambia el pasado)."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from heatsignal.lab.strategies import CATALOG, MarketData, exits_with_levels, hold_events

T0 = pd.Timestamp("2026-01-01", tz="UTC")
MIN = pd.Timedelta("1min")


def _klines(n: int, seed: int = 0, drift: float = 0.0, shock_at: int | None = None, shock: float = 0.0) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    r = rng.normal(drift, 0.0008, n)
    if shock_at is not None:
        r[shock_at:shock_at + 10] += shock / 10
    close = 100 * np.exp(np.cumsum(r))
    open_ = np.r_[100.0, close[:-1]]
    qv = rng.uniform(1e5, 2e5, n)
    buy = qv * rng.uniform(0.4, 0.6, n)
    if shock_at is not None:
        qv[shock_at:shock_at + 10] *= 20
        buy[shock_at:shock_at + 10] = qv[shock_at:shock_at + 10] * (0.05 if shock < 0 else 0.95)
    return pd.DataFrame({
        "open": open_, "high": np.maximum(open_, close) * 1.0003, "low": np.minimum(open_, close) * 0.9997,
        "close": close, "volume": qv / close, "quote_volume": qv, "count": 10,
        "taker_buy_volume": buy / close, "taker_buy_quote_volume": buy,
    }, index=pd.date_range(T0, periods=n, freq="1min"))


def _funding(n_days: int, seed: int, base: float = 0.0001) -> pd.Series:
    rng = np.random.default_rng(seed)
    idx = pd.date_range(T0, periods=n_days * 3, freq="8h")
    return pd.Series(base + rng.normal(0, 0.00005, len(idx)), index=idx)


def _metrics(k: pd.DataFrame, seed: int) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    idx = pd.date_range(k.index[0], k.index[-1], freq="5min")
    return pd.DataFrame({"oi_contracts": 1e6 * np.exp(np.cumsum(rng.normal(0, 0.002, len(idx)))),
                         "oi_usd": 1e8}, index=idx)


# ---- utilidades -------------------------------------------------------------------------------------


def test_hold_events_no_overlap() -> None:
    e = pd.Series([1.0, -1.0, 1.0], index=[T0, T0 + pd.Timedelta("1h"), T0 + pd.Timedelta("5h")])
    s = hold_events(e, pd.Timedelta("4h"), pd.DatetimeIndex([]))
    assert list(s.items()) == [(T0, 1.0), (T0 + pd.Timedelta("4h"), 0.0),
                               (T0 + pd.Timedelta("5h"), 1.0), (T0 + pd.Timedelta("9h"), 0.0)]


def test_exits_stop_first_when_both_touched_in_same_candle() -> None:
    k = _klines(120)
    t = k.index[10]
    k.loc[k.index[12], ["high", "low"]] = [1e9, 1e-9]  # la vela toca TP y SL
    s = exits_with_levels(pd.Series([1.0], index=[t]), k, pd.Timedelta("1h"),
                          tp=pd.Series([150.0], index=[t]), sl=pd.Series([50.0], index=[t]))
    assert list(s.items()) == [(t, 1.0), (k.index[12] + MIN, 0.0)]  # sale al cierre de esa vela (el SL)


def test_exits_time_limit_when_no_level_is_hit() -> None:
    k = _klines(300)
    t = k.index[10]
    s = exits_with_levels(pd.Series([-1.0], index=[t]), k, pd.Timedelta("1h"),
                          tp=pd.Series([1.0], index=[t]), sl=pd.Series([1e6], index=[t]))
    assert list(s.items()) == [(t, -1.0), (t + pd.Timedelta("1h"), 0.0)]


# ---- reglas ------------------------------------------------------------------------------------------


def test_donchian_goes_long_in_uptrend_and_is_scaled() -> None:
    spec, rule = CATALOG["donchian-ensemble-v1"]
    sig = rule({"X": MarketData(_klines(60 * 24 * 120, drift=0.00004))}, spec.params)["X"]
    assert sig.iloc[-1] > 0 and sig.max() <= spec.params["max_leverage"] + 1e-12 and sig.min() >= 0


def test_capitulacion_fires_on_synthetic_cascade() -> None:
    spec, rule = CATALOG["capitulacion-long-v1"]
    n = 60 * 24 * 35
    k = _klines(n, seed=3, shock_at=n - 600, shock=-0.08)
    sig = rule({"X": MarketData(k)}, spec.params)["X"]
    assert (sig > 0).any() and sig.index[sig > 0][0] > k.index[n - 600]


def test_funding_xs_long_lowest_short_highest() -> None:
    spec, rule = CATALOG["funding-crowding-xs-v1"]
    data = {f"S{i}": MarketData(_klines(10), funding=_funding(30, i, base=0.0001 * i)) for i in range(6)}
    sig = rule(data, spec.params)
    first = min(s.index.min() for s in sig.values() if len(s))
    at = {s: v.get(first, 0.0) for s, v in sig.items()}
    assert sorted(at.values()) == [-1.0, 0.0, 0.0, 0.0, 0.0, 1.0]


# ---- anti look-ahead de todas las reglas backtesteables ---------------------------------------------------


def _data(n_days: int = 40) -> dict[str, MarketData]:
    out = {}
    for i in range(5):
        n = 60 * 24 * n_days
        k = _klines(n, seed=i, drift=0.00002 * (i - 2), shock_at=n // 2 + i * 777, shock=(-0.08 if i % 2 else 0.08))
        out[f"S{i}"] = MarketData(k, funding=_funding(n_days, 10 + i, base=0.0001 * (i - 2)), metrics=_metrics(k, 20 + i))
    return out


def _truncate(data: dict[str, MarketData], cut: pd.Timestamp) -> dict[str, MarketData]:
    return {s: MarketData(md.klines[md.klines.index < cut],
                          None if md.funding is None else md.funding[md.funding.index <= cut],
                          None if md.metrics is None else md.metrics[md.metrics.index < cut])
            for s, md in data.items()}


@pytest.mark.parametrize("sid", [s for s, (spec, _) in CATALOG.items() if spec.backtestable])
def test_rules_have_no_lookahead(sid) -> None:
    spec, rule = CATALOG[sid]
    data = _data()
    full = rule(data, spec.params)
    start = next(iter(data.values())).klines.index[0]
    for frac in (0.55, 0.75, 0.9):
        cut = start + pd.Timedelta(days=40 * frac)
        part = rule(_truncate(data, cut), spec.params)
        for sym, s in part.items():
            # las decisiones tomadas hasta el corte (incluidas las salidas ya fijadas) no pueden cambiar
            p = s[s.index <= cut - MIN]
            f = full.get(sym, pd.Series(dtype=float))
            f = f[f.index <= cut - MIN]
            if sid == "funding-crowding-xs-v1" or not len(p):
                pd.testing.assert_series_equal(p, f, check_names=False, check_freq=False, check_index_type=False)
                continue
            pd.testing.assert_series_equal(p, f, check_names=False, check_freq=False, check_index_type=False)


def test_catalog_specs_hash_and_metadata() -> None:
    hashes = {spec.spec_hash() for spec, _ in CATALOG.values()}
    assert len(hashes) == len(CATALOG)
    assert any(spec.family == "control" for spec, _ in CATALOG.values())
    assert not CATALOG["funding-spread-hl-cex-v1"][0].backtestable
