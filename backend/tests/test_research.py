"""Investigacion semanal (research/): contexto, alta idempotente, bajada de informes y endpoints del panel. Sin red."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace as NS

import httpx

from heatsignal.api.panel import PanelContext, create_panel_app
from heatsignal.ingestion.health import HealthRegistry
from heatsignal.research import context as C
from heatsignal.research import managed as M

SUMMARY = {"generated_at": "2026-10-25T00:00:00Z", "strategies": [
    {"strategy_id": "a-v1", "family": "carry", "verdict": {"pasa": False}},
    {"strategy_id": "b-v1", "family": "carry", "verdict": {"pasa": True}},
    {"strategy_id": "c-v1", "family": "ruptura-n", "verdict": {"pasa": True}},
]}


class _Download:
    def __init__(self, text: str) -> None:
        self.text = text

    def write_to_file(self, path: str) -> None:
        Path(path).write_text(self.text, encoding="utf-8")


class FakeClient:
    """Lo minimo de anthropic.Anthropic que usa research.managed, con registro de llamadas."""

    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.runs: list[NS] = []
        self.sessions: dict[str, NS] = {}
        self.idle: dict[str, NS] = {}
        self.outputs: dict[str, list[NS]] = {}
        self.downloads: dict[str, str] = {}
        c = self

        class _Env:
            def create(self, **kw):
                c.calls.append(("env.create", kw)); return NS(id="env_1")

        class _Agents:
            def create(self, **kw):
                c.calls.append(("agent.create", kw)); return NS(id="agent_1", version=1)

        class _Deps:
            def create(self, **kw):
                c.calls.append(("dep.create", kw)); return NS(id="depl_1")

            def pause(self, i):
                c.calls.append(("dep.pause", i))

            def update(self, i, **kw):
                c.calls.append(("dep.update", i, kw))

        class _Runs:
            def list(self, **kw):
                return iter(c.runs)

        class _Events:
            def list(self, session_id, **kw):
                e = c.idle.get(session_id)
                return NS(data=[e] if e else [])

            def send(self, session_id, events):
                c.calls.append(("events.send", session_id, events))
                c.idle.pop(session_id, None)

        class _Sessions:
            events = _Events()

            def retrieve(self, session_id):
                return c.sessions[session_id]

        class _BetaFiles:
            def list(self, scope_id, betas):
                return NS(data=c.outputs.get(scope_id, []))

        class _Files:
            def upload(self, file):
                c.calls.append(("files.upload", file[0], file[1].decode()))
                return NS(id=f"file_{sum(1 for x in c.calls if x[0] == 'files.upload')}")

            def download(self, fid):
                return _Download(c.downloads[fid])

        self.beta = NS(environments=_Env(), agents=_Agents(), deployments=_Deps(), deployment_runs=_Runs(),
                       sessions=_Sessions(), files=_BetaFiles())
        self.files = _Files()


def _data(tmp_path: Path) -> Path:
    (tmp_path / "lab_summary.json").write_text(json.dumps(SUMMARY))
    return tmp_path


def test_context_lists_tested_families_and_rules() -> None:
    text = C.build(SUMMARY)
    assert "| carry | 2 | 1 | a-v1, b-v1 |" in text and "| ruptura-n | 1 | 1 | c-v1 |" in text
    assert "Pre-registro" in text and "Datos disponibles" in text
    assert "todavía no hay resumen" in C.build(None)


def test_provision_is_idempotent_and_starts_paused(tmp_path) -> None:
    base, fc = _data(tmp_path), FakeClient()
    st = M.provision(fc, base=base)
    assert st == {**st, "environment_id": "env_1", "agent_id": "agent_1", "deployment_id": "depl_1", "paused": True}
    kinds = [c[0] for c in fc.calls]
    assert kinds == ["env.create", "agent.create", "files.upload", "dep.create", "dep.pause"]
    env = fc.calls[0][1]["config"]["networking"]
    assert env["type"] == "limited" and env["allowed_hosts"] == M.HOSTS and not env["allow_package_managers"]
    tools = fc.calls[1][1]["tools"][0]
    assert tools["default_config"]["permission_policy"] == {"type": "auto"}
    assert {c["name"]: c["enabled"] for c in tools["configs"]} == {"web_search": True, "web_fetch": True}
    dep = fc.calls[3][1]
    assert dep["schedule"] == M.SCHEDULE and dep["budget"]["max_list_cost"]["amount"] == "5"  # US$0,05
    assert [e["type"] for e in dep["initial_events"]] == ["user.message"]  # sin evaluador: no entra en el tope
    assert fc.calls[1][1]["model"]["id"] == "claude-haiku-5-5"
    assert dep["resources"] == [{"type": "file", "file_id": "file_1", "mount_path": "/contexto.md"}]
    M.provision(fc, base=base)  # segunda vez: no crea nada
    assert len(fc.calls) == 5


def test_refresh_context_only_when_it_changes(tmp_path) -> None:
    base, fc = _data(tmp_path), FakeClient()
    st = M.provision(fc, base=base)
    assert M.refresh_context(fc, st, base=base) is False
    (base / "lab_summary.json").write_text(json.dumps({**SUMMARY, "generated_at": "2026-11-18T00:00:00Z"}))
    assert M.refresh_context(fc, st, base=base) is True
    assert fc.calls[-1] == ("dep.update", "depl_1", {"resources": [{"type": "file", "file_id": "file_2", "mount_path": "/contexto.md"}]})


def test_fetch_reports_downloads_denies_and_records_errors(tmp_path) -> None:
    base, fc = _data(tmp_path), FakeClient()
    st = M.provision(fc, base=base)
    fc.runs = [
        NS(id="drun_ok", created_at="2026-11-23T02:01:00Z", trigger_context=NS(type="schedule"), session_id="s_ok", error=None),
        NS(id="drun_wait", created_at="2026-11-24T02:01:00Z", trigger_context=NS(type="manual"), session_id="s_wait", error=None),
        NS(id="drun_bad", created_at="2026-11-25T02:01:00Z", trigger_context=NS(type="schedule"), session_id=None,
           error=NS(type="session_rate_limited_error", message="limite")),
    ]
    fc.sessions = {"s_ok": NS(status="idle", outcome_evaluations=[NS(result="satisfied")]),
                   "s_wait": NS(status="idle", outcome_evaluations=[])}
    fc.idle = {"s_wait": NS(stop_reason=NS(type="requires_action", event_ids=["sevt_1", "sevt_2"]))}
    fc.outputs = {"s_ok": [NS(id="f_out", filename="informe.md")]}
    fc.downloads = {"f_out": "# Investigación semanal\nver https://arxiv.org/abs/1"}

    M.fetch_reports(fc, st, base=base)
    idx = {e["run_id"]: e for e in M.read_index(base)["items"]}
    assert idx["drun_ok"]["status"] == "ok" and idx["drun_ok"]["outcome"] == "satisfied"
    assert (base / "research" / idx["drun_ok"]["file"]).read_text().startswith("# Investigación")
    assert idx["drun_wait"]["status"] == "en_curso" and idx["drun_wait"]["denied_calls"] == 2
    sent = [c for c in fc.calls if c[0] == "events.send"][0][2]
    assert {e["result"] for e in sent} == {"deny"} and {e["tool_use_id"] for e in sent} == {"sevt_1", "sevt_2"}
    assert idx["drun_bad"]["status"] == "error" and "session_rate_limited_error" in idx["drun_bad"]["error"]

    # segunda pasada: la negada termino sin informe; las cerradas no se vuelven a consultar
    fc.outputs = {}
    M.fetch_reports(fc, st, base=base)
    idx = {e["run_id"]: e for e in M.read_index(base)["items"]}
    assert idx["drun_wait"]["status"] == "sin_informe" and idx["drun_ok"]["status"] == "ok"


async def test_panel_research_endpoints(tmp_path) -> None:
    base, fc = _data(tmp_path), FakeClient()
    ctx = PanelContext(health=HealthRegistry(), lab_summary_path=base / "lab_summary.json")
    app = create_panel_app(ctx)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://panel") as c:
        assert (await c.get("/api/research")).json()["configured"] is False
        st = M.provision(fc, base=base)
        fc.runs = [NS(id="drun_ok", created_at="2026-11-23T02:01:00Z", trigger_context=NS(type="schedule"),
                      session_id="s_ok", error=None)]
        fc.sessions = {"s_ok": NS(status="terminated", outcome_evaluations=[])}
        fc.outputs = {"s_ok": [NS(id="f_out", filename="informe.md")]}
        fc.downloads = {"f_out": "# informe"}
        M.fetch_reports(fc, st, base=base)
        d = (await c.get("/api/research")).json()
        assert d["configured"] and d["paused"] is True and d["items"][0]["run_id"] == "drun_ok"
        r = (await c.get("/api/research/drun_ok")).json()
        assert r["text"] == "# informe" and r["status"] == "ok"
        assert (await c.get("/api/research/drun_nada")).status_code == 404
        assert (await c.get("/api/research/..%2Fsecreto")).status_code in (400, 404)


def test_agent_strategies_are_validated_registered_and_sent_to_the_lab(tmp_path) -> None:
    """Fase 2 (2026-11-21): estrategias.json del agente -> bloques validados, registro 'agente', cola del laboratorio."""
    import json as _json
    import sqlite3

    from heatsignal.lab import builder as B

    base, fc = _data(tmp_path), FakeClient()
    st = M.provision(fc, base=base)
    good = {"name": "rsi-tendencia-1h", "timeframe": "1h", "direction": "long",
            "entry": {"type": "rsi", "period": 2, "low": 10, "high": 90}, "filters": [{"type": "daily_trend", "ema": 50}],
            "exit": {"bars": 24, "stop_atr": 2.0}, "description": "reversion corta a favor de la tendencia [1]"}
    other = {**good, "name": "ruptura-1h", "entry": {"type": "breakout", "n": 48}}
    bad = {**good, "name": "magia", "entry": {"type": "magia"}}
    fc.runs = [NS(id="drun_ok", created_at="2026-11-23T02:01:00Z", trigger_context=NS(type="schedule"),
                  session_id="s_ok", error=None)]
    fc.sessions = {"s_ok": NS(status="idle", outcome_evaluations=[])}
    fc.outputs = {"s_ok": [NS(id="f_inf", filename="informe.md"), NS(id="f_est", filename="estrategias.json")]}
    fc.downloads = {"f_inf": "# informe", "f_est": _json.dumps([good, bad, other, good])}
    M.fetch_reports(fc, st, base=base)
    (e,) = M.read_index(base)["items"]
    res = e["strategies"]
    assert [r["ok"] for r in res] == [True, False, True, True]
    assert "desconocido" in res[1]["error"] and res[3]["added"] is False  # repetida: no se duplica
    entries, _ = B.load_entries(base / "lab_generated.jsonl")
    assert [x["origin"] for x in entries] == ["agente", "agente"] and "drun_ok" in entries[0]["note"]
    n = sqlite3.connect(base / "heatsignal_eval.sqlite").execute("SELECT COUNT(*) FROM lab_requests").fetchone()[0]
    assert n == 2
    text = C.build(None, C.load_generated(base))
    assert "Formato para proponer estrategias" in text and entries[0]["strategy_id"] in text
