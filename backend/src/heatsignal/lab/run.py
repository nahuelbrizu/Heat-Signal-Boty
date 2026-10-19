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
     al azar (p-valor empirico). El reporte trae ademas las curvas de capital diarias OOS (`oos.equity_daily`
     y `baselines.equity_daily`: canasta, buy & hold medio y por simbolo) solo para graficar.
  6. Veredicto con los criterios del plan. El holdout se abre aparte (`--open-holdout`), una sola vez.
     Con una sola combinacion el "DSR" del reporte es el PSR (`oos.dsr_label`); el veredicto del programa con la
     regla del 2026-10-19 lo arma `lab.publish` (`veredicto_programa`) con `lab.program`, sin reescribir este JSON.
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
from .baselines import buy_and_hold, equal_weight_basket, random_entries, random_shift_joint
from .exits import EXITS
from .sim import Costs, simulate
from .store import load_funding, load_klines, load_metrics
from .strategies import CATALOG, SEVEN, MarketData
from .trials import HoldoutAlreadyOpenedError, TrialRegistry
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


def null_kind(spec) -> str:
    """Nulo de "entradas al azar": conjunto para las neutrales (varias patas que se cubren entre si), por simbolo
    para el resto. Las neutrales corridas antes del 2026-10-18 (carry-xs-funding, funding-crowding-xs) usaron el
    nulo por simbolo; queda registrado en cada reporte (`baselines.random_entries.kind`)."""
    return "joint_shift" if spec.direction == "neutral" else "per_symbol"


def _null_signals(spec, stitched: dict[str, pd.Series], cfg, start, end) -> Iterable[dict[str, pd.Series]]:
    if cfg.n_perm <= 0:
        return
    if null_kind(spec) == "joint_shift":
        yield from random_shift_joint(stitched, cfg.n_perm, cfg.seed, start=start, end=end)
        return
    gens = {s: random_entries(v, cfg.n_perm, cfg.seed + i, start=start, end=end)
            for i, (s, v) in enumerate(stitched.items()) if len(v)}
    if not gens:
        return
    for _ in range(cfg.n_perm):
        yield {s: next(g) for s, g in gens.items()}


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


@dataclass
class _Daily:
    daily_returns: pd.Series
    daily_gross_returns: pd.Series
    daily_turnover: pd.Series


def _dedupe(s: pd.Series) -> pd.Series:
    s = s.sort_index()
    return s[~s.index.duplicated(keep="last")]


def _prices(data: dict[str, MarketData]) -> dict[str, pd.DataFrame]:
    return {s: md.klines[["open", "high", "low", "close"]] for s, md in data.items()}


def _funding(data: dict[str, MarketData]) -> dict[str, pd.Series]:
    return {s: md.funding for s, md in data.items() if md.funding is not None}


def holdout_start_of(data: dict[str, MarketData], holdout_months: int) -> pd.Timestamp:
    """Inicio del holdout sellado: `holdout_months` antes del ultimo dato comun, al inicio del dia UTC."""
    last = min(md.klines.index[-1] for md in data.values()) + pd.Timedelta(minutes=1)
    return (last - pd.DateOffset(months=holdout_months)).floor("D")


def development(data: dict[str, MarketData], holdout_start: pd.Timestamp) -> dict[str, MarketData]:
    """Solo el periodo de desarrollo [inicio, holdout_start): velas, funding y metrics cortados antes del holdout."""
    return {s: MarketData(md.klines[md.klines.index < holdout_start],
                          None if md.funding is None else md.funding[md.funding.index < holdout_start],
                          None if md.metrics is None else md.metrics[md.metrics.index < holdout_start])
            for s, md in data.items()}


def simulate_combo(rule, dev: dict[str, MarketData], params: dict[str, Any], costs: Costs,
                   prices: dict[str, pd.DataFrame] | None = None,
                   funding: dict[str, pd.Series] | None = None) -> tuple[dict[str, pd.Series], _Daily]:
    """Una combinacion de parametros simulada sobre `dev` (lo mismo que hace `evaluate` con cada combinacion de la
    grilla). Devuelve la senal y solo las series diarias: el resultado minuto a minuto pesa GBs."""
    sig = rule(dev, params)
    res = simulate(prices if prices is not None else _prices(dev), sig,
                   funding if funding is not None else _funding(dev), costs)
    return sig, _Daily(res.daily_returns, res.daily_gross_returns, res.daily_turnover)


def evaluate(cfg: RunConfig, registry: TrialRegistry | None = None, data: dict[str, MarketData] | None = None) -> dict[str, Any]:
    spec, rule = CATALOG[cfg.strategy_id]
    if not spec.backtestable:
        raise ValueError(f"{cfg.strategy_id} no se puede backtestear (solo forward): {spec.needs}")
    data = data or load_market(cfg.symbols, spec.needs, cfg.start, cfg.end)
    first = max(md.klines.index[0] for md in data.values())
    last = min(md.klines.index[-1] for md in data.values()) + pd.Timedelta(minutes=1)
    holdout_start = holdout_start_of(data, cfg.holdout_months)
    dev = development(data, holdout_start)
    prices, funding = _prices(dev), _funding(dev)
    data = None  # liberar la copia completa: solo se usa el desarrollo (el holdout queda sellado)

    combos = grid_combos(spec)
    sims = []
    for params in combos:
        # solo lo necesario: el resultado minuto a minuto de cada combinacion pesa GBs (OOM con 4 en paralelo)
        sig, daily = simulate_combo(rule, dev, params, cfg.costs, prices, funding)
        sims.append((params, sig, daily))
        log.info("%s %s: Sharpe dev %.2f", cfg.strategy_id, {k: params[k] for k in spec.grid}, stats.sharpe_ratio(daily.daily_returns))

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

    # nulo de entradas al azar: por simbolo, o desplazamiento conjunto de todas las patas si es neutral (null_kind)
    null = []
    if cfg.n_perm > 0:
        for sig in _null_signals(spec, stitched, cfg, oos_start, oos_end):
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
            "dsr_label": dsr_label(len(combos)),
            "min_trl_days": dsr["min_trl_obs"], "days": dsr["n_obs"],
            "total_return": float(oos.sum()), "max_drawdown": stats.max_drawdown(oos),
            "breakeven_bps_per_side": breakeven,
            "cost_curve": stats.cost_curve(oos_g, oos_t) if len(oos_res.trades) else None,
            "trades": int(len(trades_oos)), "hit_rate": hr,
            "trade_quality": trade_quality_block(cfg.strategy_id, trades_oos, oos),
            "breakdown": oos_res.breakdown,
        },
        "baselines": {
            "buy_and_hold_sharpe": {s: stats.sharpe_ratio(r.daily_returns) for s, r in bh.items()},
            "basket_sharpe": stats.sharpe_ratio(basket.daily_returns),
            "random_entries": {"n": len(null), "median_sharpe": float(np.median(null)) if null else None,
                               "p_value": p_random, "kind": null_kind(spec)},
        },
    }
    # Curvas de capital diarias (solo presentacion para el panel; no entran en ninguna metrica ni criterio).
    # Retorno acumulado = suma de los retornos diarios, la misma convencion que `total_return`.
    report["oos"]["equity_daily"] = equity_daily(oos)
    bh_daily = {s: r.daily_returns for s, r in bh.items()}
    report["baselines"]["equity_daily"] = {
        "basket": equity_daily(basket.daily_returns),
        "buy_and_hold_mean": equity_daily(pd.concat(bh_daily, axis=1).mean(axis=1)) if bh_daily else [],
        "buy_and_hold": {s: equity_daily(r) for s, r in bh_daily.items()},
    }
    report["verdict"] = verdict(report)
    return report


def barrier_of(strategy_id: str) -> tuple[float, float] | None:
    """(objetivo, stop) en ATR si la ficha de salidas tiene los dos niveles en ATR; si no, None (no hay win-rate de
    azar por barreras: sale por tiempo, senal, VWAP, etc.)."""
    f = EXITS.get(strategy_id) or {}
    st, tp = f.get("stop") or {}, f.get("take_profit") or {}
    if st.get("tipo") == "atr" and tp.get("tipo") == "atr" and st.get("valor") and tp.get("valor"):
        return (float(tp["valor"]), float(st["valor"]))
    return None


def trade_quality_block(strategy_id: str, trades: pd.DataFrame, daily: pd.Series) -> dict[str, Any] | None:
    """Calidad de los trades (stats.trade_quality) con los trades en orden de cierre. None si no hubo trades."""
    if not len(trades):
        return None
    t = trades.sort_values(["exit_time", "entry_time"], kind="stable")
    return stats.trade_quality(t["pnl_net"].to_numpy(), t["ret_net"].to_numpy(), daily, barrier_of(strategy_id))


PSR_LABEL = "PSR (sin corrección de selección)"


def dsr_label(n_combinations: int) -> str:
    """Como se llama el "DSR propio" del reporte. Con UNA combinacion la varianza de los intentos es 0, SR0 = 0 y el
    DSR es exactamente el PSR: no corrige ninguna seleccion (ni la del programa). El calculo no cambia; solo se
    marca. El liston del programa (`lab.program`, regla del 2026-10-19) es el que corrige por todos los intentos."""
    return PSR_LABEL if n_combinations <= 1 else "DSR"


HOLDOUT_CRITERIA = {"retorno_neto_min": 0.0, "le_gana_a_la_canasta": True, "p_random_max": 0.10}


def open_holdout(cfg: RunConfig, registry: TrialRegistry, data: dict[str, MarketData] | None = None) -> dict[str, Any]:
    """Abre el holdout UNA vez (decisions.md, criterio fijado antes de abrir).

    1. La combinacion se elige con la regla del walk-forward: mejor Sharpe en los ultimos `train_days` antes del
       holdout (solo datos de desarrollo).
    2. Las senales son causales: se calculan con todos los datos y se simula SOLO [holdout_start, ultimo dato), con
       la posicion vigente al arrancar.
    3. Se registra en el registro de intentos con split 'holdout' (un indice unico impide una segunda apertura).
    """
    spec, rule = CATALOG[cfg.strategy_id]
    if registry.count(cfg.strategy_id, "holdout") > 0:
        raise HoldoutAlreadyOpenedError(f"el holdout de {cfg.strategy_id} ya se abrio")
    data = data or load_market(cfg.symbols, spec.needs, cfg.start, cfg.end)
    last = min(md.klines.index[-1] for md in data.values()) + pd.Timedelta(minutes=1)
    holdout_start = holdout_start_of(data, cfg.holdout_months)
    sel0 = holdout_start - pd.Timedelta(days=cfg.train_days)
    dev = development(data, holdout_start)
    dev_prices, dev_funding = _prices(dev), _funding(dev)
    scores = []
    for params in grid_combos(spec):
        r = simulate(dev_prices, rule(dev, params), dev_funding, cfg.costs).daily_returns
        sc = stats.sharpe_ratio(_slice(r, sel0, holdout_start))
        scores.append((sc if math.isfinite(sc) else -np.inf, params))
    sel_sharpe, params = max(scores, key=lambda x: x[0])
    del dev, dev_prices, dev_funding

    sig = rule(data, params)  # causal: computar con todo equivale a computar en vivo
    stitched = {}
    for s, v in sig.items():
        prev = v[v.index < holdout_start]
        head = pd.Series([prev.iloc[-1] if len(prev) else 0.0], index=pd.DatetimeIndex([holdout_start]))
        stitched[s] = _dedupe(pd.concat([head, v[v.index >= holdout_start]]))
    prices = {s: p[p.index >= holdout_start] for s, p in _prices(data).items()}
    funding = {s: f[f.index >= holdout_start] for s, f in _funding(data).items()}
    res = simulate(prices, stitched, funding, cfg.costs)
    r = res.daily_returns
    sharpe = stats.sharpe_ratio(r) if float(np.nanstd(r)) > 0 else float("nan")
    basket = equal_weight_basket(prices, funding, cfg.costs)
    bh = buy_and_hold(prices, funding, cfg.costs)
    null = []
    for sig in _null_signals(spec, stitched, cfg, holdout_start, last):
        null.append(stats.sharpe_ratio(simulate(prices, sig, funding, cfg.costs).daily_returns))
    p_random = stats_pvalue(sharpe, null) if math.isfinite(sharpe) else None
    basket_sharpe = stats.sharpe_ratio(basket.daily_returns)
    hr = stats.hit_rate(res.trades["pnl_net"]) if len(res.trades) else {}
    checks = {
        "retorno_neto_positivo": float(r.sum()) > HOLDOUT_CRITERIA["retorno_neto_min"],
        "le_gana_a_la_canasta": math.isfinite(sharpe) and sharpe > basket_sharpe,
        "le_gana_al_azar": p_random is not None and p_random <= HOLDOUT_CRITERIA["p_random_max"],
    }
    report = {
        "strategy_id": cfg.strategy_id, "spec_hash": spec.spec_hash(), "split": "holdout",
        "window": [str(holdout_start), str(last)], "days": int(len(r)),
        "selected_params": {k: params[k] for k in spec.grid}, "selection_sharpe_last_train": sel_sharpe,
        "holdout": {
            "sharpe": sharpe, "sharpe_ci95": (lambda c: [c["lo"], c["hi"]])(stats.sharpe_ci(r, seed=cfg.seed))
            if math.isfinite(sharpe) else None,
            "total_return": float(r.sum()), "max_drawdown": stats.max_drawdown(r), "trades": int(len(res.trades)),
            "hit_rate": hr, "trade_quality": trade_quality_block(cfg.strategy_id, res.trades, r),
            "breakdown": res.breakdown, "equity_daily": equity_daily(r),
        },
        "baselines": {
            "basket_sharpe": basket_sharpe, "basket_return": float(basket.daily_returns.sum()),
            "buy_and_hold_sharpe": {s: stats.sharpe_ratio(x.daily_returns) for s, x in bh.items()},
            "random_entries": {"n": len(null), "median_sharpe": float(np.median(null)) if null else None,
                               "p_value": p_random, "kind": null_kind(spec)},
            "equity_daily": {"basket": equity_daily(basket.daily_returns)},
        },
        "verdict": {"confirma": all(checks.values()), "checks": checks, "criterios": HOLDOUT_CRITERIA},
    }
    registry.register(cfg.strategy_id, spec.spec_hash(), params, "holdout", cfg.symbols,
                      {"sharpe": sharpe, "total_return": float(r.sum()), "confirma": report["verdict"]["confirma"]},
                      holdout_start, last)
    return report


def equity_daily(daily_returns: pd.Series) -> list[list[Any]]:
    """[[fecha UTC 'YYYY-MM-DD', retorno acumulado al cierre de esa fecha], ...] (suma simple, 6 decimales)."""
    r = daily_returns.sort_index()
    r = r[~r.index.duplicated(keep="last")].fillna(0.0)
    return [[str(pd.Timestamp(ts).date()), round(float(v), 6)] for ts, v in r.cumsum().items()]


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
    p.add_argument("--open-holdout", action="store_true",
                   help="abrir el holdout sellado (UNA sola vez por estrategia; criterio en decisions.md)")
    p.add_argument("--registry", default=str(Path(os.environ.get("HEATSIGNAL_LAB_HOME", Path.home() / "heatsignal-lab")) / "trials.sqlite"))
    args = p.parse_args(list(argv) if argv is not None else None)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    cfg = RunConfig(args.strategy_id, [s.strip().upper() for s in args.symbols.split(",") if s.strip()],
                    pd.Timestamp(args.start, tz="UTC") if args.start else None,
                    pd.Timestamp(args.end, tz="UTC") if args.end else None, n_perm=args.perm)
    reg = TrialRegistry(args.registry)
    if args.open_holdout:
        try:
            report = open_holdout(cfg, reg)
        finally:
            reg.close()
        out = lab_dir().parent / "results" / args.strategy_id / "holdout" / f"{pd.Timestamp.now(tz='UTC'):%Y%m%dT%H%M%S}.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(report, indent=2, default=_json_default, ensure_ascii=False))
        h = report["holdout"]
        print(f"{args.strategy_id} HOLDOUT {report['window']}: Sharpe {h['sharpe']} | retorno {h['total_return']} | "
              f"trades {h['trades']} | confirma: {report['verdict']['confirma']} {report['verdict']['checks']}\n-> {out}")
        return 0
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
