"""Alertas de oportunidad (fase 3, 2026-11-21): base append-only, deteccion de entradas / salidas / giros sobre velas
cerradas, corrida completa con una base historica sintetica y las rutas del panel."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import httpx
import pandas as pd
import pytest

from heatsignal.alerts import engine as E
from heatsignal.alerts.store import AlertError, AlertStore
from heatsignal.history.store import HistoryStore
from heatsignal.lab.strategies import Spec, _changes

MIN = 60_000
T0 = 1_795_000_000_000 - 1_795_000_000_000 % 3_600_000


def _alert(sid="s1", sym="BTCUSDT", t=T0, kind="entrada", **kw):
    return {"alert_id": E.alert_id(sid, sym, t, kind), "run_id": "r", "created_ms": t, "decision_ms": t, "strategy_id": sid,
            "symbol": sym, "kind": kind, "direction": "long", "target": 1.0, "prev_target": 0.0, "price": 100.0,
            "timeframe": "5min", "source": "catalogo", "lab_status": "no pasa", "lab_sharpe": -1.0, "paper_role": None,
            "exit_plan": "máx. 1 h", **kw}


def test_store_is_append_only_dedupes_and_marks_auto(tmp_path: Path) -> None:
    st = AlertStore(tmp_path / "a.sqlite")
    st.set_auto("s2", True)
    assert st.record_run("r1", "ok", window=(T0 - 5 * MIN, T0), alerts=[_alert(), _alert(sid="s2")]) == 2
    assert st.record_run("r2", "ok", window=(T0, T0 + 5 * MIN), alerts=[_alert()]) == 0  # misma alerta: no se duplica
    by = {a["strategy_id"]: a for a in st.recent()}
    assert by["s1"]["state"] == "nueva" and by["s2"]["state"] == "auto"
    assert [a["strategy_id"] for a in st.queued()] == ["s2"]
    st.act(by["s1"]["alert_id"], "enviada")
    with pytest.raises(AlertError, match="ya fue"):
        st.act(by["s1"]["alert_id"], "descartada")
    assert {a["strategy_id"] for a in st.queued()} == {"s1", "s2"}
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        st.conn.execute("DELETE FROM alerts")
    assert st.last_ok_window_end() == T0 + 5 * MIN


def test_changes_in_window_kinds() -> None:
    ts = lambda m: pd.Timestamp(T0 + m * MIN, unit="ms", tz="UTC")  # noqa: E731
    sig = _changes({ts(-30): 1.0, ts(-10): 0.0, ts(2): 0.5, ts(3): 1.0, ts(4): -1.0, ts(6): 0.0})
    got = E.changes_in_window(sig, T0, T0 + 5 * MIN)
    assert [(t - T0) // MIN for t, *_ in got] == [2, 4]  # 0.5 -> 1.0 es solo tamano: no alerta
    assert [E._kind(p, n) for _, p, n in got] == ["entrada", "giro"]


def _history(path: Path, end_ms: int, days: int = 2) -> None:
    hist = HistoryStore(path)
    start = end_ms - days * 86_400_000
    rows, p = [], 100.0
    for i, t in enumerate(range(start, end_ms, MIN)):
        p *= 1.0001 if (i // 60) % 2 == 0 else 0.9999
        rows.append(("binance", "BTC-USDT", "1m", t, p, p * 1.001, p * 0.999, p, 10.0, 1000.0, 5, 500.0))
    hist.add_klines(rows)
    hist.close()


def _catalog(enter_ms: int):
    spec = Spec("prueba-v1", "test", "entra en un instante fijo", "5min", "long", "10 min", {"symbols": ["BTCUSDT"]})

    def rule(data, p):
        k = data["BTCUSDT"].klines
        assert k.index[-1] < pd.Timestamp(enter_ms + 10 * MIN, unit="ms", tz="UTC") + pd.Timedelta(minutes=30)
        t = pd.Timestamp(enter_ms, unit="ms", tz="UTC")
        return {"BTCUSDT": _changes({t: 1.0, t + pd.Timedelta(minutes=10): 0.0})}

    daily = Spec("diaria-v1", "test", "", "1D", "long", "1 d", {})
    metrics = Spec("metrics-v1", "test", "", "5min", "long", "1 h", {}, needs=("klines", "metrics"))
    return {"prueba-v1": (spec, rule), "diaria-v1": (daily, rule), "metrics-v1": (metrics, rule)}


def test_run_once_detects_entry_then_exit_without_duplicates(tmp_path: Path) -> None:
    _history(tmp_path / "h.sqlite", T0 + 30 * MIN)
    cfg = E.AlertConfig(alerts_db=tmp_path / "a.sqlite", history_db=tmp_path / "h.sqlite", symbols=["BTCUSDT"],
                        download=False, catalog=_catalog(T0 - 2 * MIN))
    out = E.run_once(cfg, now_ms=T0 + 20_000)  # T = T0: la entrada (T0-2) cae en la ventana (T0-5, T0]
    assert out["status"] == "ok" and out["n_eval"] == 1 and out["alerts"] == 1
    out2 = E.run_once(cfg, now_ms=T0 + 10 * MIN + 20_000)
    assert out2["alerts"] == 1  # la salida (T0+8); la entrada no se repite
    st = AlertStore(tmp_path / "a.sqlite")
    kinds = sorted(a["kind"] for a in st.recent())
    run = st.runs(1)[0]
    assert kinds == ["entrada", "salida"] and run["detail"]["salteadas"] == 2
    assert set(run["detail"]["salteadas_por_motivo"]) == {"temporalidad 1D", "necesita ['metrics'] (metrics 5m"}


def test_engine_only_feeds_closed_candles(tmp_path: Path) -> None:
    _history(tmp_path / "h.sqlite", T0 + 30 * MIN)  # la base tiene velas "del futuro"
    seen = {}

    def rule(data, p):
        seen["last"] = data["BTCUSDT"].klines.index[-1]
        return {"BTCUSDT": _changes({})}

    spec = Spec("ver-v1", "test", "", "5min", "long", "", {"symbols": ["BTCUSDT"]})
    cfg = E.AlertConfig(alerts_db=tmp_path / "a.sqlite", history_db=tmp_path / "h.sqlite", symbols=["BTCUSDT"],
                        download=False, catalog={"ver-v1": (spec, rule)})
    E.run_once(cfg, now_ms=T0 + 20_000)
    assert seen["last"] == pd.Timestamp(T0 - MIN, unit="ms", tz="UTC")  # ultima vela cerrada: apertura T - 1m


def test_run_once_refuses_stale_candles(tmp_path: Path) -> None:
    _history(tmp_path / "h.sqlite", T0 - 30 * MIN)  # la ultima vela es de hace 30 min
    cfg = E.AlertConfig(alerts_db=tmp_path / "a.sqlite", history_db=tmp_path / "h.sqlite", symbols=["BTCUSDT"],
                        download=False, catalog=_catalog(T0 - 2 * MIN))
    out = E.run_once(cfg, now_ms=T0 + 20_000)
    assert out["status"] == "error" and "velas atrasadas" in out["error"]
    assert AlertStore(tmp_path / "a.sqlite").recent() == []


async def test_panel_alert_routes(tmp_path: Path) -> None:
    from heatsignal.api.panel import PanelContext, create_panel_app
    from heatsignal.ingestion.health import HealthRegistry

    db = tmp_path / "alerts.sqlite"
    st = AlertStore(db)
    st.record_run("r1", "ok", window=(T0 - 5 * MIN, T0), alerts=[_alert(t=T0)])
    st.close()
    ctx = PanelContext(health=HealthRegistry(), alerts_db=db)
    H = {"X-HeatSignal-Action": "1"}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_panel_app(ctx)), base_url="http://panel") as c:
        r = await c.get("/api/alerts", params={"hours": 24 * 30})
        assert r.status_code == 200 and r.json()["configured"] is True
        (a,) = r.json()["alerts"]
        assert (await c.post(f"/api/alerts/{a['alert_id']}/action", json={"action": "enviada"})).status_code in (400, 403)
        r = await c.post(f"/api/alerts/{a['alert_id']}/action", json={"action": "enviada"}, headers=H)
        assert r.status_code == 200 and r.json()["state"] == "enviada"
        assert (await c.post(f"/api/alerts/{a['alert_id']}/action", json={"action": "descartada"}, headers=H)).status_code == 409
        r = await c.post("/api/alerts/auto", json={"strategy_id": "s1", "enabled": True}, headers=H)
        assert r.json()["auto_map"] == {"s1": True}
        r = await c.get("/api/alerts", params={"hours": 24 * 30})
        assert r.json()["queued"] == 1 and r.json()["auto"] == {"s1": True}
    empty = PanelContext(health=HealthRegistry(), alerts_db=tmp_path / "nope.sqlite")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_panel_app(empty)), base_url="http://panel") as c:
        assert (await c.get("/api/alerts")).json()["configured"] is False
