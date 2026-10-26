"""`lab/regime.py`: htf-trend-v1 vectorizado == `features.htf.build_htf_context` en cada cierre de vela de 1h."""

from __future__ import annotations

import numpy as np
import pandas as pd

from heatsignal.features.htf import build_htf_context
from heatsignal.lab.clock import resample_bars
from heatsignal.lab.regime import daily_trend_1h
from heatsignal.schema.features import Candle1h


def _k1m(days: int = 26, seed: int = 4) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    n = days * 1440
    drift = np.repeat(rng.choice([-0.0004, 0.0, 0.0004], size=days * 6), 240)[:n]  # tramos de 4 h con tendencia
    close = 100 * np.exp(np.cumsum(rng.normal(0, 0.0012, n) + drift))
    open_ = np.r_[100.0, close[:-1]]
    hi = np.maximum(open_, close) * (1 + np.abs(rng.normal(0, 0.0005, n)))
    lo = np.minimum(open_, close) * (1 - np.abs(rng.normal(0, 0.0005, n)))
    idx = pd.date_range("2026-01-01", periods=n, freq="min", tz="UTC")
    return pd.DataFrame({"open": open_, "high": hi, "low": lo, "close": close, "volume": 1.0}, index=idx)


def test_vectorized_trend_matches_live_rule():
    k = _k1m()
    got = daily_trend_1h(k)
    bars = resample_bars(k, "1h")
    cands = [Candle1h(open_ms=int(t.value // 1_000_000) - 3_600_000, open=r.open, high=r.high, low=r.low, close=r.close,
                      source="klines") for t, r in bars.iterrows()]
    seen = set()
    for i in list(range(0, len(cands), 7)) + list(range(len(cands) - 30, len(cands))):
        t = bars.index[i]
        now = int(t.value // 1_000_000)
        want = build_htf_context("BTCUSDT", cands[: i + 1], now).daily_trend
        assert got.loc[t] == want, (t, got.loc[t], want)
        seen.add(want)
    assert {"alcista", "bajista", "lateral", "sin_datos"} <= seen  # la comparacion cubre las cuatro salidas
