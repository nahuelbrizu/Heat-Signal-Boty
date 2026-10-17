"""
Orquestador del laboratorio: walk-forward + holdout sellado + metricas contra baselines y azar.

    python -m heatsignal.lab.run donchian-ensemble-v1 [--symbols ...] [--end 2026-10-15] [--perm 100]

Procedimiento (plan-laboratorio.md, research/ml4t-metodologia.md):
  1. Datos [start, end). Los ultimos `holdout_months` quedan SELLADOS: este comando no los usa.
  2. Desarrollo = [start, holdout_start). Cada combinacion de la grilla congelada del spec se simula sobre
     todo el desarrollo (las senales son causales: computarlas de una vez equivale a computarlas en vivo;
     lo garantizan los tests de look-ahead de cada regla).
  3. Walk-forward: ventanas de `train_days` / `test_days` que avanzan de a `test_days`. En cada ventana se
     elige la combinacion con mejor Sharpe de TRAIN y se toman sus retornos de TEST. Los tests concatenados
     son el resultado fuera de muestra (OOS).
  4. Cada (combinacion, ventana) se registra en el registro de intentos (append-only). K = combinaciones.
  5. Metricas OOS: Sharpe + IC bootstrap, PSR, DSR (con los Sharpe de todos los intentos), MinTRL, max DD,
     hit rate, descomposicion del PnL, curva de costos y break-even; contra buy & hold, canasta y entradas
     al azar (p-valor empirico).
  6. Veredicto con los criterios del plan. El holdout se abre aparte (`--open-holdout`), una sola vez.
"""

from __future__ import annotations

import argparse
import itertools
import json
import logging
import math
import os
import sys
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from . import stats
from .baselines import buy_and_hold, equal_weight_basket, random_entries
from .sim import Costs, simulate
from .store import load_funding, load_klines, load_metrics
from .strategies import CATALOG, SEVEN, MarketData
from .trials import TrialRegistry
from .vision import lab_dir

log = logging.getLogger("heatsignal.lab.run")

# Criterios para pasar a la etapa 3 (plan-laboratorio.md). Fijados ANTES de ver resultados.
CRITERIA = {"dsr_min": 0.95, "breakeven_vs_cost_min": 2.0, "p_random_max": 0.05}
EXPECTED_COST_BPS = 7.0  # 5 comision + 2 slippage por lado


@dataclass
class RunConfig:
    strategy_id: str
    symbols: list[str] = field(default_factory=lambda: list(SEVEN))
    start: pd.Timestamp | None = None
    end: pd.Timestamp | None = None
    holdout_months: int = 3
    train_days: int = 180
    test_days: int = 30
    n_perm: int = 100
    seed: int = 7
    costs: Costs = field(default_factory=Costs)


def grid_combos(spec) -> list[dict[str, Any]]:
    keys = sorted(spec.grid)
    if not keys:
        return [dict(spec.params)]
    return [{**spec.params, **dict(zip(keys, vals))} for vals in itertools.product(*(spec.grid[k] for k in keys))]


def load_market(symbols: Iterable[str], needs: tuple[str, ...], start=None, end=None, root=None) -> dict[str, MarketData]:
    out = {}
    for sym in symbols:
        k = load_klines(sym, start, end, root)
        f = load_funding(sym, start, end, root)  # siempre: el funding es un costo de cualquier posicion
        m = load_metrics(sym, start, end, root) if "metrics" in needs else None
        out[sym] = MarketData(k, f, m)
    return out


def walk_forward_windows(start: pd.Timestamp, end: pd.Timestamp, train_days: int, test_days: int):
    t = start
    while t + pd.Timedelta(days=train_days + test_days) <= end:
        tr = (t, t + pd.Timedelta(days=train_days))
        te = (tr[1], tr[1] + pd.Timedelta(days=test_days))
        yield tr, te
        t = t + pd.Timedelta(days=test_days)


def _slice(r: pd.Series, a: pd.Timestamp, b: pd.Timestamp) -> pd.Series:
    return r[(r.index >= a) & (r.index < b)]


def _dedupe(s: pd.Series) -> pd.Series:
    s = s.sort_index()
    return s[~s.index.duplicated(keep="last")]


def _prices(data: dict[str, MarketData]) -> dict[str, pd.DataFrame]:
    return {s: md.klines[["open", "high", "low", "close"]] for s, md in data.items()}


def _funding(data: dict[str, MarketData]) -> dict[str, pd.Series]:
    return {s: md.funding for s, md in data.items() if md.funding is not None}


def evaluate(cfg: RunConfig, registry: TrialRegistry | None = None, data: dict[str, MarketData] | None = None) -> dict[str, Any]:
    spec, rule = CATALOG[cfg.strategy_id]
    if not spec.backtestable:
        raise ValueError(f"{cfg.strategy_id} no se puede backtestear (solo forward): {spec.needs}")
    data = data or load_market(cfg.symbols, spec.needs, cfg.start, cfg.end)
    first = max(md.klines.index[0] for md in data.values())
    last = min(md.klines.index[-1] for md in data.values()) + pd.Timedelta(minutes=1)
    holdout_start = (last - pd.DateOffset(months=cfg.holdout_months)).floor("D")
    dev = {s: MarketData(md.klines[md.klines.index < holdout_start],
                         None if md.funding is None else md.funding[md.funding.index < holdout_start],
                         None if md.metrics is None else md.metrics[md.metrics.index < holdout_start])
           for s, md in data.items()}
    prices, funding = _prices(dev), _funding(dev)

    combos = grid_combos(spec)
    sims = []
    for params in combos:
        sig = rule(dev, params)
        res = simulate(prices, sig, funding, cfg.costs)
        sims.append((params, sig, res))
        log.info("%s %s: Sharpe dev %.2f", cfg.strategy_id, {k: params[k] for k in spec.grid}, stats.sharpe_ratio(res.daily_returns))

    windows = list(walk_forward_windows(first.floor("D"), holdout_start, cfg.train_days, cfg.test_days))
    if not windows:
        raise ValueError("no alcanza la historia para una ventana de walk-forward")
    oos_sig: dict[str, list[pd.Series]] = {s: [] for s in dev}
    chosen = []
    for (tr0, tr1), (te0, te1) in windows:
        scores = [stats.sharpe_ratio(_slice(res.daily_returns, tr0, tr1)) for _, _, res in sims]
        scores = [s if math.isfinite(s) else -np.inf for s in scores]
        best = int(np.argmax(scores))
        params, sig, res = sims[best]
        for s in dev:  # senal elegida en esta ventana, con su estado vigente al arrancar el test
            v = sig.get(s, pd.Series(dtype=float))
            prev = v[v.index < te0]
            head = pd.Series([prev.iloc[-1] if len(prev) else 0.0], index=pd.DatetimeIndex([te0]))
            oos_sig[s].append(pd.concat([head, v[(v.index >= te0) & (v.index < te1)]]))
        chosen.append({"train": [str(tr0.date()), str(tr1.date())], "test": [str(te0.date()), str(te1.date())],
                       "params": {k: params[k] for k in spec.grid}, "train_sharpe": scores[best]})
        if registry is not None:
            for i, (p, _, r) in enumerate(sims):
                registry.register(cfg.strategy_id, spec.spec_hash(), p, "train", cfg.symbols,
                                  {"sharpe": scores[i]}, tr0, tr1)
            registry.register(cfg.strategy_id, spec.spec_hash(), params, "test", cfg.symbols,
                              {"sharpe": stats.sharpe_ratio(_slice(res.daily_returns, te0, te1))}, te0, te1)

    oos_start, oos_end = windows[0][1][0], windows[-1][1][1]
    oos_prices = {s: p[(p.index >= oos_start) & (p.index < oos_end)] for s, p in prices.items()}
    oos_funding = {s: f[(f.index >= oos_start) & (f.index < oos_end)] for s, f in funding.items()}
    stitched = {s: _dedupe(pd.concat(parts)) for s, parts in oos_sig.items() if parts}
    oos_res = simulate(oos_prices, stitched, oos_funding, cfg.costs)
    oos, oos_g, oos_t = oos_res.daily_returns, oos_res.daily_gross_returns, oos_res.daily_turnover

    # Sharpe de todos los intentos (combinaciones) sobre el desarrollo completo -> DSR
    trial_sharpes = [stats.sharpe_ratio(res.daily_returns) for _, _, res in sims]
    if len(trial_sharpes) == 1:
        trial_sharpes = trial_sharpes + trial_sharpes  # var=0 -> SR0=0: el DSR coincide con el PSR
    if len(oos_res.trades) == 0 or float(np.nanstd(oos)) == 0.0:
        # la regla no opero en el OOS: no hay nada que medir (no es un error, es un resultado)
        dsr = {"dsr": None, "psr": None, "sr0_annual": None, "min_trl_obs": None, "n_obs": int(len(oos))}
        ci = {"lo": None, "hi": None}
    else:
        dsr = stats.dsr_from_returns(oos, [x for x in trial_sharpes if math.isfinite(x)] or [0.0, 0.0])
        ci = stats.sharpe_ci(oos, seed=cfg.seed)

    trades_oos = oos_res.trades
    hr = stats.hit_rate(trades_oos["pnl_net"]) if len(trades_oos) else {}

    # baselines en la misma ventana OOS
    bh = buy_and_hold(oos_prices, oos_funding, cfg.costs)
    basket = equal_weight_basket(oos_prices, oos_funding, cfg.costs)

    # nulo de entradas al azar: mismas operaciones por simbolo, en posiciones al azar dentro del OOS
    null = []
    if cfg.n_perm > 0:
        sig_oos = stitched
        gens = {s: random_entries(v, cfg.n_perm, cfg.seed + i, start=oos_start, end=oos_end)
                for i, (s, v) in enumerate(sig_oos.items()) if len(v)}
        for _ in range(cfg.n_perm):
            sig = {s: next(g) for s, g in gens.items()}
            if not sig:
                break
            null.append(stats.sharpe_ratio(simulate(oos_prices, sig, oos_funding, cfg.costs).daily_returns))
    sharpe_oos = stats.sharpe_ratio(oos) if float(np.nanstd(oos)) > 0 else float("nan")
    p_random = stats_pvalue(sharpe_oos, null)

    breakeven = stats.breakeven_cost_bps(oos_g, oos_t) if len(oos_res.trades) else None
    report = {
        "strategy_id": cfg.strategy_id,
        "spec_hash": spec.spec_hash(),
        "symbols": cfg.symbols,
        "data": {"first": str(first), "last": str(last), "holdout_start": str(holdout_start), "holdout": "SELLADO"},
        "walk_forward": {"train_days": cfg.train_days, "test_days": cfg.test_days, "windows": len(windows),
                         "oos": [str(oos_start.date()), str(oos_end.date())], "chosen": chosen},
        "n_combinations": len(combos),
        "oos": {
            "sharpe": sharpe_oos, "sharpe_ci95": [ci.get("lo"), ci.get("hi")] if isinstance(ci, dict) else ci,
            "psr": dsr["psr"], "dsr": dsr["dsr"], "sr0_annual": dsr["sr0_annual"],
            "min_trl_days": dsr["min_trl_obs"], "days": dsr["n_obs"],
            "total_return": float(oos.sum()), "max_drawdown": stats.max_drawdown(oos),
            "breakeven_bps_per_side": breakeven,
            "cost_curve": stats.cost_curve(oos_g, oos_t) if len(oos_res.trades) else None,
            "trades": int(len(trades_oos)), "hit_rate": hr,
            "breakdown": oos_res.breakdown,
        },
        "baselines": {
            "buy_and_hold_sharpe": {s: stats.sharpe_ratio(r.daily_returns) for s, r in bh.items()},
            "basket_sharpe": stats.sharpe_ratio(basket.daily_returns),
            "random_entries": {"n": len(null), "median_sharpe": float(np.median(null)) if null else None,
                               "p_value": p_random},
        },
    }
    report["verdict"] = verdict(report)
    return report


def stats_pvalue(observed: float, null: list[float]) -> float | None:
    if not null:
        return None
    from .baselines import empirical_pvalue

    return empirical_pvalue(observed, np.asarray(null))


def verdict(r: dict[str, Any]) -> dict[str, Any]:
    o, b = r["oos"], r["baselines"]
    bh = [v for v in b["buy_and_hold_sharpe"].values() if v is not None and math.isfinite(v)]
    sh = o["sharpe"] if o["sharpe"] is not None and math.isfinite(o["sharpe"]) else -np.inf
    checks = {
        "opera": o["trades"] > 0,
        "dsr": (o["dsr"] is not None and o["dsr"] >= CRITERIA["dsr_min"]),
        "breakeven": (o["breakeven_bps_per_side"] is not None
                      and o["breakeven_bps_per_side"] >= CRITERIA["breakeven_vs_cost_min"] * EXPECTED_COST_BPS),
        "le_gana_a_la_canasta": sh > (b["basket_sharpe"] if math.isfinite(b["basket_sharpe"]) else -np.inf),
        "le_gana_al_buy_and_hold_medio": sh > (float(np.mean(bh)) if bh else -np.inf),
        "le_gana_al_azar": (b["random_entries"]["p_value"] is not None
                            and b["random_entries"]["p_value"] <= CRITERIA["p_random_max"]),
    }
    return {"pasa": all(checks.values()), "checks": checks, "criterios": CRITERIA}


def _json_default(o):
    if isinstance(o, (np.floating, np.integer)):
        return o.item()
    if isinstance(o, (pd.Timestamp, pd.Timedelta)):
        return str(o)
    if isinstance(o, (pd.Series, pd.DataFrame)):
        return o.to_dict()
    return str(o)


def main(argv: Iterable[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Walk-forward de una estrategia del catalogo (holdout sellado).")
    p.add_argument("strategy_id", choices=sorted(CATALOG))
    p.add_argument("--symbols", default=",".join(SEVEN))
    p.add_argument("--start", default=None)
    p.add_argument("--end", default=None)
    p.add_argument("--perm", type=int, default=100)
    p.add_argument("--registry", default=str(Path(os.environ.get("HEATSIGNAL_LAB_HOME", Path.home() / "heatsignal-lab")) / "trials.sqlite"))
    args = p.parse_args(list(argv) if argv is not None else None)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    cfg = RunConfig(args.strategy_id, [s.strip().upper() for s in args.symbols.split(",") if s.strip()],
                    pd.Timestamp(args.start, tz="UTC") if args.start else None,
                    pd.Timestamp(args.end, tz="UTC") if args.end else None, n_perm=args.perm)
    reg = TrialRegistry(args.registry)
    try:
        report = evaluate(cfg, reg)
    finally:
        reg.close()
    out = lab_dir().parent / "results" / args.strategy_id / f"{pd.Timestamp.now(tz='UTC'):%Y%m%dT%H%M%S}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2, default=_json_default, ensure_ascii=False))
    o = report["oos"]
    print(f"{args.strategy_id}: Sharpe OOS {o['sharpe']} | DSR {o['dsr']} | break-even {o['breakeven_bps_per_side']} bps "
          f"| trades {o['trades']} | pasa: {report['verdict']['pasa']}\n-> {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
