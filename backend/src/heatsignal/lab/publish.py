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

Diagnosticos (`lab.diagnostics`, results/<id>/diagnostics_<ts>.json): el resumen y el detalle llevan un bloque
`diagnostics` con el ultimo PBO (CSCV) y la ultima robustez por vecinos (meseta, caida maxima, Sharpe del desarrollo
con costos x3). Son diagnostico: no entran al conteo K del programa (`program` solo lee `lab_trials`).
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


DIAG_PREFIX = "diagnostics_"  # = lab.diagnostics.DIAG_PREFIX (no se importa: arrastra pandas)


def _latest_runs(results_dir: Path) -> Iterable[tuple[Path, list[Path]]]:
    for folder in sorted(p for p in results_dir.glob("*") if p.is_dir() and not p.name.startswith("_")):
        runs = sorted(p for p in folder.glob("*.json") if not p.name.startswith(DIAG_PREFIX))
        if runs:
            yield folder, runs


def _latest_diagnostics(folder: Path) -> dict[str, tuple[dict[str, Any], dict[str, Any]]]:
    """Ultimo diagnostico de cada tipo: {"pbo": (reporte, bloque), "robustez": (reporte, bloque)}."""
    out: dict[str, tuple[dict[str, Any], dict[str, Any]]] = {}
    for f in sorted(folder.glob(DIAG_PREFIX + "*.json"), reverse=True):
        try:
            rep = json.loads(f.read_text())
        except (OSError, ValueError):
            continue
        for kind in ("pbo", "robustez"):
            if kind not in out and isinstance(rep.get(kind), dict):
                out[kind] = (rep, rep[kind])
        if len(out) == 2:
            break
    return out


def diagnostics_block(folder: Path, current_run: str | None, full: bool = False) -> dict[str, Any] | None:
    """Bloque `diagnostics` del resumen (full=False) o del detalle (full=True). None si nunca se corrio.
    `stale`: algun diagnostico se hizo sobre un resultado anterior al publicado."""
    found = _latest_diagnostics(folder)
    if not found:
        return None
    pb = found.get("pbo", ({}, {}))
    rb = found.get("robustez", ({}, {}))
    pr, p = pb
    rr, r = rb
    runs = {x.get("run") for x in (pr, rr) if x}
    out: dict[str, Any] = {
        "pbo": p.get("pbo"), "pbo_skipped": p.get("skipped"), "pbo_partitions": p.get("n_partitions"),
        "pbo_combinations": p.get("n_combinations"), "pbo_slope": (p.get("degradation") or {}).get("slope"),
        "prob_oos_loss": p.get("prob_oos_loss"), "pbo_generated_at": pr.get("generated_at"), "pbo_run": pr.get("run"),
        "meseta": r.get("meseta"), "meseta_x3": r.get("meseta_x3"), "max_drop": r.get("max_drop"),
        "max_drop_rel": r.get("max_drop_rel"), "n_neighbors": r.get("n_neighbors"),
        "sharpe_dev": (r.get("base") or {}).get("sharpe"), "sharpe_dev_x3": (r.get("base") or {}).get("sharpe_x3"),
        "cost_mult": r.get("cost_mult"), "robustez_generated_at": rr.get("generated_at"), "robustez_run": rr.get("run"),
        "stale": bool(current_run) and any(x and x != current_run for x in runs),
        "adds_trials": False,
    }
    if full:
        out["pbo_detail"] = p or None
        out["robustez_detail"] = r or None
        out["development"] = (rr or pr).get("development")
        out["holdout"] = (rr or pr).get("holdout")
    return out


def _spec_info(strategy_id: str | None) -> dict[str, Any]:
    """Descripcion y ficha de la estrategia (del catalogo congelado), para mostrar junto al resultado."""
    from .strategies import CATALOG

    if strategy_id not in CATALOG:
        return {"description": None}
    spec, _ = CATALOG[strategy_id]
    return {"description": spec.description, "family": spec.family, "timeframe": spec.timeframe,
            "direction": spec.direction, "horizon": spec.horizon, "params": spec.params, "grid": spec.grid,
            "source": spec.source}


def summarize(results_dir: Path) -> dict[str, Any]:
    out: dict[str, Any] = {"strategies": []}
    for _, runs in _latest_runs(results_dir):
        r = json.loads(runs[-1].read_text())
        o, b = r.get("oos", {}), r.get("baselines", {})
        hold = _holdout(runs[-1].parent)
        out["strategies"].append({
            "strategy_id": r.get("strategy_id"), "spec_hash": r.get("spec_hash"), "run": runs[-1].stem,
            **_spec_info(r.get("strategy_id")),
            "runs": len(runs), "symbols": r.get("symbols"), "oos_range": r.get("walk_forward", {}).get("oos"),
            "windows": r.get("walk_forward", {}).get("windows"), "n_combinations": r.get("n_combinations"),
            "oos_days": o.get("days"), "sharpe": o.get("sharpe"), "sharpe_ci95": o.get("sharpe_ci95"), "psr": o.get("psr"), "dsr": o.get("dsr"),
            "total_return": o.get("total_return"), "max_drawdown": o.get("max_drawdown"), "trades": o.get("trades"),
            "hit_rate": (o.get("hit_rate") or {}).get("hit_rate"), "breakeven_bps": o.get("breakeven_bps_per_side"),
            "breakdown": o.get("breakdown"),
            "basket_sharpe": b.get("basket_sharpe"),
            "random_p": (b.get("random_entries") or {}).get("p_value"),
            "verdict": r.get("verdict"),
            "holdout": ("ABIERTO" if hold else r.get("data", {}).get("holdout")),
            "holdout_result": _holdout_result(hold), "holdout_start": r.get("data", {}).get("holdout_start"),
            "has_detail": bool(r.get("strategy_id")) and SAFE_ID.match(str(r.get("strategy_id"))) is not None,
            "diagnostics": diagnostics_block(runs[-1].parent, runs[-1].stem),
        })
    out["benchmark_ta"] = benchmark_ta(results_dir / "_benchmark_ta")
    out["program"] = program(results_dir.parent / "trials.sqlite", out)
    return out


def benchmark_ta(folder: Path) -> dict[str, Any] | None:
    """Benchmark de estrategias de TA de Freqtrade (ft_benchmark): la corrida de rango mas largo."""
    best = None
    for f in folder.glob("summary_*.json"):
        r = json.loads(f.read_text())
        res = r.get("results") or []
        r["results"] = [{"strategy": k, **v} for k, v in res.items()] if isinstance(res, dict) else res
        span = max((x.get("days") or 0 for x in r["results"]), default=0)
        if best is None or span > best[0]:
            best = (span, r)
    if best is None:
        return None
    r = best[1]
    return {"start": r.get("start"), "end": r.get("end"), "source": r.get("source"), "settings": r.get("settings"),
            "notes": r.get("notes"),
            "results": [{k: x.get(k) for k in ("strategy", "run_as", "timeframe", "ok", "error", "compat", "trades", "trades_long",
                                                "trades_short", "return_total", "sharpe_daily", "max_dd", "win_rate",
                                                "funding_total", "days", "leverage_max")} for x in r.get("results") or []]}


def program(trials_db: Path, summary: dict[str, Any]) -> dict[str, Any] | None:
    """Liston del programa entero: K = combinaciones registradas en trials + estrategias del benchmark; SR0 =
    E[max] de K Sharpe ruido con la varianza de los Sharpe OOS observados (lab + benchmark). Agrega a cada
    estrategia `dsr_program`: su DSR contra ese liston (el DSR de la tabla solo corrige por sus propias combinaciones).

    K se cuenta SOLO de `lab_trials`. Los diagnosticos (PBO y vecinos de robustez, `lab.diagnostics`) viven en la
    tabla aparte `lab_diagnostics` y NO son intentos: no suman a K ni sus Sharpe entran a la varianza."""
    import math
    import sqlite3

    from .stats import deflated_sharpe_ratio, expected_max_sharpe

    if not trials_db.exists():
        return None
    con = sqlite3.connect(f"file:{trials_db}?mode=ro", uri=True)
    try:
        k_lab = con.execute("SELECT COUNT(DISTINCT strategy_id || '|' || params) FROM lab_trials "
                            "WHERE split != 'holdout'").fetchone()[0]
    finally:
        con.close()
    bench = [x for x in ((summary.get("benchmark_ta") or {}).get("results") or []) if x.get("ok")]
    srs = [s["sharpe"] for s in summary["strategies"] if isinstance(s.get("sharpe"), (int, float))]
    srs += [x["sharpe_daily"] for x in bench if isinstance(x.get("sharpe_daily"), (int, float))]
    k = k_lab + len(bench)
    if len(srs) < 2:
        return {"k": k, "k_lab": k_lab, "k_benchmark": len(bench), "sr0_annual": None}
    mean = sum(srs) / len(srs)
    var_d = sum((x - mean) ** 2 for x in srs) / (len(srs) - 1) / 365
    for s in summary["strategies"]:
        sr, n = s.get("sharpe"), s.get("oos_days")
        s["dsr_program"] = (deflated_sharpe_ratio(sr / math.sqrt(365), int(n), k, var_d)
                            if isinstance(sr, (int, float)) and n else None)
    return {"k": k, "k_lab": k_lab, "k_benchmark": len(bench), "n_sharpes": len(srs),
            "sr0_annual": expected_max_sharpe(k, var_d) * math.sqrt(365)}


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
        "diagnostics": diagnostics_block(run_files[-1].parent, run_files[-1].stem, full=True),
        "equity_daily": None if o.get("equity_daily") is None else {
            "strategy": o.get("equity_daily"),
            "basket": beq.get("basket"),
            "buy_and_hold_mean": beq.get("buy_and_hold_mean"),
            "buy_and_hold": beq.get("buy_and_hold"),
        },
        "notes": notes,
    }


def _holdout_result(hold: dict[str, Any] | None) -> dict[str, Any] | None:
    """Resumen del holdout para lab_summary.json (lo muestran el laboratorio y el bot de paper)."""
    if not hold:
        return None
    b = hold.get("baselines") or {}
    v = hold.get("verdict") or {}
    return {"sharpe": hold.get("sharpe"), "total_return": hold.get("total_return"), "trades": hold.get("trades"),
            "max_drawdown": hold.get("max_drawdown"), "window": hold.get("window"), "days": hold.get("days"),
            "basket_sharpe": b.get("basket_sharpe"), "basket_return": b.get("basket_return"),
            "random_p": (b.get("random_entries") or {}).get("p_value"),
            "confirma": v.get("confirma"), "checks": v.get("checks"), "criterios": v.get("criterios")}


def _holdout(strategy_dir: Path) -> dict[str, Any] | None:
    """Resultado de la apertura del holdout (results/<id>/holdout/*.json), si se abrio. Se abre una sola vez."""
    files = sorted((strategy_dir / "holdout").glob("*.json"))
    if not files:
        return None
    h = json.loads(files[-1].read_text())
    return {"window": h.get("window"), "days": h.get("days"), "selected_params": h.get("selected_params"),
            **(h.get("holdout") or {}), "baselines": h.get("baselines"), "verdict": h.get("verdict")}


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
        d["holdout"] = _holdout(runs[-1].parent)
        d["spec"] = _spec_info(sid)
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
