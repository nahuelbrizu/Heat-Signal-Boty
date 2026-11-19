"""Enviar estrategias del laboratorio al bot desde el panel (2026-11-19): params.enroll y /api/paperbot/candidates
y /api/paperbot/enroll. Sin red: params.json y el laboratorio en tmp_path."""

from __future__ import annotations

import json

import httpx
import pytest

from heatsignal.api import panel, panel_api
from heatsignal.api.panel import PanelContext, create_panel_app
from heatsignal.ingestion.health import HealthRegistry
from heatsignal.lab.strategies import CATALOG
from heatsignal.paperbot import params as P

H = {"X-HeatSignal-Action": "1"}
GRID, NOGRID, METRICS = "donchian-ensemble-v1", "l6-reg-atrp50-long-v1", "l6-der-oibaja-fneg-d3-v1"
VOLUME = "cartera-tendencia-v1"  # combina ema-cruce, que pide `volume`: el bot no lo tiene
SYMS = ["BTCUSDT", "ETHUSDT"]


def _spec(sid: str):
    return CATALOG[sid][0]


def _detail(sid: str, **over) -> dict:
    d = {"strategy_id": sid, "spec_hash": _spec(sid).spec_hash(), "run": "20261101T000000", "symbols": SYMS,
         "walk_forward": {"chosen": [{"params": {"target_vol": 0.2}, "test": ["2026-09-01", "2026-10-01"]}]}}
    return {**d, **over}


@pytest.fixture()
def env(tmp_path, monkeypatch):
    for c in (panel_api._CACHE, panel._HISTORY_CACHE, panel._PER_SYMBOL_CACHE, panel._LAB_CACHE):
        c.clear()
    params = tmp_path / "params.json"
    running = json.loads(P.PARAMS_PATH.read_text())["strategies"]["tsmom-diario-v1"]
    params.write_text(json.dumps({"strategies": {"tsmom-diario-v1": running}}))
    monkeypatch.setattr(P, "PARAMS_PATH", params)
    data = tmp_path / "data"
    data.mkdir()
    rows = []
    for sid, sharpe in ((NOGRID, 0.8), (GRID, 1.1), (METRICS, 0.6), (VOLUME, 0.7), ("tsmom-diario-v1", 2.0)):
        rows.append({"strategy_id": sid, "spec_hash": _spec(sid).spec_hash(), "sharpe": sharpe, "dsr": 0.97, "trades": 100,
                     "verdict": {"pasa": sid == GRID}, "veredicto_programa": {"pasa": False}})
        (data / f"lab_detail_{sid}.json").write_text(json.dumps(_detail(sid)))
    (data / "lab_summary.json").write_text(json.dumps({"generated_at": "2026-11-19T00:00:00+00:00", "strategies": rows}))
    ctx = PanelContext(health=HealthRegistry(), lab_summary_path=data / "lab_summary.json", config={"symbols": SYMS})
    yield ctx, params
    panel._LAB_CACHE.clear()


def _client(ctx: PanelContext) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=create_panel_app(ctx)), base_url="http://panel")


# ---- params.enroll -------------------------------------------------------------------------------------

def test_enroll_takes_lab_params_and_is_seguimiento(tmp_path) -> None:
    path = tmp_path / "p.json"
    path.write_text(json.dumps({"strategies": {}}))
    e = P.enroll(GRID, _spec(GRID), _detail(GRID), sharpe=1.08, path=path, today="2026-11-19")
    assert e["rol"] == "seguimiento" and e["params"] == {"target_vol": 0.2} and e["universe"] == SYMS
    assert e["desde"] == "2026-11-19" and "Sharpe OOS 1.08" in e["origen"] and "NO decide" in e["origen"]
    assert json.loads(path.read_text())["strategies"][GRID] == e
    with pytest.raises(P.ParamsError, match="ya corre"):
        P.enroll(GRID, _spec(GRID), _detail(GRID), path=path)


def test_enroll_rejects_other_rule_version_and_off_grid(tmp_path) -> None:
    path = tmp_path / "p.json"
    path.write_text(json.dumps({"strategies": {}}))
    with pytest.raises(P.ParamsError, match="otra version"):
        P.enroll(NOGRID, _spec(NOGRID), _detail(NOGRID, spec_hash="x"), path=path)
    bad = _detail(GRID, walk_forward={"chosen": [{"params": {"target_vol": 0.99}}]})
    with pytest.raises(P.ParamsError, match="fuera de la grilla"):
        P.enroll(GRID, _spec(GRID), bad, path=path)
    assert json.loads(path.read_text())["strategies"] == {}  # nada escrito si falla


# ---- endpoints -----------------------------------------------------------------------------------------

async def test_candidates_sorted_by_sharpe_without_running_ones(env) -> None:
    ctx, _ = env
    async with _client(ctx) as c:
        d = (await c.get("/api/paperbot/candidates")).json()
    ids = [x["strategy_id"] for x in d["items"]]
    assert ids == [GRID, NOGRID, VOLUME, METRICS]  # tsmom ya corre; orden por Sharpe
    by = {x["strategy_id"]: x for x in d["items"]}
    assert not by[METRICS]["enviable"] and "metrics" in by[METRICS]["motivo"]
    assert not by[VOLUME]["enviable"] and "volume" in by[VOLUME]["motivo"]  # la prueba con los datos del bot
    assert d["items"][0]["pasa_lab"] and d["items"][0]["enviable"]


async def test_enroll_endpoint_writes_params_and_guards(env) -> None:
    ctx, params = env
    async with _client(ctx) as c:
        assert (await c.post("/api/paperbot/enroll", json={"strategy_ids": [GRID]})).status_code == 403  # sin header
        r = await c.post("/api/paperbot/enroll", json={"strategy_ids": [METRICS]}, headers=H)
        assert r.status_code == 400 and "metrics" in r.text
        r = await c.post("/api/paperbot/enroll", json={"strategy_ids": [VOLUME]}, headers=H)
        assert r.status_code == 400 and "volume" in r.text and VOLUME not in json.loads(params.read_text())["strategies"]
        r = await c.post("/api/paperbot/enroll", json={"strategy_ids": [GRID, NOGRID]}, headers=H)
        assert r.status_code == 201, r.text
        assert [a["strategy_id"] for a in r.json()["added"]] == [GRID, NOGRID]
        saved = json.loads(params.read_text())["strategies"]
        assert saved[GRID]["params"] == {"target_vol": 0.2} and saved[NOGRID]["rol"] == "seguimiento"
        assert P.effective(NOGRID, saved[NOGRID], _spec(NOGRID)) is not None  # el bot lo acepta
        r = await c.post("/api/paperbot/enroll", json={"strategy_ids": [GRID]}, headers=H)
        assert r.status_code == 400 and "ya corren" in r.text
        ids = [x["strategy_id"] for x in (await c.get("/api/paperbot/candidates")).json()["items"]]
        assert ids == [VOLUME, METRICS]
