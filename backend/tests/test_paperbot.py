"""Bot propio de paper trading del lote 3 (paperbot/): idempotencia, no retroactivo, costos, funding, huecos,
equivalencia con las reglas del laboratorio, append-only, parametros. Sin red: todo con fakes."""

from __future__ import annotations

import sqlite3

import numpy as np
import pandas as pd
import pytest

from heatsignal.history import klines as hk
from heatsignal.lab.strategies import CATALOG, LOTE3, MarketData, Spec
from heatsignal.paperbot import params as P
from heatsignal.paperbot.engine import EngineConfig, run_once, target_at
from heatsignal.paperbot.ledger import Book
from heatsignal.paperbot.store import PaperbotStore
from paperbot_fakes import DAY, H, MIN, SEVEN, T0, fake_binance, fill_funding, fill_history, walk

# ---- estrategia de juguete: eventos fijos (decision -> peso) ---------------------------------------------


def toy_rule(data, p):
    out = {}
    for s, md in data.items():
        last = md.klines.index[-1] + pd.Timedelta(minutes=1)  # causal: solo decisiones ya conocidas
        ev = {pd.Timestamp(t, unit="ms", tz="UTC"): float(w) for sym, t, w in p["events"] if sym == s}
        ev = {t: w for t, w in ev.items() if t <= last}
        out[s] = pd.Series(list(ev.values()), index=pd.DatetimeIndex(list(ev.keys()), tz="UTC") if not ev
                           else pd.DatetimeIndex(list(ev.keys())), dtype=float).sort_index()
    return out


def toy(events, universe=("BTCUSDT", "ETHUSDT")):
    spec = Spec("toy-v1", "test", "toy", "1h", "both", "h", {"events": [list(e) for e in events]})
    entries = {"toy-v1": {"spec_hash": spec.spec_hash(), "universe": list(universe), "params": {}}}
    return {"toy-v1": (spec, toy_rule)}, entries


@pytest.fixture
def env(tmp_path):
    syms = ["BTCUSDT", "ETHUSDT"]
    hist = fill_history(tmp_path / "hist.sqlite", syms, T0 - 3 * DAY, T0 + 12 * H)
    hist.close()
    fill_funding(tmp_path / "pb.sqlite", syms, T0 - 3 * DAY, T0 + 12 * H)
    return tmp_path


def cfg(tmp_path, catalog, entries, **kw):
    return EngineConfig(paperbot_db=tmp_path / "pb.sqlite", history_db=tmp_path / "hist.sqlite", window_days=2,
                        funding_days=3, download=False, catalog=catalog, entries=entries, **kw)


def rows(tmp_path, sql, args=()):
    c = sqlite3.connect(tmp_path / "pb.sqlite")
    c.row_factory = sqlite3.Row
    try:
        return c.execute(sql, args).fetchall()
    finally:
        c.close()


def candle(tmp_path, sym, t):
    c = sqlite3.connect(tmp_path / "hist.sqlite")
    try:
        return c.execute("SELECT open, close FROM klines WHERE symbol=? AND open_time_ms=?", (hk.internal_symbol(sym), t)).fetchone()
    finally:
        c.close()


# ---- idempotencia ------------------------------------------------------------------------------------------


def test_same_hour_twice_does_not_duplicate(env):
    cat, ent = toy([("BTCUSDT", T0 + H, 1.0)])
    c = cfg(env, cat, ent)
    assert run_once(c, now_ms=T0 + 2 * MIN)["status"] == "ok"
    r1 = run_once(c, now_ms=T0 + H + 2 * MIN)
    assert r1["status"] == "ok" and r1["detail"]["modo"] == "consecutiva"
    r2 = run_once(c, now_ms=T0 + H + 40 * MIN)
    assert r2["status"] == "skipped" and "ya hay una corrida ok" in r2["reason"]
    orders = rows(env, "SELECT * FROM orders")
    assert len(orders) == 1 and orders[0]["reason"] == "senal"
    assert len(rows(env, "SELECT * FROM equity WHERE hour_ms=?", (T0 + H,))) == 1
    assert [r["status"] for r in rows(env, "SELECT status FROM runs ORDER BY started_ms")] == ["ok", "ok", "skipped"]


def test_unique_ok_per_hour_enforced_by_db(env):
    cat, ent = toy([])
    run_once(cfg(env, cat, ent), now_ms=T0 + 2 * MIN)
    st = PaperbotStore(env / "pb.sqlite")
    with pytest.raises(sqlite3.IntegrityError):
        st.record_run({"run_id": "x", "hour_ms": T0, "started_ms": T0, "finished_ms": T0, "status": "ok",
                       "reason": None, "detail": {}, "peak_rss_kb": 0})
    st.close()


def test_append_only(env):
    cat, ent = toy([("BTCUSDT", T0, 1.0)])
    run_once(cfg(env, cat, ent), now_ms=T0 + 2 * MIN)
    c = sqlite3.connect(env / "pb.sqlite")
    for sql in ("UPDATE runs SET status='error'", "DELETE FROM orders", "DELETE FROM funding_rates", "UPDATE equity SET equity=0"):
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            c.execute(sql)
    c.close()


# ---- no retroactivo ----------------------------------------------------------------------------------------


def test_vm_off_is_a_gap_not_simulated(env):
    cat, ent = toy([("BTCUSDT", T0 + H, 1.0), ("ETHUSDT", T0 + 2 * H, 0.5), ("BTCUSDT", T0 + 3 * H, 0.0)])
    c = cfg(env, cat, ent)
    run_once(c, now_ms=T0 + 2 * MIN)
    r = run_once(c, now_ms=T0 + 5 * H + 2 * MIN)  # 4 horas sin correr
    assert r["status"] == "ok" and r["detail"]["modo"] == "hueco" and r["detail"]["hueco_horas"] == 4
    gaps = rows(env, "SELECT * FROM gaps")
    assert len(gaps) == 1 and gaps[0]["from_ms"] == T0 + H and gaps[0]["to_ms"] == T0 + 4 * H and gaps[0]["hours"] == 4
    orders = rows(env, "SELECT * FROM orders")
    # el long de BTC (abierto y cerrado durante el hueco) NO se simula; ETH se lleva al objetivo en H
    assert [(o["symbol"], o["weight_to"], o["reason"], o["decision_ms"]) for o in orders] == [("ETHUSDT", 0.5, "hueco", T0 + 5 * H)]
    assert orders[0]["fill_ms"] == T0 + 5 * H  # open de la vela que abre en H (ya cerrada a los :02)


def test_first_run_syncs_to_rule_target(env):
    cat, ent = toy([("BTCUSDT", T0 - 5 * H, -1.0)])
    r = run_once(cfg(env, cat, ent), now_ms=T0 + 2 * MIN)
    assert r["detail"]["modo"] == "inicio"
    o = rows(env, "SELECT * FROM orders")
    assert [(x["symbol"], x["weight_to"], x["reason"]) for x in o] == [("BTCUSDT", -1.0, "inicio")]


# ---- costos, funding y contabilidad ---------------------------------------------------------------------------


def test_costs_funding_and_equity_identity(env):
    cat, ent = toy([("BTCUSDT", T0 + H, 1.0), ("ETHUSDT", T0 + 8 * H, -1.0)])
    c = cfg(env, cat, ent)
    for h in range(0, 9):
        r = run_once(c, now_ms=T0 + h * H + 2 * MIN)
        assert r["status"] == "ok", r["reason"]
    btc = rows(env, "SELECT * FROM orders WHERE symbol='BTCUSDT'")[0]
    o_open, _ = candle(env, "BTCUSDT", T0 + H)
    assert btc["fill_ms"] == T0 + H and btc["price"] == pytest.approx(o_open) and btc["price_source"] == "open_1m_siguiente"
    units = 1.0 * 5000 / o_open  # 10.000 / 2 simbolos
    assert btc["units_to"] == pytest.approx(units)
    assert btc["fee"] == pytest.approx(units * o_open * 5e-4) and btc["slippage"] == pytest.approx(units * o_open * 2e-4)

    fund = rows(env, "SELECT * FROM funding_events ORDER BY symbol")
    # BTC: liquidacion de las 08:00 con el long abierto; ETH: su orden se ejecuta al open de las 08:00 -> llega tarde
    assert [(f["symbol"], f["funding_ms"]) for f in fund] == [("BTCUSDT", T0 + 8 * H)]
    _, close_before = candle(env, "BTCUSDT", T0 + 8 * H - MIN)
    assert fund[0]["cash"] == pytest.approx(-units * close_before * 0.0001)

    eth = rows(env, "SELECT * FROM orders WHERE symbol='ETHUSDT'")[0]
    eq = rows(env, "SELECT * FROM equity WHERE hour_ms=?", (T0 + 8 * H,))[0]
    _, mark_btc = candle(env, "BTCUSDT", T0 + 8 * H - MIN)
    _, mark_eth = candle(env, "ETHUSDT", T0 + 8 * H - MIN)
    expected = (10_000 + units * (mark_btc - o_open) + eth["units_to"] * (mark_eth - eth["price"])
                - btc["fee"] - btc["slippage"] - eth["fee"] - eth["slippage"] + fund[0]["cash"])
    assert eq["equity"] == pytest.approx(expected)
    assert eq["equity"] == pytest.approx(10_000 + eq["realized"] + eq["unrealized"] + eq["funding"] - eq["fees"] - eq["slippage"])
    pos = {p["symbol"]: p for p in rows(env, "SELECT * FROM positions WHERE hour_ms=?", (T0 + 8 * H,))}
    assert pos["BTCUSDT"]["weight"] == 1.0 and pos["ETHUSDT"]["weight"] == -1.0
    assert all(p["weight"] == p["target"] for p in pos.values())


def test_closed_trade_and_basket(env):
    cat, ent = toy([("BTCUSDT", T0 + H, 1.0), ("BTCUSDT", T0 + 2 * H + 17 * MIN, 0.0)])
    c = cfg(env, cat, ent)
    for h in range(0, 4):
        run_once(c, now_ms=T0 + h * H + 2 * MIN)
    tr = rows(env, "SELECT * FROM trades")
    assert len(tr) == 1 and tr[0]["entry_ms"] == T0 + H and tr[0]["exit_ms"] == T0 + 2 * H + 17 * MIN
    o_in, _ = candle(env, "BTCUSDT", T0 + H)
    o_out, _ = candle(env, "BTCUSDT", T0 + 2 * H + 17 * MIN)  # salida intra-hora: open del minuto de la decision
    u = 5000 / o_in
    assert tr[0]["pnl_price"] == pytest.approx(u * (o_out - o_in))
    assert tr[0]["pnl_net"] == pytest.approx(tr[0]["pnl_price"] - (u * o_in + u * o_out) * 7e-4)
    bk = rows(env, "SELECT * FROM basket ORDER BY symbol")
    assert [b["start_ms"] for b in bk] == [T0, T0]  # canasta desde la primera corrida
    eq = rows(env, "SELECT * FROM equity ORDER BY hour_ms")
    assert eq[0]["basket_equity"] == pytest.approx(10_000 - 10_000 * 7e-4)


def test_ledger_reversal_splits_costs():
    b = Book(["X"], capital=1000.0)
    b.order("X", 0, 0, 100.0, 1.0, "senal", "open_1m_siguiente")           # long 10 u
    b.order("X", 1, 1, 110.0, -1.0, "senal", "open_1m_siguiente")          # reversion a short ~9.09 u
    p = b.pos["X"]
    assert len(p.closed) == 1
    t = p.closed[0]
    assert t["pnl_price"] == pytest.approx(10 * 10)
    close_cost = 10 * 110 * 7e-4
    assert t["fees"] + t["slippage"] == pytest.approx(10 * 100 * 7e-4 + close_cost)
    tot = b.totals({"X": 105.0})
    assert tot["equity"] == pytest.approx(1000 + 100 + p.units * (105 - 110) - tot["fees"] - tot["slippage"])


# ---- huecos de datos => no opera ---------------------------------------------------------------------------


def test_missing_minute_blocks_trading_then_gap(tmp_path):
    syms = ["BTCUSDT", "ETHUSDT"]
    fill_history(tmp_path / "hist.sqlite", syms, T0 - 3 * DAY, T0 + 12 * H, skip={"ETHUSDT": [T0 + 2 * H - 5 * MIN]}).close()
    fill_funding(tmp_path / "pb.sqlite", syms, T0 - 3 * DAY, T0 + 12 * H)
    cat, ent = toy([("BTCUSDT", T0 + 2 * H, 1.0)])
    c = cfg(tmp_path, cat, ent)
    assert run_once(c, now_ms=T0 + 2 * MIN)["status"] == "ok"
    r = run_once(c, now_ms=T0 + 2 * H + 2 * MIN)
    assert r["status"] == "error" and "hueco de datos" in r["reason"] and "ETHUSDT" in r["reason"]
    assert rows(tmp_path, "SELECT COUNT(*) FROM orders")[0][0] == 0
    assert rows(tmp_path, "SELECT COUNT(*) FROM equity WHERE hour_ms=?", (T0 + 2 * H,))[0][0] == 0
    # cuando el minuto faltante sale de las ultimas 3 h vuelve a operar, como hueco (no rellena)
    r = run_once(c, now_ms=T0 + 5 * H + 2 * MIN)
    assert r["status"] == "ok" and r["detail"]["modo"] == "hueco"
    assert [o["reason"] for o in rows(tmp_path, "SELECT * FROM orders")] == ["hueco"]


def test_stale_klines_and_stale_funding_are_errors(env):
    cat, ent = toy([])
    r = run_once(cfg(env, cat, ent), now_ms=T0 + 20 * H + 2 * MIN)  # la historia termina en T0+12h
    assert r["status"] == "error" and "ultima vela" in r["reason"]
    # funding: cache sin liquidaciones recientes
    syms = ["BTCUSDT", "ETHUSDT"]
    d = env / "b"
    d.mkdir()
    fill_history(d / "hist.sqlite", syms, T0 - 3 * DAY, T0 + 12 * H).close()
    fill_funding(d / "pb.sqlite", syms, T0 - 3 * DAY, T0 - DAY)
    r = run_once(cfg(d, cat, ent), now_ms=T0 + 2 * MIN)
    assert r["status"] == "error" and "funding atrasado" in r["reason"]


def test_missing_history_start_is_error(tmp_path):
    syms = ["BTCUSDT", "ETHUSDT"]
    fill_history(tmp_path / "hist.sqlite", syms, T0 - DAY, T0 + 2 * H).close()  # ventana pide 2 dias
    fill_funding(tmp_path / "pb.sqlite", syms, T0 - 3 * DAY, T0 + 2 * H)
    cat, ent = toy([])
    r = run_once(cfg(tmp_path, cat, ent), now_ms=T0 + 2 * MIN)
    assert r["status"] == "error" and "la historia empieza" in r["reason"]


def test_rule_exception_is_recorded(env):
    def boom(data, p):
        raise RuntimeError("regla rota")
    spec = Spec("boom-v1", "test", "x", "1h", "both", "h", {})
    r = run_once(cfg(env, {"boom-v1": (spec, boom)}, {"boom-v1": {"spec_hash": spec.spec_hash(), "universe": ["BTCUSDT"],
                                                                  "params": {}}}), now_ms=T0 + 2 * MIN)
    assert r["status"] == "error" and "regla rota" in r["reason"]


# ---- descarga incremental (fakes de Binance) ---------------------------------------------------------------


def test_download_klines_and_funding_with_fake_binance(tmp_path):
    syms = ["BTCUSDT", "ETHUSDT"]
    end = T0 + 2 * H
    fill_history(tmp_path / "hist.sqlite", syms, T0 - 3 * DAY, T0 - H).close()  # base atrasada 3 h
    arrays = {s: walk(s, T0 - 3 * DAY, end + 10 * MIN) for s in syms}
    funding = {s: [(t, 0.0001) for t in range(T0 - 4 * DAY, end, 8 * H)] for s in syms}
    calls = []
    cat, ent = toy([])
    c = cfg(tmp_path, cat, ent)
    c.download = True
    r = run_once(c, now_ms=T0 + 2 * H + 2 * MIN, http_get=fake_binance(arrays, funding, calls), sleep=lambda s: None)
    assert r["status"] == "ok", r["reason"]
    assert r["detail"]["velas_nuevas"]["BTCUSDT"] == 3 * 60 + 2  # hasta la vela H+1m (cerrada a los :02)
    st = PaperbotStore(tmp_path / "pb.sqlite")
    assert st.last_funding_ms("BTCUSDT") == T0  # fundingTime con ms de mas, redondeado al minuto
    st.close()
    assert any("/fapi/v1/fundingRate" in u for u in calls) and any("/fapi/v1/klines" in u for u in calls)


def test_funding_http_error_is_error(tmp_path):
    syms = ["BTCUSDT", "ETHUSDT"]
    fill_history(tmp_path / "hist.sqlite", syms, T0 - 3 * DAY, T0 + 2 * H).close()
    arrays = {s: walk(s, T0 - 3 * DAY, T0 + 2 * H) for s in syms}

    def get(url, timeout):
        if "fundingRate" in url:
            return 429, b'{"code":-1003}'
        return fake_binance(arrays, {})(url, timeout)
    cat, ent = toy([])
    c = cfg(tmp_path, cat, ent)
    c.download = True
    r = run_once(c, now_ms=T0 + H + 2 * MIN, http_get=get, sleep=lambda s: None)
    assert r["status"] == "error" and "funding" in r["reason"]


# ---- misma posicion objetivo que la regla del laboratorio --------------------------------------------------


@pytest.fixture(scope="module")
def lote3_env(tmp_path_factory):
    d = tmp_path_factory.mktemp("lote3")
    now = T0 + 5 * H + 2 * MIN
    fill_history(d / "hist.sqlite", SEVEN, T0 - 70 * DAY, T0 + 6 * H).close()
    rng = np.random.default_rng(3)
    rates = {}

    def rate(s, t):
        return rates.setdefault((s, t), float(rng.normal(0.0001, 0.0003)))
    fill_funding(d / "pb.sqlite", SEVEN, T0 - 110 * DAY, T0 + 6 * H, rate=rate)
    return d, now


def _lab_data(d, end_ms, days=70):
    from heatsignal.paperbot.engine import load_klines
    from heatsignal.history.store import HistoryStore

    hist = HistoryStore(d / "hist.sqlite")
    st = PaperbotStore(d / "pb.sqlite")
    out = {}
    for s in SEVEN:
        k = load_klines(hist, s, T0 - days * DAY, end_ms)
        f = st.funding_rates(s, 0, end_ms)
        out[s] = MarketData(k, pd.Series([r[1] for r in f], index=pd.to_datetime([r[0] for r in f], unit="ms", utc=True)))
    hist.close()
    st.close()
    return out


def test_targets_match_lab_rules_with_longer_history(lote3_env):
    d, now = lote3_env
    Hh = now - now % H
    c = EngineConfig(paperbot_db=d / "pb.sqlite", history_db=d / "hist.sqlite", download=False)
    r = run_once(c, now_ms=now)
    assert r["status"] == "ok", r["reason"]
    lab = _lab_data(d, Hh)  # 70 dias (el bot usa 64), solo velas cerradas en H
    entries = P.load()
    nonzero = 0
    for sid in LOTE3:
        spec, rule = CATALOG[sid]
        params = P.effective(sid, entries[sid], spec)
        universe = entries[sid]["universe"]
        sig = rule({s: lab[s] for s in universe}, params)
        got = r["detail"]["estrategias"][sid]["targets"]
        for s in universe:
            want = target_at(sig.get(s), Hh)
            assert got[s] == pytest.approx(want, abs=1e-9), (sid, s)
            nonzero += abs(want) > 0
    assert nonzero >= 7  # al menos tsmom tiene posicion en los 7: la comparacion no es trivial
    pos = rows(d, "SELECT * FROM positions WHERE hour_ms=?", (Hh,))
    assert all(p["weight"] == pytest.approx(p["target"]) for p in pos)


def test_rule_never_sees_open_candle(lote3_env):
    """Las velas >= H (ya en la base) no cambian los objetivos: el bot solo le pasa a la regla velas cerradas en H."""
    d, now = lote3_env
    Hh = now - now % H
    lab_closed = _lab_data(d, Hh, days=64)
    lab_more = _lab_data(d, Hh + 30 * MIN, days=64)
    spec, rule = CATALOG["rango-reversion-v1"]
    p = spec.params
    a = rule({s: lab_closed[s] for s in p["symbols"]}, p)
    b = rule({s: lab_more[s] for s in p["symbols"]}, p)
    # con velas posteriores a H la regla puede decidir cosas en (H, H+30m]: el bot las ignora porque no le llegan
    for s in p["symbols"]:
        assert target_at(a.get(s), Hh) == pytest.approx(target_at(b.get(s), Hh))


# ---- parametros --------------------------------------------------------------------------------------------


def test_params_json_matches_catalog_and_grid():
    entries = P.load()
    assert set(entries) == set(LOTE3)
    for sid in LOTE3:
        spec = CATALOG[sid][0]
        eff = P.effective(sid, entries[sid], spec)
        assert eff == {**spec.params, **entries[sid]["params"]}


def test_params_validation():
    spec = CATALOG["tsmom-diario-v1"][0]
    good = {"spec_hash": spec.spec_hash(), "universe": SEVEN, "params": {"target_vol": 0.25}}
    assert P.effective("tsmom-diario-v1", good, spec)["target_vol"] == 0.25
    with pytest.raises(P.ParamsError, match="fuera de la grilla"):
        P.effective("tsmom-diario-v1", {**good, "params": {"target_vol": 0.20}}, spec)
    with pytest.raises(P.ParamsError, match="no esta en la grilla"):
        P.effective("tsmom-diario-v1", {**good, "params": {"target_vol": 0.15, "lookback_hours": 24}}, spec)
    with pytest.raises(P.ParamsError, match="la regla cambio"):
        P.effective("tsmom-diario-v1", {**good, "spec_hash": "x"}, spec)
    with pytest.raises(P.ParamsError, match="falta elegir"):
        P.effective("tsmom-diario-v1", {**good, "params": {}}, spec)


def test_params_from_lab_detail():
    spec = CATALOG["pares-eth-btc-v1"][0]
    det = {"spec_hash": spec.spec_hash(), "run": "r1",
           "walk_forward": {"chosen": [{"test": ["2026-01-01", "2026-01-31"], "params": {"entry_z": 2.0}},
                                       {"test": ["2026-02-01", "2026-03-02"], "params": {"entry_z": 2.5}}]}}
    p, origin = P.params_from_lab_detail(det, spec)
    assert p == {"entry_z": 2.5} and "walk-forward" in origin
    p, origin = P.params_from_lab_detail({**det, "holdout": {"selected_params": {"entry_z": 2.0}}}, spec)
    assert p == {"entry_z": 2.0} and "holdout" in origin
    with pytest.raises(P.ParamsError):
        P.params_from_lab_detail({**det, "spec_hash": "otro"}, spec)


def test_cli_reset_not_allowed(capsys):
    from heatsignal.paperbot.__main__ import main

    assert main(["reset-not-allowed"]) == 2
    assert "append-only" in capsys.readouterr().out
