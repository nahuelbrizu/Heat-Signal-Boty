"""
Diagnosticos anti-sobreajuste del laboratorio: PBO por CSCV y robustez por parametros vecinos.

    python -m heatsignal.lab.diagnostics pbo tsmom-diario-v1 [--blocks 16]
    python -m heatsignal.lab.diagnostics robustez tsmom-diario-v1
    python -m heatsignal.lab.diagnostics ambos tsmom-diario-v1
    python -m heatsignal.lab.diagnostics pbo --all          # todas las que tienen resultado (y grilla, para PBO)
    python -m heatsignal.lab.diagnostics salidas tsmom-diario-v1   # solo la atribucion de salidas (ambos tambien la corre)

Escribe ~/heatsignal-lab/results/<id>/diagnostics_<ts>.json y deja cada corrida en la tabla append-only
`lab_diagnostics` (misma base que `lab_trials`, ~/heatsignal-lab/trials.sqlite). `lab.publish` los lleva al panel.

1. PBO (Probability of Backtest Overfitting) por CSCV: Bailey, Borwein, Lopez de Prado & Zhu (2015), "The
   Probability of Backtest Overfitting", Journal of Computational Finance 20(4).
   - Matriz M de T dias x N combinaciones de la grilla congelada: retornos diarios netos (7 bps por lado) de cada
     combinacion simulada sobre TODO el desarrollo [inicio, holdout_start), con el mismo codigo que `lab.run`
     (`run.simulate_combo`). Se recortan los dias iniciales en que todas las columnas son 0 (calentamiento de las
     reglas) y los T mod S dias mas viejos, para que los S bloques tengan el mismo largo.
   - Para cada una de las C(S, S/2) particiones en mitades IS / OOS (bloques contiguos en el tiempo): n* = la
     combinacion de mejor Sharpe IS; w = rango (1..N, promedio en empates) de n* entre los Sharpe OOS / (N + 1);
     logit = ln(w / (1 - w)). PBO = fraccion de particiones con logit <= 0 (el mejor IS quedo en la mitad de abajo
     OOS). Ademas: degradacion = pendiente de la regresion Sharpe OOS(n*) ~ Sharpe IS(n*) entre particiones, y
     probabilidad de perdida = fraccion de particiones con Sharpe OOS(n*) < 0.
   - Con N impar el rango del medio da logit 0 y cuenta como sobreajuste (la definicion con <=): con ruido puro el
     PBO esperado es (N + 1) / (2N), no 1/2. Con N = 2 el PBO solo distingue "el mejor IS fue el peor OOS".
   - NO suma intentos: son las mismas combinaciones de la grilla congelada que ya cuenta `lab_trials` (si hay que
     re-simularlas, es la misma grilla). Igual queda registrado en `lab_diagnostics` (kind = 'pbo').

2. Robustez por parametros vecinos (DIAGNOSTICO, NUNCA SELECCION):
   - Base = los parametros congelados del spec, con los de la grilla en el valor que mas eligio el walk-forward del
     ultimo resultado (empate: el elegido en la ultima ventana).
   - Cada parametro numerico escalar (no bool, no texto, no listas) se mueve -40 %, -20 %, +20 % y +40 %, de a uno,
     con el resto fijo. Los enteros se redondean; un vecino que redondea al original o repite otro se omite. Si sale
     del rango valido (`BOUNDS`), se recorta al limite; si viola una relacion entre parametros (`CONSTRAINTS`), se
     omite. Todo queda listado con su motivo.
   - Se mide el Sharpe del desarrollo con los costos del laboratorio (7 bps por lado) y con los costos x3.
   - Meseta = fraccion de vecinos evaluados con Sharpe >= 50 % del Sharpe original (solo si el original es > 0:
     con un original <= 0 no hay "meseta" que medir). Caida maxima = Sharpe original - peor vecino.
   - Reglas anti p-hacking (decisions.md):
       * se guarda en la tabla aparte `lab_diagnostics` (kind = 'robustez'), NO en `lab_trials`: el conteo K del
         programa (`publish.program`) lee solo `lab_trials`, asi que estos vecinos no son intentos ni pueden
         inflar/abaratar el liston; ademas `lab_trials.split` tiene un CHECK que solo admite train/test/holdout;
       * el resultado no tiene "mejor vecino" ni ningun campo que sugiera parametros: nada de esto modifica el spec
         congelado ni `paperbot/params.json` (no se escribe en ninguno);
       * nunca usa el holdout: los datos se cargan hasta `holdout_start` del resultado evaluado y se vuelven a cortar
         con `run.development`; `assert_no_holdout` lo verifica antes de simular.

3. Atribucion de salidas (`salidas`, tambien dentro de `ambos`; `lab/exits.py`): cuantas operaciones del desarrollo
   cerraron por stop, objetivo, tiempo, senal o rebalanceo, con los parametros base de arriba. Corre la regla una vez
   (sin simular) envolviendo `exits_with_levels` / `hold_events` y verifica que la reconstruccion coincida exactamente.
   Mismo corte del holdout. NO suma intentos y NO se escribe en `lab_diagnostics` (su CHECK solo admite pbo/robustez):
   queda solo en el JSON del diagnostico, que `lab.publish` lleva al panel.
"""

from __future__ import annotations

import argparse
import copy
import itertools
import json
import logging
import math
import re
import resource
import sqlite3
import sys
import time
from collections import Counter
from collections.abc import Callable, Iterable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from scipy.stats import rankdata

from . import stats
from .run import development, grid_combos, load_market, simulate_combo
from .sim import Costs

log = logging.getLogger("heatsignal.lab.diagnostics")

PERIODS_PER_YEAR = stats.PERIODS_PER_YEAR
DEFAULT_BLOCKS = 16
STEPS = (-0.4, -0.2, 0.2, 0.4)
MESETA_FRACTION = 0.5
COST_MULT = 3.0
DIAG_PREFIX = "diagnostics_"

# Rango valido de los parametros REALES (float) por nombre (inclusive). Primer patron que coincide. Sin coincidencia
# (y todos los enteros: ventanas, horas, n): > 0, y >= 1 si es entero. Los topes de apalancamiento quedan en 1x: el
# spec dice "sin apalancamiento" y eso no es un parametro a mover.
BOUNDS: list[tuple[str, float, float]] = [
    (r"pct", 0.01, 0.995),                             # percentiles / cuantiles trailing (pct, imb_pct, pct_hi, ...)
    (r"_frac$", 0.05, 1.0),                            # fraccion minima de datos validos
    (r"^(max_leverage|max_weight)$", 0.05, 1.0),
    (r"^(rsi_lo|rsi_hi|adx_min|adx_max)$", 1.0, 99.0),  # osciladores 0-100
    (r"^rebalance_threshold$", 0.01, 0.95),
    (r"^(tp_retrace|ofi)$", 0.01, 1.0),
]


# Relaciones entre parametros que una regla necesita para tener sentido. Si un vecino las rompe, se omite.
CONSTRAINTS: list[tuple[str, Callable[[dict[str, Any], int], bool]]] = [
    ("exit_z < entry_z < stop_z", lambda p, n: not {"exit_z", "entry_z", "stop_z"} <= p.keys()
     or p["exit_z"] < p["entry_z"] < p["stop_z"]),
    ("pct_lo < pct_hi", lambda p, n: not {"pct_lo", "pct_hi"} <= p.keys() or p["pct_lo"] < p["pct_hi"]),
    ("rsi_lo < 50 < rsi_hi", lambda p, n: ("rsi_lo" not in p or p["rsi_lo"] < 50) and ("rsi_hi" not in p or p["rsi_hi"] > 50)),
    ("ema_fast < ema_slow < ema_macro", lambda p, n: not {"ema_fast", "ema_slow", "ema_macro"} <= p.keys()
     or p["ema_fast"] < p["ema_slow"] < p["ema_macro"]),
    ("2 * n_side <= simbolos", lambda p, n: "n_side" not in p or 2 * p["n_side"] <= n),
    ("min_symbols <= simbolos", lambda p, n: "min_symbols" not in p or p["min_symbols"] <= n),
]


# ============================================================================ PBO (CSCV)


def cscv_partitions(n_blocks: int) -> np.ndarray:
    """Matriz booleana C(S, S/2) x S: cada fila marca los bloques de la mitad IS (el resto es OOS)."""
    if n_blocks < 2 or n_blocks % 2:
        raise ValueError(f"S tiene que ser par y >= 2, no {n_blocks}")
    combos = list(itertools.combinations(range(n_blocks), n_blocks // 2))
    out = np.zeros((len(combos), n_blocks), dtype=bool)
    for i, c in enumerate(combos):
        out[i, list(c)] = True
    return out


def _sharpe_from_sums(n: np.ndarray, s1: np.ndarray, s2: np.ndarray) -> np.ndarray:
    """Sharpe anualizado (ddof=1, como `stats.sharpe_ratio`) a partir de conteos, sumas y sumas de cuadrados."""
    with np.errstate(divide="ignore", invalid="ignore"):
        mean = s1 / n
        var = (s2 - s1 * s1 / n) / (n - 1)
        sd = np.sqrt(np.where(var > 0, var, np.nan))
        sr = mean / sd * math.sqrt(PERIODS_PER_YEAR)
    # desvio numericamente nulo -> NaN (igual que stats.sharpe_ratio con desvio 0)
    return np.where(np.isfinite(sr) & (var > 1e-18 * np.maximum(1.0, np.abs(mean)) ** 2), sr, np.nan)


def pbo_cscv(returns: pd.DataFrame | np.ndarray, n_blocks: int = DEFAULT_BLOCKS) -> dict[str, Any]:
    """PBO por CSCV sobre una matriz T x N (filas = dias en orden, columnas = combinaciones). Ver el docstring."""
    m = np.asarray(returns, dtype=np.float64)
    if m.ndim != 2:
        raise ValueError("la matriz tiene que ser T x N")
    t_all, n = m.shape
    if n < 2:
        raise ValueError("PBO necesita al menos 2 combinaciones")
    m = np.nan_to_num(m, nan=0.0)
    trimmed = t_all % n_blocks
    m = m[trimmed:]
    t = m.shape[0]
    block_len = t // n_blocks
    if block_len < 2:
        raise ValueError(f"muy pocos dias ({t_all}) para {n_blocks} bloques")
    blocks = m.reshape(n_blocks, block_len, n)
    b1, b2 = blocks.sum(axis=1), (blocks ** 2).sum(axis=1)       # S x N
    parts = cscv_partitions(n_blocks)                              # C x S
    is_w, oos_w = parts.astype(np.float64), (~parts).astype(np.float64)
    cnt = float(block_len * n_blocks // 2)
    sr_is = _sharpe_from_sums(np.full((len(parts), n), cnt), is_w @ b1, is_w @ b2)
    sr_oos = _sharpe_from_sums(np.full((len(parts), n), cnt), oos_w @ b1, oos_w @ b2)
    best = np.argmax(np.where(np.isnan(sr_is), -np.inf, sr_is), axis=1)
    rows = np.arange(len(parts))
    oos_filled = np.where(np.isnan(sr_oos), -np.inf, sr_oos)
    ranks = rankdata(oos_filled, axis=1, method="average")[rows, best]
    w = ranks / (n + 1)
    logits = np.log(w / (1 - w))
    is_best, oos_best = sr_is[rows, best], sr_oos[rows, best]
    ok = np.isfinite(is_best) & np.isfinite(oos_best)
    slope = intercept = r2 = None
    if ok.sum() >= 3 and np.std(is_best[ok]) > 0:
        slope, intercept = (float(x) for x in np.polyfit(is_best[ok], oos_best[ok], 1))
        r2 = float(np.corrcoef(is_best[ok], oos_best[ok])[0, 1] ** 2)
    hist_edges = np.arange(-4.0, 4.01, 0.5)
    hist, _ = np.histogram(np.clip(logits, -3.999, 3.999), bins=hist_edges)
    return {
        "pbo": float(np.mean(logits <= 0)),
        "n_partitions": int(len(parts)),
        "n_blocks": int(n_blocks),
        "block_days": int(block_len),
        "n_combinations": int(n),
        "days": int(t),
        "days_trimmed": int(trimmed),
        "logit_median": float(np.median(logits)),
        "logit_hist": {"edges": [float(x) for x in hist_edges], "counts": [int(x) for x in hist]},
        "degradation": {"slope": slope, "intercept": intercept, "r2": r2,
                        "is_sharpe_mean": float(np.nanmean(is_best)) if np.isfinite(is_best).any() else None,
                        "oos_sharpe_mean": float(np.nanmean(oos_best)) if np.isfinite(oos_best).any() else None},
        "prob_oos_loss": float(np.mean(np.where(np.isfinite(oos_best), oos_best, 0.0) < 0)),
        "best_is_counts": {str(k): int(v) for k, v in sorted(Counter(best.tolist()).items())},
    }


def _label(params: dict[str, Any], keys: Iterable[str]) -> str:
    return ", ".join(f"{k}={params[k]}" for k in keys) or "base"


def dev_matrix(spec, rule, dev: dict[str, Any], costs: Costs | None = None) -> tuple[pd.DataFrame, list[dict[str, Any]]]:
    """T x N de retornos diarios netos del desarrollo, una columna por combinacion de la grilla congelada (mismo
    codigo que `lab.run.evaluate`). Recorta los dias iniciales en que ninguna combinacion tiene retorno."""
    costs = costs or Costs()
    combos = grid_combos(spec)
    keys = sorted(spec.grid)
    cols = {}
    for params in combos:
        _, daily = simulate_combo(rule, dev, params, costs)
        cols[_label(params, keys)] = daily.daily_returns
        log.info("%s %s: Sharpe dev %.2f", spec.strategy_id, _label(params, keys), stats.sharpe_ratio(daily.daily_returns))
    mat = pd.concat(cols, axis=1).sort_index().fillna(0.0)
    active = (mat != 0).any(axis=1).to_numpy()
    if active.any():
        mat = mat.iloc[int(np.argmax(active)):]
    return mat, combos


def pbo_for(spec, rule, dev: dict[str, Any], n_blocks: int = DEFAULT_BLOCKS, costs: Costs | None = None) -> dict[str, Any]:
    if len(grid_combos(spec)) < 2:
        return {"skipped": "la estrategia no tiene grilla (1 combinacion): el PBO necesita 2 o mas"}
    mat, combos = dev_matrix(spec, rule, dev, costs)
    out = pbo_cscv(mat, n_blocks)
    keys = sorted(spec.grid)
    out["combinations"] = [{k: c[k] for k in keys} for c in combos]
    out["combination_sharpe"] = {c: stats.sharpe_ratio(mat[c]) for c in mat.columns}
    out["best_is_counts"] = {mat.columns[int(k)]: v for k, v in out["best_is_counts"].items()}
    out["window"] = [str(mat.index[0]), str(mat.index[-1] + pd.Timedelta(days=1))]
    out["adds_trials"] = False
    return out


# ============================================================================ robustez


def _bounds(name: str, is_int: bool) -> tuple[float, float]:
    if is_int:
        return 1.0, math.inf
    for pat, lo, hi in BOUNDS:
        if re.search(pat, name):
            return lo, hi
    return 1e-9, math.inf


def _is_numeric(v: Any) -> bool:
    return isinstance(v, (int, float, np.integer, np.floating)) and not isinstance(v, (bool, np.bool_))


def neighbor_values(name: str, value: Any, steps: Iterable[float] = STEPS) -> list[dict[str, Any]]:
    """Vecinos de UN parametro: value * (1 + step), redondeado si es entero y recortado al rango valido."""
    is_int = isinstance(value, (int, np.integer))
    lo, hi = _bounds(name, is_int)
    out, seen = [], {value}
    for st in steps:
        raw = value * (1 + st)
        v = int(round(raw)) if is_int else float(f"{raw:.6g}")
        status, reason = "ok", None
        if v < lo or v > hi:
            v = (int(math.ceil(lo)) if v < lo else int(math.floor(hi))) if is_int else float(min(max(v, lo), hi))
            status, reason = "recortado", f"fuera de rango [{lo}, {hi}]: se recorto al limite"
        if v in seen:
            status = "omitido"
            if reason:
                reason = f"fuera de rango [{lo}, {hi}]: recortado queda " + ("igual al original" if v == value else "igual a otro vecino")
            else:
                reason = "redondea al valor original" if v == value else "repite otro vecino"
        else:
            seen.add(v)
        out.append({"param": name, "step": st, "value": v, "status": status, "reason": reason})
    return out


def neighbors(params: dict[str, Any], n_symbols: int, steps: Iterable[float] = STEPS) -> tuple[list[dict[str, Any]], list[str]]:
    """Todos los vecinos de a un parametro por vez. Devuelve (vecinos con su `params` completo, parametros salteados)."""
    out, skipped = [], []
    for name in sorted(params):
        v = params[name]
        if not _is_numeric(v) or (isinstance(v, float) and not math.isfinite(v)) or v == 0:
            skipped.append(name)
            continue
        for nb in neighbor_values(name, v, steps):
            full = {**params, name: nb["value"]}
            if nb["status"] != "omitido":
                broken = [d for d, ok in CONSTRAINTS if not ok(full, n_symbols)]
                if broken:
                    nb["status"], nb["reason"] = "omitido", "rompe " + "; ".join(broken)
            nb["params"] = full
            out.append(nb)
    return out, skipped


def base_params(spec, result: dict[str, Any] | None) -> dict[str, Any]:
    """Parametros congelados + los de la grilla en el valor que mas eligio el walk-forward (empate: el ultimo)."""
    base = copy.deepcopy(dict(spec.params))
    chosen = [c.get("params") or {} for c in ((result or {}).get("walk_forward") or {}).get("chosen") or []]
    if spec.grid and chosen:
        keyed = [json.dumps(c, sort_keys=True) for c in chosen]
        counts = Counter(keyed)
        top = max(counts.values())
        pick = next(k for k in reversed(keyed) if counts[k] == top)
        base.update({k: v for k, v in json.loads(pick).items() if k in spec.grid})
    return base


def _sharpes(daily, mult: float) -> tuple[float, float]:
    net, gross = daily.daily_returns, daily.daily_gross_returns.reindex(daily.daily_returns.index).fillna(0.0)
    # costos lineales en el nocional operado: neto = bruto - costos -> con costos x m: bruto - m * (bruto - neto)
    return stats.sharpe_ratio(net), stats.sharpe_ratio(gross - mult * (gross - net))


def _fin(x: float) -> float | None:
    return float(x) if x is not None and math.isfinite(x) else None


def robustness(spec, rule, dev: dict[str, Any], base: dict[str, Any], costs: Costs | None = None,
               cost_mult: float = COST_MULT, steps: Iterable[float] = STEPS,
               on_variant: Callable[[dict[str, Any]], None] | None = None) -> dict[str, Any]:
    """Sharpe del desarrollo de la base y de cada vecino, con costos normales y x`cost_mult`. Solo diagnostico."""
    costs = costs or Costs()
    base = copy.deepcopy(base)  # nada de lo que pase aca toca los parametros de quien llama
    n_symbols = len(base.get("symbols") or dev)
    _, d0 = simulate_combo(rule, dev, base, costs)
    s0, s0x = _sharpes(d0, cost_mult)
    del d0
    if on_variant:
        on_variant({"variant": "base", "params": base, "sharpe": _fin(s0), "sharpe_x3": _fin(s0x)})
    nbs, skipped = neighbors(base, n_symbols, steps)
    for nb in nbs:
        if nb["status"] == "omitido":
            nb["sharpe"] = nb["sharpe_x3"] = None
            continue
        t0 = time.monotonic()
        _, d = simulate_combo(rule, dev, nb["params"], costs)
        s, sx = _sharpes(d, cost_mult)
        del d
        nb["sharpe"], nb["sharpe_x3"] = _fin(s), _fin(sx)
        log.info("%s %s %+.0f%% = %s: Sharpe %.2f (x%g %.2f) [%.0fs]", spec.strategy_id, nb["param"], nb["step"] * 100,
                 nb["value"], s, cost_mult, sx, time.monotonic() - t0)
        if on_variant:
            on_variant({"variant": f"{nb['param']} {nb['step']:+.0%}", "params": nb["params"],
                        "sharpe": nb["sharpe"], "sharpe_x3": nb["sharpe_x3"]})
    evaluated = [nb for nb in nbs if nb["status"] != "omitido"]
    # un vecino sin Sharpe (no opero o desvio 0) cuenta como fuera de la meseta
    vals = [nb["sharpe"] if nb["sharpe"] is not None else -math.inf for nb in evaluated]
    vals_x = [nb["sharpe_x3"] if nb["sharpe_x3"] is not None else -math.inf for nb in evaluated]
    meseta = meseta_x3 = None
    if math.isfinite(s0) and s0 > 0 and evaluated:
        meseta = float(np.mean([v >= MESETA_FRACTION * s0 for v in vals]))
    if math.isfinite(s0x) and s0x > 0 and evaluated:
        meseta_x3 = float(np.mean([v >= MESETA_FRACTION * s0x for v in vals_x]))
    for nb, v in zip(evaluated, vals):
        nb["en_meseta"] = None if meseta is None else bool(v >= MESETA_FRACTION * s0)
    finite = [v for v in vals if math.isfinite(v)]
    worst_i = int(np.argmin(vals)) if evaluated else None
    max_drop = (s0 - min(finite)) if finite and math.isfinite(s0) else None
    cost_bps = costs.fee_bps + costs.default_slippage_bps
    return {
        "base_params": {k: v for k, v in base.items() if _is_numeric(v) or k in spec.grid},
        "base": {"sharpe": _fin(s0), "sharpe_x3": _fin(s0x)},
        "cost_bps_per_side": cost_bps, "cost_mult": cost_mult,
        "steps": list(steps), "threshold": MESETA_FRACTION,
        "n_neighbors": len(evaluated), "n_omitted": len(nbs) - len(evaluated),
        "meseta": meseta, "meseta_x3": meseta_x3,
        "meseta_note": None if meseta is not None else "Sharpe original <= 0 (o sin vecinos): la meseta no aplica",
        "max_drop": None if max_drop is None else _fin(max_drop),
        "max_drop_rel": None if max_drop is None or not s0 > 0 else _fin(max_drop / s0),
        "n_without_sharpe": len(vals) - len(finite),
        "worst": None if worst_i is None else {k: evaluated[worst_i][k] for k in ("param", "step", "value")},
        "params_not_numeric": skipped,
        "neighbors": [{k: nb.get(k) for k in ("param", "step", "value", "status", "reason", "sharpe", "sharpe_x3", "en_meseta")}
                      for nb in nbs],
        "adds_trials": False,
        "changes_frozen_params": False,
    }


# ============================================================================ registro append-only


class DiagnosticsLog:
    """Tabla `lab_diagnostics`: APPEND-ONLY (triggers), en la misma base que `lab_trials` pero separada de ella
    para que ningun diagnostico entre al conteo K del programa (`publish.program` solo lee `lab_trials`)."""

    def __init__(self, db_path: str | Path) -> None:
        self._conn = sqlite3.connect(str(db_path), timeout=60)
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS lab_diagnostics (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id       TEXT NOT NULL,
                strategy_id  TEXT NOT NULL,
                spec_hash    TEXT NOT NULL,
                kind         TEXT NOT NULL CHECK (kind IN ('pbo', 'robustez')),
                variant      TEXT NOT NULL,
                params       TEXT NOT NULL,
                window_start TEXT,
                window_end   TEXT,
                symbols      TEXT NOT NULL,
                metrics      TEXT NOT NULL,
                created_at   TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_lab_diagnostics_strategy ON lab_diagnostics (strategy_id, kind);
            CREATE TRIGGER IF NOT EXISTS trg_lab_diagnostics_no_update BEFORE UPDATE ON lab_diagnostics
            BEGIN SELECT RAISE(ABORT, 'lab_diagnostics es append-only: UPDATE no permitido'); END;
            CREATE TRIGGER IF NOT EXISTS trg_lab_diagnostics_no_delete BEFORE DELETE ON lab_diagnostics
            BEGIN SELECT RAISE(ABORT, 'lab_diagnostics es append-only: DELETE no permitido'); END;
            """
        )
        self._conn.commit()

    def add(self, run_id: str, strategy_id: str, spec_hash: str, kind: str, variant: str, params: dict[str, Any],
            symbols: list[str], metrics: dict[str, Any], window: tuple[Any, Any] = (None, None)) -> int:
        cur = self._conn.execute(
            "INSERT INTO lab_diagnostics (run_id, strategy_id, spec_hash, kind, variant, params, window_start, window_end,"
            " symbols, metrics, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (run_id, strategy_id, spec_hash, kind, variant, json.dumps(params, sort_keys=True, default=str),
             None if window[0] is None else str(window[0]), None if window[1] is None else str(window[1]),
             json.dumps(list(symbols)), json.dumps(_clean(metrics), sort_keys=True, default=str),
             datetime.now(timezone.utc).isoformat()))
        self._conn.commit()
        return int(cur.lastrowid)

    def count(self, strategy_id: str | None = None, kind: str | None = None) -> int:
        conds, args = [], []
        if strategy_id:
            conds.append("strategy_id = ?")
            args.append(strategy_id)
        if kind:
            conds.append("kind = ?")
            args.append(kind)
        where = (" WHERE " + " AND ".join(conds)) if conds else ""
        return int(self._conn.execute(f"SELECT COUNT(*) FROM lab_diagnostics{where}", args).fetchone()[0])

    def close(self) -> None:
        self._conn.close()


def _clean(obj: Any) -> Any:
    if isinstance(obj, float) and not math.isfinite(obj):
        return None
    if isinstance(obj, (np.floating, np.integer)):
        return _clean(obj.item())
    if isinstance(obj, dict):
        return {str(k): _clean(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_clean(v) for v in obj]
    return obj


# ============================================================================ orquestacion


class HoldoutLeakError(RuntimeError):
    """Algun dato del desarrollo cae en el holdout: el diagnostico no corre."""


def assert_no_holdout(dev: dict[str, Any], holdout_start: pd.Timestamp) -> None:
    for s, md in dev.items():
        for name, obj in (("klines", md.klines), ("funding", md.funding), ("metrics", md.metrics)):
            if obj is not None and len(obj) and obj.index.max() >= holdout_start:
                raise HoldoutLeakError(f"{s} {name}: dato en {obj.index.max()} >= holdout {holdout_start}")


def latest_result(results_dir: Path, strategy_id: str) -> tuple[Path, dict[str, Any]] | None:
    """Ultimo resultado del lote (results/<id>/<ts>.json; no los diagnosticos ni las subcarpetas)."""
    runs = sorted(p for p in (results_dir / strategy_id).glob("*.json") if not p.name.startswith(DIAG_PREFIX))
    return (runs[-1], json.loads(runs[-1].read_text())) if runs else None


def diagnose(kind: str, spec, rule, result: dict[str, Any], data: dict[str, Any], *, run_name: str | None = None,
             n_blocks: int = DEFAULT_BLOCKS, costs: Costs | None = None, log_db: DiagnosticsLog | None = None) -> dict[str, Any]:
    """Corre `pbo`, `robustez` o `ambos` sobre el desarrollo del resultado evaluado (`result`). `data` puede traer
    mas de lo necesario: se corta en el holdout_start del resultado antes de nada."""
    if kind not in ("pbo", "robustez", "ambos", "salidas"):
        raise ValueError(kind)
    if result.get("spec_hash") and result["spec_hash"] != spec.spec_hash():
        raise ValueError(f"el spec de {spec.strategy_id} cambio desde el resultado ({result['spec_hash']} != "
                         f"{spec.spec_hash()}): correr de nuevo lab.run antes de diagnosticar")
    holdout_start = pd.Timestamp(result["data"]["holdout_start"])
    dev = development(data, holdout_start)
    assert_no_holdout(dev, holdout_start)
    symbols = list(result.get("symbols") or list(dev))
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    first = min(md.klines.index[0] for md in dev.values())
    out: dict[str, Any] = {
        "strategy_id": spec.strategy_id, "spec_hash": spec.spec_hash(), "run": run_name, "diagnostics_id": run_id,
        "generated_at": datetime.now(timezone.utc).isoformat(), "symbols": symbols,
        "development": [str(first), str(holdout_start)], "holdout_start": str(holdout_start), "holdout": "NO USADO",
        "costs": {"fee_bps": (costs or Costs()).fee_bps, "slippage_bps": (costs or Costs()).default_slippage_bps},
        "nota": "Diagnostico, no seleccion: no suma intentos al K del programa ni cambia parametros congelados.",
    }
    if kind in ("pbo", "ambos"):
        t0 = time.monotonic()
        p = pbo_for(spec, rule, dev, n_blocks, costs)
        p["seconds"] = round(time.monotonic() - t0, 1)
        out["pbo"] = p
        if log_db is not None and "skipped" not in p:
            log_db.add(run_id, spec.strategy_id, spec.spec_hash(), "pbo", "grilla", {"combinations": p.get("combinations")},
                       symbols, {k: v for k, v in p.items() if k not in ("combinations",)},
                       tuple(p.get("window") or (first, holdout_start)))
    if kind in ("robustez", "ambos"):
        t0 = time.monotonic()
        base = base_params(spec, result)

        def _rec(v: dict[str, Any]) -> None:
            if log_db is not None:
                log_db.add(run_id, spec.strategy_id, spec.spec_hash(), "robustez", v["variant"], v["params"], symbols,
                           {"sharpe": v["sharpe"], "sharpe_x3": v["sharpe_x3"]}, (first, holdout_start))

        r = robustness(spec, rule, dev, base, costs, on_variant=_rec)
        r["seconds"] = round(time.monotonic() - t0, 1)
        out["robustez"] = r
    if kind in ("salidas", "ambos"):
        from .exits import attribute

        t0 = time.monotonic()
        s = attribute(spec.strategy_id, rule, dev, base_params(spec, result))
        s.update({"seconds": round(time.monotonic() - t0, 1), "window": [str(first), str(holdout_start)],
                  "holdout": "NO USADO", "adds_trials": False,
                  "base_params": {k: v for k, v in base_params(spec, result).items() if k in spec.grid}})
        out["salidas"] = s
    return out


def _results_dir() -> Path:
    from .vision import lab_dir

    return lab_dir().parent / "results"


def _with_results(results_dir: Path) -> list[str]:
    from .strategies import CATALOG

    out = []
    for folder in sorted(p for p in results_dir.glob("*") if p.is_dir() and not p.name.startswith("_")):
        if folder.name in CATALOG and CATALOG[folder.name][0].backtestable and latest_result(results_dir, folder.name):
            out.append(folder.name)
    return out


def _json_default(o: Any) -> Any:
    if isinstance(o, (np.floating, np.integer)):
        return o.item()
    return str(o)


def main(argv: Iterable[str] | None = None) -> int:
    from .strategies import CATALOG
    from .vision import lab_dir

    p = argparse.ArgumentParser(description="PBO (CSCV) y robustez por vecinos. Diagnostico: no suma intentos.")
    p.add_argument("kind", choices=("pbo", "robustez", "ambos", "salidas"))
    p.add_argument("strategy_id", nargs="?")
    p.add_argument("--all", action="store_true", help="todas las estrategias con resultado")
    p.add_argument("--blocks", type=int, default=DEFAULT_BLOCKS, help="S bloques del CSCV (par)")
    p.add_argument("--registry", default=str(lab_dir().parent / "trials.sqlite"))
    args = p.parse_args(list(argv) if argv is not None else None)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    results_dir = _results_dir()
    if args.all == bool(args.strategy_id):
        p.error("pasar un strategy_id o --all (no los dos)")
    ids = _with_results(results_dir) if args.all else [args.strategy_id]
    if args.kind == "pbo" and args.all:
        ids = [i for i in ids if len(grid_combos(CATALOG[i][0])) >= 2]
    db = DiagnosticsLog(args.registry)
    rc = 0
    try:
        for sid in ids:
            if sid not in CATALOG:
                print(f"{sid}: no esta en el catalogo")
                rc = 1
                continue
            found = latest_result(results_dir, sid)
            if not found:
                print(f"{sid}: sin resultado en {results_dir / sid} (correr lab.run primero)")
                rc = 1
                continue
            path, result = found
            spec, rule = CATALOG[sid]
            t0 = time.monotonic()
            # solo hasta el holdout_start del resultado: el holdout ni se lee del disco
            data = load_market(result.get("symbols") or [], spec.needs, None, result["data"]["holdout_start"])
            t_load = time.monotonic() - t0
            try:
                rep = diagnose(args.kind, spec, rule, result, data, run_name=path.stem, n_blocks=args.blocks, log_db=db)
            except ValueError as exc:
                print(f"{sid}: {exc}")
                rc = 1
                continue
            finally:
                del data
            rep["seconds_load"] = round(t_load, 1)
            rep["seconds_total"] = round(time.monotonic() - t0, 1)
            rep["peak_rss_mb"] = round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024, 0)
            out = results_dir / sid / f"{DIAG_PREFIX}{rep['diagnostics_id']}.json"
            out.write_text(json.dumps(_clean(rep), indent=1, default=_json_default, ensure_ascii=False))
            pb, rb, sb = rep.get("pbo") or {}, rep.get("robustez") or {}, rep.get("salidas") or {}
            print(f"{sid}: PBO {pb.get('pbo', pb.get('skipped', '-'))} | meseta {rb.get('meseta', '-')} | "
                  f"caida max {rb.get('max_drop', '-')} | salidas {sb.get('por_motivo', '-')} | {rep['seconds_total']} s | "
                  f"pico {rep['peak_rss_mb']} MB\n-> {out}")
    finally:
        db.close()
    return rc


if __name__ == "__main__":
    sys.exit(main())
