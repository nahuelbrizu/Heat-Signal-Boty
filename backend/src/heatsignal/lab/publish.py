"""
Resumen del laboratorio para el panel de la VM.

    python -m heatsignal.lab.publish            # arma ~/heatsignal-lab/lab_summary.json
    python -m heatsignal.lab.publish --upload   # y lo copia a la VM (data/lab_summary.json)

El laboratorio corre en la PC; el panel vive en la VM. Se publica solo un resumen chico (ultimo resultado
de cada estrategia: metricas OOS, baselines, veredicto, conteo de intentos), nunca los datos de mercado.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from .vision import lab_dir

VM = ["--zone", "asia-northeast1-a", "--project", "vocal-operand-271906"]
VM_HOST = "instance-20261016-191100"
VM_PATH = "~/heatsignal/data/lab_summary.json"


def summarize(results_dir: Path) -> dict[str, Any]:
    out: dict[str, Any] = {"strategies": []}
    for folder in sorted(p for p in results_dir.glob("*") if p.is_dir()):
        runs = sorted(folder.glob("*.json"))
        if not runs:
            continue
        r = json.loads(runs[-1].read_text())
        o, b = r.get("oos", {}), r.get("baselines", {})
        out["strategies"].append({
            "strategy_id": r.get("strategy_id"), "spec_hash": r.get("spec_hash"), "run": runs[-1].stem,
            "runs": len(runs), "symbols": r.get("symbols"), "oos_range": r.get("walk_forward", {}).get("oos"),
            "windows": r.get("walk_forward", {}).get("windows"), "n_combinations": r.get("n_combinations"),
            "sharpe": o.get("sharpe"), "sharpe_ci95": o.get("sharpe_ci95"), "psr": o.get("psr"), "dsr": o.get("dsr"),
            "total_return": o.get("total_return"), "max_drawdown": o.get("max_drawdown"), "trades": o.get("trades"),
            "hit_rate": (o.get("hit_rate") or {}).get("hit_rate"), "breakeven_bps": o.get("breakeven_bps_per_side"),
            "breakdown": o.get("breakdown"),
            "basket_sharpe": b.get("basket_sharpe"),
            "random_p": (b.get("random_entries") or {}).get("p_value"),
            "verdict": r.get("verdict"),
            "holdout": r.get("data", {}).get("holdout"), "holdout_start": r.get("data", {}).get("holdout_start"),
        })
    return out


def main(argv: Iterable[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Resumen del laboratorio para el panel.")
    p.add_argument("--upload", action="store_true")
    args = p.parse_args(list(argv) if argv is not None else None)
    home = lab_dir().parent
    summary = summarize(home / "results")
    from datetime import datetime, timezone

    summary["generated_at"] = datetime.now(timezone.utc).isoformat()
    out = home / "lab_summary.json"
    out.write_text(json.dumps(summary, indent=1, ensure_ascii=False))
    print(f"{len(summary['strategies'])} estrategias -> {out}")
    if args.upload:
        subprocess.run(["gcloud", "compute", "scp", *VM, str(out), f"{VM_HOST}:{VM_PATH}"], check=True,
                       env=os.environ.copy())
        print("subido a la VM")
    return 0


if __name__ == "__main__":
    sys.exit(main())
