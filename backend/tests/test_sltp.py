"""SL/TP obligatorio (-v2, lab/sltp.py) y su ejecucion con estado en el bot de paper: niveles con piso/techo,
guardia de liquidacion, empate -> stop, entradas solo de la base, la operacion sigue abierta hasta tocar un nivel
(aunque la base salga), toques durante un hueco, append-only. Sin red."""

from __future__ import annotations

import sqlite3

import numpy as np
import pandas as pd
import pytest

from heatsignal.history import klines as hk
from heatsignal.history.store import HistoryStore
from heatsignal.lab import sltp as SL
from heatsignal.lab import strategies as S
from heatsignal.lab.exits import EXITS, SLTP_BASES, check_params
from heatsignal.lab.strategies import CATALOG, MarketData, Spec
from heatsignal.paperbot import params as P
from heatsignal.paperbot.engine import EngineConfig, run_once
from paperbot_fakes import DAY, H, MIN, T0, fill_funding

ts = lambda ms: pd.Timestamp(ms, unit="ms", tz="UTC")  # noqa: E731


# ---- niveles y liquidacion ------------------------------------------------------------------------------


def test_stop_fraction_floor_cap_and_target_is_twice():
    assert SL.sl_fraction(100.0, 1.0) == pytest.approx(0.08)    # 3 ATR = 3 % -> piso 8 %
    assert SL.sl_fraction(100.0, 4.0) == pytest.approx(0.12)    # 3 ATR = 12 %
    assert SL.sl_fraction(100.0, 10.0) == pytest.approx(0.20)   # 30 % -> techo 20 %
    assert SL.sl_fraction(100.0, float("nan")) is None and SL.sl_fraction(100.0, 0.0) is None
    p = SL.open_position("b", "BTCUSDT", 1.0, 1.0, 1.0, 0, 100.0, 4.0, "1D")
    assert (p.sl, p.tp) == (pytest.approx(88.0), pytest.approx(124.0))
    q = SL.open_position("b", "BTCUSDT", -1.0, 1.0, 1.0, 0, 100.0, 4.0, "1D")
    assert (q.sl, q.tp) == (pytest.approx(112.0), pytest.approx(76.0))


def test_liquidation_price_isolated():
    assert SL.liquidation_price(1, 100.0, 1.0) == 0.0                       # long 1x: no se liquida
    assert SL.liquidation_price(-1, 100.0, 1.0) == pytest.approx(200 / 1.02)
    assert SL.liquidation_price(1, 100.0, 5.0) == pytest.approx(80 / 0.98)
    assert SL.liquidation_price(-1, 100.0, 5.0) == pytest.approx(120 / 1.02)


def test_stop_never_behind_liquidation():
    # long 5x: liquidacion ~81,6; un stop a 20 % (80) quedaria detras -> no se opera asi
    with pytest.raises(SL.StopBeyondLiquidation):
        SL.check_stop(1, 100.0, 80.0, 5.0)
    # el stop a 10 % tambien: esta a mas de la mitad de la distancia a la liquidacion (18,4)
    with pytest.raises(SL.StopBeyondLiquidation):
        SL.check_stop(1, 100.0, 90.0, 5.0)
    assert SL.check_stop(1, 100.0, 92.0, 1.0) == 0.0
    # open_position achica el tamano al maximo seguro y el stop queda a <= 50 % de la distancia
    for side in (1.0, -1.0):
        p = SL.open_position("b", "X", side, 5.0, 1.0, 0, 100.0, 10.0, "1D")
        assert p.size < 5.0 and p.size == pytest.approx(SL.max_safe_leverage(side, 0.20))
        assert abs(p.ref_price - p.sl) <= 0.5 * abs(p.ref_price - p.liq_price) + 1e-9
    # a 1x (lo que corre hoy) no se achica nada
    assert SL.open_position("b", "X", -1.0, 1.0, 1.0, 0, 100.0, 10.0, "1D").size == 1.0


def test_new_entries_only_when_base_opens_or_flips():
    idx = pd.date_range("2026-01-01", periods=7, freq="h", tz="UTC")
    sig = pd.Series([0.0, 1.0, 0.5, 0.0, -1.0, 1.0, 1.0], index=idx)
    assert SL.new_entries(sig) == [(idx[1], 1.0), (idx[4], -1.0), (idx[5], 1.0)]  # el ajuste a 0,5 no es entrada


def _k1m(prices, start="2026-01-01", spread=0.001):
    idx = pd.date_range(start, periods=len(prices), freq="min", tz="UTC")
    c = np.asarray(prices, dtype=float)
    return pd.DataFrame({"open": c, "high": c * (1 + spread), "low": c * (1 - spread), "close": c}, index=idx)


def test_first_hit_tie_is_stop_and_window():
    k = _k1m([100.0] * 10)
    k.iloc[5, k.columns.get_loc("high")] = 130.0
    k.iloc[5, k.columns.get_loc("low")] = 80.0
    assert SL.first_hit(k, 1, 90.0, 120.0, k.index[0], k.index[-1] + pd.Timedelta(minutes=1)) == (k.index[5], "stop")
    assert SL.first_hit(k, 1, 90.0, 120.0, k.index[0], k.index[5]) is None       # la vela 5 no esta en la ventana
    k.iloc[5, k.columns.get_loc("low")] = 99.0
    assert SL.first_hit(k, -1, 110.0, 80.0, k.index[0], k.index[-1]) == (k.index[5], "stop")
    assert SL.first_hit(k, 1, 90.0, 120.0, k.index[0], k.index[-1]) == (k.index[5], "objetivo")


def test_advance_ignores_entries_while_open_and_exits_only_on_levels():
    k = _k1m([100.0] * 30)
    k.iloc[20, k.columns.get_loc("low")] = 91.0   # toca el stop de 92
    entries = [(k.index[2], 1.0), (k.index[10], -1.0), (k.index[25], 1.0)]
    make = lambda t, side, size: SL.open_position("b", "X", side, size, 1.0, SL._ms(t), 100.0, 1.0, "1h")  # noqa: E731
    evs, pos = SL.advance(None, entries, k, start=k.index[0], end=k.index[-1] + pd.Timedelta(minutes=1),
                          entry_after=None, make=make)
    assert [(e["kind"], e["t"], e["motivo"]) for e in evs] == [
        ("open", k.index[2], None), ("close", k.index[21], "stop"), ("open", k.index[25], None)]
    assert pos is not None and pos.side == 1.0 and pos.entry_ms == SL._ms(k.index[25])


# ---- catalogo y ficha --------------------------------------------------------------------------------


def test_every_paperbot_strategy_has_a_v2_and_v1_untouched():
    entries = P.load()
    v1 = [s for s in entries if not s.endswith("-v2")]
    assert set(SLTP_BASES) == set(v1)
    for b in SLTP_BASES:
        sid = S.sltp_id(b)
        spec = CATALOG[sid][0]
        assert SL.is_v2(spec) and not SL.is_v2(CATALOG[b][0])
        assert spec.params["base"] == b and spec.params["base_hash"] == CATALOG[b][0].spec_hash() == entries[b]["spec_hash"]
        assert spec.grid == CATALOG[b][0].grid and spec.backtestable is False
        assert {k: spec.params[k] for k in SL.PARAMS} == SL.PARAMS
        assert EXITS[sid]["sltp"] and EXITS[sid]["tiempo"] is None and EXITS[sid]["otra"] is None
        assert check_params(sid, spec.params) == []
        e = entries[sid]
        assert e["rol"] == "sltp" and e["base_v1"] == b and e["universe"] == entries[b]["universe"]
        assert e["params"] == entries[b]["params"] and e["desde"] == "2026-10-26"
        assert P.effective(sid, e, spec)["base"] == b
    assert SL.PARAMS == {"sl_atr": 3.0, "atr_n": 14, "sl_min": 0.08, "sl_max": 0.20, "rr": 2.0, "mmr": 0.02,
                         "liq_buffer": 0.5}


def test_lab_rule_family_is_one_trade_per_component():
    # dos componentes con la misma entrada: cada una su operacion; la suma ponderada da la posicion
    k = _k1m([100.0] * 3000)
    data = {"BTCUSDT": MarketData(k)}
    base = {"components": {"toy-a-v1": 0.5, "toy-b-v1": 0.5}}
    rule = lambda d, p: {"BTCUSDT": pd.Series([1.0], index=[k.index[2000]])}  # noqa: E731
    cat = {"toy-fam-v1": (Spec("toy-fam-v1", "t", "t", "1h", "long", "h", base), rule),
           "toy-a-v1": (Spec("toy-a-v1", "t", "t", "1h", "long", "h", {}), rule),
           "toy-b-v1": (Spec("toy-b-v1", "t", "t", "1h", "long", "h", {}), rule)}
    old = dict(CATALOG)
    CATALOG.update(cat)
    try:
        out = SL.run_chain(data, {"base": "toy-fam-v1", **SL.PARAMS})
    finally:
        CATALOG.clear()
        CATALOG.update(old)
    s = out["BTCUSDT"]
    assert float(s.loc[k.index[2000]]) == pytest.approx(1.0) and len(s) == 1  # sin toque: sigue abierta


# ---- bot de paper con estado ----------------------------------------------------------------------------


def _hist(path, path_fn, start, end, symbols=("BTCUSDT",)):
    """Velas 1m con precio `path_fn(t_ms)` -> (o, h, l, c)."""
    st = HistoryStore(path)
    for s in symbols:
        rows = []
        for t in range(start, end, MIN):
            o, h, lo, c = path_fn(s, t)
            rows.append((hk.EXCHANGE, hk.internal_symbol(s), "1m", t, o, h, lo, c, 1.0, c, 1, c / 2))
        st.add_klines(rows)
    st.close()


def toy_rule(data, p):
    out = {}
    for s, md in data.items():
        last = md.klines.index[-1] + pd.Timedelta(minutes=1)
        ev = {ts(t): float(w) for sym, t, w in p["events"] if sym == s and ts(t) <= last}
        out[s] = pd.Series(list(ev.values()), index=pd.DatetimeIndex(list(ev.keys()), tz="UTC"), dtype=float).sort_index()
    return out


@pytest.fixture
def v2env(tmp_path, monkeypatch):
    """BTC plano en 100 (rango de vela +-0,1 %: ATR chico -> stop en el piso de 8 %, objetivo +16 %); a las T0+3h+30m
    una vela toca 91 (stop) y a las T0+6h+10m una toca 117 (objetivo)."""
    def path(s, t):
        if t == T0 + 3 * H + 30 * MIN:
            return 100.0, 100.1, 91.0, 99.0
        if t == T0 + 6 * H + 10 * MIN:
            return 100.0, 117.0, 99.9, 100.0
        return 100.0, 100.1, 99.9, 100.0
    _hist(tmp_path / "hist.sqlite", path, T0 - 3 * DAY, T0 + 12 * H)
    fill_funding(tmp_path / "pb.sqlite", ["BTCUSDT"], T0 - 3 * DAY, T0 + 12 * H)
    # base: long en T0+30m, sale a T0+1h (la v1 cerraria ahi), long de nuevo en T0+4h+5m
    events = [["BTCUSDT", T0 + 30 * MIN, 1.0], ["BTCUSDT", T0 + H, 0.0], ["BTCUSDT", T0 + 4 * H + 5 * MIN, 1.0]]
    base = Spec("toy-v1", "test", "toy", "1h", "long", "h", {"events": events})
    monkeypatch.setitem(CATALOG, "toy-v1", (base, toy_rule))
    v2 = Spec("toy-v2", "test", "toy v2", "1h", "long", "hasta stop/objetivo", {"base": "toy-v1", **SL.PARAMS})
    cat = {"toy-v2": (v2, S.sltp_v2)}
    ent = {"toy-v2": {"spec_hash": v2.spec_hash(), "universe": ["BTCUSDT"], "params": {}, "rol": "sltp"}}
    cfg = EngineConfig(paperbot_db=tmp_path / "pb.sqlite", history_db=tmp_path / "hist.sqlite", window_days=2,
                       funding_days=3, download=False, catalog=cat, entries=ent)
    return tmp_path, cfg


def q(tmp_path, sql, args=()):
    c = sqlite3.connect(tmp_path / "pb.sqlite")
    c.row_factory = sqlite3.Row
    try:
        return c.execute(sql, args).fetchall()
    finally:
        c.close()


def test_v2_holds_until_stop_then_target(v2env):
    d, cfg = v2env
    for h in range(0, 8):
        r = run_once(cfg, now_ms=T0 + h * H + 2 * MIN)
        assert r["status"] == "ok", r["reason"]
    orders = q(d, "SELECT decision_ms, weight_to, reason FROM orders ORDER BY fill_ms, id")
    # entra a T0+30m; la base sale a T0+1h pero la v2 sigue; stop al cierre de la vela de T0+3h+30m; re-entra con la
    # entrada nueva de la base (T0+4h+5m) y sale por objetivo al cierre de la vela de T0+6h+10m
    assert [(o["decision_ms"], o["weight_to"], o["reason"]) for o in orders] == [
        (T0 + 30 * MIN, 1.0, "senal"), (T0 + 3 * H + 31 * MIN, 0.0, "senal"),
        (T0 + 4 * H + 5 * MIN, 1.0, "senal"), (T0 + 6 * H + 11 * MIN, 0.0, "senal")]
    tr = q(d, "SELECT event, entry_ms, sl, tp, sl_frac, liq_price, motivo, exit_bar_ms FROM sltp_trades ORDER BY id")
    assert [(t["event"], t["motivo"]) for t in tr] == [("open", None), ("close", "stop"), ("open", None), ("close", "objetivo")]
    assert tr[0]["sl"] == pytest.approx(92.0) and tr[0]["tp"] == pytest.approx(116.0) and tr[0]["sl_frac"] == 0.08
    assert tr[0]["liq_price"] == 0.0 and tr[1]["exit_bar_ms"] == T0 + 3 * H + 30 * MIN
    ex = q(d, "SELECT decision_ms, motivo FROM exit_reasons ORDER BY decision_ms")
    assert [(e["decision_ms"], e["motivo"]) for e in ex] == [(T0 + 3 * H + 31 * MIN, "stop"), (T0 + 6 * H + 11 * MIN, "objetivo")]
    assert q(d, "SELECT COUNT(*) FROM orders WHERE reason='resincronizacion'")[0][0] == 0
    c = sqlite3.connect(d / "pb.sqlite")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        c.execute("UPDATE sltp_trades SET sl=1")
    c.close()


def test_v2_stop_fires_during_a_gap(v2env):
    d, cfg = v2env
    assert run_once(cfg, now_ms=T0 + H + 2 * MIN)["status"] == "ok"  # entra a T0+30m (en la ultima hora)
    r = run_once(cfg, now_ms=T0 + 4 * H + 2 * MIN)                     # 2 h sin correr: el stop salto en el medio
    assert r["detail"]["modo"] == "hueco" and r["detail"]["estrategias"]["toy-v2"]["sltp"]["cierres"]["stop"] == 1
    o = q(d, "SELECT decision_ms, weight_to FROM orders ORDER BY fill_ms, id")
    assert [(x["decision_ms"], x["weight_to"]) for x in o] == [(T0 + 30 * MIN, 1.0), (T0 + 3 * H + 31 * MIN, 0.0)]


def test_v2_entries_are_not_backfilled_on_first_run(v2env):
    d, cfg = v2env
    r = run_once(cfg, now_ms=T0 + 3 * H + 2 * MIN)  # la entrada de la base (T0+30m) es anterior a la ultima hora
    assert r["status"] == "ok" and q(d, "SELECT COUNT(*) FROM orders")[0][0] == 0
    assert r["detail"]["estrategias"]["toy-v2"]["sltp"]["abiertas"] == 0


def test_panel_view_shows_levels_and_reasons(v2env):
    from heatsignal.paperbot import view as V

    d, cfg = v2env
    for h in range(0, 6):
        run_once(cfg, now_ms=T0 + h * H + 2 * MIN)
    x = V.strategy_detail(d / "pb.sqlite", "toy-v2")["sltp"]
    assert x["n_abiertas"] == 1 and x["n_cerradas"] == 1 and x["por_motivo"] == {"stop": 1, "objetivo": 0}
    a, c = x["abiertas"][0], x["cerradas"][0]
    assert a["sl"] == pytest.approx(92.0) and a["tp"] == pytest.approx(116.0) and a["tp_frac"] == pytest.approx(0.16)
    assert a["a_stop"] == pytest.approx(0.08) and a["a_objetivo"] == pytest.approx(0.16)
    assert c["motivo"] == "stop" and c["ret_nivel"] == pytest.approx(-0.08) and c["exit_ms"] == T0 + 3 * H + 31 * MIN
    s = [s for s in V.overview(d / "pb.sqlite")["strategies"] if s["strategy_id"] == "toy-v2"][0]
    assert s["rol"] == "sltp" and s["sltp"]["n_abiertas"] == 1
