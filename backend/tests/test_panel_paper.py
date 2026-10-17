"""GET /api/paper, /api/paper/compare y el resumen de paper en /api/summary (base falsa de Freqtrade, API falsa)."""

from __future__ import annotations

import json
from pathlib import Path

import httpx

from heatsignal.api.panel import PanelContext, PaperSources, create_panel_app
from heatsignal.ingestion.health import HealthRegistry
from heatsignal.ingestion.run import RunConfig
from paper_fakes import make_db

H = 3_600_000
T0 = 1_792_000_000_000 - 1_792_000_000_000 % H


def _client(ctx: PanelContext) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=create_panel_app(ctx)), base_url="http://panel")


def _sources(tmp_path: Path, *, db=True, api_down=False, calls=None) -> PaperSources:
    p = tmp_path / "freqtrade_dryrun.sqlite"
    if db:
        make_db(p, [{"id": 1, "pair": "TRX/USDT:USDT", "fills": [(T0, 1762.0, 0.33475, "donchian")]},
                    {"id": 2, "pair": "BTC/USDT:USDT", "funding": -1.0,
                     "fills": [(T0 - 10 * H, 0.01, 100_000.0, "donchian"), (T0 - 2 * H, -0.01, 99_000.0, "canal")]}],
                bot_start_ms=T0 - 12 * H, startup_ms=T0 - H)
    secrets = tmp_path / "config.secrets.json"
    secrets.write_text(json.dumps({"api_server": {"username": "u", "password": "clave-muy-secreta"}}))

    def fetch(url, headers, timeout):
        if calls is not None:
            calls.append(url)
        if api_down:
            raise OSError("Connection refused")
        name = url.rsplit("/", 1)[-1]
        body = {"show_config": {"dry_run": True, "state": "running", "strategy": "DonchianEnsembleV1"},
                "health": {"bot_startup_ts": (T0 - H) // 1000},
                "balance": {"total": 9_990.0, "starting_capital": 10_000.0},
                "status": [{"trade_id": 1, "current_rate": 0.34, "profit_abs": 8.8, "profit_ratio": 0.015}]}[name]
        return 200, json.dumps(body).encode()

    return PaperSources(db_path=p, secrets_path=secrets, api_url="http://127.0.0.1:8081",
                        summary_path=tmp_path / "paper_summary.json", fetch=fetch)


async def test_paper_endpoint_with_api(tmp_path) -> None:
    calls = []
    ctx = PanelContext(health=HealthRegistry(), paper=_sources(tmp_path, calls=calls))
    async with _client(ctx) as c:
        r = await c.get("/api/paper")
        assert r.status_code == 200
        body = r.json()
        assert body["bot"]["dry_run"] is True and body["bot"]["state"] == "running"
        assert body["totals"] == {**body["totals"], "open": 1, "closed": 1}
        assert body["open_trades"][0]["unrealized_pnl"] == 8.8 and body["open_trades"][0]["symbol"] == "TRXUSDT"
        assert body["closed_trades"][0]["pnl"] < 0
        assert body["frequi"]["tunnel"] == "hs-ft" and body["compare"] is None
        assert "clave-muy-secreta" not in r.text
        n = len(calls)
        await c.get("/api/paper")  # cache de 10 s: no vuelve a llamar a Freqtrade
        assert len(calls) == n
        s = (await c.get("/api/summary")).json()["paper"]
        assert s["available"] and s["open"] == 1 and s["closed"] == 1 and s["compare"] is None
        sysinfo = (await c.get("/api/system")).json()
        assert sysinfo["databases"]["freqtrade"]["exists"] is True


async def test_paper_api_down_still_serves_db(tmp_path) -> None:
    ctx = PanelContext(health=HealthRegistry(), paper=_sources(tmp_path, api_down=True))
    async with _client(ctx) as c:
        body = (await c.get("/api/paper")).json()
        assert body["source"]["api"]["ok"] is False and body["wallet"]["unrealized"] is None
        assert body["open_trades"][0]["unrealized_pnl"] is None
        assert any("API de Freqtrade" in n for n in body["notes"])


async def test_paper_503_when_freqtrade_missing_or_unconfigured(tmp_path) -> None:
    ctx = PanelContext(health=HealthRegistry(), paper=_sources(tmp_path, db=False))
    async with _client(ctx) as c:
        r = await c.get("/api/paper")
        assert r.status_code == 503 and "Freqtrade no esta" in r.json()["detail"]
        s = (await c.get("/api/summary")).json()["paper"]
        assert s["available"] is False and "Freqtrade no esta" in s["error"]
    async with _client(PanelContext(health=HealthRegistry())) as c:
        assert (await c.get("/api/paper")).status_code == 503
        assert (await c.get("/api/paper/compare")).status_code == 503
        assert (await c.get("/api/summary")).json()["paper"] is None


async def test_paper_compare_endpoint(tmp_path) -> None:
    src = _sources(tmp_path)
    ctx = PanelContext(health=HealthRegistry(), paper=src)
    async with _client(ctx) as c:
        r = await c.get("/api/paper/compare")
        assert r.status_code == 404 and "paper_compare --upload" in r.json()["detail"]
        summary = {"generated_at": "2026-10-18T01:00:00+00:00",
                   "latest": {"status": "ok", "window": {"days": 1.2}, "totals": {"paper": {"total": 5.0}, "sim": {"total": 6.0}, "diff": -1.0},
                              "decomposition": {"timing": float("nan")}},
                   "history": [{"date": "2026-10-18", "diff": -1.0}]}
        src.summary_path.write_text(json.dumps(summary))  # NaN en el archivo -> null en la respuesta
        r = await c.get("/api/paper/compare")
        assert r.status_code == 200 and r.json()["latest"]["decomposition"]["timing"] is None
        s = (await c.get("/api/summary")).json()["paper"]
        assert s["compare"]["diff"] == -1.0 and s["compare"]["runs"] == 1
        assert (await c.get("/api/paper")).json()["compare"]["paper_total"] == 5.0
        src.summary_path.write_text("{roto")
        import os
        os.utime(src.summary_path, (1, 1))
        r = await c.get("/api/paper/compare")
        assert r.status_code == 500 and "ilegible" in r.json()["detail"]


def test_runconfig_paper_settings() -> None:
    cfg = RunConfig.from_env({"HEATSIGNAL_FREQTRADE_DB": "/x/ft.sqlite", "HEATSIGNAL_FREQTRADE_API": "",
                              "HEATSIGNAL_PAPER_WALLET": "5000"})
    assert cfg.freqtrade_db == "/x/ft.sqlite" and cfg.freqtrade_api == "" and cfg.paper_wallet == 5000
    d = RunConfig.from_env({})
    assert d.freqtrade_db.endswith("heatsignal/data/freqtrade_dryrun.sqlite")
    assert d.freqtrade_secrets.endswith("heatsignal/freqtrade/config.secrets.json") and d.freqtrade_api == "http://127.0.0.1:8081"
