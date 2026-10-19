"""Lote 6 (2026-10-20, exploratorio, 60 variantes sin grilla): specs congeladas, reglas con datos sinteticos por
familia, reloj de disponibilidad (velas diarias al cierre, metrics +5 min, funding en la liquidacion, taker de la
ultima hora cerrada) y sesiones con DST. El look-ahead general lo cubre `test_rules_have_no_lookahead` sobre todo
el CATALOG; aca se agrega uno con historia larga para las variantes que en 40 dias no llegan a operar."""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from test_lab_strategies_lote2 import LOTE1_HASHES, LOTE2_HASHES
from test_lab_strategies_lote3 import LOTE3_HASHES
from test_lab_strategies_lote4 import LOTE4_HASHES
from test_lab_strategies_lote5 import LOTE5_HASHES

from heatsignal.lab import features as F
from heatsignal.lab.clock import resample_bars
from heatsignal.lab.strategies import CATALOG, LOTE1, LOTE2, LOTE3, LOTE4, LOTE5, LOTE6, MarketData

T0 = pd.Timestamp("2026-01-01", tz="UTC")
D = pd.Timedelta(days=1)
H = pd.Timedelta(hours=1)
MIN = pd.Timedelta(minutes=1)

# Aprobado por el usuario el 2026-10-20 (estrategias-lote6-exploratorio.md). Cualquier cambio es una -v2.
LOTE6_HASHES = {
    "l6-ruptura-n5-h1-long-v1": "0330705c59947b9d",
    "l6-ruptura-n5-h1-ambos-v1": "dbdfedd409bf22d6",
    "l6-ruptura-n5-h3-long-v1": "673f13061434cb8c",
    "l6-ruptura-n5-h3-ambos-v1": "f10def37da55c229",
    "l6-ruptura-n5-h5-long-v1": "d53505a3ce28d28c",
    "l6-ruptura-n5-h5-ambos-v1": "d8303fbcfddfef46",
    "l6-ruptura-n10-h1-long-v1": "4168b813a90777e7",
    "l6-ruptura-n10-h1-ambos-v1": "9e20151f04db5a75",
    "l6-ruptura-n10-h3-long-v1": "4f1ce2b0bafde7d2",
    "l6-ruptura-n10-h3-ambos-v1": "b6dade33f60497d5",
    "l6-ruptura-n10-h5-long-v1": "256b901a50fb0789",
    "l6-ruptura-n10-h5-ambos-v1": "a755d4fbe114a7de",
    "l6-ruptura-n20-h1-long-v1": "52454ae187d4c299",
    "l6-ruptura-n20-h1-ambos-v1": "cd9cc805fad53941",
    "l6-ruptura-n20-h3-long-v1": "bc7d38a8419ca195",
    "l6-ruptura-n20-h3-ambos-v1": "a9c0b566f80c753b",
    "l6-ruptura-n20-h5-long-v1": "0c2b23f60bc9f244",
    "l6-ruptura-n20-h5-ambos-v1": "f4fdb67c1055409f",
    "l6-ruptura-n55-h1-long-v1": "5a03bd8e6bcc3de6",
    "l6-ruptura-n55-h1-ambos-v1": "4b85a5e5e2c74fbb",
    "l6-ruptura-n55-h3-long-v1": "9372683732f834b1",
    "l6-ruptura-n55-h3-ambos-v1": "cb1272c19d890160",
    "l6-ruptura-n55-h5-long-v1": "b16b4de1db59b396",
    "l6-ruptura-n55-h5-ambos-v1": "c67368df63dc1fe7",
    "l6-tsmom-l7-fijo-long-v1": "4cd8fa01c63016cd",
    "l6-tsmom-l7-fijo-ambos-v1": "4ce37d7eb2085cfc",
    "l6-tsmom-l7-vol-long-v1": "88d3f9d680f1365b",
    "l6-tsmom-l7-vol-ambos-v1": "082bf4c63c0de38e",
    "l6-tsmom-l28-fijo-long-v1": "cdd18535deac2099",
    "l6-tsmom-l28-fijo-ambos-v1": "ef770aff5fac3a99",
    "l6-tsmom-l28-vol-long-v1": "1719460273c57b8b",
    "l6-tsmom-l28-vol-ambos-v1": "6adbde6631883451",
    "l6-tsmom-l84-fijo-long-v1": "48e994a90d939165",
    "l6-tsmom-l84-fijo-ambos-v1": "a5dcdcfe79805cd6",
    "l6-tsmom-l84-vol-long-v1": "855c681f30e86a96",
    "l6-tsmom-l84-vol-ambos-v1": "68eb8137cc4044b1",
    "l6-cal-lun-v1": "404bf6f8faaf6a8b",
    "l6-cal-mar-v1": "a36287e8f84597c3",
    "l6-cal-mie-v1": "727b7b48af362b95",
    "l6-cal-jue-v1": "08f7fa85db5336a7",
    "l6-cal-vie-v1": "475fde622c6b963f",
    "l6-cal-sab-v1": "30aa7e479f94d1ec",
    "l6-cal-dom-v1": "feae9a91fa634149",
    "l6-cal-finde-v1": "d549d6a74c48b86c",
    "l6-cal-asia-v1": "2c97af1289955692",
    "l6-cal-londres-v1": "a07a26bf4125e0de",
    "l6-cal-ny-v1": "8a0398676911eeb2",
    "l6-der-funding-z2-h24-v1": "a945e3dac12f5df0",
    "l6-der-funding-z2-h72-v1": "71faee8a2cc5fac6",
    "l6-der-oibaja-fneg-d3-v1": "9a171b3799d9988e",
    "l6-der-oibaja-fneg-d7-v1": "3225fed8acd20fc4",
    "l6-der-taker-p95-h4-v1": "8f96af43c95e214f",
    "l6-rsi2-long-h1-v1": "6d838d18eae51f11",
    "l6-rsi2-long-h3-v1": "2e78c8f1a7c31847",
    "l6-rsi2-short-h1-v1": "f562241e4b8c5792",
    "l6-rsi2-short-h3-v1": "8b89a5a4c3ef9d03",
    "l6-reg-adx25-long-v1": "cf410536851a9f23",
    "l6-reg-adx25-ambos-v1": "6050042683ba933e",
    "l6-reg-atrp50-long-v1": "29ec147ea2555caa",
    "l6-reg-atrp50-ambos-v1": "41c763b21f75ffe1",
}


# ---- helpers ------------------------------------------------------------------------------------------------


def _pos(s: pd.Series, t: pd.Timestamp) -> float:
    s = s[s.index <= t]
    return float(s.iloc[-1]) if len(s) else 0.0


def _minute_klines(r: np.ndarray, step: int, buy_share: np.ndarray | None = None, p0: float = 100.0,
                   start: pd.Timestamp = T0) -> pd.DataFrame:
    """Velas 1m donde cada bloque de `step` minutos tiene todo su retorno log en el primer minuto."""
    lr = np.zeros(len(r) * step)
    lr[::step] = r
    close = p0 * np.exp(np.cumsum(lr))
    open_ = np.r_[p0, close[:-1]]
    qv = np.full(len(lr), 1e5)
    share = np.full(len(lr), 0.5) if buy_share is None else np.repeat(buy_share, step)
    return pd.DataFrame({
        "open": open_, "high": np.maximum(open_, close), "low": np.minimum(open_, close), "close": close,
        "volume": qv / close, "quote_volume": qv, "count": 10, "taker_buy_volume": qv * share / close,
        "taker_buy_quote_volume": qv * share,
    }, index=pd.date_range(start, periods=len(lr), freq="1min"))


def _daily_klines(r: np.ndarray, p0: float = 100.0) -> pd.DataFrame:
    return _minute_klines(np.asarray(r, dtype=float), 1440, p0=p0)


def _run(sid: str, data: dict[str, MarketData], **override) -> pd.Series:
    spec, rule = CATALOG[sid]
    return rule(data, {**spec.params, **override})["X"]


def _entries(s: pd.Series) -> pd.Series:
    """Instantes donde la posicion pasa a no-cero (aperturas)."""
    prev = s.shift(1).fillna(0.0)
    return s[(s != 0) & (prev == 0)]


# ---- specs ---------------------------------------------------------------------------------------------------


def test_lote6_has_60_unique_frozen_specs_and_previous_lots_unchanged() -> None:
    for lot, hashes in ((LOTE1, LOTE1_HASHES), (LOTE2, LOTE2_HASHES), (LOTE3, LOTE3_HASHES), (LOTE4, LOTE4_HASHES),
                        (LOTE5, LOTE5_HASHES)):
        assert {s: CATALOG[s][0].spec_hash() for s in lot} == hashes
    assert len(LOTE6) == 60 and len(set(LOTE6)) == 60
    assert {s: CATALOG[s][0].spec_hash() for s in LOTE6} == LOTE6_HASHES
    assert all(CATALOG[s][0].strategy_id == s and s.endswith("-v1") for s in LOTE6)
    assert all(not CATALOG[s][0].grid for s in LOTE6)  # 1 intento cada una: K 51 -> 111
    assert len({CATALOG[s][0].spec_hash() for s in LOTE6}) == 60
    fams = pd.Series([s.split("-")[1] for s in LOTE6]).value_counts().to_dict()
    assert fams == {"ruptura": 24, "tsmom": 12, "cal": 11, "der": 5, "rsi2": 4, "reg": 4}
    for s in LOTE6:
        needs = CATALOG[s][0].needs
        if s.startswith("l6-der-funding"):
            assert needs == ("klines", "funding")
        elif s.startswith("l6-der-oibaja"):
            assert needs == ("klines", "funding", "metrics")
        else:
            assert needs == ("klines",)
    path = Path(__file__).resolve().parents[2] / ".claude" / "context" / "estrategias-lote6-hashes.json"
    if path.exists():
        assert json.loads(path.read_text()) == LOTE6_HASHES


# ---- 1. ruptura de N dias ----------------------------------------------------------------------------------------


def _ref_ruptura(c: np.ndarray, idx: pd.DatetimeIndex, n: int, h: int, side: str) -> dict:
    out, busy = {}, None
    for i in range(n - 1, len(c)):
        w = c[i - n + 1:i + 1]
        is_max, is_min = c[i] >= w.max(), c[i] <= w.min()
        d = 1.0 if is_max and not is_min else (-1.0 if side == "ambos" and is_min and not is_max else 0.0)
        t = idx[i]
        if d == 0 or (busy is not None and t < busy):
            continue
        out[t] = d
        busy = t + h * D
        out[busy] = 0.0
    return out


RUPTURAS = [(n, h, side) for n in (5, 10, 20, 55) for h in (1, 3, 5) for side in ("long", "ambos")]


@pytest.fixture(scope="module")
def walk_daily() -> dict[str, MarketData]:
    rng = np.random.default_rng(6)
    return {"X": MarketData(_daily_klines(rng.normal(0, 0.03, 200)))}


@pytest.mark.parametrize("n,h,side", RUPTURAS)
def test_ruptura_matches_reference_on_random_walk(walk_daily, n, h, side) -> None:
    sig = _run(f"l6-ruptura-n{n}-h{h}-{side}-v1", walk_daily)
    bars = resample_bars(walk_daily["X"].klines, "1D")
    ref = _ref_ruptura(bars["close"].to_numpy(), bars.index, n, h, side)
    assert len(ref) > 0
    assert sig.to_dict() == ref
    assert all(t.hour == 0 and t.minute == 0 for t in sig.index)  # decision al cierre diario 00:00 UTC
    if side == "long":
        assert (sig >= 0).all()


@pytest.mark.parametrize("n,h,side", RUPTURAS)
def test_ruptura_spike_holds_h_days_and_side(n, h, side) -> None:
    """Bajada suave con un salto: long en el cierre del salto por exactamente H dias. En la bajada, 'long' no opera
    nunca y 'ambos' va short."""
    r = np.full(130, -0.01)
    r[100] = 1.5
    r[101] = -1.7  # vuelve por debajo: ningun maximo nuevo despues
    sig = _run(f"l6-ruptura-n{n}-h{h}-{side}-v1", {"X": MarketData(_daily_klines(r))})
    t_spike = T0 + 101 * D  # cierre del dia 100
    if side == "long":
        assert sig.to_dict() == {t_spike: 1.0, t_spike + h * D: 0.0}
    else:
        assert (sig[sig.index < t_spike] <= 0).all() and (sig < 0).any()
        assert _pos(sig, T0 + n * D) == -1.0  # primer cierre con ventana completa ya es minimo
        # shorts encadenados cada H dias desde el dia n: en el salto se esta libre solo si (101 - n) es multiplo de H
        if (101 - n) % h == 0:
            assert sig[t_spike] == 1.0 and _pos(sig, t_spike + h * D - MIN) == 1.0
        else:
            assert _pos(sig, t_spike) == -1.0  # una posicion por vez: el salto se ignora


def test_ruptura_up_and_down_trends() -> None:
    up = {"X": MarketData(_daily_klines(np.full(60, 0.01)))}
    down = {"X": MarketData(_daily_klines(np.full(60, -0.01)))}
    s = _run("l6-ruptura-n20-h3-long-v1", up)
    assert s.index[0] == T0 + 20 * D and s.iloc[0] == 1.0 and (s.iloc[:-1] == 1.0).all()
    assert _run("l6-ruptura-n20-h3-long-v1", down).empty
    s = _run("l6-ruptura-n20-h3-ambos-v1", down)
    assert s.index[0] == T0 + 20 * D and s.iloc[0] == -1.0


# ---- 2. tsmom ----------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("lb", [7, 28, 84])
@pytest.mark.parametrize("sizing", ["fijo", "vol"])
@pytest.mark.parametrize("side", ["long", "ambos"])
def test_tsmom_sign_size_and_side(lb, sizing, side) -> None:
    sid = f"l6-tsmom-l{lb}-{sizing}-{side}-v1"
    base = np.tile([0.03, -0.01], 60)  # deriva +1 %/dia, desvio constante
    std = np.std(np.tile([0.03, -0.01], 15), ddof=1)
    w = 1.0 if sizing == "fijo" else min(0.20 / (std * np.sqrt(365)), 1.0)
    first = T0 + (max(lb, 30 if sizing == "vol" else 0) + 1) * D
    up = _run(sid, {"X": MarketData(_daily_klines(base))})
    assert up.index[0] == first and up.iloc[0] == pytest.approx(w)
    assert all(t.hour == 0 and t.minute == 0 for t in up.index)
    assert len(up) == 1  # mantiene hasta la proxima decision: sin cambios de objetivo no hay operaciones
    down = _run(sid, {"X": MarketData(_daily_klines(-base))})
    if side == "long":
        assert down.empty
    else:
        assert down.index[0] == first and down.iloc[0] == pytest.approx(-w)


def test_tsmom_flips_on_sign_change() -> None:
    r = np.r_[np.full(20, 0.01), np.full(20, -0.02)]
    s = _run("l6-tsmom-l7-fijo-ambos-v1", {"X": MarketData(_daily_klines(r))})
    assert s.iloc[0] == 1.0 and s.iloc[-1] == -1.0
    flip = s.index[-1]
    # primer cierre diario en que el retorno de 7 d es negativo
    c = np.cumsum(r)
    k = next(i for i in range(7, 40) if c[i] - c[i - 7] < 0)
    assert flip == T0 + (k + 1) * D
    assert _run("l6-tsmom-l7-fijo-long-v1", {"X": MarketData(_daily_klines(r))}).to_dict() == {T0 + 8 * D: 1.0, flip: 0.0}


# ---- 3. calendario y sesiones ------------------------------------------------------------------------------------


def _span(start: str, end: str) -> dict[str, MarketData]:
    """Solo importa el rango de datos (las reglas de calendario no usan precios)."""
    idx = pd.DatetimeIndex([pd.Timestamp(start, tz="UTC"), pd.Timestamp(end, tz="UTC")])
    k = pd.DataFrame({"open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0, "volume": 1.0}, index=idx)
    return {"X": MarketData(k)}


@pytest.mark.parametrize("wd,name", list(enumerate(["lun", "mar", "mie", "jue", "vie", "sab", "dom"])))
def test_calendar_day_of_week(wd, name) -> None:
    s = _run(f"l6-cal-{name}-v1", _span("2026-01-01", "2026-01-29"))
    ent = s[s == 1.0]
    assert len(ent) == (5 if wd == 3 else 4)  # 2026-01-01 y 2026-01-29 son jueves (ambos en el rango)
    for t in ent.index:
        assert t.dayofweek == wd and t.hour == 0 and t.minute == 0
        assert s[t + 24 * H] == 0.0
    assert set(s.unique()) == {0.0, 1.0}


def test_calendar_weekend_friday_to_sunday_24() -> None:
    s = _run("l6-cal-finde-v1", _span("2026-01-01", "2026-01-29"))
    ent = s[s == 1.0].index
    assert len(ent) == 4 and all(t.dayofweek == 4 and t.hour == 0 for t in ent)
    for t in ent:
        assert s[t + 72 * H] == 0.0 and (t + 72 * H).dayofweek == 0  # lunes 00:00 = domingo 24:00


def test_calendar_only_inside_data_range() -> None:
    s = _run("l6-cal-lun-v1", _span("2026-01-06", "2026-01-11"))  # martes a domingo: ningun lunes
    assert s.empty


@pytest.mark.parametrize("sid,days", [
    # Londres: BST desde el domingo 2026-03-29 hasta el domingo 2026-10-25
    ("l6-cal-londres-v1", {"2026-03-27": "08:00", "2026-03-30": "07:00", "2026-10-23": "07:00", "2026-10-26": "08:00"}),
    # Nueva York: EDT desde el domingo 2026-03-08 hasta el domingo 2026-11-01
    ("l6-cal-ny-v1", {"2026-03-06": "14:30", "2026-03-09": "13:30", "2026-10-30": "13:30", "2026-11-02": "14:30"}),
    ("l6-cal-asia-v1", {"2026-03-27": "00:00", "2026-03-30": "00:00", "2026-10-23": "00:00", "2026-10-26": "00:00"}),
])
def test_sessions_respect_dst(sid, days) -> None:
    s = _run(sid, _span("2026-03-01", "2026-11-10"))
    ent = s[s == 1.0]
    by_day = {t.strftime("%Y-%m-%d"): t.strftime("%H:%M") for t in ent.index}
    for d, hhmm in days.items():
        assert by_day[d] == hhmm
        t = pd.Timestamp(f"{d} {hhmm}", tz="UTC")
        assert s[t + 4 * H] == 0.0
    assert all(t.dayofweek < 5 for t in ent.index)  # solo dias habiles locales
    assert "2026-03-28" not in by_day and "2026-03-29" not in by_day


# ---- 4. derivados ------------------------------------------------------------------------------------------------


def _funding_case(spike_day: float, value: float, n_days: int = 40) -> pd.Series:
    idx = pd.date_range(T0, periods=n_days * 3, freq="8h")
    f = pd.Series(1e-4 + 1e-5 * np.tile([1.0, -1.0], len(idx))[:len(idx)], index=idx)
    f[T0 + spike_day * D] = value
    return f


@pytest.mark.parametrize("h", [24, 72])
@pytest.mark.parametrize("value,direction", [(0.01, -1.0), (-0.01, 1.0)])
def test_funding_z_extreme_trades_against_and_holds(h, value, direction) -> None:
    k = _daily_klines(np.zeros(40))
    t = T0 + 35 * D
    s = _run(f"l6-der-funding-z2-h{h}-v1", {"X": MarketData(k, funding=_funding_case(35, value))})
    assert s.to_dict() == {t: direction, t + h * H: 0.0}  # decision en la liquidacion, no antes


@pytest.mark.parametrize("h", [24, 72])
def test_funding_z_needs_30_days_and_ignores_normal_rates(h) -> None:
    k = _daily_klines(np.zeros(40))
    sid = f"l6-der-funding-z2-h{h}-v1"
    assert _run(sid, {"X": MarketData(k, funding=_funding_case(20, 0.01))}).empty  # sin 30 d de historia
    assert _run(sid, {"X": MarketData(k, funding=_funding_case(0, 1e-4))}).empty   # nada extremo


def _oi_case(drop_at: pd.Timestamp, funding_value: float) -> dict[str, MarketData]:
    k = _daily_klines(np.zeros(20))
    m_idx = pd.date_range(T0, T0 + 20 * D - 5 * MIN, freq="5min")
    oi = pd.Series(1e6, index=m_idx)
    oi[oi.index >= drop_at] = 0.9e6
    f = pd.Series(funding_value, index=pd.date_range(T0, periods=60, freq="8h"))
    return {"X": MarketData(k, funding=f, metrics=pd.DataFrame({"oi_contracts": oi, "oi_usd": oi * 100}))}


@pytest.mark.parametrize("d", [3, 7])
def test_oi_down_and_negative_funding_goes_long(d) -> None:
    sid = f"l6-der-oibaja-fneg-d{d}-v1"
    # snapshot de las 23:55 disponible a las 00:00: entra en ese cierre diario
    s = _run(sid, _oi_case(T0 + 10 * D - 5 * MIN, -1e-4))
    assert s.to_dict() == {T0 + 10 * D: 1.0, T0 + (10 + d) * D: 0.0}
    # snapshot de las 00:00 (disponible 00:05): recien lo ve el cierre del dia siguiente
    s = _run(sid, _oi_case(T0 + 10 * D, -1e-4))
    assert s.to_dict() == {T0 + 11 * D: 1.0, T0 + (11 + d) * D: 0.0}


@pytest.mark.parametrize("d", [3, 7])
def test_oi_funding_needs_both_conditions(d) -> None:
    sid = f"l6-der-oibaja-fneg-d{d}-v1"
    assert _run(sid, _oi_case(T0 + 10 * D, 1e-4)).empty                    # funding positivo
    assert _run(sid, _oi_case(T0 + 30 * D, -1e-4)).empty                   # OI no baja
    data = _oi_case(T0 + 10 * D, -1e-4)
    assert _run(sid, {"X": MarketData(data["X"].klines, funding=data["X"].funding)}).empty  # sin metrics


def _taker_case(spike_hour: int, share: float, n_hours: int = 31 * 24) -> MarketData:
    rng = np.random.default_rng(3)
    shares = rng.uniform(0.45, 0.55, n_hours)
    shares[spike_hour - 3:spike_hour] = 0.5  # sin extremos justo antes (no hay posicion abierta en el pico)
    shares[spike_hour + 1:spike_hour + 5] = 0.5  # ni justo despues (la salida a las 4 h no se pisa)
    shares[spike_hour] = share
    return MarketData(_minute_klines(rng.normal(0, 0.001, n_hours), 60, buy_share=shares))


@pytest.mark.parametrize("share,direction", [(0.95, -1.0), (0.05, 1.0)])
def test_taker_extreme_last_closed_hour_against(share, direction) -> None:
    md = _taker_case(735, share)
    s = _run("l6-der-taker-p95-h4-v1", {"X": md})
    t = T0 + 736 * H  # cierre de la hora 735: recien ahi se conoce su ratio taker
    assert s[t] == direction and s[t + 4 * H] == 0.0
    assert _pos(s, t - MIN) == 0.0
    hour = resample_bars(md.klines, "1h")
    imb = F.taker_imbalance(hour)
    hi, lo = F.rolling_percentile(imb, 720, 0.95), F.rolling_percentile(imb, 720, 0.05)
    for e in _entries(s).index:  # toda apertura coincide con un extremo de esa hora cerrada
        assert (imb[e] >= hi[e]) if s[e] < 0 else (imb[e] <= lo[e])


def test_taker_needs_30_days_of_history() -> None:
    s = _run("l6-der-taker-p95-h4-v1", {"X": _taker_case(500, 0.99)})
    assert s.empty or s.index[0] >= T0 + 721 * H


# ---- 5. RSI(2) ---------------------------------------------------------------------------------------------------


def _rsi_case(sign: float) -> dict[str, MarketData]:
    r = np.tile([0.01, -0.01], 20)
    r[30], r[31] = -0.08 * sign, -0.08 * sign
    return {"X": MarketData(_daily_klines(r))}


@pytest.mark.parametrize("side,h", [("long", 1), ("long", 3), ("short", 1), ("short", 3)])
def test_rsi2_fires_on_extremes_only(side, h) -> None:
    sid = f"l6-rsi2-{side}-h{h}-v1"
    sign = 1.0 if side == "long" else -1.0
    s = _run(sid, _rsi_case(sign))
    t = T0 + 31 * D  # cierre del dia 30 (primera vela extrema)
    assert s[t] == sign and s[t + h * D] == (0.0 if h == 3 else sign)  # con h=1 el dia 31 sigue extremo -> renueva
    assert _pos(s, t - MIN) == 0.0
    bars = resample_bars(_rsi_case(sign)["X"].klines, "1D")
    rsi = F.rsi(bars["close"], 2)
    for e in _entries(s).index:
        assert rsi[e] < 10 if side == "long" else rsi[e] > 90
    # el espejo no dispara con el lado contrario
    other = "short" if side == "long" else "long"
    assert _run(f"l6-rsi2-{other}-h{h}-v1", _rsi_case(sign)).empty


# ---- 6. regimen --------------------------------------------------------------------------------------------------


def test_regimen_adx_filter_is_subset_of_breakout_and_blocks() -> None:
    rng = np.random.default_rng(1)
    r = np.r_[rng.normal(0, 0.02, 80), np.full(40, 0.02)]  # ruido y despues tendencia fuerte (ADX alto)
    data = {"X": MarketData(_daily_klines(r))}
    bars = resample_bars(data["X"].klines, "1D")
    adx = F.adx(bars, 14)
    for side in ("long", "ambos"):
        reg = _run(f"l6-reg-adx25-{side}-v1", data)
        assert (reg > 0).any()
        for e in _entries(reg).index:
            assert adx[e] > 25
        base = _entries(_run(f"l6-ruptura-n20-h5-{side}-v1", data)).index
        assert set(_entries(reg).index) <= set(base) | set(reg.index)
        assert _run(f"l6-reg-adx25-{side}-v1", data, adx_min=101).empty


@pytest.mark.parametrize("side", ["long", "ambos"])
def test_regimen_atrp_only_in_calm_vs_last_year(side) -> None:
    r = np.r_[np.tile([0.06, -0.05], 185), np.full(50, 0.01)]  # 370 d volatiles y despues tendencia calma
    s = _run(f"l6-reg-atrp50-{side}-v1", {"X": MarketData(_daily_klines(r))})
    ent = _entries(s)
    assert len(ent) > 0 and (ent > 0).all()
    # recien en la fase calma (dia 370+) y con un anio de ATR/precio previo: ATR desde la vela 13 + 365 -> vela 378
    assert ent.index[0] >= T0 + 379 * D
    if side == "ambos":
        s = _run("l6-reg-atrp50-ambos-v1", {"X": MarketData(_daily_klines(-r))})
        assert len(_entries(s)) > 0 and (_entries(s) < 0).all()
    # al reves (calmo y despues tendencia volatil): el ATR/precio queda sobre la mediana del anio -> nunca entra
    r = np.r_[np.tile([0.006, -0.005], 185), np.full(50, 0.03)]
    assert _run(f"l6-reg-atrp50-{side}-v1", {"X": MarketData(_daily_klines(r))}).empty
    assert len(_entries(_run(f"l6-ruptura-n20-h5-{side}-v1", {"X": MarketData(_daily_klines(r))}))) > 0


# ---- look-ahead con historia larga (las que en 40 dias no operan) -------------------------------------------------


@pytest.mark.parametrize("sid", ["l6-tsmom-l84-vol-ambos-v1", "l6-tsmom-l84-fijo-long-v1", "l6-reg-atrp50-ambos-v1",
                                 "l6-ruptura-n55-h5-ambos-v1"])
def test_long_window_rules_have_no_lookahead(sid) -> None:
    rng = np.random.default_rng(11)
    r = np.r_[rng.normal(0.002, 0.04, 380), rng.normal(0.002, 0.01, 60)]
    k = _daily_klines(r)
    full = _run(sid, {"X": MarketData(k)})
    assert len(full) > 0
    for days in (200, 395, 420):
        cut = T0 + days * D + 7 * H  # corte a mitad de dia: la vela diaria incompleta no existe
        part = _run(sid, {"X": MarketData(k[k.index < cut])})
        a, b = part[part.index <= cut - MIN], full[full.index <= cut - MIN]
        pd.testing.assert_series_equal(a, b, check_names=False, check_freq=False, check_index_type=False)
