"""Demo de Bybit (2026-11-20): comparacion operacion por operacion contra el dry-run de Binance y GET /api/paper/demo."""

from __future__ import annotations

from pathlib import Path

import httpx

from heatsignal.api.panel import PanelContext, PaperSources, create_panel_app
from heatsignal.ingestion.health import HealthRegistry
from heatsignal.paper import freqtrade_db as ftdb
from paper_fakes import make_db

H = 3_600_000
T0 = 1_792_000_000_000 - 1_792_000_000_000 % H


def _t(i, pair, open_ms, open_rate, *, short=False, close_rate=None, profit=None):
    return {"id": i, "pair": pair, "symbol": ftdb.pair_symbol(pair), "is_short": short, "is_open": close_rate is None,
            "open_ms": open_ms, "close_ms": None if close_rate is None else open_ms + 10 * H, "open_rate": open_rate,
            "close_rate": close_rate, "close_profit": profit, "exit_reason": None if close_rate is None else "canal"}


def test_demo_vs_dryrun_pairs_trades_and_signs_bps() -> None:
    demo = {"kv": {"bot_start_time": T0}, "trades": [
        _t(1, "BTC/USDT:USDT", T0 + 60_000, 100.10, close_rate=104.9, profit=0.047),  # long: entra 10 bps peor
        _t(2, "XRP/USDT:USDT", T0 + 2 * H, 0.999, short=True),                          # short: entra 10 bps peor
        _t(3, "SOL/USDT:USDT", T0 + 3 * H, 50.0)]}                                       # solo en la demo
    dry = {"kv": {}, "trades": [
        _t(9, "BTC/USDT:USDT", T0 - 30 * H, 90.0, close_rate=95.0, profit=0.05),          # antes de la demo: no cuenta
        _t(10, "BTC/USDT:USDT", T0, 100.0, close_rate=105.0, profit=0.05),
        _t(11, "XRP/USDT:USDT", T0 + 2 * H, 1.0, short=True),
        _t(12, "ETH/USDT:USDT", T0 + 5 * H, 2000.0)]}                                     # solo en el dry-run
    v = ftdb.demo_vs_dryrun(demo, dry)
    assert v["paired"] == 2 and v["closed_paired"] == 1
    btc, xrp = v["pairs"]
    assert round(btc["entry_bps"], 6) == 10.0 and round(btc["exit_bps"], 4) == round((1 - 104.9 / 105.0) * 1e4, 4)
    assert btc["delay_s"] == 60 and round(xrp["entry_bps"], 6) == 10.0
    assert [s["symbol"] for s in v["solo_demo"]] == ["SOLUSDT"] and [s["symbol"] for s in v["solo_dryrun"]] == ["ETHUSDT"]
    assert round(v["return_diff_mean"], 6) == -0.003


async def _get(ctx: PanelContext) -> dict:
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_panel_app(ctx)), base_url="http://panel") as c:
        r = await c.get("/api/paper/demo")
        assert r.status_code == 200
        return r.json()


async def test_demo_endpoint_without_db_says_how_to_set_it_up(tmp_path: Path) -> None:
    ctx = PanelContext(health=HealthRegistry(), demo=PaperSources(db_path=tmp_path / "nope.sqlite", api_url=None))
    body = await _get(ctx)
    assert body["configured"] is False and any("Demo Trading" in s for s in body["setup"])


async def test_demo_endpoint_compares_with_dryrun(tmp_path: Path) -> None:
    fills = [(T0 - 10 * H, 0.01, 100_000.0, "donchian"), (T0 - 2 * H, -0.01, 101_000.0, "canal")]
    make_db(tmp_path / "demo.sqlite", [{"id": 1, "pair": "BTC/USDT:USDT", "fills": fills}], bot_start_ms=T0 - 12 * H,
            startup_ms=T0 - H)
    make_db(tmp_path / "dry.sqlite", [{"id": 1, "pair": "BTC/USDT:USDT", "fills": fills}], bot_start_ms=T0 - 40 * H,
            startup_ms=T0 - H)
    ctx = PanelContext(health=HealthRegistry(), demo=PaperSources(db_path=tmp_path / "demo.sqlite", api_url=None),
                       paper=PaperSources(db_path=tmp_path / "dry.sqlite", api_url=None))
    body = await _get(ctx)
    assert body["configured"] is True and body["vs_dryrun"]["paired"] == 1
    assert body["vs_dryrun"]["pairs"][0]["entry_bps"] == 0 and "spread entre exchanges" in body["nota"]
