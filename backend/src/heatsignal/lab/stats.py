"""
Metricas estadisticas del laboratorio (implementacion propia, solo numpy/scipy/pandas).

Fuentes de las formulas
-----------------------
- PSR y MinTRL: Bailey, D. H. & Lopez de Prado, M. (2012), "The Sharpe Ratio Efficient Frontier",
  Journal of Risk 15(2). Con SR, asimetria g3 y curtosis g4 (Pearson, normal = 3) de los retornos por
  periodo y n observaciones:
      PSR(SR*) = Phi( (SR - SR*) * sqrt(n - 1) / sqrt(1 - g3*SR + (g4 - 1)/4 * SR^2) )
      MinTRL   = 1 + (1 - g3*SR + (g4 - 1)/4 * SR^2) * (Z_{1-alpha} / (SR - SR*))^2
  (MinTRL usa el SR OBSERVADO en la varianza, como el paper de 2012. ml4t-diagnostic 0.1.9 usa la
  variante de Lopez de Prado, Lipton & Zoonekynd 2025 que evalua la varianza en SR* y redondea hacia
  arriba; con SR* = 0 y sin autocorrelacion coinciden salvo el redondeo.)
- DSR: Bailey, D. H. & Lopez de Prado, M. (2014), "The Deflated Sharpe Ratio: Correcting for Selection
  Bias, Backtest Overfitting and Non-Normality", Journal of Portfolio Management 40(5). Es el PSR con
  umbral SR0 = E[max de N Sharpe ruido]:
      SR0 = sqrt(V[SR_n]) * ( (1 - gamma) * Phi^-1(1 - 1/N) + gamma * Phi^-1(1 - 1/(N*e)) )
  con gamma = constante de Euler-Mascheroni. Verificado contra el ejemplo numerico del paper
  (SR anual 2.5, T = 1250, N = 100, V = 1/2 anual, g3 = -3, g4 = 10 -> DSR ~ 0.9004) y contra
  ``probabilistic_sharpe_ratio`` / ``compute_expected_max_sharpe`` de ml4t-diagnostic 0.1.9 (MIT).
- K efectivo: rango efectivo de la matriz de correlacion de los retornos de los intentos (exp de la
  entropia de los autovalores normalizados; Roy & Vetterli 2007), como ``effective_rank`` de
  ml4t-diagnostic. Alternativa ``avg_corr``: K_eff = rho_medio + (1 - rho_medio) * K.
- Bootstrap estacionario: Politis, D. N. & Romano, J. P. (1994), "The Stationary Bootstrap", JASA 89.
  Bloques de largo geometrico con media ``block_len`` y envoltura circular.
- Wilson: Wilson, E. B. (1927), intervalo score para una proporcion binomial.

Convenciones
------------
- Sharpe anualizado con 365 dias (cripto opera todos los dias) sobre retornos DIARIOS, desvio con ddof=1.
- PSR/DSR/MinTRL trabajan con el SR POR PERIODO (no anualizado). Los helpers ``*_from_returns`` hacen la
  conversion; la varianza de los Sharpe anuales de los intentos se pasa a por-periodo dividiendo por
  ``periods_per_year``.
- Asimetria y curtosis con momentos poblacionales (divisor n), como ml4t-diagnostic.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from typing import Any, Literal

import numpy as np
import pandas as pd
from scipy import stats as sps

PERIODS_PER_YEAR = 365
EULER_GAMMA = 0.5772156649015329
DEFAULT_BPS_GRID = (0, 1, 2, 3, 5, 7, 10, 15, 20, 30)


def _clean(returns: Any) -> np.ndarray:
    arr = np.asarray(returns, dtype=np.float64).ravel()
    return arr[~np.isnan(arr)]


# ---------------------------------------------------------------------------- Sharpe basico

def sharpe_ratio(returns: Any, periods_per_year: int = PERIODS_PER_YEAR) -> float:
    """Sharpe anualizado: media / desvio (ddof=1) * sqrt(periods_per_year). NaN si n < 2 o desvio 0."""
    r = _clean(returns)
    if r.size < 2:
        return float("nan")
    sd = r.std(ddof=1)
    if sd == 0:
        return float("nan")
    return float(r.mean() / sd * math.sqrt(periods_per_year))


def return_moments(returns: Any) -> dict[str, float]:
    """SR por periodo (ddof=1), asimetria y curtosis Pearson (momentos poblacionales), n."""
    r = _clean(returns)
    n = r.size
    if n < 2:
        raise ValueError("hacen falta al menos 2 retornos")
    sd = r.std(ddof=1)
    if sd == 0:
        raise ValueError("retornos con varianza 0")
    return {
        "sr": float(r.mean() / sd),
        "skew": float(sps.skew(r, bias=True)),
        "kurt": float(sps.kurtosis(r, fisher=False, bias=True)),
        "n": n,
    }


# ---------------------------------------------------------------------------- bootstrap

def stationary_bootstrap_indices(
    n: int, block_len: float, n_boot: int, rng: np.random.Generator
) -> np.ndarray:
    """Indices (n_boot x n) del bootstrap estacionario de Politis-Romano (vectorizado).

    En cada posicion se arranca un bloque nuevo con probabilidad p = 1/block_len (siempre en la primera)
    en un indice uniforme; si no, se sigue con el indice siguiente (circular).
    """
    if block_len < 1:
        raise ValueError("block_len tiene que ser >= 1")
    p = 1.0 / block_len
    new_block = rng.random((n_boot, n)) < p
    new_block[:, 0] = True
    starts = rng.integers(0, n, size=(n_boot, n))
    pos = np.broadcast_to(np.arange(n), (n_boot, n))
    last_start = np.where(new_block, pos, 0)
    np.maximum.accumulate(last_start, axis=1, out=last_start)
    start_val = np.take_along_axis(starts, last_start, axis=1)
    return (start_val + (pos - last_start)) % n


def sharpe_ci(
    returns: Any,
    *,
    block_len: float | None = None,
    holding_days: float = 1.0,
    n_boot: int = 2000,
    alpha: float = 0.05,
    seed: int | None = None,
    periods_per_year: int = PERIODS_PER_YEAR,
) -> dict[str, float]:
    """IC percentil del Sharpe anualizado por bootstrap estacionario por bloques.

    ``block_len`` (en periodos) por defecto = max(ceil(holding_days), ceil(n^(1/3)), 2): nunca menor que
    el periodo de tenencia, para no romper la dependencia que generan posiciones de varios dias.
    """
    r = _clean(returns)
    n = r.size
    if n < 3:
        raise ValueError("hacen falta al menos 3 retornos para el bootstrap")
    if block_len is None:
        block_len = max(math.ceil(holding_days), math.ceil(n ** (1 / 3)), 2)
    rng = np.random.default_rng(seed)
    out = np.empty(n_boot)
    chunk = max(1, int(2_000_000 // n))  # acotar memoria de la matriz de indices
    done = 0
    while done < n_boot:
        b = min(chunk, n_boot - done)
        sample = r[stationary_bootstrap_indices(n, block_len, b, rng)]
        sd = sample.std(axis=1, ddof=1)
        with np.errstate(divide="ignore", invalid="ignore"):
            out[done : done + b] = sample.mean(axis=1) / sd * math.sqrt(periods_per_year)
        done += b
    out = out[np.isfinite(out)]
    lo, hi = np.quantile(out, [alpha / 2, 1 - alpha / 2])
    return {
        "sharpe": sharpe_ratio(r, periods_per_year),
        "lo": float(lo),
        "hi": float(hi),
        "block_len": float(block_len),
        "n_boot": int(n_boot),
        "alpha": float(alpha),
    }


# ---------------------------------------------------------------------------- PSR / DSR / MinTRL

def _sr_std_term(sr: float, skew: float, kurt: float) -> float:
    v = 1.0 - skew * sr + (kurt - 1.0) / 4.0 * sr**2
    if v <= 0:
        raise ValueError(f"termino de varianza del SR no positivo ({v}); revisar asimetria/curtosis")
    return v


def probabilistic_sharpe_ratio(
    sr: float, n_obs: int, skew: float = 0.0, kurt: float = 3.0, sr_benchmark: float = 0.0
) -> float:
    """PSR de Bailey & Lopez de Prado (2012). ``sr`` y ``sr_benchmark`` POR PERIODO; kurt Pearson."""
    if n_obs < 2:
        raise ValueError("n_obs tiene que ser >= 2")
    z = (sr - sr_benchmark) * math.sqrt(n_obs - 1) / math.sqrt(_sr_std_term(sr, skew, kurt))
    return float(sps.norm.cdf(z))


def expected_max_sharpe(n_trials: float, var_sr: float) -> float:
    """E[max] de N Sharpe ruido (Bailey & Lopez de Prado 2014, eq. del False Strategy Theorem).

    Devuelve en las mismas unidades que ``sqrt(var_sr)``. Con N <= 1 devuelve 0.
    """
    if n_trials <= 1 or var_sr <= 0:
        return 0.0
    z1 = sps.norm.ppf(1.0 - 1.0 / n_trials)
    z2 = sps.norm.ppf(1.0 - 1.0 / (n_trials * math.e))
    return float(math.sqrt(var_sr) * ((1.0 - EULER_GAMMA) * z1 + EULER_GAMMA * z2))


def deflated_sharpe_ratio(
    sr: float,
    n_obs: int,
    n_trials: float,
    var_trials_sr: float,
    skew: float = 0.0,
    kurt: float = 3.0,
) -> float:
    """DSR (Bailey & Lopez de Prado 2014) = PSR con umbral E[max]. Todo POR PERIODO.

    ``n_trials`` puede ser el K efectivo (no entero). ``var_trials_sr`` = varianza de los SR de los
    intentos, en las mismas unidades que ``sr``.
    """
    sr0 = expected_max_sharpe(n_trials, var_trials_sr)
    return probabilistic_sharpe_ratio(sr, n_obs, skew, kurt, sr_benchmark=sr0)


def min_track_record_length(
    sr: float,
    skew: float = 0.0,
    kurt: float = 3.0,
    sr_benchmark: float = 0.0,
    alpha: float = 0.05,
) -> float:
    """MinTRL (Bailey & Lopez de Prado 2012) en cantidad de observaciones. inf si SR <= SR*."""
    if sr <= sr_benchmark:
        return float("inf")
    z = sps.norm.ppf(1.0 - alpha)
    return float(1.0 + _sr_std_term(sr, skew, kurt) * (z / (sr - sr_benchmark)) ** 2)


def effective_n_trials(
    corr: Any, method: Literal["effective_rank", "avg_corr"] = "effective_rank"
) -> float:
    """K efectivo a partir de la matriz de correlacion (K x K) de los retornos de los intentos.

    - ``effective_rank``: exp(entropia de los autovalores normalizados) (Roy & Vetterli 2007).
    - ``avg_corr``: rho_medio + (1 - rho_medio) * K, con rho_medio = media fuera de la diagonal.
    """
    c = np.asarray(corr, dtype=np.float64)
    if c.ndim != 2 or c.shape[0] != c.shape[1]:
        raise ValueError("corr tiene que ser una matriz cuadrada")
    k = c.shape[0]
    if k == 1:
        return 1.0
    if method == "effective_rank":
        ev = np.clip(np.linalg.eigvalsh((c + c.T) / 2), 0.0, None)
        ev = ev[ev > 1e-12]
        w = ev / ev.sum()
        return float(np.exp(-np.sum(w * np.log(w))))
    if method == "avg_corr":
        rho = (c.sum() - np.trace(c)) / (k * (k - 1))
        return float(min(max(rho + (1.0 - rho) * k, 1.0), k))
    raise ValueError(f"metodo desconocido: {method!r}")


def psr_from_returns(
    returns: Any,
    sr_benchmark_annual: float = 0.0,
    periods_per_year: int = PERIODS_PER_YEAR,
) -> float:
    m = return_moments(returns)
    return probabilistic_sharpe_ratio(
        m["sr"], m["n"], m["skew"], m["kurt"], sr_benchmark_annual / math.sqrt(periods_per_year)
    )


def dsr_from_returns(
    returns: Any,
    trial_sharpes_annual: Sequence[float] | None = None,
    *,
    n_trials: float | None = None,
    var_trials_sr_annual: float | None = None,
    corr: Any = None,
    k_method: Literal["effective_rank", "avg_corr"] = "effective_rank",
    periods_per_year: int = PERIODS_PER_YEAR,
    alpha: float = 0.05,
) -> dict[str, float]:
    """DSR de la estrategia elegida a partir de sus retornos y de los intentos registrados.

    - ``trial_sharpes_annual``: Sharpe anualizados de TODOS los intentos (incluido el elegido). De ahi
      salen K (si no se pasa ``n_trials``) y V (si no se pasa ``var_trials_sr_annual``, ddof=1).
    - ``corr``: matriz de correlacion entre intentos -> K efectivo (reemplaza a K).
    Devuelve dsr, psr (vs 0), sr0 anual, K usado, MinTRL (obs, vs 0) y los momentos.
    """
    m = return_moments(returns)
    if trial_sharpes_annual is not None:
        ts = _clean(trial_sharpes_annual)
        if n_trials is None:
            n_trials = float(ts.size)
        if var_trials_sr_annual is None:
            var_trials_sr_annual = float(ts.var(ddof=1)) if ts.size > 1 else 0.0
    if n_trials is None or var_trials_sr_annual is None:
        raise ValueError("pasar trial_sharpes_annual o (n_trials y var_trials_sr_annual)")
    k_raw = float(n_trials)
    k_used = effective_n_trials(corr, k_method) if corr is not None else k_raw
    var_pp = var_trials_sr_annual / periods_per_year
    sr0 = expected_max_sharpe(k_used, var_pp)
    dsr = probabilistic_sharpe_ratio(m["sr"], m["n"], m["skew"], m["kurt"], sr_benchmark=sr0)
    psr = probabilistic_sharpe_ratio(m["sr"], m["n"], m["skew"], m["kurt"], 0.0)
    ann = math.sqrt(periods_per_year)
    return {
        "dsr": dsr,
        "psr": psr,
        "sharpe_annual": m["sr"] * ann,
        "sr0_annual": sr0 * ann,
        "n_trials_raw": k_raw,
        "n_trials_used": k_used,
        "var_trials_sr_annual": float(var_trials_sr_annual),
        "min_trl_obs": min_track_record_length(m["sr"], m["skew"], m["kurt"], 0.0, alpha),
        "n_obs": m["n"],
        "skew": m["skew"],
        "kurt": m["kurt"],
    }


# ---------------------------------------------------------------------------- otras metricas

def max_drawdown(returns: Any, compounding: bool = False) -> float:
    """Max drawdown como fraccion positiva del pico de equity.

    ``compounding=False`` (default, coincide con el simulador de nocional fijo): equity = 1 + cumsum(r).
    """
    r = _clean(returns)
    if r.size == 0:
        return 0.0
    eq = np.cumprod(1.0 + r) if compounding else 1.0 + np.cumsum(r)
    eq = np.r_[1.0, eq]
    peak = np.maximum.accumulate(eq)
    with np.errstate(divide="ignore", invalid="ignore"):
        dd = np.where(peak > 0, (peak - eq) / peak, np.nan)
    return float(np.nanmax(dd))


def wilson_interval(successes: int, n: int, alpha: float = 0.05) -> tuple[float, float]:
    if n <= 0:
        return (float("nan"), float("nan"))
    z = sps.norm.ppf(1 - alpha / 2)
    p = successes / n
    denom = 1 + z**2 / n
    center = (p + z**2 / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z**2 / (4 * n**2)) / denom
    return (float(center - half), float(center + half))


def hit_rate(pnl: Any, alpha: float = 0.05) -> dict[str, float]:
    """Proporcion de trades con pnl > 0 e IC de Wilson."""
    x = _clean(pnl)
    n = int(x.size)
    wins = int((x > 0).sum())
    lo, hi = wilson_interval(wins, n, alpha)
    return {"hit_rate": wins / n if n else float("nan"), "lo": lo, "hi": hi, "n": n, "wins": wins}


def profit_factor(pnl: Any) -> float:
    """Suma de ganancias / |suma de perdidas|. inf si no hay perdidas, nan si no hay trades."""
    x = _clean(pnl)
    if x.size == 0:
        return float("nan")
    gains = x[x > 0].sum()
    losses = -x[x < 0].sum()
    if losses == 0:
        return float("inf") if gains > 0 else float("nan")
    return float(gains / losses)


# ---------------------------------------------------------------------------- costos

def _gross_and_turnover(result_or_gross: Any, turnover: Any) -> tuple[pd.Series, pd.Series]:
    if turnover is None:
        gross = result_or_gross.daily_gross_returns
        turnover = result_or_gross.daily_turnover
    else:
        gross = result_or_gross
    gross = pd.Series(gross, dtype=np.float64)
    turnover = pd.Series(turnover, dtype=np.float64)
    if len(gross) != len(turnover):
        raise ValueError("gross y turnover con largos distintos")
    return gross, turnover


def cost_curve(
    result_or_gross: Any,
    turnover: Any = None,
    bps_grid: Sequence[float] = DEFAULT_BPS_GRID,
    periods_per_year: int = PERIODS_PER_YEAR,
) -> pd.DataFrame:
    """Curva costo -> Sharpe.

    Recibe un ``SimResult`` (usa ``daily_gross_returns`` = precio + funding y ``daily_turnover``) o
    (retornos brutos diarios, turnover diario / capital). Para cada costo TOTAL por lado ``c`` (comision +
    slippage, en bps): ``r_neto = r_bruto - turnover * c / 1e4``. El costo es plano: ignora los
    multiplicadores de slippage por barra del simulador.
    """
    gross, to = _gross_and_turnover(result_or_gross, turnover)
    g = gross.to_numpy()
    t = to.to_numpy()
    rows = []
    for bps in bps_grid:
        net = g - t * bps / 1e4
        rows.append(
            {
                "cost_bps": float(bps),
                "sharpe": sharpe_ratio(net, periods_per_year),
                "ann_return": float(np.nanmean(net) * periods_per_year),
            }
        )
    return pd.DataFrame(rows)


def breakeven_cost_bps(result_or_gross: Any, turnover: Any = None) -> float:
    """Costo total por lado (bps) que lleva el retorno medio (y el Sharpe) a 0.

    = sum(r_bruto) / sum(turnover) * 1e4. inf si no hay turnover y el bruto es positivo.
    """
    gross, to = _gross_and_turnover(result_or_gross, turnover)
    tsum = float(to.sum())
    gsum = float(gross.sum())
    if tsum == 0:
        return float("inf") if gsum > 0 else float("nan")
    return gsum / tsum * 1e4
