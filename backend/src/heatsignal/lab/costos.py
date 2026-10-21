"""
Estres de costos (DIAGNOSTICO, NO SELECCION; consulta al trader 2026-10-20, decisions.md):

    python -m heatsignal.lab.costos l6-ruptura-n20-h3-long-v1 [...]
    python -m heatsignal.lab.costos --rupturas          # las 24 rupturas long y short del lote 6

Sharpe del DESARROLLO (sin holdout) de la base congelada (`diagnostics.base_params`) con los costos del lab
(5 bps de comision + 2 bps de slippage por lado) y con slippage realista de 7 bps en TRX y XLM (el resto igual;
`Costs.slippage_bps` por simbolo). Mismo codigo que el lab (`run.simulate_combo`). No suma intentos al K del
programa, no se registra en `lab_trials` ni cambia parametros. Escribe results/<id>/diagnostics_costos_<ts>.json
(clave `costos`; `lab.publish` lo muestra en el detalle como "costos realistas TRX/XLM 7 bps").
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from datetime import datetime, timezone
from typing import Any

import pandas as pd

from . import stats
from .diagnostics import (DIAG_PREFIX, _clean, _json_default, _results_dir, assert_no_holdout, base_params,
                          latest_result)
from .parada import LONG, SHORT
from .run import development, load_market, simulate_combo
from .sim import Costs

log = logging.getLogger("heatsignal.lab.costos")
STRESS_SLIPPAGE = {"TRXUSDT": 7.0, "XLMUSDT": 7.0}
LABEL = "costos realistas TRX/XLM 7 bps"


def _fin(x: float) -> float | None:
    return None if x is None or not pd.notna(x) else float(x)


def stress(spec, rule, result: dict[str, Any], data: dict[str, Any]) -> dict[str, Any]:
    holdout_start = pd.Timestamp(result["data"]["holdout_start"])
    dev = development(data, holdout_start)
    assert_no_holdout(dev, holdout_start)
    base = base_params(spec, result)
    normal, stressed = Costs(), Costs(slippage_bps=dict(STRESS_SLIPPAGE))
    _, d0 = simulate_combo(rule, dev, base, normal)
    _, d1 = simulate_combo(rule, dev, base, stressed)
    s0, s1 = stats.sharpe_ratio(d0.daily_returns), stats.sharpe_ratio(d1.daily_returns)
    first = min(md.klines.index[0] for md in dev.values())
    return {
        "label": LABEL,
        "base_params": {k: v for k, v in base.items() if k in spec.grid},
        "costos_lab": {"fee_bps": normal.fee_bps, "slippage_bps": normal.default_slippage_bps},
        "costos_estres": {"fee_bps": stressed.fee_bps, "slippage_bps_default": stressed.default_slippage_bps,
                          "slippage_bps": dict(STRESS_SLIPPAGE)},
        "sharpe_dev": _fin(s0), "sharpe_dev_estres": _fin(s1),
        "delta": None if _fin(s0) is None or _fin(s1) is None else float(s1 - s0),
        "retorno_total_dev": float(d0.daily_returns.sum()), "retorno_total_dev_estres": float(d1.daily_returns.sum()),
        "window": [str(first), str(holdout_start)], "holdout": "NO USADO",
        "adds_trials": False, "changes_frozen_params": False,
    }


def main(argv: list[str] | None = None) -> int:
    from .strategies import CATALOG

    p = argparse.ArgumentParser(description="Estres de costos TRX/XLM 7 bps (diagnostico; no suma intentos)")
    p.add_argument("ids", nargs="*")
    p.add_argument("--rupturas", action="store_true", help="las 12 rupturas long y las 12 short del lote 6")
    a = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    ids = (LONG + SHORT if a.rupturas else []) + list(a.ids)
    if not ids:
        p.error("pasar ids o --rupturas")
    results_dir = _results_dir()
    rc = 0
    for sid in ids:
        found = latest_result(results_dir, sid)
        if sid not in CATALOG or not found:
            print(f"{sid}: sin spec o sin resultado")
            rc = 1
            continue
        path, result = found
        spec, rule = CATALOG[sid]
        if result.get("spec_hash") and result["spec_hash"] != spec.spec_hash():
            print(f"{sid}: el spec cambio desde el resultado; correr lab.run antes")
            rc = 1
            continue
        t0 = time.monotonic()
        data = load_market(result.get("symbols") or [], spec.needs, None, result["data"]["holdout_start"])
        c = stress(spec, rule, result, data)
        del data
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
        rep = {"strategy_id": sid, "spec_hash": spec.spec_hash(), "run": path.stem, "diagnostics_id": ts,
               "generated_at": datetime.now(timezone.utc).isoformat(), "symbols": result.get("symbols"),
               "holdout": "NO USADO", "costos": c, "seconds_total": round(time.monotonic() - t0, 1),
               "nota": "Diagnostico, no seleccion: no suma intentos al K del programa ni cambia parametros congelados."}
        out = results_dir / sid / f"{DIAG_PREFIX}costos_{ts}.json"
        out.write_text(json.dumps(_clean(rep), indent=1, default=_json_default, ensure_ascii=False))
        print(f"{sid}: Sharpe dev {c['sharpe_dev']:.3f} -> estres {c['sharpe_dev_estres']:.3f} -> {out.name}")
    return rc


if __name__ == "__main__":
    sys.exit(main())
