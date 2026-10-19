"""Etapa 2c del meta-labeling (lab/meta/features.py, grupos macro y lateralidad): reloj de disponibilidad, a mano."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from heatsignal.features.htf import DAY_MS, HOUR_MS, build_levels, wilder_atr
from heatsignal.history.hourly import aggregate_minutes
from heatsignal.lab.meta import features as MF
from heatsignal.macro.calendar import parse_calendar
from heatsignal.macro.store import available_ms_for

T0 = pd.Timestamp("2024-01-01", tz="UTC")
N_DAYS = 70
H1 = pd.Timedelta(hours=1)
SYMS = MF.UNIVERSO
# 13:00 (dia en curso), 08:00 (Asia recien cerrada), 07:00 (Asia en curso), 00:00 (dia nuevo)
TIMES = [T0 + pd.Timedelta(days=d, hours=h) for d, h in ((45, 13), (52, 8), (58, 7), (64, 0))]


def _klines(seed: int) -> pd.DataFrame:
    n = N_DAYS * 1440
    rng = np.random.default_rng(seed)
    c = 100 * np.exp(np.cumsum(rng.normal(0, 0.0008, n)))
    o = np.r_[100.0, c[:-1]]
    wig = rng.uniform(0, 0.0006, (2, n))
    v = rng.uniform(10, 20, n)
    return pd.DataFrame({"open": o, "high": np.maximum(o, c) * (1 + wig[0]), "low": np.minimum(o, c) * (1 - wig[1]),
                         "close": c, "volume": v, "taker_buy_volume": v * 0.5},
                        index=pd.date_range(T0, periods=n, freq="1min"))


@pytest.fixture(scope="module")
def kl() -> dict[str, pd.DataFrame]:
    return {s: _klines(i + 1) for i, s in enumerate(SYMS)}


def _events(times, sym: str = "ETHUSDT") -> pd.DataFrame:
    ts = pd.DatetimeIndex(times).as_unit("us")
    return pd.DataFrame({"primaria": "ema-cruce-v1", "symbol": sym, "tipo": "apertura", "t_decision": ts,
                         "lado": 1, "tamaño": 1.0, "tamaño_previo": 0.0, "barrera": "tp", "pnl_neto": 0.01,
                         "etiqueta": 1})


# ---- datos macro sinteticos (export csv.gz, formato de lab/macro_features.py) -----------------------------------


def _fred_rows() -> pd.DataFrame:
    rows = []
    days = pd.date_range("2022-12-01", "2024-03-31", freq="D")
    for i, d in enumerate(days):
        pub = (d + pd.Timedelta(days=1)).strftime("%Y-%m-%d")  # se publica al dia siguiente
        for sid, v in (("DFF", 5.0 + 0.001 * i), ("DGS10", 4.0 + 0.002 * i), ("DTWEXBGS", 120 + 0.01 * i),
                       ("VIXCLS", 15 + 5 * np.sin(i / 9))):
            rows.append((sid, d.strftime("%Y-%m-%d"), v, pub))
    for i, m in enumerate(pd.date_range("2022-01-01", "2024-02-01", freq="MS")):
        pub = (m + pd.DateOffset(months=1) + pd.Timedelta(days=12)).strftime("%Y-%m-%d")  # CPI: ~13 del mes sig.
        rows.append(("CPIAUCSL", m.strftime("%Y-%m-%d"), 300 * 1.003 ** i, pub))
    df = pd.DataFrame(rows, columns=["series_id", "obs_date", "value", "realtime_start"])
    df["realtime_clipped"] = 0
    df["available_ms"] = [available_ms_for(r) for r in df["realtime_start"]]
    return df


def _poly_rows(start: pd.Timestamp, end: pd.Timestamp) -> pd.DataFrame:
    ts = pd.date_range(start, end, freq="1h")
    rows = []
    for i, t in enumerate(ts):
        ms = int(t.value // 1_000_000)
        rows.append(("m1", ms, "clob-history", "fed", "Will there be no change in Fed interest rates?", 0.5 + i * 1e-4))
        rows.append(("m2", ms, "clob-history", "fed", "Will the Fed decrease interest rates by 50+ bps?", 0.1))
        rows.append(("r1", ms, "clob-history", "recesion", "US recession by end of 2024?", 0.2 + i * 1e-5))
    return pd.DataFrame(rows, columns=["market_id", "fetched_ms", "source", "config_key", "question", "prob"])


def _write_src(path: Path, fred: pd.DataFrame, poly: pd.DataFrame | None) -> Path:
    d = path / "2024" / "01" / "01"
    d.mkdir(parents=True, exist_ok=True)
    fred.to_csv(d / "fred_observations.csv.gz", index=False)
    if poly is not None:
        poly.to_csv(d / "polymarket_snapshots.csv.gz", index=False)
    return path


CAL = parse_calendar({
    "calendar_id": "test", "timezone": "America/New_York", "verified_at": "2024-01-01",
    "sources": {"FOMC": "f", "CPI": "c", "NFP": "n"},
    "events": [{"kind": "FOMC", "date": "2024-01-31", "time": "14:00"}, {"kind": "FOMC", "date": "2024-03-20", "time": "14:00"},
               {"kind": "CPI", "date": "2024-02-13", "time": "08:30"}, {"kind": "CPI", "date": "2024-03-12", "time": "08:30"},
               {"kind": "NFP", "date": "2024-02-02", "time": "08:30"}, {"kind": "NFP", "date": "2024-03-08", "time": "08:30"}],
})


@pytest.fixture(scope="module")
def fred() -> pd.DataFrame:
    return _fred_rows()


def _macro(tmp: Path, fred: pd.DataFrame, poly: pd.DataFrame | None, times=TIMES) -> pd.DataFrame:
    return MF.build_macro(_events(times), None, _write_src(tmp, fred, poly), CAL)


# ---- registro ----------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("grupo,archivo,feats", [("macro", "features_macro_v1.parquet", MF.FEATURES_MACRO),
                                                 ("lateralidad", "features_lateralidad_v1.parquet",
                                                  MF.FEATURES_LATERALIDAD)])
def test_groups_registered_without_outcome_columns(grupo, archivo, feats) -> None:
    from heatsignal.lab.meta import dataset as D

    g = MF.GRUPOS[grupo]
    assert g.archivo == archivo and g.features == feats
    assert not set(feats) & MF.OUTCOME_COLS and not set(feats) & set(D.COLUMNS)
    others = [f for n, s in MF.GRUPOS.items() if n != grupo for f in s.features]
    assert not set(feats) & set(others) and len(set(feats)) == len(feats)


def test_btc_daily_trend_prev_day_reuses_the_same_rule(kl) -> None:
    from heatsignal.lab.meta.features import Fuentes

    nope = Fuentes(lambda s: None, lambda s: None, lambda s: None)
    der = MF.build_derivados(_events(TIMES), lambda s: kl[s], nope)
    bas = MF.build_basicos(_events(TIMES, "BTCUSDT"), lambda s: kl[s])
    assert "btc_daily_trend_dia_anterior" in MF.FEATURES_DERIVADOS
    assert der["btc_daily_trend_dia_anterior"].tolist() == bas["daily_trend_dia_anterior"].tolist()


# ---- macro -------------------------------------------------------------------------------------------------------


def test_macro_hand_calculations(tmp_path, fred) -> None:
    f = _macro(tmp_path, fred, None).iloc[0]
    t = TIMES[0]
    known = fred[pd.to_datetime(fred["available_ms"], unit="ms", utc=True) <= t]

    def ser(sid):
        s = known[known.series_id == sid]
        return pd.Series(s["value"].to_numpy(), index=pd.to_datetime(s["obs_date"])).sort_index()
    assert f["fred_dff"] == pytest.approx(ser("DFF").iloc[-1])
    assert f["fred_dgs10"] == pytest.approx(ser("DGS10").iloc[-1])
    cpi = ser("CPIAUCSL")
    assert f["fred_cpi_yoy"] == pytest.approx(cpi.iloc[-1] / cpi[cpi.index[-1] - pd.DateOffset(years=1)] - 1)
    dx = ser("DTWEXBGS")
    assert f["fred_dtwexbgs_chg30d"] == pytest.approx(dx.iloc[-1] / dx[dx.index[-1] - pd.Timedelta(days=30)] - 1)
    vix = ser("VIXCLS")
    w = vix[vix.index > vix.index[-1] - pd.Timedelta(days=365)]
    assert f["fred_vixcls"] == pytest.approx(vix.iloc[-1])
    assert f["fred_vixcls_pctl_1a"] == pytest.approx((w <= w.iloc[-1]).mean())


def test_fred_point_in_time_late_publication_is_invisible(tmp_path, fred) -> None:
    t = TIMES[1]
    base = _macro(tmp_path / "a", fred, None).iloc[1]
    late_ms = int((t + pd.Timedelta(minutes=1)).value // 1_000_000)
    dff = fred[(fred.series_id == "DFF") & (fred.available_ms <= t.value // 1_000_000)]
    extra = pd.DataFrame([
        ("DFF", dff["obs_date"].max(), 99.0, "x", 0, late_ms),  # revision tardia de la ultima obs conocida
        ("DGS10", (t - pd.Timedelta(hours=12)).strftime("%Y-%m-%d"), 77.0, "x", 0, late_ms),  # obs nueva, tardia
        ("CPIAUCSL", "2024-01-01", 1e6, "x", 0, late_ms),
    ], columns=fred.columns)
    got = _macro(tmp_path / "b", pd.concat([fred, extra]), None).iloc[1]
    pd.testing.assert_series_equal(got, base, check_names=False)
    # control: la misma revision disponible en t SI aparece
    extra["available_ms"] = int(t.value // 1_000_000)
    got = _macro(tmp_path / "c", pd.concat([fred, extra]), None).iloc[1]
    assert got["fred_dgs10"] == 77.0 and got["fred_dff"] == 99.0 and got["fred_cpi_yoy"] != base["fred_cpi_yoy"]


def test_macro_poisoning_after_decision_changes_nothing(tmp_path, fred) -> None:
    poly = _poly_rows(TIMES[0] - pd.Timedelta(days=2), TIMES[-1] + pd.Timedelta(days=2))
    full = _macro(tmp_path / "a", fred, poly)
    for i, t in enumerate(TIMES):
        f2, p2 = fred.copy(), poly.copy()
        f2.loc[f2["available_ms"] > t.value // 1_000_000, "value"] *= 10
        p2.loc[p2["fetched_ms"] > t.value // 1_000_000, "prob"] = 0.99
        got = _macro(tmp_path / f"p{i}", f2, p2)
        pd.testing.assert_series_equal(got.iloc[i], full.iloc[i], check_names=False)
        if i < len(TIMES) - 1:  # control: los posteriores SI cambian
            assert got.iloc[i + 1]["fred_dff"] != full.iloc[i + 1]["fred_dff"]
            assert got.iloc[i + 1]["pm_recesion"] == 0.99


def test_calendar_counts_days_without_looking_at_outcomes(tmp_path, fred) -> None:
    times = [pd.Timestamp("2024-01-15", tz="UTC"), pd.Timestamp("2024-01-31 19:00", tz="UTC"),
             pd.Timestamp("2024-02-02 13:30", tz="UTC"), pd.Timestamp("2024-03-21", tz="UTC")]
    f = MF.calendar_features(CAL, pd.DatetimeIndex(times))
    # antes del primer FOMC listado no se sabe cual era el proximo -> NaN (fuera de cobertura)
    assert np.isnan(f["dias_hasta_fomc"].iloc[0])
    assert f["dias_hasta_fomc"].iloc[1] == 0.0  # comunicado justo en t (14:00 ET = 19:00 UTC en invierno)
    assert f["dias_hasta_fomc"].iloc[2] == pytest.approx((pd.Timestamp("2024-03-20 18:00", tz="UTC") - times[2])
                                                         / pd.Timedelta(days=1))  # 14:00 EDT
    assert f["dias_hasta_nfp"].iloc[2] == 0.0 and np.isnan(f["dias_hasta_nfp"].iloc[0])
    assert np.isnan(f["dias_hasta_cpi"].iloc[2])  # antes del primer CPI listado (02-13)
    t = pd.Timestamp("2024-02-20", tz="UTC")
    assert MF.calendar_features(CAL, pd.DatetimeIndex([t]))["dias_hasta_cpi"].iloc[0] == pytest.approx(
        (pd.Timestamp("2024-03-12 12:30", tz="UTC") - t) / pd.Timedelta(days=1))  # 08:30 EDT
    assert np.isnan(f.iloc[3]).all()  # despues del ultimo: sin proximo
    # solo fechas: sin datos FRED/Polymarket da lo mismo
    empty = fred.iloc[:0]
    a = MF.build_macro(_events(times), None, _write_src(tmp_path / "e", empty, None), CAL)
    b = MF.build_macro(_events(times), None, _write_src(tmp_path / "f", fred, None), CAL)
    cols = list(MF.CAL_KINDS.values())
    pd.testing.assert_frame_equal(a[cols], b[cols])
    pd.testing.assert_frame_equal(a[cols].set_axis(f.index), f[cols])


def test_polymarket_coverage_and_staleness(tmp_path, fred) -> None:
    t = TIMES[0]
    poly = _poly_rows(t - pd.Timedelta(days=1), t - pd.Timedelta(hours=1))
    f = _macro(tmp_path / "a", fred, poly)
    assert f.iloc[0]["pm_fed_sin_cambio"] == pytest.approx(poly[poly.market_id == "m1"]["prob"].iloc[-1])
    assert f.iloc[0]["pm_recesion"] == pytest.approx(poly[poly.market_id == "r1"]["prob"].iloc[-1])
    assert f.iloc[1:][list(MF.PM_COLS)].isna().all().all()  # > 3 h de antiguedad -> NaN
    s = f.attrs["resumen"]["polymarket"]
    assert s["cobertura_pct"] == 25.0 and s["usable"]
    # historia posterior a todos los eventos: cobertura 0, columnas presentes y NaN, declarado
    late = _poly_rows(TIMES[-1] + H1, TIMES[-1] + 5 * H1)
    g = _macro(tmp_path / "b", fred, late)
    assert set(MF.PM_COLS) <= set(g.columns) and g[list(MF.PM_COLS)].isna().all().all()
    assert g.attrs["resumen"]["polymarket"]["cobertura_pct"] == 0.0 and not g.attrs["resumen"]["polymarket"]["usable"]


# ---- lateralidad -------------------------------------------------------------------------------------------------


@pytest.fixture(scope="module")
def lat(kl) -> pd.DataFrame:
    return MF.build_lateralidad(_events(TIMES), lambda s: kl[s])


def _live_levels(k: pd.DataFrame, t: pd.Timestamp):
    """Camino del vivo: 1m -> `aggregate_minutes` -> velas cerradas -> `build_levels`."""
    now = int(t.value // 1_000_000)
    first, last = MF.htf_window(now)
    w = k[(k.index >= pd.Timestamp(first, unit="ms", tz="UTC")) & (k.index < pd.Timestamp(last + HOUR_MS, unit="ms",
                                                                                             tz="UTC"))]
    rows = list(zip((w.index.as_unit("ms").asi8).tolist(), w["open"], w["high"], w["low"], w["close"], w["volume"]))
    cs = [c for _, c in sorted(aggregate_minutes(rows, "klines", 60).items())]
    day = now - now % DAY_MS
    today = [c for c in cs if c.open_ms >= day]
    y = [c for c in cs if day - DAY_MS <= c.open_ms < day]
    prev = {"high": max(c.high for c in y), "low": min(c.low for c in y)} if len(y) == 24 else None
    atr = wilder_atr(cs)
    return build_levels(cs, today, day, prev, atr, "lateral"), atr, cs[-1]


@pytest.mark.parametrize("i", range(len(TIMES)))
def test_lateralidad_matches_live_build_levels(kl, lat, i) -> None:
    t, f = TIMES[i], lat.iloc[i]
    lv, atr, last = _live_levels(kl["ETHUSDT"], t)
    ap = lambda x: pytest.approx(x, rel=1e-9, abs=1e-12)  # noqa: E731
    assert f["adx14_1h_htf"] == ap(lv.adx14_1h)
    if lv.asia_high is not None:
        assert f["asia_rango_atr"] == ap((lv.asia_high - lv.asia_low) / atr)
        assert f["dist_asia_max_atr"] == ap((last.close - lv.asia_high) / atr)
        assert f["dist_asia_min_atr"] == ap((last.close - lv.asia_low) / atr)
    assert f["dist_max_ayer_atr"] == ap((last.close - lv.prev_day_high) / atr)
    assert f["dist_min_ayer_atr"] == ap((last.close - lv.prev_day_low) / atr)
    if lv.vwap is not None:
        assert f["dist_vwap_atr"] == ap(lv.dist_vwap_atr)
    if lv.upper_wick_frac is not None:
        assert f["mecha_sup_frac_1h"] == ap(lv.upper_wick_frac) and f["mecha_inf_frac_1h"] == ap(lv.lower_wick_frac)


def test_asia_and_vwap_nan_before_they_exist(kl, lat) -> None:
    asia = ["asia_rango_atr", "dist_asia_max_atr", "dist_asia_min_atr"]
    assert lat.iloc[[0, 1]][asia].notna().all().all()  # 13:00 y 08:00 (las 8 velas de Asia cerradas)
    assert lat.iloc[[2, 3]][asia].isna().all().all()  # 07:00 y 00:00: Asia no existe todavia
    assert lat.iloc[:3]["dist_vwap_atr"].notna().all() and np.isnan(lat.iloc[3]["dist_vwap_atr"])  # 00:00: sin VWAP
    lv, _, _ = _live_levels(kl["ETHUSDT"], TIMES[3])
    assert lv.vwap is None and lv.asia_high is None
    # 00:00: mechas de la ultima vela de 1h cerrada (23:00 de ayer)
    k = kl["ETHUSDT"]
    b = k[(k.index >= TIMES[3] - H1) & (k.index < TIMES[3])]
    o, h, low, c = b["open"].iloc[0], b["high"].max(), b["low"].min(), b["close"].iloc[-1]
    assert lat.iloc[3]["mecha_sup_frac_1h"] == pytest.approx((h - max(o, c)) / (h - low))
    assert lat.iloc[3]["mecha_inf_frac_1h"] == pytest.approx((min(o, c) - low) / (h - low))
    assert lat[MF.FEATURES_LATERALIDAD].drop(columns=asia + ["dist_vwap_atr"]).notna().all().all()


@pytest.mark.parametrize("i", range(len(TIMES)))
def test_lateralidad_poisoning_after_decision_changes_nothing(kl, lat, i) -> None:
    t = TIMES[i]
    k = kl["ETHUSDT"].copy()
    a = k.index >= t  # la vela 1m que abre en t todavia no se conoce
    k.loc[a, ["open", "high", "low", "close"]] *= np.linspace(0.3, 4.0, a.sum())[:, None]
    k.loc[a, "volume"] *= 100
    got = MF.build_lateralidad(_events(TIMES), lambda s: k)
    pd.testing.assert_series_equal(got.iloc[i], lat.iloc[i], check_names=False)
    if i < len(TIMES) - 1:
        assert not np.isclose(got.iloc[i + 1]["dist_max_ayer_atr"], lat.iloc[i + 1]["dist_max_ayer_atr"])


# ---- union -------------------------------------------------------------------------------------------------------


def test_build_and_load_all_four_parquets(tmp_path, kl, fred, monkeypatch) -> None:
    monkeypatch.setattr(MF, "default_fuentes", lambda: MF.Fuentes(lambda s: None, lambda s: None, lambda s: None))
    monkeypatch.setattr(MF, "MACRO_DB", _write_src(tmp_path / "macro", fred, _poly_rows(TIMES[0] - H1, TIMES[0])))
    from heatsignal.macro import calendar as C
    monkeypatch.setattr(C, "load_calendar", lambda: CAL)
    ev = pd.concat([_events(TIMES[:2]), _events(TIMES[1:3], "BTCUSDT")], ignore_index=True)
    ev.to_parquet(tmp_path / MF.EVENTS_FILE, index=False)
    s = MF.build(["basicos", "derivados_mercado", "macro", "lateralidad"], tmp_path, loader=lambda sym: kl[sym])
    assert s["macro"]["polymarket"]["cobertura_pct"] == 25.0 and "nan_por_construccion" in s["lateralidad"]
    allf = MF.load_all(tmp_path)
    assert len(allf) == 4 and allf[MF.KEY].is_unique
    feats = [f for g in MF.GRUPOS.values() for f in g.features]
    assert set(feats) <= set(allf.columns) and not [c for c in allf.columns if c.endswith(("_x", "_y"))]
    for g, spec in (("macro", MF.FEATURES_MACRO), ("lateralidad", MF.FEATURES_LATERALIDAD)):
        p = pd.read_parquet(tmp_path / MF.GRUPOS[g].archivo).set_index(MF.KEY)
        pd.testing.assert_frame_equal(allf.set_index(MF.KEY).loc[p.index, spec], p[spec])
