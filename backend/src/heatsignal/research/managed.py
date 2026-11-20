"""
Agente de investigacion semanal en Claude Managed Agents (beta): crearlo, refrescar su contexto y bajar los informes.

    python -m heatsignal.research.managed provision   # crea entorno + agente + contexto + deployment (PAUSADO)
    python -m heatsignal.research.managed update      # aplica cambios de modelo/prompt/herramientas/tope (version nueva)
    python -m heatsignal.research.managed run-now     # corrida manual (sirve con el deployment pausado)
    python -m heatsignal.research.managed unpause     # activa el horario (lunes 02:00 UTC)
    python -m heatsignal.research.managed pause
    python -m heatsignal.research.managed sync        # contexto al dia + baja informes nuevos (timer diario)
    python -m heatsignal.research.managed status

El agente investiga y propone: escribe /mnt/session/outputs/informe.md y, desde la fase 2 (2026-11-21), hasta 5
estrategias en /mnt/session/outputs/estrategias.json con el formato de BLOQUES de lab/builder.py (nunca codigo). `sync`
las valida, las registra (origen 'agente', registro append-only) y las manda al laboratorio; lo invalido queda con el
motivo en el indice. Nada vuelve a ningun prompt del grafo. Red `limited` a una lista fija de sitios de investigacion (HOSTS), sin credenciales en el contenedor.
Herramientas con permiso `auto`: si el servidor pausa una llamada para pedir aprobacion, `sync` la niega (nadie mira
la corrida). Tope de US$0,05 por corrida (Haiku 5.5); al llegar al tope la sesion queda `idle` y `sync` baja el
informe que haya escrito hasta ahi.

Estado (IDs de Anthropic, no secretos) en <data>/research/managed.json; informes en <data>/research/reports/ y el
indice en <data>/research/index.json (lo lee GET /api/research del panel).
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from collections.abc import Iterable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import context as C

log = logging.getLogger("heatsignal.research")

HERE = Path(__file__).parent
NAME = "HeatSignal - investigacion semanal"
# Tope pedido por el usuario (2026-11-18): US$0,05 por informe (prueba: US$0,03 en 61 s). web_search US$0,01 c/u, sesion
# US$0,08/h, Haiku 5.5 US$0,10/US$0,50 por M tokens. Por eso Haiku, <= 2 busquedas, web_fetch solo de URLs de los
# resultados (no abre URLs inventadas), recortado, y sin evaluador de rubrica (llamadas extra).
MODEL = {"id": "claude-haiku-5-5", "effort": "low"}
SCHEDULE = {"type": "cron", "expression": "0 2 * * 1", "timezone": "UTC"}  # lunes 02:00 UTC, tras la revision semanal
BUDGET = {"type": "limit", "max_list_cost": {"currency": "USD", "amount": "5"}}  # centavos: US$0,05 por corrida
FETCH_MAX_TOKENS = 6000
CONTEXT_MOUNT = "/contexto.md"  # queda en /mnt/session/uploads/contexto.md
REPORT_NAME = "informe.md"
STRATEGIES_NAME = "estrategias.json"  # propuestas en bloques (lab/builder.py), fase 2
MA_BETA = "managed-agents-2026-04-01"
# Sitios que puede leer (aprobados por el usuario 2026-11-18). Bajo red `limited`, web_search/web_fetch solo llegan a
# estos hosts (coincidencia exacta); allowed_domains ademas filtra cada herramienta.
HOSTS = [
    "arxiv.org", "export.arxiv.org", "ssrn.com", "papers.ssrn.com", "www.ssrn.com", "quantpedia.com",
    "www.quantpedia.com", "research.binance.com", "www.binance.com", "blog.kaiko.com", "research.kaiko.com",
    "www.kaiko.com", "insights.glassnode.com", "docs.coinglass.com", "www.cmegroup.com", "www.bis.org",
    "federalreserve.gov", "www.federalreserve.gov",
]
DOMAINS = ["arxiv.org", "ssrn.com", "quantpedia.com", "binance.com", "kaiko.com", "glassnode.com", "coinglass.com",
           "cmegroup.com", "bis.org", "federalreserve.gov"]
KICKOFF = ("Hacé la investigación de esta semana siguiendo tus instrucciones. Empezá por leer "
           "/mnt/session/uploads/contexto.md y dejá el informe en /mnt/session/outputs/informe.md.")
FINAL_SESSION = {"idle", "terminated"}
TRACE_BASE = "https://platform.claude.com/workspaces/default/sessions/"  # traza de la sesion en la consola


def data_dir() -> Path:
    eval_db = os.environ.get("HEATSIGNAL_EVAL_DB", str(Path.home() / "heatsignal" / "data" / "heatsignal_eval.sqlite"))
    return Path(eval_db).expanduser().parent


def research_dir(base: Path | None = None) -> Path:
    return (base or data_dir()) / "research"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def _write_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def _get(obj: Any, name: str, default: Any = None) -> Any:
    return obj.get(name, default) if isinstance(obj, dict) else getattr(obj, name, default)


# ---- definicion -----------------------------------------------------------------------------------------


def environment_config() -> dict[str, Any]:
    return {"type": "cloud", "networking": {"type": "limited", "allowed_hosts": list(HOSTS),
                                             "allow_package_managers": False, "allow_mcp_servers": False}}


def agent_tools() -> list[dict[str, Any]]:
    return [{
        "type": "agent_toolset_20260401",
        "default_config": {"enabled": True, "permission_policy": {"type": "auto"}},
        "configs": [
            {"name": "web_search", "enabled": True, "allowed_domains": list(DOMAINS)},
            {"name": "web_fetch", "enabled": True, "allowed_domains": list(DOMAINS), "max_content_tokens": FETCH_MAX_TOKENS},
        ],
    }]


def initial_events() -> list[dict[str, Any]]:
    # Solo el mensaje: un user.define_outcome sumaria un evaluador (llamadas extra) que no entra en US$0,05.
    return [{"type": "user.message", "content": [{"type": "text", "text": KICKOFF}]}]


def update_definition(client: Any, st: dict[str, Any]) -> dict[str, Any]:
    """Lleva el agente (nueva version) y el deployment a la definicion actual de este archivo y de prompt.md."""
    agent = client.beta.agents.update(st["agent_id"], model=MODEL, system=(HERE / "prompt.md").read_text(encoding="utf-8"),
                                      tools=agent_tools())
    st["agent_version"] = agent.version
    client.beta.deployments.update(st["deployment_id"], agent={"type": "agent", "id": st["agent_id"], "version": agent.version},
                                   initial_events=initial_events(), budget=BUDGET, schedule=SCHEDULE)
    return st


def _upload_context(client: Any, text: str) -> str:
    f = client.files.upload(file=("contexto.md", text.encode("utf-8"), "text/markdown"))
    return f.id


# ---- provision ------------------------------------------------------------------------------------------


def provision(client: Any, *, base: Path | None = None, lab_summary: Path | None = None) -> dict[str, Any]:
    """Crea lo que falte (idempotente por managed.json). El deployment nace PAUSADO: se activa con `unpause`."""
    rdir = research_dir(base)
    state_path = rdir / "managed.json"
    st = _read_json(state_path, {})
    if not st.get("environment_id"):
        env = client.beta.environments.create(name="heatsignal-investigacion", config=environment_config(),
                                              description="Red limitada a sitios de investigacion; sin credenciales")
        st["environment_id"] = env.id
        _write_json(state_path, st)
    if not st.get("agent_id"):
        agent = client.beta.agents.create(name=NAME, model=MODEL, system=(HERE / "prompt.md").read_text(encoding="utf-8"),
                                          tools=agent_tools(), description="Investiga y propone familias nuevas; no decide ni opera",
                                          metadata={"proyecto": "heatsignal"})
        st["agent_id"], st["agent_version"] = agent.id, agent.version
        _write_json(state_path, st)
    text = C.build(C.load_summary(lab_summary or (base or data_dir()) / "lab_summary.json"), C.load_generated(base or data_dir()))
    if not st.get("context_file_id"):
        st["context_file_id"], st["context_sha"] = _upload_context(client, text), C.sha(text)
        _write_json(state_path, st)
    if not st.get("deployment_id"):
        dep = client.beta.deployments.create(
            name="Investigacion semanal", agent={"type": "agent", "id": st["agent_id"], "version": st["agent_version"]},
            environment_id=st["environment_id"], initial_events=initial_events(), schedule=SCHEDULE, budget=BUDGET,
            resources=[{"type": "file", "file_id": st["context_file_id"], "mount_path": CONTEXT_MOUNT}],
            description="Lunes 02:00 UTC; informe en /mnt/session/outputs/informe.md")
        st["deployment_id"] = dep.id
        _write_json(state_path, st)
        client.beta.deployments.pause(dep.id)
        st["paused"] = True
        _write_json(state_path, st)
    return st


# ---- sync -----------------------------------------------------------------------------------------------


def refresh_context(client: Any, st: dict[str, Any], *, base: Path | None = None, lab_summary: Path | None = None) -> bool:
    """Si el contexto cambio (nuevo lab_summary), lo sube y apunta el deployment al archivo nuevo."""
    text = C.build(C.load_summary(lab_summary or (base or data_dir()) / "lab_summary.json"), C.load_generated(base or data_dir()))
    if C.sha(text) == st.get("context_sha"):
        return False
    fid = _upload_context(client, text)
    client.beta.deployments.update(st["deployment_id"], resources=[{"type": "file", "file_id": fid, "mount_path": CONTEXT_MOUNT}])
    st["context_file_id"], st["context_sha"] = fid, C.sha(text)
    return True


def _deny_pending(client: Any, session_id: str) -> int:
    """Una sesion esperando aprobacion (auto sin decision) se queda colgada: nadie mira la corrida, se niega."""
    idle = client.beta.sessions.events.list(session_id=session_id, types=["session.status_idle"], order="desc", limit=1)
    last = next(iter(_get(idle, "data", []) or []), None)
    stop = _get(last, "stop_reason") if last is not None else None
    if _get(stop, "type") != "requires_action":
        return 0
    ids = list(_get(stop, "event_ids", []) or [])
    if ids:
        client.beta.sessions.events.send(session_id=session_id, events=[
            {"type": "user.tool_confirmation", "tool_use_id": i, "result": "deny",
             "deny_message": "Corrida desatendida: no hay nadie para aprobar esta llamada. Seguí sin ella."} for i in ids])
    return len(ids)


def _download_report(client: Any, session_id: str, dest: Path, name: str = REPORT_NAME) -> bool:
    files = client.beta.files.list(scope_id=session_id, betas=[MA_BETA])
    for f in _get(files, "data", []) or []:
        if _get(f, "filename") == name:
            dest.parent.mkdir(parents=True, exist_ok=True)
            tmp = dest.with_name(f".{dest.name}.tmp")
            client.files.download(_get(f, "id")).write_to_file(str(tmp))
            tmp.replace(dest)
            return True
    return False


def _list_cost_usd(session: Any) -> float | None:
    """usage.list_cost de la sesion (centavos como string entero) en dolares."""
    lc = _get(_get(session, "usage"), "list_cost")
    try:
        return int(_get(lc, "amount")) / 100 if lc is not None else None
    except (TypeError, ValueError):
        return None


def _outcome(session: Any) -> str | None:
    evals = _get(session, "outcome_evaluations") or []
    return _get(evals[-1], "result") if evals else None


def _ingest_strategies(client: Any, session_id: str, rdir: Path, created: str, run_id: str,
                       data: Path) -> list[dict[str, Any]] | None:
    """estrategias.json de la corrida -> lab/builder.ingest (validar, registrar como 'agente', pedir el laboratorio).
    None = el agente no propuso estrategias esta semana (valido)."""
    dest = rdir / "strategies" / f"{created[:10] or 'sin-fecha'}_{session_id}.json"
    try:
        if not _download_report(client, session_id, dest, STRATEGIES_NAME):
            return None
        raw = json.loads(dest.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return [{"ok": False, "error": f"estrategias.json ilegible: {exc}"[:300]}]
    from ..lab import builder as B

    return B.ingest(raw, origin="agente", note=f"investigacion semanal {created[:10]} ({run_id})",
                    path=data / "lab_generated.jsonl", eval_db=data / "heatsignal_eval.sqlite")


def fetch_reports(client: Any, st: dict[str, Any], *, base: Path | None = None) -> list[dict[str, Any]]:
    """Recorre las corridas del deployment y deja cada informe nuevo en reports/. Devuelve las entradas cambiadas."""
    rdir = research_dir(base)
    idx_path = rdir / "index.json"
    index = _read_json(idx_path, {"items": []})
    by_run = {e["run_id"]: e for e in index["items"]}
    changed = []
    for run in client.beta.deployment_runs.list(deployment_id=st["deployment_id"]):
        rid = _get(run, "id")
        prev = by_run.get(rid)
        if prev and prev.get("status") in ("ok", "sin_informe", "error"):
            continue
        created = str(_get(run, "created_at") or "")
        trig = _get(_get(run, "trigger_context"), "type")
        entry = prev or {"run_id": rid, "created_at": created, "trigger": trig, "session_id": _get(run, "session_id")}
        err = _get(run, "error")
        if err is not None:
            entry.update(status="error", error=f"{_get(err, 'type')}: {_get(err, 'message')}")
        else:
            sid = entry["session_id"]
            session = client.beta.sessions.retrieve(session_id=sid)
            status = _get(session, "status")
            denied = _deny_pending(client, sid) if status == "idle" else 0
            if denied:
                entry["denied_calls"] = entry.get("denied_calls", 0) + denied
                entry["status"] = "en_curso"
            elif status in FINAL_SESSION:
                fname = f"{created[:10] or 'sin-fecha'}_{sid}.md"
                got = _download_report(client, sid, rdir / "reports" / fname)
                entry.update(status="ok" if got else "sin_informe", file=f"reports/{fname}" if got else None,
                             outcome=_outcome(session))
                entry["strategies"] = _ingest_strategies(client, sid, rdir, created, rid, base or data_dir())
                cost = _list_cost_usd(session)
                if cost is not None and not entry.get("cost_recorded"):
                    # cuenta para el tope semanal de LLM (agents/spend.py), con la fecha de la corrida
                    from ..agents.spend import SpendLedger

                    SpendLedger((base or data_dir()) / "llm_spend.sqlite").add("research", MODEL["id"], cost=cost)
                    entry.update(cost_usd=cost, cost_recorded=True)
            else:
                entry["status"] = "en_curso"
        entry["checked_at"] = _now()
        by_run[rid] = entry
        changed.append(entry)
    index["items"] = sorted(by_run.values(), key=lambda e: e.get("created_at") or "", reverse=True)
    index["updated_at"] = _now()
    _write_json(idx_path, index)
    return changed


def sync(client: Any, *, base: Path | None = None, lab_summary: Path | None = None) -> dict[str, Any]:
    rdir = research_dir(base)
    st = _read_json(rdir / "managed.json", {})
    if not st.get("deployment_id"):
        raise RuntimeError("no hay deployment: correr primero `python -m heatsignal.research.managed provision`")
    refreshed = refresh_context(client, st, base=base, lab_summary=lab_summary)
    _write_json(rdir / "managed.json", st)
    return {"context_refreshed": refreshed, "runs": fetch_reports(client, st, base=base)}


# ---- lectura para el panel ------------------------------------------------------------------------------


def read_index(base: Path) -> dict[str, Any]:
    rdir = research_dir(base)
    st = _read_json(rdir / "managed.json", {})
    idx = _read_json(rdir / "index.json", {"items": []})
    return {"configured": bool(st.get("deployment_id")), "paused": st.get("paused"), "schedule": SCHEDULE,
            "deployment_id": st.get("deployment_id"), "agent_id": st.get("agent_id"),
            "updated_at": idx.get("updated_at"), "items": idx.get("items", [])}


def read_report(base: Path, run_id: str) -> dict[str, Any] | None:
    idx = read_index(base)
    entry = next((e for e in idx["items"] if e.get("run_id") == run_id), None)
    if entry is None:
        return None
    text = None
    if entry.get("file"):
        p = research_dir(base) / entry["file"]
        if p.resolve().parent == (research_dir(base) / "reports").resolve():  # solo dentro de reports/
            try:
                text = p.read_text(encoding="utf-8")
            except OSError:
                text = None
    trace = f"{TRACE_BASE}{entry['session_id']}" if entry.get("session_id") else None
    return {**entry, "text": text, "trace_url": trace}


# ---- CLI ------------------------------------------------------------------------------------------------


def _client() -> Any:
    import anthropic

    return anthropic.Anthropic()


def main(argv: Iterable[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Agente de investigacion semanal (Claude Managed Agents).")
    p.add_argument("cmd", choices=["provision", "update", "run-now", "pause", "unpause", "sync", "status"])
    args = p.parse_args(list(argv) if argv is not None else None)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    client = _client()
    rdir = research_dir()
    st = _read_json(rdir / "managed.json", {})
    if args.cmd == "provision":
        st = provision(client)
        dep = client.beta.deployments.retrieve(st["deployment_id"])
        print(json.dumps({**st, "status": _get(dep, "status"),
                          "upcoming_runs_at": [str(x) for x in (_get(_get(dep, "schedule"), "upcoming_runs_at") or [])]},
                         ensure_ascii=False, indent=2))
    elif args.cmd == "update":
        st = update_definition(client, st)
        _write_json(rdir / "managed.json", st)
        print(json.dumps({"agent_version": st["agent_version"], "model": MODEL, "budget_usd_cents": BUDGET["max_list_cost"]["amount"]}))
    elif args.cmd == "run-now":
        run = client.beta.deployments.run(st["deployment_id"])
        sid = _get(run, "session_id")
        print(json.dumps({"run_id": _get(run, "id"), "session_id": sid,
                          "trace": f"{TRACE_BASE}{sid}"}, indent=2))
    elif args.cmd in ("pause", "unpause"):
        getattr(client.beta.deployments, args.cmd)(st["deployment_id"])
        st["paused"] = args.cmd == "pause"
        _write_json(rdir / "managed.json", st)
        print(f"deployment {st['deployment_id']}: {'pausado' if st['paused'] else 'activo (lunes 02:00 UTC)'}")
    elif args.cmd == "sync":
        out = sync(client)
        print(json.dumps(out, ensure_ascii=False, indent=2, default=str))
    else:
        print(json.dumps(read_index(data_dir()), ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
