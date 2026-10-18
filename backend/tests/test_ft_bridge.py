"""Puente hipotesis de 1h -> Freqtrade de hipotesis (dry-run): tag, estrategia, idempotencia, par ocupado, API caida,
vista del panel y render. Sin red ni Freqtrade: API falsa (ft_bridge_fakes.FakeFt) y bases SQLite temporales."""

from __future__ import annotations

import importlib.util
import json
import shutil
import sqlite3
import subprocess
import sys
import time
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest

from ft_bridge_fakes import (H, PASSWORD, S_1H, S_1H_B, S_OLD, FakeFt, add_hyp, make_eval_db, make_ft_db, resolve_hyp,
                             write_secrets)
from heatsignal.api.panel import HypBridgeSources, PanelContext, create_panel_app
from heatsignal.ingestion.health import HealthRegistry
from heatsignal.paper import ft_bridge as fb
from heatsignal.paper import ft_bridge_view as fbv

ROOT = Path(__file__).resolve().parents[2]
STRATEGY = ROOT / "freqtrade" / "user_data_hyp" / "strategies" / "HypothesisBridgeV1.py"
HARNESS = Path(__file__).with_name("js") / "render_view.mjs"
UI = Path(__file__).resolve().parents[1] / "src" / "heatsignal" / "api" / "static" / "app" / "hyp-ft-ui.js"
NOW = 1_792_000_000.0  # epoch s
NOW_MS = int(NOW * 1000)
HID = "1a2b3c4d-0000-4000-8000-000000000001"
HID2 = "2b3c4d5e-0000-4000-8000-000000000002"


# ---- estrategia de Freqtrade (sin Freqtrade instalado: modulos falsos) ------------------------------------------


@pytest.fixture()
def strat(monkeypatch):
    ft = types.ModuleType("freqtrade")
    fs = types.ModuleType("freqtrade.strategy")
    fp = types.ModuleType("freqtrade.persistence")
    fs.IStrategy = type("IStrategy", (), {})
    fp.Trade = type("Trade", (), {})
    for name, mod in (("freqtrade", ft), ("freqtrade.strategy", fs), ("freqtrade.persistence", fp)):
        monkeypatch.setitem(sys.modules, name, mod)
    spec = importlib.util.spec_from_file_location("hyp_bridge_strategy_test", STRATEGY)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_tag_roundtrip_and_strategy_uses_same_format(strat):
    tag = fb.encode_tag(HID, 1_792_003_600.7)
    assert tag == "hyp:1a2b3c4d:1792003600"
    assert fb.decode_tag(tag) == ("1a2b3c4d", 1_792_003_600) == strat.decode_tag(tag)
    for bad in (None, "", "force_entry", "hyp:1A2B3C4D:1792003600", "hyp:1a2b3c4:1792003600", "hyp:1a2b3c4d:abc",
                "hyp:1a2b3c4d:1792003600:x"):
        assert fb.decode_tag(bad) is None and strat.decode_tag(bad) is None
    with pytest.raises(ValueError):
        fb.encode_tag("zz", 1)
    assert len(tag) <= 64


def test_custom_exit_closes_at_due_and_not_before(strat):
    s = strat.HypothesisBridgeV1()
    due = datetime(2026, 10, 18, 16, 0, tzinfo=timezone.utc)
    tag = fb.encode_tag(HID, due.timestamp())
    trade = types.SimpleNamespace(enter_tag=tag)
    kw = {"current_rate": 1.0, "current_profit": 0.0}
    assert s.custom_exit("BTC/USDT:USDT", trade, due - timedelta(seconds=1), **kw) is None
    assert s.custom_exit("BTC/USDT:USDT", trade, due, **kw) == "hyp_due"
    assert s.custom_exit("BTC/USDT:USDT", trade, due + timedelta(minutes=3), **kw) == "hyp_due"
    assert s.custom_exit("BTC/USDT:USDT", trade, (due + timedelta(seconds=5)).replace(tzinfo=None), **kw) == "hyp_due"
    assert s.custom_exit("BTC/USDT:USDT", types.SimpleNamespace(enter_tag="manual"), due, **kw) == "hyp_tag_invalido"
    # solo entra lo que manda el puente y todavia no vencio
    ce = dict(order_type="market", amount=1.0, rate=1.0, time_in_force="GTC", side="long")
    assert s.confirm_trade_entry("BTC/USDT:USDT", current_time=due - timedelta(hours=1), entry_tag=tag, **ce) is True
    assert s.confirm_trade_entry("BTC/USDT:USDT", current_time=due, entry_tag=tag, **ce) is False
    assert s.confirm_trade_entry("BTC/USDT:USDT", current_time=due, entry_tag="force_entry", **ce) is False
    # configuracion que exige el diseno
    assert s.can_short is True and s.use_exit_signal is True and s.exit_profit_only is False
    assert s.position_adjustment_enable is False and s.stoploss == -0.20 and s.minimal_roi == {"0": 100.0}
    assert s.leverage("BTC/USDT:USDT", due, 1.0, 5.0, 10.0, tag, "short") == 1.0


def test_config_hyp_is_dry_run_and_separate():
    cfg = json.loads((ROOT / "freqtrade" / "config.hyp.json").read_text())
    assert cfg["dry_run"] is True and cfg["force_entry_enable"] is True and cfg["strategy"] == "HypothesisBridgeV1"
    assert cfg["trading_mode"] == "futures" and cfg["timeframe"] == "1h" and cfg["stake_amount"] == 1000
    assert cfg["max_open_trades"] == len(cfg["exchange"]["pair_whitelist"]) == 7
    assert cfg["exchange"]["key"] == "" and cfg["exchange"]["secret"] == "" and "api_server" not in cfg
    svc = (ROOT / "deploy" / "heatsignal-freqtrade-hyp.service").read_text()
    assert "freqtrade_hyp.sqlite" in svc and "config.hyp.secrets.json" in svc and "MemoryMax=" in svc
    assert "freqtrade_dryrun.sqlite" not in svc and "config.dryrun.json" not in svc


# ---- puente --------------------------------------------------------------------------------------------------


@pytest.fixture()
def env(tmp_path):
    eval_db = make_eval_db(tmp_path / "eval.sqlite")
    ft = FakeFt()
    cfg = fb.BridgeConfig(eval_db=eval_db, bridge_db=tmp_path / "ft_bridge.sqlite", api_url="http://127.0.0.1:8082",
                          secrets_path=write_secrets(tmp_path / "config.hyp.secrets.json"), stake=1000.0, call=ft.call)
    return types.SimpleNamespace(eval_db=eval_db, ft=ft, cfg=cfg, tmp=tmp_path)


def _rows(env):
    conn = sqlite3.connect(env.cfg.bridge_db)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute("SELECT * FROM ft_bridge_orders ORDER BY id")]
    finally:
        conn.close()


def test_sends_once_and_is_idempotent(env):
    add_hyp(env.eval_db, HID, direction="SHORT", created_ts=NOW - 60, due_ts=NOW + 4 * H)
    r1 = fb.run_once(env.cfg, now_ms=NOW_MS)
    assert r1["status"] == "ok" and r1["sent"] == 1
    enters = env.ft.enters()
    assert enters == [{"pair": "BTC/USDT:USDT", "side": "short", "stakeamount": 1000.0, "leverage": 1,
                       "entry_tag": fb.encode_tag(HID, NOW + 4 * H), "ordertype": "market"}]
    r2 = fb.run_once(env.cfg, now_ms=NOW_MS + 300_000)
    r3 = fb.run_once(env.cfg, now_ms=NOW_MS + 600_000)
    assert r2["candidates"] == r3["candidates"] == 0 and r2["status"] == "idle"
    assert len(env.ft.enters()) == 1  # nunca dos veces
    rows = _rows(env)
    assert [r["status"] for r in rows] == ["sent"] and rows[0]["trade_id"] == 1 and rows[0]["open_rate"] == 100_000.0
    assert rows[0]["strategy_name"] == "1h-apertura-continuacion-a-favor-v1"
    # a nivel de motor: un segundo estado final para la misma hipotesis no entra, y la tabla es append-only
    store = fb.BridgeStore(env.cfg.bridge_db)
    try:
        assert store.add({**rows[0], "status": "skipped_late", "trade_id": None}) is False
        with pytest.raises(sqlite3.DatabaseError):
            store.conn.execute("UPDATE ft_bridge_orders SET status = 'error'")
        with pytest.raises(sqlite3.DatabaseError):
            store.conn.execute("DELETE FROM ft_bridge_orders")
    finally:
        store.close()


def test_pair_busy_is_recorded_not_silent(env):
    env.ft.open["BTC/USDT:USDT"] = {"trade_id": 9, "pair": "BTC/USDT:USDT", "enter_tag": "hyp:ffffffff:1792099999"}
    add_hyp(env.eval_db, HID, created_ts=NOW - 60, due_ts=NOW + 4 * H)
    # dos hipotesis nuevas en el mismo par libre: la primera entra, la segunda queda "par ocupado"
    add_hyp(env.eval_db, HID2, strategy=S_1H_B, symbol="SOL-USDT", created_ts=NOW - 50, due_ts=NOW + 2 * H)
    add_hyp(env.eval_db, "3c4d5e6f-0000-4000-8000-000000000003", strategy=S_1H, symbol="SOL-USDT",
            created_ts=NOW - 40, due_ts=NOW + 3 * H)
    run = fb.run_once(env.cfg, now_ms=NOW_MS)
    rows = {r["hypothesis_id"][:8]: r for r in _rows(env)}
    assert rows["1a2b3c4d"]["status"] == "skipped_pair_busy" and "trade 9" in rows["1a2b3c4d"]["detail"]
    assert rows["2b3c4d5e"]["status"] == "sent"
    assert rows["3c4d5e6f"]["status"] == "skipped_pair_busy"
    assert [e["pair"] for e in env.ft.enters()] == ["SOL/USDT:USDT"]
    assert run["sent"] == 1 and run["skipped"] == 2 and run["status"] == "ok"
    # final: no se reintenta aunque el par se libere
    env.ft.open.clear()
    fb.run_once(env.cfg, now_ms=NOW_MS + 300_000)
    assert len(env.ft.enters()) == 1


def test_502_already_open_from_freqtrade_is_pair_busy(env):
    add_hyp(env.eval_db, HID, created_ts=NOW - 60, due_ts=NOW + 4 * H)
    real = env.ft.call

    def racy(method, url, headers, body, timeout):  # el par se ocupa entre /status y /forceenter
        if url.endswith("/forceenter"):
            env.ft.open["BTC/USDT:USDT"] = {"trade_id": 5, "pair": "BTC/USDT:USDT", "enter_tag": "otro"}
        return real(method, url, headers, body, timeout)

    env.cfg.call = racy
    fb.run_once(env.cfg, now_ms=NOW_MS)
    (row,) = _rows(env)
    assert row["status"] == "skipped_pair_busy" and "already open" in row["detail"]


def test_api_down_errors_then_retries_only_if_more_than_1h_left(env):
    add_hyp(env.eval_db, HID, created_ts=NOW - 60, due_ts=NOW + 4 * H)
    add_hyp(env.eval_db, HID2, strategy=S_1H_B, symbol="ETH-USDT", created_ts=NOW - 60, due_ts=NOW + H + 600)
    env.ft.down = True
    r1 = fb.run_once(env.cfg, now_ms=NOW_MS)
    assert r1["status"] == "api_down" and r1["errors"] == 2 and "Connection refused" in r1["detail"]
    assert all(r["status"] == "error" and "sin respuesta" in r["detail"] for r in _rows(env))
    # 15 min despues vuelve la API: la de 4 h se reintenta; a la otra le quedan < 1 h -> skipped_late, sin mandar
    env.ft.down = False
    r2 = fb.run_once(env.cfg, now_ms=NOW_MS + 900_000)
    rows = _rows(env)
    last = {r["hypothesis_id"][:8]: r for r in rows}
    assert last["1a2b3c4d"]["status"] == "sent" and last["2b3c4d5e"]["status"] == "skipped_late"
    assert "no se reintenta" in last["2b3c4d5e"]["detail"]
    assert [e["pair"] for e in env.ft.enters()] == ["BTC/USDT:USDT"]
    assert r2["sent"] == 1 and r2["skipped"] == 1
    assert len(rows) == 4  # 2 errores + 2 finales: todo queda registrado (append-only)


def test_timeout_on_enter_is_reconciled_by_tag_not_resent(env):
    add_hyp(env.eval_db, HID, created_ts=NOW - 60, due_ts=NOW + 4 * H)
    env.ft.timeout_on_enter = True
    r1 = fb.run_once(env.cfg, now_ms=NOW_MS)
    assert r1["status"] == "api_down" and _rows(env)[0]["status"] == "error"
    env.ft.timeout_on_enter = False
    fb.run_once(env.cfg, now_ms=NOW_MS + 300_000)
    final = _rows(env)[-1]
    assert final["status"] == "sent" and final["trade_id"] == 1 and "reconciliada" in final["detail"]
    assert len(env.ft.enters()) == 1


def test_only_new_hourly_pending_hypotheses(env):
    fb.run_once(env.cfg, now_ms=NOW_MS - 3_600_000)  # primer arranque del puente: hace 1 h
    add_hyp(env.eval_db, "aaaaaaaa-0000-4000-8000-000000000001", created_ts=NOW - 3 * H, due_ts=NOW + 2 * H)  # vieja
    add_hyp(env.eval_db, "bbbbbbbb-0000-4000-8000-000000000001", strategy=S_OLD, symbol="XRP-USDT",
            created_ts=NOW - 60, due_ts=NOW + 90 * 86400, timeframe=None)  # no es de 1h
    add_hyp(env.eval_db, "cccccccc-0000-4000-8000-000000000001", symbol="DOGE-USDT", created_ts=NOW - 60,
            due_ts=NOW + 4 * H)  # fuera de la whitelist
    add_hyp(env.eval_db, "dddddddd-0000-4000-8000-000000000001", symbol="ETH-USDT", created_ts=NOW - 60,
            due_ts=NOW + 120)  # le quedan 2 min
    add_hyp(env.eval_db, "eeeeeeee-0000-4000-8000-000000000001", symbol="SOL-USDT", created_ts=NOW - 1800,
            due_ts=NOW - 60)  # nueva pero vencio sin ningun intento
    fb.run_once(env.cfg, now_ms=NOW_MS)
    st = {r["hypothesis_id"][:8]: r["status"] for r in _rows(env)}
    assert st == {"cccccccc": "skipped_unsupported", "dddddddd": "skipped_late", "eeeeeeee": "skipped_late"}
    assert env.ft.enters() == []


def test_missing_secrets_is_explicit_error_and_secret_never_stored(env, tmp_path):
    add_hyp(env.eval_db, HID, created_ts=NOW - 60, due_ts=NOW + 4 * H)
    env.cfg.secrets_path = tmp_path / "no-existe.json"
    r = fb.run_once(env.cfg, now_ms=NOW_MS)
    assert r["status"] == "api_down" and "no existe no-existe.json" in r["detail"]
    env.cfg.secrets_path = write_secrets(tmp_path / "config.hyp.secrets.json")
    fb.run_once(env.cfg, now_ms=NOW_MS + 300_000)
    raw = Path(env.cfg.bridge_db).read_bytes()
    assert PASSWORD.encode() not in raw
    assert all(a.startswith("Basic ") for a in env.ft.auth_seen)  # va en el header, nunca en la base ni en logs


def test_cli_run_exit_codes(env, monkeypatch, capsys):
    monkeypatch.setenv("HEATSIGNAL_EVAL_DB", str(env.eval_db))
    monkeypatch.setenv("HEATSIGNAL_FT_BRIDGE_DB", str(env.cfg.bridge_db))
    monkeypatch.setenv("HEATSIGNAL_FT_HYP_SECRETS", str(env.cfg.secrets_path))
    monkeypatch.setenv("HEATSIGNAL_FT_HYP_API", "http://127.0.0.1:1")  # nada escucha: sin red real
    assert fb.main(["run"]) == 0  # sin candidatas no toca la API
    add_hyp(env.eval_db, HID, created_ts=time.time(), due_ts=4_000_000_000)
    assert fb.main(["run"]) == 1  # API caida -> codigo 1 (systemd lo muestra como failed)
    out = capsys.readouterr().out
    assert "api_down" in out and PASSWORD not in out


# ---- vista del panel y endpoint --------------------------------------------------------------------------------


def _scenario(env):
    """Una SHORT cerrada por tiempo y comparada, una LONG abierta, una omitida por par ocupado."""
    add_hyp(env.eval_db, HID, direction="SHORT", created_ts=NOW - 60, due_ts=NOW + 4 * H, entry="100000")
    add_hyp(env.eval_db, HID2, strategy=S_1H_B, symbol="SOL-USDT", created_ts=NOW - 30, due_ts=NOW + 2 * H, entry="200")
    add_hyp(env.eval_db, "3c4d5e6f-0000-4000-8000-000000000003", strategy=S_1H_B, symbol="SOL-USDT",
            created_ts=NOW - 20, due_ts=NOW + 3 * H)
    fb.run_once(env.cfg, now_ms=NOW_MS)
    # la SHORT: Freqtrade entro a 100.050 (5 bps mas caro = a favor del short) y salio a 99.000 a los 40 s del due
    resolve_hyp(env.eval_db, HID, exit_price=99_100.0, pnl_pct=0.8, gross=0.9, win=True, at_ts=NOW + 4 * H + 120)
    ft_db = make_ft_db(env.tmp / "freqtrade_hyp.sqlite", [
        {"id": 1, "pair": "BTC/USDT:USDT", "is_short": True, "tag": fb.encode_tag(HID, NOW + 4 * H),
         "open_ms": NOW_MS + 20_000, "open_rate": 100_050.0, "close_ms": NOW_MS + 4 * H * 1000 + 40_000,
         "close_rate": 99_000.0, "profit_abs": 9.5, "profit": 0.0095, "exit_reason": "hyp_due"},
        {"id": 2, "pair": "SOL/USDT:USDT", "tag": fb.encode_tag(HID2, NOW + 2 * H), "open_ms": NOW_MS + 30_000,
         "open_rate": 200.1},
    ])
    return ft_db


def test_view_compares_execution_with_resolution(env):
    ft_db = _scenario(env)
    api = {"ok": True, "status": [{"trade_id": 2, "current_rate": 202.0, "profit_abs": 9.4, "profit_ratio": 0.0094}]}
    ov = fbv.overview(env.cfg.bridge_db, env.eval_db, ft_db, api=api, now_ms=NOW_MS + 5 * H * 1000)
    assert "SIMULADO" in ov["disclaimer"]
    assert ov["totals"] == {**ov["totals"], "hypotheses": 3, "sent": 2, "skipped": 1, "open": 1, "closed": 1}
    by = {s["name"]: s for s in ov["strategies"]}
    a = by["1h-apertura-continuacion-a-favor-v1"]
    (op,) = a["ops"]
    c = op["cmp"]
    assert op["status"] == "sent" and op["ft"]["exit_reason"] == "hyp_due" and op["hyp"]["win"] is True
    assert c["entry_gap_bps"] == pytest.approx(5.0) and c["entry_effect_bps"] == pytest.approx(5.0)  # short: + a favor
    assert c["exit_gap_bps"] == pytest.approx((99_000 / 99_100 - 1) * 1e4)
    assert c["exit_effect_bps"] == pytest.approx(-c["exit_gap_bps"])
    assert c["pnl_diff_pp"] == pytest.approx(0.95 - 0.8) and c["same_outcome"] is True
    assert c["entry_delay_s"] == pytest.approx(80.0) and c["exit_delay_s"] == pytest.approx(40.0)
    assert op["ft"]["gross_pct"] == pytest.approx(-(99_000 / 100_050 - 1) * 100)
    assert a["closed"] == 1 and a["ft_wins"] == 1 and a["compared"] == 1 and a["mean_pnl_diff_pp"] == pytest.approx(0.15)
    b = by["1h-retroceso-ema-a-favor-v1"]
    assert b["sent"] == 1 and b["pair_busy"] == 1 and b["open"] == 1
    open_op = next(o for o in b["ops"] if o["status"] == "sent")
    assert open_op["ft"]["is_open"] and open_op["ft"]["current_rate"] == 202.0 and open_op["cmp"].get("pnl_diff_pp") is None
    assert any("Muestra chica" in n for n in ov["notes"])


async def test_endpoint_and_system(env):
    ft_db = _scenario(env)
    calls = []

    def fetch(url, headers, timeout):
        calls.append(url)
        raise OSError("Connection refused")

    src = HypBridgeSources(bridge_db=env.cfg.bridge_db, ft_db=ft_db, eval_db=env.eval_db,
                           secrets_path=env.cfg.secrets_path, api_url="http://127.0.0.1:8082", fetch=fetch)
    ctx = PanelContext(health=HealthRegistry(), hyp_bridge=src)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_panel_app(ctx)), base_url="http://panel") as c:
        r = await c.get("/api/paper/hypotheses")
        assert r.status_code == 200
        body = r.json()
        assert body["totals"]["sent"] == 2 and body["source"]["api"]["ok"] is False
        assert any("no respondio" in n for n in body["notes"])
        assert PASSWORD not in r.text
        assert all(u.startswith("http://127.0.0.1:8082/") for u in calls)
        sysinfo = (await c.get("/api/system")).json()
        assert sysinfo["databases"]["ft_bridge"]["exists"] is True and sysinfo["databases"]["freqtrade_hyp"]["exists"] is True
        js = await c.get("/static/app/hyp-ft-ui.js")
        assert js.status_code == 200 and "renderOverview" in js.text
        assert "mountHypFt" in (await c.get("/static/app/views/paper.js")).text


async def test_endpoint_503_when_unconfigured_or_bridge_never_ran(tmp_path):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_panel_app(PanelContext(health=HealthRegistry()))),
                                 base_url="http://panel") as c:
        r = await c.get("/api/paper/hypotheses")
        assert r.status_code == 503 and "no configurado" in r.json()["detail"]
    src = HypBridgeSources(bridge_db=tmp_path / "ft_bridge.sqlite", ft_db=tmp_path / "x.sqlite",
                           eval_db=make_eval_db(tmp_path / "eval.sqlite"), api_url=None)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_panel_app(PanelContext(health=HealthRegistry(), hyp_bridge=src))),
                                 base_url="http://panel") as c:
        r = await c.get("/api/paper/hypotheses")
        assert r.status_code == 503 and "heatsignal-ft-bridge.timer" in r.json()["detail"]


def test_view_without_freqtrade_db_still_shows_bridge(env):
    add_hyp(env.eval_db, HID, created_ts=NOW - 60, due_ts=NOW + 4 * H)
    fb.run_once(env.cfg, now_ms=NOW_MS)
    ov = fbv.overview(env.cfg.bridge_db, env.eval_db, env.tmp / "no-hay.sqlite", now_ms=NOW_MS)
    assert ov["source"]["ft_available"] is False and ov["totals"]["sent"] == 1
    assert any("no existe la base del Freqtrade de hipotesis" in n for n in ov["notes"])


@pytest.mark.skipif(shutil.which("node") is None, reason="sin node")
def test_ui_renders_and_escapes(env):
    ft_db = _scenario(env)
    ov = json.loads(json.dumps(fbv.overview(env.cfg.bridge_db, env.eval_db, ft_db, now_ms=NOW_MS + 5 * H * 1000)))
    ov["strategies"][0]["name"] = "<img src=x onerror=alert(1)>"
    ov["strategies"][0]["ops"][0]["detail"] = "<b>detalle</b>"
    f = env.tmp / "data.json"
    f.write_text(json.dumps({"overview": ov, "detail": ov["strategies"][0]}))
    out = subprocess.run(["node", str(HARNESS), str(UI), str(f)], capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    res = json.loads(out.stdout)
    assert "SIMULADO — sin plata real" in res["overview"]
    assert "Por estrategia" in res["overview"] and "omitida: par ocupado" in res["overview"]
    assert "enviada" in res["detail"] and "✓ acertó" in res["detail"]
    assert "<img src=x onerror=alert(1)>" in res["overview"] and "<b>detalle</b>" in res["detail"]
    assert "img" not in res["tags"] and "b" in res["tags"]  # texto de datos como nodos de texto, nunca HTML


def test_bridge_and_view_do_not_import_pandas():
    code = ("import sys\n"
            "class B:\n"
            "    def find_spec(self, n, p=None, t=None):\n"
            "        if n.split('.')[0] in ('pandas', 'numpy', 'pydantic'): raise ImportError('bloqueado ' + n)\n"
            "sys.meta_path.insert(0, B())\n"
            "import heatsignal.paper.ft_bridge, heatsignal.paper.ft_bridge_view\n")
    src = str(Path(__file__).parents[1] / "src")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env={"PYTHONPATH": src, "PATH": ""})
    assert out.returncode == 0, out.stderr
