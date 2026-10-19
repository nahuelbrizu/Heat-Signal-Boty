"""Etapa 2b del meta-labeling (lab/meta/features.py, grupo derivados_mercado): reloj de disponibilidad, a mano."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from heatsignal.lab.meta import features as MF

T0 = pd.Timestamp("2024-01-01", tz="UTC")
N_DAYS = 260
H1 = pd.Timedelta(hours=1)
M5 = pd.Timedelta(minutes=5)
SYMS = MF.UNIVERSO


def _klines(seed: int) -> pd.DataFrame:
    n = N_DAYS * 1440
    rng = np.random.default_rng(seed)
    common = np.random.default_rng(0).normal(0, 0.0005, n)  # factor comun: correlacion positiva con BTC
    c = 100 * np.exp(np.cumsum(common + rng.normal(0, 0.0005, n) + 0.000002 * (seed % 3 - 1)))
    o = np.r_[100.0, c[:-1]]
    wig = rng.uniform(0, 0.0006, (2, n))
    v = rng.uniform(10, 20, n)
    return pd.DataFrame({"open": o, "high": np.maximum(o, c) * (1 + wig[0]), "low": np.minimum(o, c) * (1 - wig[1]),
                         "close": c, "volume": v, "taker_buy_volume": v * rng.uniform(0.3, 0.7, n)},
                        index=pd.date_range(T0, periods=n, freq="1min"))


def _metrics(seed: int) -> pd.DataFrame:
    idx = pd.date_range(T0, T0 + pd.Timedelta(days=N_DAYS), freq="5min", inclusive="left")
    rng = np.random.default_rng(seed + 100)
    oi = 1e5 * np.exp(np.cumsum(rng.normal(0, 0.003, len(idx))))
    return pd.DataFrame({"oi_contracts": oi, "oi_usd": oi * rng.uniform(50, 150, len(idx)),
                         "ls_top_accounts": rng.uniform(0.5, 2.5, len(idx)), "ls_global": 1.0, "taker_ratio": 1.0},
                        index=idx)


def _funding(seed: int) -> pd.Series:
    idx = pd.date_range(T0, T0 + pd.Timedelta(days=N_DAYS), freq="8h", inclusive="left")
    return pd.Series(np.random.default_rng(seed + 200).normal(1e-4, 1e-4, len(idx)), index=idx, name="rate")


def _premium(seed: int) -> pd.Series:
    idx = pd.date_range(T0, periods=N_DAYS * 1440, freq="1min")
    return pd.Series(np.random.default_rng(seed + 300).normal(0, 5e-4, len(idx)), index=idx, name="premium")


@pytest.fixture(scope="module")
def data() -> dict:
    return {s: {"k": _klines(i + 1), "m": _metrics(i), "f": _funding(i), "p": _premium(i)} for i, s in enumerate(SYMS)}


def _fuentes(data: dict) -> MF.Fuentes:
    return MF.Fuentes(lambda s: data[s]["m"], lambda s: data[s]["f"], lambda s: data[s]["p"])


def _events(times, sym: str = "ETHUSDT") -> pd.DataFrame:
    ts = pd.DatetimeIndex(times).as_unit("us")
    return pd.DataFrame({"primaria": "ema-cruce-v1", "symbol": sym, "tipo": "apertura", "t_decision": ts,
                         "lado": [1 if i % 2 else -1 for i in range(len(ts))], "tamaño": 1.0, "tamaño_previo": 0.0,
                         "barrera": "tp", "pnl_neto": 0.01, "etiqueta": 1})


TIMES = [T0 + pd.Timedelta(days=d, hours=h) for d, h in ((215, 0), (228, 13), (241, 7), (255, 16))]


def _build(data, times=TIMES, sym="ETHUSDT") -> pd.DataFrame:
    return MF.build_derivados(_events(times, sym), lambda s: data[s]["k"], _fuentes(data))


@pytest.fixture(scope="module")
def full(data) -> pd.DataFrame:
    return _build(data)


def test_features_derivados_registered_without_outcome_columns() -> None:
    from heatsignal.lab.meta import dataset as D

    g = MF.GRUPOS["derivados_mercado"]
    assert g.archivo == "features_derivados_v1.parquet" and g.features == MF.FEATURES_DERIVADOS
    assert not set(MF.FEATURES_DERIVADOS) & MF.OUTCOME_COLS
    assert not set(MF.FEATURES_DERIVADOS) & set(D.COLUMNS)
    assert not set(MF.FEATURES_DERIVADOS) & set(MF.FEATURES_BASICAS)
    assert len(set(MF.FEATURES_DERIVADOS)) == len(MF.FEATURES_DERIVADOS)


def test_output_shape(full, data) -> None:
    assert list(full.columns) == [MF.KEY, *MF.ID_COLS, *MF.FEATURES_DERIVADOS]
    num = [c for c in MF.FEATURES_DERIVADOS if c not in MF.CATEGORICAS_DERIVADOS]
    assert full[num].notna().all().all()
    assert full["cuadrante_oi_funding"].str.match(r"^oi_(sube|baja)_funding_(pos|neg)$").all()
    assert (full["n_sobre_ema200_1d"].between(0, 7)).all() and (full["corr_btc_30d"] > 0).all()
    btc = _build(data, TIMES[:1], "BTCUSDT").iloc[0]
    assert btc["corr_btc_30d"] == 1.0  # BTC contra si mismo: 1 (decision documentada)


def _poison(data: dict, t: pd.Timestamp) -> dict:
    out = {}
    for s, d in data.items():
        k, m, f, p = d["k"].copy(), d["m"].copy(), d["f"].copy(), d["p"].copy()
        a = k.index >= t  # la vela 1m que abre en t todavia no se conoce
        k.loc[a, ["open", "high", "low", "close"]] *= np.linspace(0.3, 4.0, a.sum())[:, None]
        k.loc[a, ["volume", "taker_buy_volume"]] *= [100, 1]
        late = m.index + M5 > t  # metrics se conoce en ts + 5m
        m.loc[late, ["oi_contracts", "ls_top_accounts"]] *= np.linspace(2.0, 9.0, late.sum())[:, None]
        f.loc[f.index > t] = 0.05
        p.loc[p.index >= t] = 0.9
        out[s] = {"k": k, "m": m, "f": f, "p": p}
    return out


@pytest.mark.parametrize("i", range(len(TIMES)))
def test_poisoning_after_decision_changes_nothing(data, full, i) -> None:
    got = _build(_poison(data, TIMES[i]))
    pd.testing.assert_series_equal(got.iloc[i], full.iloc[i], check_names=False)
    if i < len(TIMES) - 1:  # control: los eventos posteriores SI cambian
        assert not np.isclose(got.iloc[i + 1]["funding_actual"], full.iloc[i + 1]["funding_actual"])
        assert not np.isclose(got.iloc[i + 1]["delta_oi_24h"], full.iloc[i + 1]["delta_oi_24h"])
        assert not np.isclose(got.iloc[i + 1]["ret_7d_canasta"], full.iloc[i + 1]["ret_7d_canasta"])


def test_metrics_availability_edge(data) -> None:
    t = TIMES[1]
    d = {s: dict(v) for s, v in data.items()}
    m = data["ETHUSDT"]["m"].copy()
    m.loc[t - M5, "oi_contracts"] = 123_456.0  # snapshot de t-5m: disponible justo en t -> se usa
    edge = pd.DataFrame({"oi_contracts": [9e9], "oi_usd": [1.0], "ls_top_accounts": [99.0], "ls_global": [1.0],
                         "taker_ratio": [1.0]}, index=[t - pd.Timedelta(minutes=4)])  # disponible en t+1m -> no
    d["ETHUSDT"]["m"] = pd.concat([m, edge]).sort_index()
    f = _build(d, [t]).iloc[0]
    base = m.loc[t - H1 - M5, "oi_contracts"]
    assert f["delta_oi_1h"] == pytest.approx(123_456.0 / base - 1)
    assert f["ls_top_cuentas"] == pytest.approx(m.loc[t - M5, "ls_top_accounts"])


def test_funding_only_settled_at_or_before_decision(data) -> None:
    t = TIMES[2] - pd.Timedelta(hours=7)  # 00:00: liquidacion justo en t
    d = {s: dict(v) for s, v in data.items()}
    fr = data["ETHUSDT"]["f"].copy()
    fr.loc[t] = 0.0123
    fr.loc[t + pd.Timedelta(minutes=1)] = -0.5  # posterior: no se conoce
    d["ETHUSDT"]["f"] = fr.sort_index()
    f = _build(d, [t, t + pd.Timedelta(hours=7)])
    assert f.iloc[0]["funding_actual"] == 0.0123
    assert f.iloc[1]["funding_actual"] == -0.5  # control: a las 07:00 la ultima liquidacion es la de 00:01


def test_zero_oi_snapshot_is_skipped(data) -> None:
    t = TIMES[1]
    d = {s: dict(v) for s, v in data.items()}
    m = data["ETHUSDT"]["m"].copy()
    m.loc[t - M5, "oi_contracts"] = 0.0  # basura: vale el snapshot valido anterior
    d["ETHUSDT"]["m"] = m
    f = _build(d, [t]).iloc[0]
    assert f["delta_oi_1h"] == pytest.approx(m.loc[t - 2 * M5, "oi_contracts"] / m.loc[t - H1 - M5, "oi_contracts"] - 1)


def test_delta_oi_is_in_contracts_not_usd(data, full) -> None:
    d = {s: dict(v) for s, v in data.items()}
    m = data["ETHUSDT"]["m"].copy()
    m["oi_usd"] = m["oi_usd"] * np.linspace(0.1, 10, len(m))  # el USD cambia, los contratos no
    d["ETHUSDT"]["m"] = m
    got = _build(d)
    cols = [c for c in full.columns if c.startswith(("delta_oi", "oi_rel"))]
    pd.testing.assert_frame_equal(got[cols], full[cols])
    m2 = m.copy()
    m2["oi_contracts"] = 5e4  # contratos constantes -> ΔOI = 0 aunque el USD se mueva
    d["ETHUSDT"]["m"] = m2
    got = _build(d)
    assert (got[[f"delta_oi_{k}" for k in MF.OI_DELTAS]] == 0).all().all()
    assert (got["oi_rel_media_30d"] == 1).all()


def test_hand_calculations(data, full) -> None:
    i, t = 2, TIMES[2]
    m, fr = data["ETHUSDT"]["m"], data["ETHUSDT"]["f"]
    # ΔOI 4h en contratos: snapshot disponible en t (ts = t-5m) contra el disponible en t-4h
    assert full.iloc[i]["delta_oi_4h"] == pytest.approx(
        m.loc[t - M5, "oi_contracts"] / m.loc[t - 4 * H1 - M5, "oi_contracts"] - 1)
    # z-score del funding contra las ultimas 90 liquidaciones (incluida la actual)
    r = fr[fr.index <= t].to_numpy()[-90:]
    assert full.iloc[i]["funding_z_30d"] == pytest.approx((r[-1] - r.mean()) / r.std(ddof=1))
    # correlacion de 30 dias de los retornos log de 1h con BTC
    def hc(s):
        k = data[s]["k"]
        return k.loc[k.index < t, "close"].resample("1h", closed="left", label="right").last()
    a, b = np.diff(np.log(hc("ETHUSDT").to_numpy()))[-720:], np.diff(np.log(hc("BTCUSDT").to_numpy()))[-720:]
    assert full.iloc[i]["corr_btc_30d"] == pytest.approx(np.corrcoef(a, b)[0, 1])
    # canasta: media de los retornos de 7 dias de las 7 monedas
    rets = [hc(s).iloc[-1] / hc(s).iloc[-1 - 168] - 1 for s in SYMS]
    assert full.iloc[i]["ret_7d_canasta"] == pytest.approx(np.mean(rets))


def test_missing_sources_are_nan(data) -> None:
    def nope(s):
        raise FileNotFoundError(s)
    fu = MF.Fuentes(nope, nope, nope)
    f = MF.build_derivados(_events(TIMES[:1]), lambda s: data[s]["k"], fu).iloc[0]
    assert np.isnan(f["funding_actual"]) and np.isnan(f["delta_oi_24h"]) and np.isnan(f["premium_1h"])
    assert f["cuadrante_oi_funding"] is None and not np.isnan(f["ret_7d_canasta"])


def test_build_and_load_all_three_parquets(tmp_path, data, monkeypatch) -> None:
    monkeypatch.setattr(MF, "default_fuentes", lambda: _fuentes(data))
    ev = pd.concat([_events(TIMES[:2]), _events(TIMES[1:3], "BTCUSDT")], ignore_index=True)
    ev.to_parquet(tmp_path / MF.EVENTS_FILE, index=False)
    s = MF.build(["basicos", "derivados_mercado"], tmp_path, loader=lambda sym: data[sym]["k"])
    assert s["derivados_mercado"]["n"] == 4 and (tmp_path / "features_derivados_v1_resumen.json").exists()
    allf = MF.load_all(tmp_path)
    assert len(allf) == 4 and allf[MF.KEY].is_unique
    assert {"barrera", "dist_ema20_1h_atr", "daily_trend_dia_anterior", "funding_z_30d", "ret_7d_canasta"} <= set(allf.columns)
    assert not [c for c in allf.columns if c.endswith(("_x", "_y"))]
    der = pd.read_parquet(tmp_path / "features_derivados_v1.parquet").set_index(MF.KEY)
    assert allf.set_index(MF.KEY).loc[der.index, "corr_btc_30d"].equals(der["corr_btc_30d"])
