"""Prueba hacia adelante (2026-10-20): familias pre-registradas `l6-familia-ruptura-long-v1` / `-short-v1` y las 12
rupturas short de la enmienda (familia espejo). Hashes congelados, la familia = 1/12 de la suma de sus componentes,
short dispara y no dispara, sin look-ahead (el general de test_lab_strategies recorre todo el CATALOG; aca uno con
historia larga para N = 55)."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from test_lab_strategies_lote6 import LOTE6_HASHES, D, H, MIN, T0, _daily_klines, _pos, _ref_ruptura, _run

from heatsignal.lab import exits as E
from heatsignal.lab.clock import resample_bars
from heatsignal.lab.strategies import (CATALOG, L6_FAMILIA_RUPTURA_LONG, L6_FAMILIA_RUPTURA_SHORT, LOTE6, LOTE6_SHORT,
                                       MarketData)

FAMILIA_HASHES = {"l6-familia-ruptura-long-v1": "f7b68dcd66c5c7e4", "l6-familia-ruptura-short-v1": "d39d02babf1466e2"}
NETA = "l6-familia-ruptura-neta-v1"
NETA_HASH = "81005404f0426130"
# Enmienda aprobada por el usuario el 2026-10-20 (decisions.md). Cualquier cambio es una -v2.
LOTE6_SHORT_HASHES = {
    "l6-ruptura-n5-h1-short-v1": "e98f7daa2eb7ff46", "l6-ruptura-n5-h3-short-v1": "29f7f328866a85a7",
    "l6-ruptura-n5-h5-short-v1": "3bcec80fd5a90c60", "l6-ruptura-n10-h1-short-v1": "8c8c0a69ebf311e3",
    "l6-ruptura-n10-h3-short-v1": "69558a715969f6e5", "l6-ruptura-n10-h5-short-v1": "e3acc4c9c0611042",
    "l6-ruptura-n20-h1-short-v1": "f2c4244da1ac9df7", "l6-ruptura-n20-h3-short-v1": "a1008312349b1be4",
    "l6-ruptura-n20-h5-short-v1": "dd67bfd15bfef058", "l6-ruptura-n55-h1-short-v1": "05f6f134e2e0748c",
    "l6-ruptura-n55-h3-short-v1": "befbb1a7b434ba99", "l6-ruptura-n55-h5-short-v1": "a23cfee1810be310",
}
SHORTS = [(n, h) for n in (5, 10, 20, 55) for h in (1, 3, 5)]


def test_frozen_hashes_and_lote6_unchanged() -> None:
    assert {s: CATALOG[s][0].spec_hash() for s in FAMILIA_HASHES} == FAMILIA_HASHES
    assert {s: CATALOG[s][0].spec_hash() for s in LOTE6_SHORT} == LOTE6_SHORT_HASHES
    assert len(LOTE6) == 60 and {s: CATALOG[s][0].spec_hash() for s in LOTE6} == LOTE6_HASHES
    assert not set(LOTE6_SHORT) & set(LOTE6) and "l6-familia-ruptura-long-v1" not in LOTE6
    for fam, comps, side in (("l6-familia-ruptura-long-v1", L6_FAMILIA_RUPTURA_LONG, "long"),
                             ("l6-familia-ruptura-short-v1", L6_FAMILIA_RUPTURA_SHORT, "short")):
        spec = CATALOG[fam][0]
        assert spec.params["components"] == {c: 1 / 12 for c in comps} and len(comps) == 12
        assert spec.direction == side and not spec.grid and "NO se corre en el laboratorio" in spec.source
        assert all(CATALOG[c][0].params["side"] == side for c in comps)
        assert E.EXITS[fam]["hereda"] == list(comps)
    neta = CATALOG[NETA][0]
    assert neta.spec_hash() == NETA_HASH and not neta.grid and "NO se corre en el laboratorio" in neta.source
    assert neta.params["components"] == {c: 1 / 12 for c in (*L6_FAMILIA_RUPTURA_LONG, *L6_FAMILIA_RUPTURA_SHORT)}
    assert E.EXITS[NETA]["hereda"] == [*L6_FAMILIA_RUPTURA_LONG, *L6_FAMILIA_RUPTURA_SHORT]
    for s in LOTE6_SHORT:
        sp = CATALOG[s][0]
        assert sp.direction == "short" and not sp.grid and sp.needs == ("klines",)
        assert E.check_params(s, sp.params) == []


@pytest.fixture(scope="module")
def seven_walks() -> dict[str, MarketData]:
    out = {}
    for i in range(3):
        rng = np.random.default_rng(40 + i)
        out[f"S{i}"] = MarketData(_daily_klines(rng.normal(0.001 * (i - 1), 0.035, 160), p0=50.0 + i))
    return out


@pytest.mark.parametrize("fam", list(FAMILIA_HASHES))
def test_familia_is_one_twelfth_of_the_sum(seven_walks, fam) -> None:
    spec, rule = CATALOG[fam]
    sig = rule(seven_walks, spec.params)
    comps = {c: CATALOG[c][1](seven_walks, dict(CATALOG[c][0].params)) for c in spec.params["components"]}
    rng = np.random.default_rng(7)
    idx = next(iter(seven_walks.values())).klines.index
    instants = [idx[0] + pd.Timedelta(minutes=int(m)) for m in rng.integers(0, len(idx), 200)]
    instants += list(sig["S0"].index[:20])  # tambien justo en los cambios
    nonzero = 0
    for sym in seven_walks:
        assert len(sig[sym]) > 0
        for t in instants:
            want = sum(_pos(c[sym], t) for c in comps.values()) / 12
            assert _pos(sig[sym], t) == pytest.approx(want, abs=1e-12)
            nonzero += abs(want) > 0
            lo, hi = (0.0, 1.0) if fam.endswith("long-v1") else (-1.0, 0.0)
            assert lo - 1e-12 <= _pos(sig[sym], t) <= hi + 1e-12
    assert nonzero > 20


def test_neta_is_long_plus_short(seven_walks) -> None:
    """Balance de fuerzas: (long activas - short activas) / 12 = familia long + familia short, en [-1, 1]."""
    run = {f: CATALOG[f][1](seven_walks, CATALOG[f][0].params) for f in (NETA, *FAMILIA_HASHES)}
    rng = np.random.default_rng(8)
    idx = next(iter(seven_walks.values())).klines.index
    instants = [idx[0] + pd.Timedelta(minutes=int(m)) for m in rng.integers(0, len(idx), 300)]
    both = 0
    for sym in seven_walks:
        assert len(run[NETA][sym]) > 0
        for t in instants:
            lo, sh = _pos(run["l6-familia-ruptura-long-v1"][sym], t), _pos(run["l6-familia-ruptura-short-v1"][sym], t)
            got = _pos(run[NETA][sym], t)
            assert got == pytest.approx(lo + sh, abs=1e-12) and -1 - 1e-12 <= got <= 1 + 1e-12
            assert round(got * 12) == pytest.approx(got * 12, abs=1e-9)  # votos enteros
            both += lo > 0 and sh < 0
    assert both > 0  # hubo instantes con las dos posturas compitiendo


@pytest.mark.parametrize("n,h", SHORTS)
def test_short_matches_ambos_short_leg(n, h) -> None:
    """Misma condicion que la pata short de 'ambos': en una bajada suave ambas van short igual."""
    r = np.full(130, -0.01)
    a = _run(f"l6-ruptura-n{n}-h{h}-short-v1", {"X": MarketData(_daily_klines(r))})
    b = _run(f"l6-ruptura-n{n}-h{h}-ambos-v1", {"X": MarketData(_daily_klines(r))})
    assert len(a) > 0 and a.to_dict() == b.to_dict() and (a <= 0).all()
    assert _pos(a, T0 + n * D) == -1.0  # primer cierre con ventana completa ya es minimo


@pytest.mark.parametrize("n,h", SHORTS)
def test_short_fires_on_drop_and_not_in_uptrend(n, h) -> None:
    up = _run(f"l6-ruptura-n{n}-h{h}-short-v1", {"X": MarketData(_daily_klines(np.full(130, 0.01)))})
    assert up.empty  # nunca es minimo: no dispara
    r = np.full(130, 0.01)
    r[100] = -1.5
    r[101] = 1.7  # vuelve por encima: ningun minimo nuevo despues
    sig = _run(f"l6-ruptura-n{n}-h{h}-short-v1", {"X": MarketData(_daily_klines(r))})
    t = T0 + 101 * D
    assert sig.to_dict() == {t: -1.0, t + h * D: 0.0}


def test_short_matches_reference_on_random_walk() -> None:
    rng = np.random.default_rng(6)
    k = _daily_klines(rng.normal(0, 0.03, 200))
    bars = resample_bars(k, "1D")
    for n, h in SHORTS:
        sig = _run(f"l6-ruptura-n{n}-h{h}-short-v1", {"X": MarketData(k)})
        # referencia: la de 'ambos' con el precio espejado (-log) da el short del original con signo cambiado
        ref = _ref_ruptura(-np.log(bars["close"].to_numpy()), bars.index, n, h, "long")
        assert sig.to_dict() == {t: -v for t, v in ref.items()} and len(ref) > 0


@pytest.mark.parametrize("sid", ["l6-ruptura-n55-h5-short-v1", "l6-familia-ruptura-short-v1", "l6-familia-ruptura-long-v1",
                                 NETA])
def test_long_history_no_lookahead(sid) -> None:
    rng = np.random.default_rng(12)
    k = _daily_klines(rng.normal(0, 0.04, 220))
    full = _run(sid, {"X": MarketData(k)})
    assert len(full) > 0
    for days in (90, 150, 200):
        cut = T0 + days * D + 7 * H
        part = _run(sid, {"X": MarketData(k[k.index < cut])})
        a, b = part[part.index <= cut - MIN], full[full.index <= cut - MIN]
        pd.testing.assert_series_equal(a, b, check_names=False, check_freq=False, check_index_type=False)
