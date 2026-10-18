"""
Liston del programa: los tres metodos de la regla aprobada por el usuario (decisions.md, 2026-10-19).

    python -m heatsignal.lab.program                 # -> ~/heatsignal-lab/results/_program/program_<ts>.json
    python -m heatsignal.lab.program --solo s1,s2    # prueba de humo con algunas estrategias (no es el liston)
    python -m heatsignal.lab.program --combos-desarrollo   # K efectivo de las grillas re-simulando el desarrollo

La regla: se calculan y publican TRES metodos en paralelo; ninguno se elige por lo que aprueba.
Criterio de aprobacion: (iii) p ajustado <= 0,05 (principal) y (ii) DSR corregido >= 0,95 (confirmacion). Nada pasa
a plata real sin paper trading. Si los metodos divergen se investiga.

Entradas (solo lectura; nunca el holdout)
-----------------------------------------
- Retornos diarios OOS netos de cada estrategia del lab: ultimo resultado de ``results/<id>/*.json`` (el mismo que
  publica ``lab.publish``), campo ``oos.equity_daily`` = retorno ACUMULADO por suma simple -> se diferencia
  (``r_t = eq_t - eq_{t-1}``, con ``r_0 = eq_0``). Son los retornos del walk-forward, anteriores al holdout: la
  carpeta ``results/<id>/holdout/`` no se lee y si una fecha de la curva cae en o despues de
  ``data.holdout_start`` el programa se niega a correr (``HoldoutLeakError``). La curva viene redondeada a 6
  decimales (error <= 1e-6 por dia, despreciable frente a desvios diarios del orden de 1e-3 a 1e-2).
- K real: combinaciones distintas (``strategy_id`` + ``params``) de ``lab_trials`` sin el holdout + las estrategias
  OK del benchmark de Freqtrade (``results/_benchmark_ta``). Hoy 44 + 7 = 51. Ningun estado lo cambia.
- Estados por intento (``lab_trial_status``, eventos append-only; ver ``lab.trials``): una estrategia con alguna
  combinacion ``invalido`` o ``sin_resultado`` sale de la dispersion (metodo i), de la matriz de correlacion
  (metodo ii) y de la matriz del bootstrap (metodo iii), pero sigue en K real y suma a K efectivo como
  independiente (conservador: el intento se hizo, aunque su resultado no sirva). Tambien quedan fuera, como
  "sin resultado (derivado)", las que no tienen curva o tienen desvio 0 en la ventana comun.

Alineacion de fechas: INTERSECCION
----------------------------------
Las ventanas OOS no son iguales (las que arrancan en 2020-06-29 o 2020-08-08 terminan antes, en 2026-06-27 /
2026-07-07). Dentro de su ventana cada curva ya tiene un valor por dia, con 0 los dias sin posicion. Fuera de su
ventana la estrategia NO fue evaluada: rellenar con 0 (union) inventaria dias "planos" que nunca se midieron y
achicaria su varianza y su correlacion. Por eso la matriz T x N usa la interseccion de fechas (hoy ~1933 dias,
2021-03-13 -> 2026-06-27; se pierde < 1 % de los dias de las ventanas mas largas). ``align(..., how="union")``
queda disponible para comparar, pero no es lo que se publica.

Metodo (i) - referencia: el DSR del programa tal como existia
--------------------------------------------------------------
``lab.publish.program`` sin cambios de formula: K = 51, dispersion = varianza de los Sharpe OOS observados (lab +
Freqtrade), SR0 = E[max] de K Sharpe ruido, DSR sin asimetria ni curtosis. Lo unico nuevo: un intento no valido no
entra a esa varianza (con todos validos el numero es identico al de antes).

Metodo (ii) - DSR corregido
---------------------------
1. Dispersion nula ANALITICA por estrategia. Varianza asintotica del estimador del Sharpe por periodo con retornos
   iid no normales (Mertens 2002; Bailey & Lopez de Prado 2012, 2014):
       V[SR_hat] = (1 - g3 * SR + (g4 - 1) / 4 * SR^2) / T        (g4 = curtosis Pearson, normal = 3)
   Bajo H0 (SR = 0) los terminos de asimetria y curtosis se anulan y queda EXACTAMENTE
       V0_i = 1 / T_i        (por periodo; anual: 365 / T_i -> desvio ~ 0,433 con T = 1950)
   con T_i = dias de la ventana OOS completa de la estrategia i. Esa es la dispersion de los Sharpe de K
   estrategias SIN ventaja: reemplaza a la varianza observada (1,96 anual), que mezclaba estrategias rotas y otras
   construcciones. La asimetria y la curtosis entran donde importan: en el PSR final de cada estrategia (abajo).
   Se informa tambien, solo como referencia, la varianza evaluada en el SR observado.
2. K efectivo por correlacion: ``stats.effective_n_trials`` (rango efectivo, exp de la entropia de los autovalores)
   sobre la matriz de correlacion de los retornos diarios OOS (interseccion) de las estrategias validas.
3. Combinaciones de una misma estrategia: sus retornos del desarrollo no quedan guardados. Por defecto se usa la
   aproximacion CONSERVADORA: cada combinacion extra cuenta como un intento independiente,
       K_eff = K_eff(estrategias) + sum_i (c_i - 1) + sum_{no validas} c_i + n_freqtrade
   Con ``--combos-desarrollo`` se re-simula la grilla de cada estrategia con c_i >= 2 sobre su desarrollo (mismo
   codigo que ``lab.diagnostics.dev_matrix``, cortado en el holdout_start) y se usa K_eff_i = rango efectivo de
   sus c_i columnas en lugar de c_i. La alternativa multiplicativa (K_eff(estrategias) x K_lab / N) se informa
   como referencia: es siempre <= la aditiva (porque K_eff <= N), o sea menos conservadora.
4. Freqtrade (7) suma a K como independiente y NO entra a la dispersion (es otra construccion: PnL realizado).
5. DSR_i = PSR(SR_i; SR0_i, g3_i, g4_i) con SR0_i = E[max de K_eff Sharpe ruido con varianza V0_i] y la
   asimetria/curtosis de SUS retornos diarios OOS (``stats.deflated_sharpe_ratio`` con skew/kurt). Con SR > 0,
   asimetria negativa o colas gordas agrandan la varianza del estimador y bajan el DSR.

Metodo (iii) - test de maximo por bootstrap (Romano-Wolf)
---------------------------------------------------------
- Matriz X (T x N) de retornos diarios OOS netos de las estrategias validas, interseccion de fechas.
- Nula impuesta centrando cada columna (media 0): el mundo donde NINGUNA tiene ventaja pero cada una conserva su
  volatilidad, sus colas, su autocorrelacion y su correlacion con las demas.
- Bootstrap estacionario CONJUNTO (Politis-Romano 1994): los mismos indices para todas las columnas (preserva la
  correlacion cruzada), bloques de largo geometrico con media 20 dias, B = 5000, semilla fija.
- Estadistico: max_j Sharpe anualizado_j. p del maximo = (1 + #{max*_b >= max observado}) / (B + 1).
- Romano-Wolf escalonado (Romano & Wolf 2005, "Stepwise multiple testing as formalized data snooping",
  Econometrica 73(4); p ajustados como en Romano & Wolf 2016): se ordenan los Sharpe observados de mayor a menor;
  la estrategia del paso j se compara con el maximo bootstrap de las que quedan (j..N), y el p ajustado se hace
  monotono (max con el del paso anterior). Controla el FWER y dice CUALES superan la nula, no solo si alguna.

Por que es un test de Sharpe maximo y no un SPA de perdidas predictivas
-----------------------------------------------------------------------
- White (2000), Reality Check: H0: max_k E[d_k] <= 0, con d_k = diferencia de perdida (o de performance) del modelo
  k contra un benchmark; estadistico max_k sqrt(T) * mean(d_k) SIN estudentizar; nula por recentrado de todas las
  columnas a 0 (configuracion menos favorable); bootstrap estacionario. Se vuelve conservador si hay modelos muy
  malos (que con el recentrado a 0 aportan ruido al maximo).
- Hansen (2005), SPA: estudentiza cada d_k (desvio de largo plazo) y recentra solo a los modelos que no son
  claramente malos (umbral -sqrt(2 log log T)); asi los modelos pesimos no inflan el p. Esta pensado para
  comparar PERDIDAS DE PRONOSTICO (MSE, etc.) de muchos modelos contra un benchmark.
- Lo de aca: (a) no hay pronosticos ni funcion de perdida: el "rendimiento" es el retorno neto diario de una regla
  de trading y el benchmark es 0 (caja), no otro modelo; (b) el estadistico es el SHARPE, o sea la media
  estudentizada por el desvio de los retornos (no por un desvio HAC de la media como en el SPA); (c) la nula
  recentra TODAS las columnas a 0, como el Reality Check (configuracion menos favorable: conservador con las
  estrategias rotas, en vez de la correccion de Hansen); (d) se agrega el escalonado de Romano-Wolf para
  identificar cuales. Es un "Reality Check de Sharpe maximo" con stepdown, no un SPA.

Salida: results/_program/program_<ts>.json (lo publica ``lab.publish`` en el bloque ``program.metodos``).
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import sys
from collections.abc import Iterable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np

from . import stats

log = logging.getLogger("heatsignal.lab.program")

PERIODS_PER_YEAR = stats.PERIODS_PER_YEAR
BLOCK_LEN = 20          # dias, largo medio de los bloques del bootstrap estacionario
N_BOOT = 5000
SEED = 20261019         # fija: la regla se aprobo el 2026-10-19
P_MAX = 0.05            # (iii) principal
DSR_MIN = 0.95          # (ii) confirmacion
PROGRAM_DIR = "_program"
PROGRAM_PREFIX = "program_"
RULE = "decisions.md: Liston del programa, regla aprobada por el usuario (2026-10-19)"
PSR_LABEL = "PSR (sin corrección de selección)"


class HoldoutLeakError(RuntimeError):
    """Una curva OOS tiene fechas en o despues del inicio del holdout: el liston no corre."""


# ============================================================================ entradas


def returns_from_equity(eq: list[list[Any]]) -> tuple[list[str], np.ndarray]:
    """[[fecha, acumulado], ...] (suma simple) -> (fechas, retornos diarios). r_0 = eq_0 (la curva arranca en 0)."""
    dates = [str(d)[:10] for d, _ in eq]
    v = np.asarray([float(x) for _, x in eq], dtype=np.float64)
    return dates, np.diff(v, prepend=0.0)


def load_lab_curves(results_dir: Path, only: Iterable[str] | None = None) -> dict[str, dict[str, Any]]:
    """Ultimo resultado de cada estrategia (el que publica lab.publish) -> fechas y retornos diarios OOS.

    No lee ``results/<id>/holdout/`` ni los diagnosticos. Falla si alguna fecha cae en el holdout."""
    from .publish import _latest_runs

    keep = set(only) if only is not None else None
    out: dict[str, dict[str, Any]] = {}
    for folder, runs in _latest_runs(results_dir):
        r = json.loads(runs[-1].read_text())
        sid = r.get("strategy_id") or folder.name
        if keep is not None and sid not in keep:
            continue
        o = r.get("oos") or {}
        eq = o.get("equity_daily")
        item: dict[str, Any] = {"strategy_id": sid, "run": runs[-1].stem, "n_combinations": r.get("n_combinations"),
                                "sharpe_reported": o.get("sharpe"), "oos_range": (r.get("walk_forward") or {}).get("oos"),
                                "holdout_start": (r.get("data") or {}).get("holdout_start"), "dates": [], "returns": None}
        if eq:
            dates, ret = returns_from_equity(eq)
            hs = item["holdout_start"]
            if hs and dates and max(dates) >= str(hs)[:10]:
                raise HoldoutLeakError(f"{sid}: la curva OOS llega a {max(dates)} >= holdout {str(hs)[:10]}")
            item["dates"], item["returns"] = dates, ret
        out[sid] = item
    return out


def align(curves: dict[str, dict[str, Any]], ids: list[str], how: str = "intersection") -> tuple[list[str], np.ndarray]:
    """Matriz T x N (columnas en el orden de ``ids``). ``intersection`` (lo que se usa) o ``union`` con 0."""
    maps = [dict(zip(curves[i]["dates"], curves[i]["returns"])) for i in ids]
    if not maps:
        return [], np.zeros((0, 0))
    if how == "intersection":
        dates = sorted(set.intersection(*(set(m) for m in maps)))
    elif how == "union":
        dates = sorted(set.union(*(set(m) for m in maps)))
    else:
        raise ValueError(how)
    x = np.array([[m.get(d, 0.0) for m in maps] for d in dates], dtype=np.float64).reshape(len(dates), len(ids))
    return dates, x


# ============================================================================ metodo (ii)


def null_sr_variance(n_obs: int, skew: float = 0.0, kurt: float = 3.0, sr: float = 0.0) -> float:
    """Varianza del estimador del Sharpe POR PERIODO: (1 - g3*SR + (g4-1)/4*SR^2) / T. Con SR = 0 (H0) = 1/T."""
    return (1.0 - skew * sr + (kurt - 1.0) / 4.0 * sr * sr) / n_obs


def corr_matrix(x: np.ndarray) -> np.ndarray:
    """Correlacion de Pearson entre columnas (T x N)."""
    if x.shape[1] == 1:
        return np.ones((1, 1))
    c = np.corrcoef(x, rowvar=False)
    return np.clip(np.nan_to_num(c, nan=0.0), -1.0, 1.0)


def effective_k(
    corr: np.ndarray, combos: list[int], excluded_combos: int, n_benchmark: int,
    combo_keff: list[float] | None = None,
) -> dict[str, float]:
    """K efectivo total del metodo (ii). ``combos[i]`` = combinaciones de la estrategia i (columna i de ``corr``);
    ``combo_keff[i]`` (opcional) = K efectivo de su grilla medido en el desarrollo; si falta se usa c_i."""
    n = corr.shape[0]
    k_strat = stats.effective_n_trials(corr) if n >= 1 else 0.0
    within = [float(c) for c in combos] if combo_keff is None else [float(k) for k in combo_keff]
    k_add = k_strat + sum(w - 1.0 for w in within) + excluded_combos + n_benchmark
    k_lab_valid = sum(combos)
    k_mult = (k_strat * k_lab_valid / n if n else 0.0) + excluded_combos + n_benchmark
    return {"k_eff": float(k_add), "k_eff_estrategias": float(k_strat), "k_eff_multiplicativa": float(k_mult),
            "k_combinaciones_extra": float(sum(w - 1.0 for w in within))}


def dsr_corrected(returns: np.ndarray, k_eff: float, use_moments: bool = True) -> dict[str, Any]:
    """DSR del metodo (ii) para una estrategia: dispersion nula 1/T y PSR con su asimetria y curtosis."""
    m = stats.return_moments(returns)
    t = int(m["n"])
    v0 = null_sr_variance(t)
    sr0 = stats.expected_max_sharpe(k_eff, v0)
    skew, kurt = (m["skew"], m["kurt"]) if use_moments else (0.0, 3.0)
    try:
        dsr = stats.deflated_sharpe_ratio(m["sr"], t, k_eff, v0, skew, kurt)
    except ValueError:  # termino de varianza no positivo (asimetria extrema): no se inventa un numero
        dsr = None
    ann = math.sqrt(PERIODS_PER_YEAR)
    try:
        v_obs = null_sr_variance(t, m["skew"], m["kurt"], m["sr"])
    except ZeroDivisionError:
        v_obs = None
    return {"dsr": dsr, "sharpe_annual": m["sr"] * ann, "sr0_annual": sr0 * ann, "days": t,
            "skew": m["skew"], "kurt": m["kurt"], "var_null_period": v0, "sd_null_annual": math.sqrt(v0) * ann,
            "sd_at_observed_sr_annual": math.sqrt(v_obs) * ann if v_obs and v_obs > 0 else None}


# ============================================================================ metodo (iii)


def _boot_sharpes(xc: np.ndarray, block_len: float, n_boot: int, rng: np.random.Generator) -> np.ndarray:
    """B x N Sharpe anualizados de remuestras conjuntas (mismos indices para todas las columnas)."""
    t, n = xc.shape
    out = np.empty((n_boot, n))
    chunk = max(1, int(4_000_000 // max(1, t * n)))
    done = 0
    while done < n_boot:
        b = min(chunk, n_boot - done)
        idx = stats.stationary_bootstrap_indices(t, block_len, b, rng)
        s = xc[idx]                                   # b x T x N
        sd = s.std(axis=1, ddof=1)
        with np.errstate(divide="ignore", invalid="ignore"):
            sr = s.mean(axis=1) / sd * math.sqrt(PERIODS_PER_YEAR)
        out[done:done + b] = np.where(np.isfinite(sr), sr, -np.inf)
        done += b
    return out


def romano_wolf_stepdown(observed: np.ndarray, boot: np.ndarray) -> np.ndarray:
    """p ajustados de Romano-Wolf (escalonado, monotono) para estadisticos 'mas grande = mas evidencia'.

    ``observed`` (N,), ``boot`` (B x N) bajo la nula impuesta. Paso j: la j-esima mayor observada contra el maximo
    bootstrap de las que todavia no se rechazaron/ordenaron (j..N)."""
    obs = np.where(np.isfinite(observed), observed, -np.inf)
    order = np.argsort(-obs, kind="stable")
    b = boot.shape[0]
    p_adj = np.ones(obs.size)
    prev = 0.0
    for j, i in enumerate(order):
        max_rest = boot[:, order[j:]].max(axis=1)
        p = (1.0 + float(np.sum(max_rest >= obs[i]))) / (b + 1.0)
        prev = max(prev, p)
        p_adj[i] = prev
    return p_adj


def max_sharpe_bootstrap(
    x: np.ndarray, block_len: float = BLOCK_LEN, n_boot: int = N_BOOT, seed: int = SEED,
) -> dict[str, Any]:
    """Test de Sharpe maximo (metodo iii) sobre T x N: nula por centrado, bootstrap estacionario conjunto,
    p del maximo y p ajustados de Romano-Wolf por columna."""
    x = np.asarray(x, dtype=np.float64)
    if x.ndim != 2 or x.shape[1] < 1 or x.shape[0] < 3:
        raise ValueError("hace falta una matriz T x N con T >= 3 y N >= 1")
    t, n = x.shape
    sd = x.std(axis=0, ddof=1)
    with np.errstate(divide="ignore", invalid="ignore"):
        obs = np.where(sd > 0, x.mean(axis=0) / sd * math.sqrt(PERIODS_PER_YEAR), np.nan)
    xc = x - x.mean(axis=0, keepdims=True)
    rng = np.random.default_rng(seed)
    boot = _boot_sharpes(xc, block_len, n_boot, rng)
    obs_f = np.where(np.isfinite(obs), obs, -np.inf)
    i_max = int(np.argmax(obs_f))
    boot_max = boot.max(axis=1)
    p_max = (1.0 + float(np.sum(boot_max >= obs_f[i_max]))) / (n_boot + 1.0)
    p_adj = romano_wolf_stepdown(obs, boot)
    q = np.quantile(boot_max[np.isfinite(boot_max)], [0.5, 0.9, 0.95, 0.99]) if np.isfinite(boot_max).any() else [None] * 4
    return {"sharpe_observed": [None if not math.isfinite(v) else float(v) for v in obs], "max_observed": float(obs_f[i_max]),
            "argmax": i_max, "p_max": p_max, "p_adjusted": [float(p) for p in p_adj],
            "boot_max_quantiles": {"q50": q[0], "q90": q[1], "q95": q[2], "q99": q[3]},
            "block_len": float(block_len), "n_boot": int(n_boot), "seed": int(seed), "days": int(t), "n": int(n)}


# ============================================================================ combinaciones (opcional)


def combo_keff_from_development(strategy_id: str, result_path: Path) -> float:
    """K efectivo de la grilla de una estrategia, re-simulando sus combinaciones sobre el DESARROLLO (cortado en el
    holdout_start del resultado). Mismo codigo que lab.diagnostics (PBO). Pesado: solo con --combos-desarrollo."""
    from .diagnostics import assert_no_holdout, dev_matrix
    from .run import development, load_market
    from .strategies import CATALOG

    import pandas as pd

    result = json.loads(result_path.read_text())
    spec, rule = CATALOG[strategy_id]
    hs = pd.Timestamp(result["data"]["holdout_start"])
    data = load_market(result.get("symbols") or [], spec.needs, None, result["data"]["holdout_start"])
    dev = development(data, hs)
    assert_no_holdout(dev, hs)
    mat, _ = dev_matrix(spec, rule, dev)
    x = mat.to_numpy()
    x = x[:, x.std(axis=0) > 0]
    if x.shape[1] < 2:
        return float(max(1, x.shape[1]))
    return stats.effective_n_trials(corr_matrix(x))


# ============================================================================ orquestacion


def _finite(v: Any) -> bool:
    return isinstance(v, (int, float)) and math.isfinite(float(v))


def compute(
    lab_home: Path, *, only: Iterable[str] | None = None, n_boot: int = N_BOOT, block_len: float = BLOCK_LEN,
    seed: int = SEED, combos_desarrollo: bool = False, how: str = "intersection",
) -> dict[str, Any]:
    """Los tres metodos del liston. ``lab_home`` = carpeta con results/ y trials.sqlite."""
    from .publish import benchmark_ta, program as program_i
    from .trials import read_statuses

    results_dir, trials_db = lab_home / "results", lab_home / "trials.sqlite"
    curves = load_lab_curves(results_dir, only)
    statuses = read_statuses(trials_db)
    bench_block = benchmark_ta(results_dir / "_benchmark_ta")
    bench = [x for x in ((bench_block or {}).get("results") or []) if x.get("ok")]
    if only is not None:  # prueba de humo: solo esas estrategias (no es el liston del programa)
        statuses = {k: v for k, v in statuses.items() if k in set(only)}

    # estado por estrategia: registrado o derivado
    estados: dict[str, dict[str, Any]] = {}
    for sid in sorted(set(curves) | set(statuses)):
        st = statuses.get(sid) or {}
        c = curves.get(sid) or {}
        n_comb = int(st.get("n_combinations") or c.get("n_combinations") or 1)
        status, motivo, fuente = st.get("status", "valido"), st.get("motivo"), "registro"
        if status == "valido":
            r = c.get("returns")
            if r is None or len(r) < 3:
                status, motivo, fuente = "sin_resultado", "sin curva OOS publicada", "derivado"
            elif float(np.std(r)) == 0.0:
                status, motivo, fuente = "sin_resultado", "retornos OOS con desvio 0 (no opero)", "derivado"
        estados[sid] = {"status": status, "motivo": motivo, "fuente": fuente, "n_combinations": n_comb,
                        "run": c.get("run")}
    valid = [s for s, e in estados.items() if e["status"] == "valido"]
    dates, x = align(curves, valid, how)
    # una columna sin varianza en la ventana comun no puede entrar a la correlacion ni al bootstrap
    if x.size:
        flat = [s for j, s in enumerate(valid) if float(np.std(x[:, j])) == 0.0]
        for s in flat:
            estados[s].update(status="sin_resultado", motivo="desvio 0 en la ventana comun", fuente="derivado")
        if flat:
            valid = [s for s in valid if s not in flat]
            dates, x = align(curves, valid, how)

    k_lab = sum(e["n_combinations"] for e in estados.values())
    k_real = k_lab + len(bench)

    # ------------------------------------------------------------- (i) referencia: publish.program tal cual
    light = {"strategies": [{"strategy_id": s, "sharpe": curves[s]["sharpe_reported"] if s in valid else None,
                             "oos_days": len(curves[s]["dates"]) if s in valid else None}
                            for s in estados if s in curves],
             "benchmark_ta": bench_block}
    m1 = program_i(trials_db, light, exclude=[s for s in estados if s not in valid]) if trials_db.exists() else None
    per1 = {s["strategy_id"]: s.get("dsr_program") for s in light["strategies"]}
    metodo_i = {"nombre": "DSR del programa (referencia)", **(m1 or {}),
                "dsr": per1, "pasan": sorted(s for s, v in per1.items() if _finite(v) and v >= DSR_MIN),
                "nota": "formula sin cambios: dispersion = varianza de los Sharpe OOS observados (lab + Freqtrade), "
                        "sin asimetria ni curtosis"}

    # ------------------------------------------------------------- (ii) DSR corregido
    combos_valid = [estados[s]["n_combinations"] for s in valid]
    excluded_combos = sum(e["n_combinations"] for s, e in estados.items() if s not in valid)
    combo_keff = None
    combo_src = "aproximacion conservadora: cada combinacion extra de una grilla cuenta como intento independiente"
    if combos_desarrollo:
        combo_keff, combo_src = [], "K efectivo de cada grilla medido con los retornos del desarrollo (re-simulados)"
        for s, c in zip(valid, combos_valid):
            if c < 2:
                combo_keff.append(1.0)
                continue
            path = results_dir / s / f"{curves[s]['run']}.json"
            combo_keff.append(min(float(c), combo_keff_from_development(s, path)))
    corr = corr_matrix(x) if x.size else np.zeros((0, 0))
    kk = effective_k(corr, combos_valid, excluded_combos, len(bench), combo_keff) if valid else \
        {"k_eff": float(excluded_combos + len(bench)), "k_eff_estrategias": 0.0, "k_eff_multiplicativa": 0.0,
         "k_combinaciones_extra": 0.0}
    k_eff = min(kk["k_eff"], float(k_real))
    per2: dict[str, dict[str, Any]] = {}
    for s in valid:
        d = dsr_corrected(curves[s]["returns"], k_eff)
        d["dsr_sin_momentos"] = dsr_corrected(curves[s]["returns"], k_eff, use_moments=False)["dsr"]
        per2[s] = d
    metodo_ii = {
        "nombre": "DSR corregido (dispersion nula analitica + K efectivo)",
        "k_real": k_real, "k_eff": k_eff, **{k: v for k, v in kk.items() if k != "k_eff"},
        "k_eff_combinaciones": combo_keff, "combinaciones_fuente": combo_src,
        "k_benchmark": len(bench), "k_no_validas": excluded_combos,
        "dispersion": "V0 = 1/T por periodo (Mertens con SR = 0: asimetria y curtosis se anulan); anual = 365/T",
        "dispersiones": {s: {"days": d["days"], "var_null_period": d["var_null_period"],
                             "sd_null_annual": d["sd_null_annual"],
                             "sd_at_observed_sr_annual": d["sd_at_observed_sr_annual"]} for s, d in per2.items()},
        "por_estrategia": per2,
        "pasan": sorted(s for s, d in per2.items() if _finite(d["dsr"]) and d["dsr"] >= DSR_MIN),
    }

    # ------------------------------------------------------------- (iii) bootstrap de Sharpe maximo
    if len(valid) >= 1 and x.shape[0] >= 3:
        bt = max_sharpe_bootstrap(x, block_len, n_boot, seed)
        p_adj = dict(zip(valid, bt["p_adjusted"]))
        metodo_iii = {
            "nombre": "Sharpe maximo por bootstrap estacionario conjunto + Romano-Wolf",
            "p_max": bt["p_max"], "max_observed": bt["max_observed"], "estrategia_max": valid[bt["argmax"]],
            "p_ajustado": p_adj, "sharpe_observado": dict(zip(valid, bt["sharpe_observed"])),
            "boot_max_quantiles": bt["boot_max_quantiles"], "block_len": bt["block_len"], "n_boot": bt["n_boot"],
            "seed": bt["seed"], "days": bt["days"], "n": bt["n"], "nula": "centrado de cada columna (media 0)",
            "pasan": sorted(s for s, p in p_adj.items() if p <= P_MAX),
        }
    else:
        metodo_iii = {"nombre": "Sharpe maximo por bootstrap estacionario conjunto + Romano-Wolf", "p_max": None,
                      "p_ajustado": {}, "pasan": [], "nota": "sin estrategias validas suficientes"}

    pasan_prog = sorted(set(metodo_ii["pasan"]) & set(metodo_iii["pasan"]))
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "regla": RULE,
        "criterio": {"p_ajustado_max": P_MAX, "dsr_corregido_min": DSR_MIN,
                     "texto": "(iii) p ajustado <= 0,05 (principal) y (ii) DSR corregido >= 0,95 (confirmacion); "
                              "nada pasa a plata real sin paper trading; si los metodos divergen se investiga"},
        "prueba_de_humo": only is not None,
        "k_real": k_real, "k_lab": k_lab, "k_benchmark": len(bench), "k_eff": k_eff,
        "alineacion": {"how": how, "desde": dates[0] if dates else None, "hasta": dates[-1] if dates else None,
                       "days": len(dates), "estrategias": valid},
        "holdout": "NO USADO",
        "runs": {s: e["run"] for s, e in estados.items()},
        "estados": estados,
        "metodo_i": metodo_i, "metodo_ii": metodo_ii, "metodo_iii": metodo_iii,
        "pasan": {"i": metodo_i["pasan"], "ii": metodo_ii["pasan"], "iii": metodo_iii["pasan"], "programa": pasan_prog},
    }


def latest_program(results_dir: Path) -> tuple[Path, dict[str, Any]] | None:
    files = sorted((results_dir / PROGRAM_DIR).glob(PROGRAM_PREFIX + "*.json"))
    for f in reversed(files):
        try:
            return f, json.loads(f.read_text())
        except (OSError, ValueError):
            continue
    return None


def _clean(obj: Any) -> Any:
    if isinstance(obj, float) and not math.isfinite(obj):
        return None
    if isinstance(obj, (np.floating, np.integer)):
        return _clean(obj.item())
    if isinstance(obj, dict):
        return {k: _clean(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_clean(v) for v in obj]
    return obj


def main(argv: Iterable[str] | None = None) -> int:
    from .vision import lab_dir

    p = argparse.ArgumentParser(description="Liston del programa: tres metodos (DSR referencia, DSR corregido, bootstrap RW).")
    p.add_argument("--home", default=None, help="carpeta del laboratorio (default ~/heatsignal-lab)")
    p.add_argument("--solo", default=None, help="prueba de humo: lista de strategy_id separados por coma")
    p.add_argument("--boot", type=int, default=N_BOOT)
    p.add_argument("--combos-desarrollo", action="store_true",
                   help="K efectivo de cada grilla re-simulando el desarrollo (pesado; sin esto: aproximacion conservadora)")
    args = p.parse_args(list(argv) if argv is not None else None)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    home = Path(args.home) if args.home else lab_dir().parent
    only = [s.strip() for s in args.solo.split(",") if s.strip()] if args.solo else None
    rep = compute(home, only=only, n_boot=args.boot, combos_desarrollo=args.combos_desarrollo)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    out_dir = home / "results" / PROGRAM_DIR
    if only is not None:
        out_dir = home / "tmp" / "program_humo"  # una prueba de humo no se publica como liston
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"{PROGRAM_PREFIX}{ts}.json"
    out.write_text(json.dumps(_clean(rep), indent=1, ensure_ascii=False))
    m2, m3 = rep["metodo_ii"], rep["metodo_iii"]
    print(f"K real {rep['k_real']} | K efectivo {rep['k_eff']:.1f} (estrategias {m2['k_eff_estrategias']:.1f}) | "
          f"p max {m3.get('p_max')} ({m3.get('estrategia_max')})")
    for k in ("i", "ii", "iii", "programa"):
        print(f"  pasan ({k}): {', '.join(rep['pasan'][k]) or 'ninguna'}")
    print(f"-> {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
