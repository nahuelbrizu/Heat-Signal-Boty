"""
Worker del laboratorio: procesa en la PC las solicitudes que el panel deja en la VM ("Mandar al laboratorio").

    python -m heatsignal.lab.worker            # cada 300 s
    python -m heatsignal.lab.worker --once     # una pasada (todas las pendientes) y sale
    python -m heatsignal.lab.worker --interval 600

Por cada solicitud (la toma por SSH con `heatsignal.evaluation.lab_requests claim-next`, que corre en la VM):
  1. Corre `lab.run.evaluate` de cada estrategia con los simbolos pedidos (n_perm 100) contra el registro
     real de intentos (~/heatsignal-lab/trials.sqlite): cada corrida CUENTA como intentos (anti p-hacking).
     El holdout NUNCA se abre desde aca (evaluate no lo toca; open_holdout no se importa).
  2. Guarda cada reporte en results/<strategy_id>/symbols-<SYMS>/<ts>.json: subcarpeta propia, asi el
     resultado del lote (results/<strategy_id>/*.json, el que publica lab.publish) no se pisa.
  3. Arma data/lab_requests/<request_id>.json (resumen + detalle por estrategia, o el error de cada una),
     lo sube a la VM y marca la solicitud `done`. Si todas las estrategias fallan (p.ej. no hay datos de
     ese simbolo: "correr lab.vision"), la marca `error` con el motivo de cada una.

El acceso a la VM esta detras de `Remote` (inyectable): los tests no usan red ni SSH.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import shlex
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Iterable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

from .publish import VM, VM_HOST, _nan_to_null, detail
from .vision import lab_dir

log = logging.getLogger("heatsignal.lab.worker")

VM_ROOT = "~/heatsignal"
N_PERM = 100
DEFAULT_INTERVAL_S = 300.0


class Remote(Protocol):
    """Lo que el worker necesita de la VM."""

    def cli(self, args: list[str]) -> dict[str, Any]: ...  # corre lab_requests en la VM, devuelve su JSON

    def upload(self, local: Path, request_id: str) -> None: ...  # -> data/lab_requests/<request_id>.json


class GcloudRemote:
    """VM por `gcloud compute ssh/scp` (igual que lab.publish)."""

    def __init__(self, host: str = VM_HOST, flags: list[str] | None = None, root: str = VM_ROOT) -> None:
        self.host, self.flags, self.root = host, list(VM if flags is None else flags), root

    def _ssh(self, command: str) -> str:
        res = subprocess.run(["gcloud", "compute", "ssh", *self.flags, self.host, "--command", command],
                             capture_output=True, text=True, timeout=300, env=os.environ.copy())
        if res.returncode != 0 and not res.stdout.strip():
            raise RuntimeError(f"ssh fallo (rc={res.returncode}): {res.stderr.strip()[-500:]}")
        return res.stdout

    def cli(self, args: list[str]) -> dict[str, Any]:
        command = (f"cd {self.root}/backend && PYTHONPATH=src HEATSIGNAL_EVAL_DB={self.root}/data/heatsignal_eval.sqlite "
                   f".venv/bin/python -m heatsignal.evaluation.lab_requests {' '.join(shlex.quote(a) for a in args)}")
        out = self._ssh(command).strip().splitlines()
        if not out:
            raise RuntimeError("la CLI remota no devolvio nada")
        return json.loads(out[-1])  # la ultima linea es el JSON (gcloud puede imprimir avisos antes)

    def upload(self, local: Path, request_id: str) -> None:
        self._ssh(f"mkdir -p {self.root}/data/lab_requests")
        subprocess.run(["gcloud", "compute", "scp", *self.flags, str(local),
                        f"{self.host}:{self.root}/data/lab_requests/{request_id}.json"],
                       check=True, timeout=300, env=os.environ.copy())


def _default_evaluate(cfg: Any, registry: Any) -> dict[str, Any]:
    from .run import evaluate

    return evaluate(cfg, registry)


def _json_default(o: Any) -> Any:
    from .run import _json_default as jd

    return jd(o)


def symbols_dirname(symbols: list[str]) -> str:
    return "symbols-" + "-".join(sorted(s.upper() for s in symbols))


def run_strategy(strategy_id: str, symbols: list[str], *, home: Path, registry: Any,
                 evaluate_fn: Callable[[Any, Any], dict[str, Any]]) -> dict[str, Any]:
    """Una estrategia sobre los simbolos pedidos. Devuelve el bloque de resultados (ok o error, nunca lanza)."""
    from .run import RunConfig
    from .strategies import CATALOG

    if strategy_id not in CATALOG:
        return {"strategy_id": strategy_id, "status": "error", "error": "no existe en el catalogo de la PC"}
    spec, _ = CATALOG[strategy_id]
    if not spec.backtestable:
        return {"strategy_id": strategy_id, "status": "error", "error": f"no se puede backtestear: {spec.needs}"}
    try:
        report = evaluate_fn(RunConfig(strategy_id, list(symbols), n_perm=N_PERM), registry)
    except FileNotFoundError as exc:
        return {"strategy_id": strategy_id, "status": "error", "error": f"faltan datos: {exc}"}
    except Exception as exc:  # noqa: BLE001 - una estrategia rota no frena las demas
        log.exception("%s sobre %s fallo", strategy_id, symbols)
        return {"strategy_id": strategy_id, "status": "error", "error": f"{type(exc).__name__}: {exc}"[:1000]}
    out = home / "results" / strategy_id / symbols_dirname(symbols) / f"{datetime.now(timezone.utc):%Y%m%dT%H%M%S}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2, default=_json_default, ensure_ascii=False))
    report = json.loads(out.read_text())  # tipos JSON puros (numpy -> float) para el resumen
    o, b = report.get("oos", {}), report.get("baselines", {})
    summary = {
        "sharpe": o.get("sharpe"), "sharpe_ci95": o.get("sharpe_ci95"), "dsr": o.get("dsr"), "psr": o.get("psr"),
        "total_return": o.get("total_return"), "max_drawdown": o.get("max_drawdown"), "trades": o.get("trades"),
        "hit_rate": (o.get("hit_rate") or {}).get("hit_rate"), "breakeven_bps": o.get("breakeven_bps_per_side"),
        "basket_sharpe": b.get("basket_sharpe"), "random_p": (b.get("random_entries") or {}).get("p_value"),
        "pasa": (report.get("verdict") or {}).get("pasa"),
    }
    return {"strategy_id": strategy_id, "status": "ok", "file": str(out.relative_to(home)), "summary": summary,
            "detail": detail(report, [out])}


def process(req: dict[str, Any], *, remote: Remote, home: Path, registry: Any,
            evaluate_fn: Callable[[Any, Any], dict[str, Any]], worker: str = "pc") -> dict[str, Any]:
    """Procesa una solicitud ya tomada (running): corre, sube y marca done/error."""
    rid = req["request_id"]
    try:
        blocks = [run_strategy(sid, req["symbols"], home=home, registry=registry, evaluate_fn=evaluate_fn)
                  for sid in req["strategy_ids"]]
        payload = _nan_to_null({
            "request_id": rid, "symbols": req["symbols"], "strategy_ids": req["strategy_ids"], "note": req.get("note"),
            "generated_at": datetime.now(timezone.utc).isoformat(), "worker": worker, "n_perm": N_PERM,
            "holdout": "SELLADO (la cola nunca lo abre)", "strategies": blocks,
        })
        if not any(b["status"] == "ok" for b in blocks):
            reasons = "; ".join(f"{b['strategy_id']}: {b['error']}" for b in blocks)
            return remote.cli(["fail", rid, "--error", reasons or "sin estrategias", "--worker", worker])
        with tempfile.TemporaryDirectory() as tmp:
            local = Path(tmp) / f"{rid}.json"
            local.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
            remote.upload(local, rid)
        return remote.cli(["done", rid, "--worker", worker])
    except Exception as exc:  # noqa: BLE001 - se informa en la cola en vez de dejarla colgada en running
        log.exception("solicitud %s fallo", rid)
        return remote.cli(["fail", rid, "--error", f"{type(exc).__name__}: {exc}"[:1000], "--worker", worker])


def run_once(*, remote: Remote, home: Path, registry: Any,
             evaluate_fn: Callable[[Any, Any], dict[str, Any]] = _default_evaluate, worker: str = "pc",
             max_requests: int = 20) -> list[dict[str, Any]]:
    """Toma y procesa pendientes hasta vaciar la cola (o `max_requests`). Devuelve lo que respondio la VM."""
    done = []
    for _ in range(max_requests):
        req = remote.cli(["claim-next", "--worker", worker]).get("request")
        if req is None:
            break
        log.info("solicitud %s: %s sobre %s", req["request_id"], req["strategy_ids"], req["symbols"])
        done.append(process(req, remote=remote, home=home, registry=registry, evaluate_fn=evaluate_fn, worker=worker))
    return done


def main(argv: Iterable[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Procesa las solicitudes del panel al laboratorio.")
    p.add_argument("--once", action="store_true", help="una pasada y salir")
    p.add_argument("--interval", type=float, default=DEFAULT_INTERVAL_S)
    p.add_argument("--worker", default=os.environ.get("HEATSIGNAL_LAB_WORKER", "pc"))
    p.add_argument("--registry", default=str(Path(os.environ.get("HEATSIGNAL_LAB_HOME", Path.home() / "heatsignal-lab")) / "trials.sqlite"))
    args = p.parse_args(list(argv) if argv is not None else None)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    from .trials import TrialRegistry

    home = lab_dir().parent
    remote = GcloudRemote()
    while True:
        reg = TrialRegistry(args.registry)
        try:
            results = run_once(remote=remote, home=home, registry=reg, worker=args.worker)
        except Exception:  # noqa: BLE001 - VM inalcanzable: se reintenta en la proxima vuelta
            log.exception("no se pudo hablar con la VM")
            results = []
        finally:
            reg.close()
        for r in results:
            log.info("-> %s", r)
        if args.once:
            return 0
        time.sleep(args.interval)


if __name__ == "__main__":
    sys.exit(main())
