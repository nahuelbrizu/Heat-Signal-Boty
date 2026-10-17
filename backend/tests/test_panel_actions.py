"""Acciones del panel: "Analizar ahora" (POST /api/analyze) y la cola del laboratorio (POST /api/lab/requests),
mas la CLI de la cola (corre en la VM) y el worker de la PC. Sin red, sin SSH, sin LLM real."""

from __future__ import annotations

import asyncio
import io
import json
import sqlite3
from contextlib import redirect_stdout
from pathlib import Path

import httpx
import pytest

from heatsignal.agents.graph import build_graph
from heatsignal.api import panel, panel_api
from heatsignal.api.panel import PanelContext, create_panel_app
from heatsignal.evaluation import lab_requests as lr
from heatsignal.evaluation.lab_requests import LabRequestError, LabRequestStore
from heatsignal.evaluation.strategy_registry import StrategyHypothesis, StrategyRegistry
from heatsignal.ingestion.health import HealthRegistry
from heatsignal.pipeline.analysis import AnalysisLoop
from heatsignal.pipeline.cycle_log import CycleLog

from _agent_fakes import ScriptedChatModel, full_script
from features_helpers import SYM
from test_analysis_loop import T0, Clock, _filled_memory

H = {"X-HeatSignal-Action": "1"}


@pytest.fixture(autouse=True)
def _clear_caches():
    for c in (panel_api._CACHE, panel._HISTORY_CACHE, panel._PER_SYMBOL_CACHE, panel._LAB_CACHE):
        c.clear()
    yield


def _client(ctx: PanelContext) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=create_panel_app(ctx)), base_url="http://panel")


async def _analysis_ctx(tmp_path, *, gate: asyncio.Event | None = None, with_llm: bool = True):
    clock = Clock(T0)
    memory = await _filled_memory(clock)
    registry = StrategyRegistry(tmp_path / "eval.sqlite")
    registry.register(StrategyHypothesis(description="s", entry_conditions={"symbol": "BTC-USDT"},
                                         exit_conditions={"horizon_s": 60}, author="t"))
    calls: list[list[str]] = []

    async def runner(state, run_id):
        syms = list(state["features_by_symbol"])
        calls.append(syms)
        if gate is not None:
            await gate.wait()
        return await build_graph(ScriptedChatModel(full_script(syms))).ainvoke(state)

    cl = CycleLog(tmp_path / "eval.sqlite")
    loop = AnalysisLoop(memory, [SYM, "BTC-USDT"], registry, graph_runner=runner if with_llm else None,
                        clock_ms=clock, cycle_log=cl, rotation_per_cycle=1)
    ctx = PanelContext(health=HealthRegistry(), memory=memory, analysis=loop, registry=registry, cycle_log=cl,
                       config={"symbols": ["SOLUSDT", "BTCUSDT"]})
    return ctx, calls


async def _wait_done(c: httpx.AsyncClient, run_id: str) -> dict:
    for _ in range(200):
        body = (await c.get(f"/api/analyze/{run_id}")).json()
        if body["status"] not in ("queued", "running"):
            return body
        await asyncio.sleep(0.01)
    raise AssertionError("el analisis manual no termino")


# ---------------------------------------------------------------------------- run_symbols

async def test_run_symbols_runs_only_that_symbol_without_advancing_rotation(tmp_path) -> None:
    ctx, calls = await _analysis_ctx(tmp_path)
    loop = ctx.analysis
    res = await loop.run_symbols([SYM], reason="manual", run_id="manual-123")
    assert res.status == "ran" and calls == [[SYM]] and res.run_id == "manual-123"
    assert res.detail.startswith("manual: ") and f"{SYM}=no_hypothesis" in res.detail
    row = ctx.cycle_log.get("manual-123")
    assert row["status"] == "ran" and row["detail"].startswith("manual: ") and row["report"]["symbols"][SYM]
    assert loop._turn == 0  # el manual no avanza la rotacion
    await loop.run_cycle()  # el agendado: BTC por estrategia + SOL por el turno 0 de la rotacion
    assert set(calls[-1]) == {"BTC-USDT", SYM} and loop._turn == 1
    with pytest.raises(ValueError, match="no configurados"):
        await loop.run_symbols(["DOGE-USDT"])


async def test_run_symbols_respects_warmup_and_missing_llm(tmp_path) -> None:
    ctx, _ = await _analysis_ctx(tmp_path, with_llm=False)
    res = await ctx.analysis.run_symbols([SYM])
    assert res.status == "no_llm" and res.detail.startswith("manual: ")
    ctx.analysis._memory.started_at_ms = ctx.analysis._clock_ms() - 1000  # recien arrancado
    assert (await ctx.analysis.run_symbols([SYM])).status == "warmup"


# ---------------------------------------------------------------------------- POST /api/analyze

async def test_analyze_now_runs_and_reports_status(tmp_path) -> None:
    ctx, calls = await _analysis_ctx(tmp_path)
    c = _client(ctx)
    r = await c.post("/api/analyze", json={"symbol": "solusdt"}, headers=H)
    assert r.status_code == 202
    body = r.json()
    assert body["status"] == "queued" and body["symbol"] == SYM and body["run_id"].startswith("manual-")
    assert body["queued_at"].endswith("+00:00")
    done = await _wait_done(c, body["run_id"])
    assert done["status"] == "ran" and done["detail"].startswith("manual: ")
    assert done["cycle_url"] == f"/api/cycles/{body['run_id']}" and calls == [[SYM]]
    assert (await c.get(done["cycle_url"])).json()["report_source"] == "cycle_log"
    # cooldown de 5 min por simbolo
    again = await c.post("/api/analyze", json={"symbol": SYM}, headers=H)
    assert again.status_code == 429 and 290 <= again.json()["retry_after_s"] <= 300
    assert int(again.headers["retry-after"]) == again.json()["retry_after_s"]
    # otro simbolo si puede
    assert (await c.post("/api/analyze", json={"symbol": "BTCUSDT"}, headers=H)).status_code == 202
    # tras un reinicio (sin el trabajo en memoria) el estado sale del historial
    ctx2 = PanelContext(health=HealthRegistry(), analysis=ctx.analysis, cycle_log=ctx.cycle_log)
    old = (await _client(ctx2).get(f"/api/analyze/{body['run_id']}")).json()
    assert old["status"] == "ran" and old["symbol"] == SYM
    assert (await c.get("/api/analyze/manual-1")).status_code == 404
    assert (await c.get("/api/analyze/bad id")).status_code == 400


async def test_analyze_only_one_in_flight(tmp_path) -> None:
    gate = asyncio.Event()
    ctx, _ = await _analysis_ctx(tmp_path, gate=gate)
    c = _client(ctx)
    first = (await c.post("/api/analyze", json={"symbol": SYM}, headers=H)).json()
    await asyncio.sleep(0.05)
    assert (await c.get(f"/api/analyze/{first['run_id']}")).json()["status"] == "running"
    busy = await c.post("/api/analyze", json={"symbol": "BTCUSDT"}, headers=H)
    assert busy.status_code == 429 and busy.json()["run_id"] == first["run_id"] and busy.json()["retry_after_s"] > 0
    gate.set()
    assert (await _wait_done(c, first["run_id"]))["status"] == "ran"


async def test_analyze_validation_and_security(tmp_path) -> None:
    ctx, calls = await _analysis_ctx(tmp_path)
    c = _client(ctx)
    assert (await c.post("/api/analyze", json={"symbol": SYM})).status_code == 403  # sin header
    assert (await c.post("/api/analyze", content=b"symbol=SOLUSDT", headers={**H, "content-type":
                                                                             "application/x-www-form-urlencoded"})).status_code == 415
    assert (await c.post("/api/analyze", content=b"{", headers={**H, "content-type": "application/json"})).status_code == 400
    big = json.dumps({"symbol": "x" * 5000}).encode()
    assert (await c.post("/api/analyze", content=big, headers={**H, "content-type": "application/json"})).status_code == 413
    assert (await c.post("/api/analyze", json={"symbol": SYM, "extra": 1}, headers=H)).status_code == 422
    assert (await c.post("/api/analyze", json={}, headers=H)).status_code == 422
    assert (await c.post("/api/analyze", json={"symbol": "DOGEUSDT"}, headers=H)).status_code == 400
    assert (await c.get("/api/analyze")).status_code in (404, 405)
    assert calls == []
    (tmp_path / "b").mkdir()
    no_llm, _ = await _analysis_ctx(tmp_path / "b", with_llm=False)
    assert (await _client(no_llm).post("/api/analyze", json={"symbol": SYM}, headers=H)).status_code == 503
    assert (await _client(PanelContext(health=HealthRegistry())).post(
        "/api/analyze", json={"symbol": SYM}, headers=H)).status_code == 503


# ---------------------------------------------------------------------------- cola del laboratorio (store)

def test_store_is_append_only_and_tracks_state(tmp_path) -> None:
    store = LabRequestStore(tmp_path / "eval.sqlite")
    r = store.create(["a-v1"], ["XRPUSDT"], "nota")
    assert r["status"] == "pending" and r["strategy_ids"] == ["a-v1"] and lr.REQUEST_ID.match(r["request_id"])
    claimed = store.claim_next("pc")
    assert claimed["request_id"] == r["request_id"] and claimed["status"] == "running" and claimed["worker"] == "pc"
    assert store.claim_next("pc") is None
    with pytest.raises(LabRequestError, match="running"):
        store._transition(r["request_id"], "running")
    store.done(r["request_id"], {"ok": 1})
    assert store.get(r["request_id"])["result"] == {"ok": 1}
    with pytest.raises(LabRequestError):
        store.fail(r["request_id"], "tarde")
    assert [e["status"] for e in store.events(r["request_id"])] == ["pending", "running", "done"]
    conn = sqlite3.connect(tmp_path / "eval.sqlite", isolation_level=None)  # sin transaccion colgada
    for sql in ("UPDATE lab_request_events SET status='pending'", "DELETE FROM lab_requests"):
        with pytest.raises(sqlite3.DatabaseError, match="append-only"):
            conn.execute(sql)
    other = store.create(["b-v1"], ["XRPUSDT"])
    store.fail(other["request_id"], "x" * 5000)
    assert len(store.get(other["request_id"])["error"]) == lr.MAX_ERROR
    assert store.list(status="error")[0] == 1 and store.list()[0] == 2


def test_claim_takes_oldest_first(tmp_path) -> None:
    clock = Clock(1000)
    store = LabRequestStore(tmp_path / "eval.sqlite", clock_ms=clock)
    first = store.create(["a"], ["XRPUSDT"])
    clock.ms = 2000
    store.create(["b"], ["XRPUSDT"])
    assert store.claim_next()["request_id"] == first["request_id"]


def _cli(db: Path, *args: str) -> tuple[int, dict]:
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = lr.main(["--db", str(db), *args])
    return rc, json.loads(buf.getvalue().strip().splitlines()[-1])


def test_cli_list_claim_done_fail(tmp_path) -> None:
    db = tmp_path / "eval.sqlite"
    store = LabRequestStore(db)
    a = store.create(["a"], ["XRPUSDT"])["request_id"]
    b = store.create(["b"], ["SOLUSDT"])["request_id"]
    assert _cli(db, "list", "--status", "pending")[1]["total"] == 2
    rc, out = _cli(db, "claim-next", "--worker", "pc")
    assert rc == 0 and out["request"]["request_id"] == a
    rc, out = _cli(db, "done", a)
    assert rc == 1 and "falta el archivo" in out["error"]  # sin resultados subidos no se marca done
    (tmp_path / "lab_requests").mkdir()
    (tmp_path / "lab_requests" / f"{a}.json").write_text(json.dumps({"strategies": [
        {"status": "ok", "summary": {"pasa": True}}, {"status": "error", "error": "faltan datos"}]}))
    rc, out = _cli(db, "done", a)
    assert rc == 0 and out["request"]["result"] == {"file": f"{a}.json", "strategies": 2, "ok": 1, "errors": 1,
                                                    "passing": 1}
    rc, out = _cli(db, "fail", b, "--error", "sin datos de SOLUSDT")
    assert rc == 0 and out["request"]["status"] == "error" and out["request"]["error"] == "sin datos de SOLUSDT"
    assert _cli(db, "fail", b, "--error", "otra vez")[0] == 1  # ya termino
    assert _cli(db, "done", "../../etc")[0] == 2
    assert _cli(db, "claim-next")[1] == {"request": None}


# ---------------------------------------------------------------------------- cola del laboratorio (API)

async def _queue_ctx(tmp_path) -> PanelContext:
    (tmp_path / "lab_summary.json").write_text(json.dumps({"strategies": [
        {"strategy_id": "donchian-ensemble-v1"}, {"strategy_id": "capitulacion-long-v1"}]}))
    return PanelContext(health=HealthRegistry(), config={"symbols": ["XRPUSDT", "SOLUSDT"]},
                        lab_summary_path=tmp_path / "lab_summary.json",
                        lab_requests=LabRequestStore(tmp_path / "eval.sqlite"))


async def test_lab_request_create_list_detail_result(tmp_path) -> None:
    ctx = await _queue_ctx(tmp_path)
    c = _client(ctx)
    r = await c.post("/api/lab/requests", headers=H,
                     json={"strategy_ids": ["donchian-ensemble-v1"], "symbols": ["xrp-usdt"], "note": " probar "})
    assert r.status_code == 201
    body = r.json()
    rid = body["request_id"]
    assert body["status"] == "pending" and body["symbols"] == ["XRPUSDT"] and body["note"] == "probar"
    assert body["created_at"].endswith("+00:00") and body["result_url"] is None
    star = (await c.post("/api/lab/requests", headers=H, json={"strategy_ids": ["*"], "symbols": ["SOLUSDT"]})).json()
    assert star["strategy_ids"] == ["capitulacion-long-v1", "donchian-ensemble-v1"]

    lst = (await c.get("/api/lab/requests")).json()
    assert lst["total"] == 2 and lst["pending"] == 2 and lst["max_pending"] == 10
    assert (await c.get("/api/lab/requests", params={"status": "done"})).json()["total"] == 0
    assert (await c.get("/api/lab/requests", params={"status": "x"})).status_code == 422

    # el worker la toma y la termina (por la CLI, como en la VM)
    ctx.lab_requests.claim_next("pc")
    (tmp_path / "lab_requests").mkdir()
    (tmp_path / "lab_requests" / f"{rid}.json").write_text(
        '{"request_id": "%s", "strategies": [{"status": "ok", "summary": {"sharpe": NaN, "pasa": false}}]}' % rid)
    assert (await c.get(f"/api/lab/requests/{rid}/result")).status_code == 404  # todavia running
    _cli(tmp_path / "eval.sqlite", "done", rid)
    det = (await c.get(f"/api/lab/requests/{rid}")).json()
    assert det["status"] == "done" and [e["status"] for e in det["events"]] == ["pending", "running", "done"]
    assert det["result"]["ok"] == 1 and det["result_url"] == f"/api/lab/requests/{rid}/result"
    res = (await c.get(det["result_url"])).json()
    assert res["strategies"][0]["summary"]["sharpe"] is None  # NaN -> null
    assert (await c.get("/api/lab/requests/" + "0" * 32)).status_code == 404
    assert (await c.get("/api/lab/requests/nope")).status_code == 400
    assert (await c.get("/api/lab/donchian-ensemble-v1")).status_code == 404  # la ruta de detalle sigue igual


async def test_lab_request_validation(tmp_path) -> None:
    ctx = await _queue_ctx(tmp_path)
    c = _client(ctx)
    ok = {"strategy_ids": ["donchian-ensemble-v1"], "symbols": ["XRPUSDT"]}
    assert (await c.post("/api/lab/requests", json=ok)).status_code == 403
    assert (await c.post("/api/lab/requests", headers=H, json={**ok, "strategy_ids": ["inventada-v9"]})).status_code == 400
    assert (await c.post("/api/lab/requests", headers=H, json={**ok, "strategy_ids": ["*", "x"]})).status_code == 400
    assert (await c.post("/api/lab/requests", headers=H, json={**ok, "symbols": ["DOGEUSDT"]})).status_code == 400
    assert (await c.post("/api/lab/requests", headers=H, json={**ok, "symbols": []})).status_code == 422
    assert (await c.post("/api/lab/requests", headers=H, json={**ok, "note": "x" * 501})).status_code == 422
    for _ in range(10):
        assert (await c.post("/api/lab/requests", headers=H, json=ok)).status_code == 201
    full = await c.post("/api/lab/requests", headers=H, json=ok)
    assert full.status_code == 429 and full.json()["retry_after_s"] > 0
    (tmp_path / "lab_summary.json").unlink()
    panel._LAB_CACHE.clear()
    assert (await c.post("/api/lab/requests", headers=H, json=ok)).status_code == 503
    no_queue = _client(PanelContext(health=HealthRegistry()))
    assert (await no_queue.get("/api/lab/requests")).status_code == 503


# ---------------------------------------------------------------------------- worker de la PC

class FakeRemote:
    """La VM simulada: corre la CLI real contra una base local y 'sube' copiando al directorio de datos."""

    def __init__(self, db: Path) -> None:
        self.db, self.calls = db, []

    def cli(self, args: list[str]) -> dict:
        self.calls.append(args)
        return _cli(self.db, *args)[1]

    def upload(self, local: Path, request_id: str) -> None:
        dest = self.db.parent / "lab_requests" / f"{request_id}.json"
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(local.read_bytes())


def test_worker_processes_queue_without_touching_the_batch_results(tmp_path, monkeypatch) -> None:
    from heatsignal.lab import run as lab_run
    from heatsignal.lab import worker
    from heatsignal.lab.trials import TrialRegistry

    from test_lab_run import _md

    vm = tmp_path / "vm"
    store = LabRequestStore(vm / "eval.sqlite")
    ok = store.create(["absorcion-flujo-v1", "no-existe-v1"], ["AAAUSDT"])["request_id"]
    bad = store.create(["absorcion-flujo-v1"], ["ZZZUSDT"])["request_id"]
    home = tmp_path / "lab"
    batch = home / "results" / "absorcion-flujo-v1" / "20260101T000000.json"
    batch.parent.mkdir(parents=True)
    batch.write_text("{}")
    monkeypatch.setattr(lab_run, "open_holdout", lambda *a, **k: pytest.fail("la cola no abre el holdout"))

    def fake_eval(cfg, registry):
        assert cfg.n_perm == worker.N_PERM and registry is reg
        if cfg.symbols != ["AAAUSDT"]:
            raise FileNotFoundError(f"sin datos de klines para {cfg.symbols[0]} (correr lab.vision)")
        cfg.holdout_months, cfg.train_days, cfg.test_days, cfg.n_perm = 1, 20, 10, 3
        return lab_run.evaluate(cfg, registry, data={"AAAUSDT": _md(80, 1)})

    reg = TrialRegistry(tmp_path / "trials.sqlite")
    remote = FakeRemote(vm / "eval.sqlite")
    out = worker.run_once(remote=remote, home=home, registry=reg, evaluate_fn=fake_eval)
    assert [o["request"]["status"] for o in out] == ["done", "error"]
    assert reg.count("absorcion-flujo-v1", "train") > 0  # cuenta como intentos
    assert reg.count("absorcion-flujo-v1", "holdout") == 0

    done = store.get(ok)
    assert done["result"] == {"file": f"{ok}.json", "strategies": 2, "ok": 1, "errors": 1, "passing": 0}
    payload = json.loads((vm / "lab_requests" / f"{ok}.json").read_text())
    blocks = {b["strategy_id"]: b for b in payload["strategies"]}
    assert blocks["no-existe-v1"]["status"] == "error" and "catalogo" in blocks["no-existe-v1"]["error"]
    good = blocks["absorcion-flujo-v1"]
    assert good["file"].startswith("results/absorcion-flujo-v1/symbols-AAAUSDT/")
    assert good["summary"]["trades"] is not None and good["detail"]["walk_forward"]["windows"] >= 2
    assert "SELLADO" in payload["holdout"]
    # el resultado del lote no se toca y lab.publish no ve la subcarpeta como otra corrida
    assert batch.read_text() == "{}" and sorted(p.name for p in batch.parent.glob("*.json")) == [batch.name]
    from heatsignal.lab.publish import summarize

    assert summarize(home / "results")["strategies"][0]["runs"] == 1

    err = store.get(bad)
    assert err["status"] == "error" and "faltan datos" in err["error"] and "ZZZUSDT" in err["error"]
    assert worker.run_once(remote=remote, home=home, registry=reg, evaluate_fn=fake_eval) == []


def test_worker_marks_error_when_upload_fails(tmp_path) -> None:
    from heatsignal.lab import worker

    store = LabRequestStore(tmp_path / "eval.sqlite")
    rid = store.create(["absorcion-flujo-v1"], ["AAAUSDT"])["request_id"]

    class Broken(FakeRemote):
        def upload(self, local, request_id):
            raise RuntimeError("scp: conexion rechazada")

    rep = {"oos": {"sharpe": 1.0, "trades": 3}, "baselines": {}, "verdict": {"pasa": False}}
    worker.run_once(remote=Broken(tmp_path / "eval.sqlite"), home=tmp_path / "lab", registry=None,
                    evaluate_fn=lambda cfg, reg: rep)
    assert store.get(rid)["status"] == "error" and "conexion rechazada" in store.get(rid)["error"]


def test_gcloud_remote_builds_quoted_commands(monkeypatch) -> None:
    from heatsignal.lab import worker

    seen = []

    class R:
        returncode, stdout, stderr = 0, 'aviso de gcloud\n{"request": null}\n', ""

    monkeypatch.setattr(worker.subprocess, "run", lambda cmd, **kw: seen.append(cmd) or R())
    out = worker.GcloudRemote(host="vm", flags=["--zone", "z"]).cli(["fail", "abc", "--error", "a'b; rm -rf /"])
    assert out == {"request": None}
    cmd = seen[0]
    assert cmd[:4] == ["gcloud", "compute", "ssh", "--zone"] and cmd[5] == "vm" and cmd[6] == "--command"
    assert "heatsignal.evaluation.lab_requests fail abc --error 'a'\"'\"'b; rm -rf /'" in cmd[7]
