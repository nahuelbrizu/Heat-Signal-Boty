"""GET /api/paperbot y /api/paperbot/{id} (base del bot armada con corridas falsas, sin red) y render basico de la
vista paperbot.js en node con un DOM minimo."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import httpx
import pytest

from heatsignal.api.panel import PanelContext, create_panel_app
from heatsignal.ingestion.health import HealthRegistry
from heatsignal.lab.strategies import Spec
from heatsignal.paperbot.engine import EngineConfig, run_once
from paperbot_fakes import DAY, H, MIN, T0, fill_funding, fill_history

sys.path.insert(0, str(Path(__file__).parent))
from test_paperbot import toy_rule  # noqa: E402

VIEW = Path(__file__).parents[1] / "src/heatsignal/api/static/app/views/paperbot.js"
HARNESS = Path(__file__).parent / "js" / "render_view.mjs"


@pytest.fixture
def bot_db(tmp_path):
    syms = ["BTCUSDT", "ETHUSDT"]
    fill_history(tmp_path / "hist.sqlite", syms, T0 - 3 * DAY, T0 + 12 * H).close()
    fill_funding(tmp_path / "paperbot.sqlite", syms, T0 - 3 * DAY, T0 + 12 * H)
    spec = Spec("toy-v1", "test", "<img src=x onerror=alert(1)>", "1h", "both", "h",
                {"events": [["BTCUSDT", T0 + H, 1.0], ["BTCUSDT", T0 + 2 * H + 5 * MIN, 0.0], ["ETHUSDT", T0 + 2 * H, -0.5]]})
    cfg = EngineConfig(paperbot_db=tmp_path / "paperbot.sqlite", history_db=tmp_path / "hist.sqlite", window_days=2,
                       funding_days=3, download=False, catalog={"toy-v1": (spec, toy_rule)},
                       entries={"toy-v1": {"spec_hash": spec.spec_hash(), "universe": syms, "params": {},
                                           "origen": "<b>origen</b>"}})
    for h in (0, 1, 2, 3, 6):
        run_once(cfg, now_ms=T0 + h * H + 2 * MIN)
    (tmp_path / "lab_summary.json").write_text(json.dumps({"generated_at": "2026-10-18T00:00:00Z", "strategies": [
        {"strategy_id": "toy-v1", "spec_hash": spec.spec_hash(), "run": "r1", "sharpe": -0.42, "sharpe_ci95": [-1.2, 0.3],
         "dsr": 0.01, "total_return": -0.1, "max_drawdown": 0.2, "basket_sharpe": 0.6, "random_p": 0.7,
         "oos_range": ["2021-01-01", "2026-07-15"], "holdout": "SELLADO",
         "verdict": {"pasa": False, "checks": {"opera": True, "dsr": False, "le_gana_a_la_canasta": False}}}]}))
    return tmp_path


def _client(ctx: PanelContext) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=create_panel_app(ctx)), base_url="http://panel")


async def test_paperbot_endpoints(bot_db):
    ctx = PanelContext(health=HealthRegistry(), paperbot_db=bot_db / "paperbot.sqlite",
                       lab_summary_path=bot_db / "lab_summary.json")
    async with _client(ctx) as c:
        r = await c.get("/api/paperbot")
        assert r.status_code == 200
        ov = r.json()
        assert ov["simulated"] is True and "SIMULADO" in ov["disclaimer"]
        assert ov["counts"] == {"ok": 5, "error": 0, "skipped": 0}
        assert [g["hours"] for g in ov["gaps"]] == [2]
        (s,) = ov["strategies"]
        assert s["strategy_id"] == "toy-v1" and s["origen"] == "<b>origen</b>"
        assert {p["symbol"]: p["weight"] for p in s["positions"]} == {"BTCUSDT": 0.0, "ETHUSDT": -0.5}
        assert s["lab"]["pasa"] is False and s["lab"]["sharpe"] == -0.42 and s["lab"]["same_spec"] is True
        assert "traceback" not in json.dumps(ov)

        r = await c.get("/api/paperbot/toy-v1")
        assert r.status_code == 200
        d = r.json()
        assert len(d["equity_hourly"]) == 5 and d["equity_hourly"][0][2] is not None
        assert len(d["trades"]) == 1 and d["trades"][0]["symbol"] == "BTCUSDT"
        assert {o["reason"] for o in d["orders"]} == {"senal"}
        b = d["breakdown"]
        assert b["total"] == pytest.approx(b["precio"] + b["funding"] - b["comisiones"] - b["slippage"])
        assert d["equity"] == pytest.approx(10_000 + b["total"])
        assert d["lab"]["checks"]["dsr"] is False

        assert (await c.get("/api/paperbot/otra-v1")).status_code == 404
        assert (await c.get("/api/paperbot/..%2Fx")).status_code in (400, 404)
        assert (await c.get("/api/paperbot/BAD ID")).status_code == 400


async def test_paperbot_503_when_missing(tmp_path):
    async with _client(PanelContext(health=HealthRegistry(), paperbot_db=tmp_path / "nope.sqlite")) as c:
        r = await c.get("/api/paperbot")
        assert r.status_code == 503 and "todavia no corrio" in r.json()["detail"]
    async with _client(PanelContext(health=HealthRegistry())) as c:
        assert (await c.get("/api/paperbot")).status_code == 503


async def test_paperbot_view_is_served(bot_db):
    async with _client(PanelContext(health=HealthRegistry())) as c:
        r = await c.get("/static/app/views/paperbot.js")
        assert r.status_code == 200 and "renderOverview" in r.text
        main = await c.get("/static/app/main.js")
        assert 'paperbot: { title: "Bot paper' in main.text


@pytest.mark.skipif(shutil.which("node") is None, reason="sin node")
async def test_paperbot_view_renders_and_escapes(bot_db, tmp_path):
    ctx = PanelContext(health=HealthRegistry(), paperbot_db=bot_db / "paperbot.sqlite",
                       lab_summary_path=bot_db / "lab_summary.json")
    async with _client(ctx) as c:
        data = {"overview": (await c.get("/api/paperbot")).json(), "detail": (await c.get("/api/paperbot/toy-v1")).json()}
    data["overview"]["strategies"][0]["strategy_id"] = data["detail"]["strategy_id"] = "<img src=x onerror=alert(1)>"
    f = tmp_path / "data.json"
    f.write_text(json.dumps(data))
    out = subprocess.run(["node", str(HARNESS), str(VIEW), str(f)], capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    res = json.loads(out.stdout)
    assert "Simulado, sin plata real" in res["overview"]
    assert "no pasan los criterios del laboratorio" in res["overview"] and "backtest: no pasa" in res["overview"]
    assert "Equity horaria vs canasta" in res["detail"] and "NO pasó los criterios" in res["detail"]
    assert "<b>origen</b>" in res["detail"]           # texto escapado (nodo de texto), no HTML
    assert "<img src=x onerror=alert(1)>" in res["overview"]
    assert "img" not in res["tags"]                     # ningun elemento creado a partir de datos


def test_panel_and_paperbot_view_do_not_import_pandas():
    code = ("import sys\n"
            "class B:\n"
            "    def find_spec(self, n, p=None, t=None):\n"
            "        if n.split('.')[0] == 'pandas': raise ImportError('pandas bloqueado')\n"
            "sys.meta_path.insert(0, B())\n"
            "import heatsignal.api.panel_api, heatsignal.paperbot.view, heatsignal.paperbot.params, heatsignal.paperbot.__main__\n")
    src = str(Path(__file__).parents[1] / "src")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env={"PYTHONPATH": src, "PATH": ""})
    assert out.returncode == 0, out.stderr
