"""Etapa 2a del meta-labeling (lab/meta/features.py, grupo basicos): sin look-ahead, velas cerradas, a mano."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from heatsignal.features.htf import build_htf_context
from heatsignal.history.hourly import aggregate_minutes
from heatsignal.lab.meta import features as MF

T0 = pd.Timestamp("2024-01-01", tz="UTC")
N_DAYS = 400
H1 = pd.Timedelta(hours=1)


def _klines(n_days: int = N_DAYS, seed: int = 7) -> pd.DataFrame:
    n = n_days * 1440
    rng = np.random.default_rng(seed)
    mu = np.repeat(rng.choice([-1.0, 1.0], n // (1440 * 5) + 1) * 0.00003, 1440 * 5)[:n]
    c = 100 * np.exp(np.cumsum(rng.normal(0, 0.0007, n) + mu))
    o = np.r_[100.0, c[:-1]]
    wig = rng.uniform(0, 0.0006, (2, n))
    return pd.DataFrame({"open": o, "high": np.maximum(o, c) * (1 + wig[0]), "low": np.minimum(o, c) * (1 - wig[1]),
                         "close": c, "volume": rng.uniform(10, 20, n)},
                        index=pd.date_range(T0, periods=n, freq="1min"))


@pytest.fixture(scope="module")
def kl() -> pd.DataFrame:
    return _klines()


def _events(times) -> pd.DataFrame:
    ts = pd.DatetimeIndex(times).as_unit("us")
    return pd.DataFrame({"primaria": "ema-cruce-v1", "symbol": "BTCUSDT", "tipo": "apertura", "t_decision": ts,
                         "lado": [1 if i % 2 else -1 for i in range(len(ts))], "tamaño": 1.0, "tamaño_previo": 0.0,
                         "barrera": "tp", "pnl_neto": 0.01, "etiqueta": 1})


TIMES = [T0 + pd.Timedelta(days=d, hours=h) for d, h in ((250, 0), (271, 13), (300, 7), (333, 19), (365, 0), (390, 22))]


@pytest.fixture(scope="module")
def full(kl) -> pd.DataFrame:
    return MF.build_basicos(_events(TIMES), lambda s: kl)


def test_features_basicas_has_no_outcome_columns() -> None:
    from heatsignal.lab.meta import dataset as D

    assert not set(MF.FEATURES_BASICAS) & MF.OUTCOME_COLS
    assert not {c for c in MF.FEATURES_BASICAS if c in D.COLUMNS} - {"lado", "tamaño", "tipo"}
    assert {"barrera", "pnl_neto", "t_salida", "mae", "mfe", "etiqueta"} <= MF.OUTCOME_COLS
    assert len(set(MF.FEATURES_BASICAS)) == len(MF.FEATURES_BASICAS)


def test_output_shape_and_key(full) -> None:
    assert list(full.columns) == [MF.KEY, *MF.ID_COLS, *MF.FEATURES_BASICAS]
    assert full[MF.KEY].is_unique and full[MF.KEY].iloc[0].startswith("ema-cruce-v1|BTCUSDT|")
    num = [c for c in MF.FEATURES_BASICAS if c not in MF.CATEGORICAS_BASICAS]
    assert full[num].notna().all().all()  # con 250+ dias de historia nada queda NaN
    assert set(full["daily_trend"]) <= {"alcista", "bajista", "lateral", "sin_datos"}


@pytest.mark.parametrize("i", range(len(TIMES)))
def test_poisoning_after_decision_changes_nothing(kl, full, i) -> None:
    t = TIMES[i]
    k = kl.copy()
    after = k.index >= t  # la vela 1m que ABRE en t_decision todavia no se conoce
    k.loc[after, ["open", "high", "low", "close"]] *= np.linspace(0.3, 4.0, after.sum())[:, None]
    k.loc[after, "high"] *= 1.05
    k.loc[after, "volume"] *= 100
    got = MF.build_basicos(_events(TIMES), lambda s: k)
    pd.testing.assert_series_equal(got.iloc[i], full.iloc[i], check_names=False)


def test_poisoning_changes_later_events(kl, full) -> None:  # control: el envenenamiento SI se nota despues
    k = kl.copy()
    after = k.index >= TIMES[2]
    k.loc[after, ["open", "high", "low", "close"]] *= 2.0
    k.loc[after, "high"] *= 1.05
    got = MF.build_basicos(_events(TIMES), lambda s: k)
    assert not np.isclose(got.iloc[3]["mecha_sup_atr_1h"], full.iloc[3]["mecha_sup_atr_1h"])


def test_candle_closing_at_decision_is_known_and_next_is_not(kl) -> None:
    t = TIMES[1]  # 13:00 UTC: la vela 12:00-13:00 cierra justo en t
    k = kl.copy()
    win = (k.index >= t - H1) & (k.index < t)
    k.loc[win, "close"] = k.loc[win, "open"]  # cuerpo 0 en la vela que cierra en t
    nxt = (k.index >= t) & (k.index < t + H1)
    k.loc[nxt, "high"] *= 1.5
    f = MF.build_basicos(_events([t]), lambda s: k).iloc[0]
    h = k[win]
    body = abs(h["close"].iloc[-1] - h["open"].iloc[0]) / (h["high"].max() - h["low"].min())
    assert f["body_pct_1h"] == pytest.approx(body)
    # diaria: a las 00:00 la vela del dia que termina es conocida
    t0 = TIMES[0]
    d = kl[(kl.index >= t0 - pd.Timedelta(days=1)) & (kl.index < t0)]
    fd = MF.build_basicos(_events([t0]), lambda s: kl).iloc[0]
    rng = d["high"].max() - d["low"].min()
    assert fd["close_pos_1d"] == pytest.approx((d["close"].iloc[-1] - d["low"].min()) / rng)
    assert fd["body_pct_1d"] == pytest.approx(abs(d["close"].iloc[-1] - d["open"].iloc[0]) / rng)


def test_hand_calculations(kl, full) -> None:
    i, t = 2, TIMES[2]
    hb = kl[kl.index < t].resample("1h", closed="left", label="right").agg(
        {"open": "first", "high": "max", "low": "min", "close": "last"})
    # retorno log de 24 velas de 1h
    assert full.iloc[i]["ret_log_24h"] == pytest.approx(np.log(hb["close"].iloc[-1] / hb["close"].iloc[-25]))
    # (close - EMA20) / ATR14 de Wilder, con bucles explicitos
    c, hi, lo = hb["close"].to_numpy(), hb["high"].to_numpy(), hb["low"].to_numpy()
    ema, alpha = c[0], 2 / 21
    for x in c[1:]:
        ema = alpha * x + (1 - alpha) * ema
    tr = np.maximum(hi[1:] - lo[1:], np.maximum(abs(hi[1:] - c[:-1]), abs(lo[1:] - c[:-1])))
    atr = hi[0] - lo[0]
    for x in tr:
        atr = atr + (x - atr) / 14
    assert full.iloc[i]["dist_ema20_1h_atr"] == pytest.approx((c[-1] - ema) / atr, rel=1e-9)
    assert full.iloc[i]["atr_pct_1h"] == pytest.approx(atr / c[-1], rel=1e-9)
    # vol realizada de 1 dia: desvio de los ultimos 24 retornos log 1h, en unidades diarias
    r = np.diff(np.log(c))[-24:]
    assert full.iloc[i]["vol_real_1d"] == pytest.approx(r.std(ddof=1) * np.sqrt(24))
    # contexto
    assert full.iloc[i]["hora_utc"] == 7 and full.iloc[i]["dia_semana"] == t.dayofweek


def test_percentile_is_backward_only() -> None:
    rng = np.random.default_rng(1)
    x = pd.Series(rng.normal(size=300))
    p = MF.pct_rank_trailing(x, 50, 25)
    for j in (30, 120, 299):
        w = x.iloc[max(0, j - 49): j + 1]
        assert p.iloc[j] == pytest.approx((w <= x.iloc[j]).mean())
    y = x.copy()
    y.iloc[121:] = 1e9  # el futuro no mueve el percentil de hoy
    assert MF.pct_rank_trailing(y, 50, 25).iloc[:121].equals(p.iloc[:121])
    assert np.isnan(p.iloc[23])


def test_daily_trend_matches_htf_live_window(kl, full) -> None:
    seen = set()
    rows = list(zip(kl.index.as_unit("ms").asi8.tolist(), kl["open"], kl["high"], kl["low"], kl["close"]))
    for i, t in enumerate(TIMES):
        now = int(t.value // 1_000_000)
        first, last = MF.htf_window(now)
        sub = [r for r in rows if first <= r[0] < last + 3_600_000]
        cs = list(aggregate_minutes(sub, "klines", 60).values())
        ref = build_htf_context("BTCUSDT", cs, now).daily_trend
        seen.add(ref)
        assert full.iloc[i]["daily_trend"] == ref
    assert len(seen) >= 3  # el caso ejercita varias ramas de la regla (sin_datos a las 00:00, lateral, tendencia)


def test_daily_trend_prev_day_is_rule_at_close_of_previous_day(kl, full) -> None:
    rows = list(zip(kl.index.as_unit("ms").asi8.tolist(), kl["open"], kl["high"], kl["low"], kl["close"]))
    seen = set()
    for i, t in enumerate(TIMES):
        d0 = t.floor("D")
        now = int(d0.value // 1_000_000)
        first, last = MF.htf_window(now)
        cs = list(aggregate_minutes([r for r in rows if first <= r[0] < last + 3_600_000], "klines", 60).values())
        ref = build_htf_context("BTCUSDT", cs, now, day_start_ms=now - 86_400_000)
        assert ref.day_hours_closed == 24  # el dia anterior completo
        seen.add(ref.daily_trend)
        assert full.iloc[i]["daily_trend_dia_anterior"] == ref.daily_trend
    assert "sin_datos" not in seen and len(seen) >= 2
    # a las 00:00 daily_trend es sin_datos pero el del dia anterior no
    assert full.iloc[0]["daily_trend"] == "sin_datos" != full.iloc[0]["daily_trend_dia_anterior"]


def test_daily_trend_prev_day_ignores_today(kl, full) -> None:
    i, t = 1, TIMES[1]  # 13:00: las velas de hoy (00:00-13:00) ya se conocen pero no entran en el dia anterior
    k = kl.copy()
    today = k.index >= t.floor("D")
    k.loc[today, ["open", "high", "low", "close"]] *= np.linspace(1.0, 3.0, today.sum())[:, None]
    k.loc[today, "high"] *= 1.05
    got = MF.build_basicos(_events(TIMES), lambda s: k)
    assert got.iloc[i]["daily_trend_dia_anterior"] == full.iloc[i]["daily_trend_dia_anterior"]
    assert not np.isclose(got.iloc[i]["ret_log_4h"], full.iloc[i]["ret_log_4h"])  # control


def test_sessions() -> None:
    assert MF.session_name(pd.Timestamp("2025-01-15 00:00", tz="UTC")) == "asia"
    assert MF.session_name(pd.Timestamp("2025-01-15 08:00", tz="UTC")) == "europa"  # Londres en invierno
    assert MF.session_name(pd.Timestamp("2025-07-15 07:00", tz="UTC")) == "europa"  # BST
    assert MF.session_name(pd.Timestamp("2025-01-15 14:00", tz="UTC")) == "europa"  # NY abre 14:30 en invierno
    assert MF.session_name(pd.Timestamp("2025-01-15 15:00", tz="UTC")) == "eeuu"
    assert MF.session_name(pd.Timestamp("2025-01-15 23:00", tz="UTC")) == "fuera"


def test_build_and_load_all(tmp_path, kl) -> None:
    ev = _events(TIMES[:2])
    ev.to_parquet(tmp_path / MF.EVENTS_FILE, index=False)
    s = MF.build(["basicos"], tmp_path, loader=lambda sym: kl)
    assert s["basicos"]["n"] == 2 and (tmp_path / "features_basicas_v1_resumen.json").exists()
    allf = MF.load_all(tmp_path)
    assert len(allf) == 2 and {"barrera", "dist_ema20_1h_atr", MF.KEY} <= set(allf.columns)
    assert allf["primaria"].notna().all()
