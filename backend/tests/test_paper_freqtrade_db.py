"""paper/freqtrade_db.py: lectura read-only de la base de Freqtrade, fills, API REST (falsa) y vista del panel."""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from heatsignal.paper import freqtrade_db as ftdb
from paper_fakes import make_db

H = 3_600_000
T0 = 1_792_000_000_000 - 1_792_000_000_000 % H


def _db(tmp_path: Path) -> Path:
    return make_db(tmp_path / "ft.sqlite", [
        {"id": 1, "pair": "BTC/USDT:USDT", "funding": -0.5,
         "fills": [(T0, 0.01, 100_000.0, "donchian"), (T0 + 4 * H, 0.005, 101_000.0, "ajuste+"),
                   (T0 + 8 * H, -0.015, 102_000.0, "canal")]},
        {"id": 2, "pair": "XRP/USDT:USDT", "funding": -0.1,
         "fills": [(T0 + 12 * H, 500.0, 2.0, "donchian"), (T0 + 16 * H, -100.0, 2.1, "ajuste-")]},
        {"id": 3, "pair": "SOL/USDT:USDT", "funding": 0.0,
         "fills": [(T0 + H, 10.0, 150.0, "donchian"), (T0 + 2 * H, -10.0, 140.0, "canal")]},
    ], bot_start_ms=T0 - H, startup_ms=T0 + 10 * H)


def test_time_parsing_and_pairs() -> None:
    assert ftdb.ft_time_ms("2026-10-17 16:45:37.487875") == 1792255537487
    assert ftdb.ft_time_ms("2026-10-17T16:45:37Z") == 1792255537000
    assert ftdb.ft_time_ms(None) is None and ftdb.ft_time_ms("basura") is None
    assert ftdb.pair_symbol("TRX/USDT:USDT") == "TRXUSDT"


def test_dump_is_read_only_and_complete(tmp_path) -> None:
    db = _db(tmp_path)
    before = db.read_bytes()
    d = ftdb.dump(db)
    assert db.read_bytes() == before
    assert len(d["trades"]) == 3 and len(d["orders"]) == 7
    assert d["kv"]["bot_start_time"] == T0 - H and d["kv"]["startup_time"] == T0 + 10 * H
    t1 = next(t for t in d["trades"] if t["id"] == 1)
    assert t1["symbol"] == "BTCUSDT" and t1["is_open"] is False and t1["open_ms"] == T0 and t1["funding_fees"] == -0.5
    conn = ftdb.connect_ro(db)
    with pytest.raises(sqlite3.OperationalError):
        conn.execute("DELETE FROM trades")
    conn.close()


def test_missing_db_and_missing_columns(tmp_path) -> None:
    with pytest.raises(FileNotFoundError):
        ftdb.dump(tmp_path / "no.sqlite")
    p = tmp_path / "viejo.sqlite"
    conn = sqlite3.connect(p)
    conn.execute("CREATE TABLE trades (id INTEGER PRIMARY KEY, pair TEXT, is_open BOOLEAN, open_date DATETIME)")
    conn.execute("INSERT INTO trades VALUES (1, 'ETH/USDT:USDT', 1, '2026-10-17 00:00:00')")
    conn.commit()
    conn.close()
    d = ftdb.dump(p)
    assert d["orders"] == [] and d["kv"] == {}
    assert d["trades"][0]["funding_fees"] is None and d["trades"][0]["symbol"] == "ETHUSDT"


def test_fills_kinds_signs_and_fees(tmp_path) -> None:
    fl = ftdb.fills(ftdb.dump(_db(tmp_path)))
    assert [f["kind"] for f in fl if f["symbol"] == "BTCUSDT"] == ["entrada", "ajuste+", "salida"]
    assert [f["kind"] for f in fl if f["symbol"] == "XRPUSDT"] == ["entrada", "ajuste-"]
    btc = [f for f in fl if f["symbol"] == "BTCUSDT"]
    assert sum(f["du"] for f in btc) == pytest.approx(0.0)
    assert btc[0]["fee"] == pytest.approx(0.01 * 100_000 * 0.0005)
    assert [f["time_ms"] for f in fl] == sorted(f["time_ms"] for f in fl)


def _fake_api(open_trades, *, status=200, down=False):
    calls = []

    def fetch(url, headers, timeout):
        calls.append((url, headers))
        if down:
            raise OSError("Connection refused")
        name = url.rsplit("/", 1)[-1]
        if status != 200:
            return status, b"{}"
        body = {"show_config": {"dry_run": True, "state": "running", "strategy": "DonchianEnsembleV1", "version": "2026.9",
                                "api_server": {"password": "NO"}},
                "health": {"last_process_ts": T0 // 1000 + 100, "bot_start_ts": (T0 - H) // 1000, "bot_startup_ts": (T0 + 10 * H) // 1000},
                "balance": {"total": 10_050.0, "starting_capital": 10_000.0},
                "status": [{"trade_id": 2, "current_rate": 2.2, "profit_abs": 79.0, "profit_ratio": 0.098, "secret": "x"}]}[name]
        return 200, json.dumps(body).encode()
    return fetch, calls


def test_api_snapshot_ok_down_and_unauthorized(tmp_path) -> None:
    secrets = tmp_path / "config.secrets.json"
    secrets.write_text(json.dumps({"api_server": {"username": "u", "password": "clave-secreta"}}))
    fetch, calls = _fake_api([])
    snap = ftdb.api_snapshot("http://127.0.0.1:8081", secrets, fetch=fetch)
    assert snap["ok"] and snap["error"] is None and snap["show_config"]["state"] == "running"
    assert "api_server" not in snap["show_config"] and "secret" not in snap["status"][0]
    assert calls[0][0] == "http://127.0.0.1:8081/api/v1/show_config" and calls[0][1]["Authorization"].startswith("Basic ")
    assert "clave-secreta" not in json.dumps(snap)

    fetch, calls = _fake_api([], down=True)
    snap = ftdb.api_snapshot("http://127.0.0.1:8081", secrets, fetch=fetch)
    assert not snap["ok"] and "Connection refused" in snap["error"] and len(calls) == 1
    fetch, _ = _fake_api([], status=401)
    assert "401" in ftdb.api_snapshot("http://x", secrets, fetch=fetch)["error"]
    assert "no existe" in ftdb.api_snapshot("http://x", tmp_path / "nada.json", fetch=fetch)["error"]
    (tmp_path / "roto.json").write_text("{}")
    assert "ilegible" in ftdb.api_snapshot("http://x", tmp_path / "roto.json", fetch=fetch)["error"]


def test_paper_state_with_and_without_api(tmp_path) -> None:
    d = ftdb.dump(_db(tmp_path))
    fetch, _ = _fake_api([])
    secrets = tmp_path / "s.json"
    secrets.write_text(json.dumps({"api_server": {"username": "u", "password": "p"}}))
    st = ftdb.paper_state(d, api=ftdb.api_snapshot("http://x", secrets, fetch=fetch), now_ms=T0 + 20 * H)
    assert st["bot"]["dry_run"] is True and st["bot"]["bot_start_ms"] == T0 - H
    assert st["bot"]["uptime_s"] == pytest.approx(10 * 3600)
    assert st["totals"]["open"] == 1 and st["totals"]["closed"] == 2
    o = st["open_trades"][0]
    assert o["symbol"] == "XRPUSDT" and o["current_rate"] == 2.2 and o["unrealized_pnl"] == 79.0 and o["adjustments"] == 1
    closed = {c["symbol"]: c for c in st["closed_trades"]}
    # BTC: 0.01@100k + 0.005@101k -> 0.015@102k; precio +25, fees 0.0005*(1000+505+1530), funding -0.5
    assert closed["BTCUSDT"]["pnl"] == pytest.approx(25 - 0.0005 * 3035 - 0.5)
    assert closed["SOLUSDT"]["win"] is False
    assert st["wallet"]["unrealized"] == 79.0 and st["wallet"]["equity"] == pytest.approx(10_000 + st["wallet"]["realized"] + 79.0)
    eq = st["equity"]
    assert eq[0]["equity"] == 10_000 and eq[-1].get("mark_to_market") and len(eq) == 4
    assert any("Muestra chica" in n for n in st["notes"])
    assert st["totals"]["ci95"] is not None

    st2 = ftdb.paper_state(d, api={"ok": False, "error": "caido"}, wallet_start=5_000, now_ms=T0 + 20 * H)
    assert st2["wallet"]["start"] == 5_000 and st2["wallet"]["unrealized"] is None and st2["wallet"]["equity"] is None
    assert any("API de Freqtrade no respondio" in n for n in st2["notes"])
    by = {p["symbol"]: p for p in st2["by_pair"]}
    assert by["XRPUSDT"]["open"] and by["BTCUSDT"]["closed"] == 1


def test_brief(tmp_path) -> None:
    b = ftdb.brief(ftdb.dump(_db(tmp_path)))
    assert b["open"] == 1 and b["closed"] == 2 and b["bot_start_ms"] == T0 - H


def test_cli_dump_via_stdin_like_ssh(tmp_path) -> None:
    """Lo mismo que hace la PC por SSH: el archivo por stdin a `python3 - dump RUTA` (sin el paquete instalado)."""
    db = _db(tmp_path)
    src = Path(ftdb.__file__).read_text()
    res = subprocess.run([sys.executable, "-I", "-", "dump", str(db)], input=src, capture_output=True, text=True, cwd=tmp_path)
    assert res.returncode == 0, res.stderr
    out = json.loads(res.stdout.strip().splitlines()[-1])
    assert len(out["trades"]) == 3
    res = subprocess.run([sys.executable, "-I", "-", "dump", str(tmp_path / "x.sqlite")], input=src, capture_output=True, text=True)
    assert res.returncode == 3 and json.loads(res.stdout)["missing"] is True
