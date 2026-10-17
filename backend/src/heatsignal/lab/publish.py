"""
Resumen y detalle del laboratorio para el panel de la VM.

    python -m heatsignal.lab.publish            # arma ~/heatsignal-lab/lab_summary.json + lab_detail_<id>.json
    python -m heatsignal.lab.publish --upload   # y los copia todos a la VM (~/heatsignal/data/)

El laboratorio corre en la PC; el panel vive en la VM. Se publica solo lo necesario para mirar resultados,
nunca los datos de mercado:
  - lab_summary.json            ultimo resultado de cada estrategia (metricas OOS, baselines, veredicto)
  - lab_detail_<strategy_id>.json  detalle de ese ultimo resultado: ventanas walk-forward elegidas, curva
                                costo -> Sharpe, break-even, descomposicion del PnL, baselines, checks del
                                veredicto y curvas de capital diarias OOS (estrategia, canasta, buy & hold).
Los resultados viejos (antes de `oos.equity_daily`) se publican igual, con `equity_daily: null` y una nota.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from .vision import lab_dir

VM = ["--zone", "asia-northeast1-a", "--project", "vocal-operand-271906"]
VM_HOST = "instance-20261016-191100"
VM_DIR = "~/heatsignal/data/"
VM_PATH = VM_DIR + "lab_summary.json"  # compatibilidad: donde lo busca el panel

# strategy_id del laboratorio -> nombre de archivo. El panel valida con la misma expresion (sin rutas).
SAFE_ID = re.compile(r"^[a-z0-9][a-z0-9._-]{0,99}$")
EXPECTED_COST_BPS = 7.0  # = lab.run.EXPECTED_COST_BPS (no se importa: lab.run arrastra pandas)


def _nan_to_null(obj: Any) -> Any:
    """JSON estricto: NaN/inf -> null (json.dumps escribiria NaN, que no es JSON valido)."""
    if isinstance(obj, float) and (obj != obj or obj in (float("inf"), float("-inf"))):
        return None
    if isinstance(obj, dict):
        return {k: _nan_to_null(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_nan_to_null(v) for v in obj]
    return obj


def _latest_runs(results_dir: Path) -> Iterable[tuple[Path, list[Path]]]:
    for folder in sorted(p for p in results_dir.glob("*") if p.is_dir()):
        runs = sorted(folder.glob("*.json"))
        if runs:
            yield folder, runs


def summarize(results_dir: Path) -> dict[str, Any]:
    out: dict[str, Any] = {"strategies": []}
    for _, runs in _latest_runs(results_dir):
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
            "has_detail": bool(r.get("strategy_id")) and SAFE_ID.match(str(r.get("strategy_id"))) is not None,
        })
    return out


def cost_curve_rows(curve: Any) -> list[dict[str, Any]] | None:
    """La curva costo -> Sharpe como lista de filas {cost_bps, sharpe, ann_return}.

    `lab.run` la guarda como DataFrame serializado (dict de columnas {"0": .., "1": ..}); tambien acepta
    una lista de filas ya armada.
    """
    if curve is None:
        return None
    if isinstance(curve, list):
        return curve
    if isinstance(curve, dict) and curve:
        cols = list(curve)
        keys = sorted(curve[cols[0]], key=lambda k: int(k) if str(k).isdigit() else str(k))
        return [{c: curve[c].get(k) for c in cols} for k in keys]
    return None


def detail(run: dict[str, Any], run_files: list[Path]) -> dict[str, Any]:
    """Detalle del ultimo resultado de una estrategia (lo que muestra /api/lab/{strategy_id})."""
    o, b, wf = run.get("oos", {}), run.get("baselines", {}), run.get("walk_forward", {})
    beq = b.get("equity_daily") or {}
    notes = []
    if o.get("equity_daily") is None:
        notes.append("este resultado es anterior a la curva diaria: volver a correr `python -m heatsignal.lab.run` "
                     "para tener oos.equity_daily")
    be = o.get("breakeven_bps_per_side")
    return {
        "strategy_id": run.get("strategy_id"),
        "spec_hash": run.get("spec_hash"),
        "run": run_files[-1].stem,
        "runs": [p.stem for p in run_files],
        "symbols": run.get("symbols"),
        "data": run.get("data"),
        "n_combinations": run.get("n_combinations"),
        "walk_forward": {
            "train_days": wf.get("train_days"), "test_days": wf.get("test_days"), "windows": wf.get("windows"),
            "oos": wf.get("oos"), "chosen": wf.get("chosen", []),
        },
        "oos": {k: o.get(k) for k in ("sharpe", "sharpe_ci95", "psr", "dsr", "sr0_annual", "min_trl_days", "days",
                                      "total_return", "max_drawdown", "trades", "hit_rate")},
        "costs": {
            "breakeven_bps_per_side": be,
            "expected_cost_bps_per_side": EXPECTED_COST_BPS,
            "breakeven_vs_expected": None if be is None else be / EXPECTED_COST_BPS,
            "cost_curve": cost_curve_rows(o.get("cost_curve")),
        },
        "breakdown": o.get("breakdown"),
        "baselines": {k: b.get(k) for k in ("buy_and_hold_sharpe", "basket_sharpe", "random_entries")},
        "verdict": run.get("verdict"),
        "equity_daily": None if o.get("equity_daily") is None else {
            "strategy": o.get("equity_daily"),
            "basket": beq.get("basket"),
            "buy_and_hold_mean": beq.get("buy_and_hold_mean"),
            "buy_and_hold": beq.get("buy_and_hold"),
        },
        "notes": notes,
    }


def write_all(home: Path) -> list[Path]:
    """Escribe lab_summary.json y un lab_detail_<id>.json por estrategia en `home`. Devuelve los archivos."""
    from datetime import datetime, timezone

    generated_at = datetime.now(timezone.utc).isoformat()
    summary = summarize(home / "results")
    summary["generated_at"] = generated_at
    out = home / "lab_summary.json"
    out.write_text(json.dumps(_nan_to_null(summary), indent=1, ensure_ascii=False))
    files = [out]
    for _, runs in _latest_runs(home / "results"):
        run = json.loads(runs[-1].read_text())
        sid = str(run.get("strategy_id") or "")
        if not SAFE_ID.match(sid):
            print(f"strategy_id invalido para archivo, se omite el detalle: {sid!r}")
            continue
        d = detail(run, runs)
        d["generated_at"] = generated_at
        path = home / f"lab_detail_{sid}.json"
        path.write_text(json.dumps(_nan_to_null(d), ensure_ascii=False, separators=(",", ":")))
        files.append(path)
    return files


def main(argv: Iterable[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Resumen y detalle del laboratorio para el panel.")
    p.add_argument("--upload", action="store_true")
    args = p.parse_args(list(argv) if argv is not None else None)
    home = lab_dir().parent
    files = write_all(home)
    print(f"{len(files) - 1} detalles + resumen -> {home}")
    if args.upload:
        # un solo scp con todos los archivos al directorio de datos de la VM (donde los lee el panel)
        subprocess.run(["gcloud", "compute", "scp", *VM, *map(str, files), f"{VM_HOST}:{VM_DIR}"], check=True,
                       env=os.environ.copy())
        print(f"subidos {len(files)} archivos a la VM ({VM_DIR})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
