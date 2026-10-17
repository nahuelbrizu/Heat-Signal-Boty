"""Reloj de disponibilidad, features sin look-ahead y carga de la historia (sin red)."""

from __future__ import annotations

import json

import numpy as np
import pandas as pd
import pytest

from heatsignal.lab import features as F
from heatsignal.lab.clock import (
    LookAheadError,
    asof,
    assert_no_lookahead,
    available_1m,
    available_metrics,
    resample_bars,
)
from heatsignal.lab.store import load_funding, load_klines, load_metrics, update_current_funding

T0 = pd.Timestamp("2026-01-01", tz="UTC")


def _klines(n: int = 600, seed: int = 1) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    close = 100 * np.exp(np.cumsum(rng.normal(0, 0.001, n)))
    open_ = np.r_[100.0, close[:-1]]
    idx = pd.date_range(T0, periods=n, freq="1min")
    qv = rng.uniform(1e5, 2e5, n)
    return pd.DataFrame({
        "open": open_, "high": np.maximum(open_, close) * 1.0005, "low": np.minimum(open_, close) * 0.9995,
        "close": close, "volume": qv / close, "quote_volume": qv, "count": 10,
        "taker_buy_volume": qv * 0.6 / close, "taker_buy_quote_volume": qv * 0.6,
    }, index=idx)


def test_resample_labels_at_close_and_drops_incomplete_blocks() -> None:
    k = _klines(120)
    bars = resample_bars(k.drop(k.index[70]), "1h")  # falta un minuto de la segunda hora
    assert list(bars.index) == [T0 + pd.Timedelta("1h")]  # fechada al CIERRE; la incompleta no esta
    first_hour = k.iloc[:60]
    assert bars.iloc[0]["open"] == first_hour["open"].iloc[0] and bars.iloc[0]["close"] == first_hour["close"].iloc[-1]
    assert bars.iloc[0]["high"] == first_hour["high"].max()
    assert bars.iloc[0]["taker_buy_quote_volume"] == pytest.approx(first_hour["taker_buy_quote_volume"].sum())


def test_availability_shifts() -> None:
    k = _klines(3)
    assert available_1m(k).index[0] == T0 + pd.Timedelta("1min")
    m = pd.DataFrame({"oi_contracts": [1.0]}, index=[T0])
    assert available_metrics(m).index[0] == T0 + pd.Timedelta("5min")


def test_asof_uses_only_available_values_with_tolerance() -> None:
    right = pd.Series([1.0, 2.0], index=[T0, T0 + pd.Timedelta("10min")], name="x")
    left = pd.DatetimeIndex([T0 - pd.Timedelta("1min"), T0 + pd.Timedelta("9min"), T0 + pd.Timedelta("40min")])
    got = asof(left, right, tolerance=pd.Timedelta("15min"))
    assert np.isnan(got.iloc[0]) and got.iloc[1] == 1.0 and np.isnan(got.iloc[2])  # viejo de mas -> NaN


@pytest.mark.parametrize("fn", [
    lambda b: F.realized_vol(b["close"], 20),
    lambda b: F.zscore(b["close"], 30),
    lambda b: F.rolling_percentile(b["close"], 50, 0.95),
    lambda b: F.taker_imbalance(b),
    lambda b: F.donchian(b, 20),
    lambda b: F.atr(b, 14),
    lambda b: F.log_returns(b["close"], 5),
])
def test_features_have_no_lookahead(fn) -> None:
    assert_no_lookahead(fn, resample_bars(_klines(1200), "5min"))


@pytest.mark.parametrize("cheat", [
    # el truco de DevilStra/GodStraNew/Zeus (freqtrade-strategies/lookahead_bias): normalizar con toda la serie
    lambda b: (b["close"] - b["close"].min()) / (b["close"].max() - b["close"].min()),
    lambda b: b["close"].rolling(21, center=True).mean(),  # ventana centrada
    lambda b: b["close"].shift(-1),  # el proximo close
])
def test_detector_catches_lookahead(cheat) -> None:
    with pytest.raises(LookAheadError):
        assert_no_lookahead(cheat, resample_bars(_klines(1200), "5min"))


def test_rolling_percentile_excludes_current_bar() -> None:
    x = pd.Series([1.0, 2, 3, 100])
    assert F.rolling_percentile(x, 3, 1.0).iloc[3] == 3.0  # el 100 actual no entra en su propio umbral


def test_store_loads_month_files_and_filters(tmp_path) -> None:
    d = tmp_path / "klines" / "XRPUSDT"
    d.mkdir(parents=True)
    k = _klines(3)
    raw = k.reset_index(names="t")
    raw["open_time"] = raw.pop("t").dt.as_unit("ms").astype("int64")
    raw.to_parquet(d / "2026-01.parquet", index=False)
    got = load_klines("xrpusdt", start=T0 + pd.Timedelta("1min"), root=tmp_path)
    assert list(got.index) == list(k.index[1:]) and got["close"].iloc[0] == k["close"].iloc[1]
    with pytest.raises(FileNotFoundError):
        load_metrics("XRPUSDT", root=tmp_path)


def test_update_current_funding_writes_month_and_loads(tmp_path) -> None:
    body = json.dumps([{"fundingTime": 1792195200005, "fundingRate": "-0.00004226", "markPrice": "1.31"},
                       {"fundingTime": 1792224000000, "fundingRate": "0.0001", "markPrice": "1.30"}]).encode()
    calls = []

    def fake_get(url, timeout):
        calls.append(url)
        return 200, body

    assert update_current_funding("XRPUSDT", root=tmp_path, http_get=fake_get, now_ms=1792230000000) == 2
    assert "startTime=1790812800000" in calls[0]  # inicio del mes UTC
    s = load_funding("XRPUSDT", root=tmp_path)
    assert s.index[0] == pd.Timestamp(1792195200000, unit="ms", tz="UTC") and s.iloc[0] == pytest.approx(-0.00004226)
