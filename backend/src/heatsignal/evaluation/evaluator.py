"""
Evaluador/Juez: determina si una StrategyHypothesis tiene edge estadístico
real (Bloqueante C) — el núcleo de honestidad estadística del producto
(ver CLAUDE.md, "Honestidad estadística" y "Anti-p-hacking").

Un win-rate solo no dice nada. Este módulo exige, antes de aprobar cualquier
hipótesis: tamaño de muestra mínimo, significancia (binomial test vs 0.5),
intervalo de confianza de Wilson, Sharpe mínimo, y superar los 3 baselines
pasivos (`baseline.py`) en un criterio COMPUESTO — avg_return mayor, Sharpe
mayor y max_drawdown menor en valor absoluto, no alcanza con ganar en una
sola métrica (ver `_beats_baseline`). El criterio (`Evaluator.PASS_*`) es
fijo y no se relaja caso por caso: relajarlo ad-hoc es exactamente el
mecanismo del p-hacking que este módulo existe para prevenir.

Contrato de integración: se asume que el `strategy_id` recibido ya fue
pre-registrado en `StrategyRegistry` antes de llamar a `evaluate()` — este
módulo no lo valida (evitamos acoplar el Evaluador a esa persistencia); es
responsabilidad de quien orquesta el pipeline (grafo LangGraph) garantizarlo.
"""

from __future__ import annotations

import math
import statistics
from decimal import Decimal
from uuid import UUID

from pydantic import BaseModel
from scipy.stats import binomtest, norm

from ..schema.signals import Signal
from .baseline import AlwaysLongBaseline, BaselineStrategy, MomentumBaseline, RandomBaseline

_SECONDS_PER_YEAR = 365 * 24 * 3600


class EvalResult(BaseModel):
    """Resultado de evaluar una StrategyHypothesis contra sus señales resueltas."""

    strategy_id: UUID
    n_signals: int
    win_rate: float
    avg_return: float
    sharpe_ratio: float
    max_drawdown: float
    profit_factor: float
    wilson_ci: tuple[float, float]
    p_value: float
    vs_random: dict[str, float]
    vs_always_long: dict[str, float]
    vs_momentum: dict[str, float]
    passed: bool
    reason: str | None = None


def _normalize(signals: list[Signal]) -> list[float]:
    """Extrae y valida la serie de retornos (`pnl_pct`) de una lista de señales.

    Punto único de "sin fallos silenciosos" para el input del Evaluador: una
    señal con `pnl_pct` no finito (NaN/inf, p.ej. de una división por cero
    aguas arriba) revienta acá explícito en vez de ensuciar en silencio
    win_rate/sharpe/wilson_ci con NaN.
    """
    normalized: list[float] = []
    for s in signals:
        pnl_pct = float(s.pnl_pct)
        if not math.isfinite(pnl_pct):
            raise ValueError(f"signal {s.signal_id} tiene pnl_pct no finito: {s.pnl_pct}")
        normalized.append(pnl_pct)
    return normalized


def wilson_ci(successes: int, n: int, confidence: float = 0.95) -> tuple[float, float]:
    """Intervalo de confianza de Wilson para una proporción (ej. win_rate).

    Preferido sobre el intervalo normal ingenuo: no colapsa ni se sale de
    [0, 1] con n chico o proporciones cerca de 0/1 — justo el régimen en el
    que vive este proyecto antes de acumular n>=30.
    """
    if n == 0:
        return (0.0, 1.0)  # sin datos: incertidumbre total, no 0/0
    z = float(norm.ppf(1 - (1 - confidence) / 2))
    phat = successes / n
    denom = 1 + z**2 / n
    centre = phat + z**2 / (2 * n)
    adj = z * math.sqrt(phat * (1 - phat) / n + z**2 / (4 * n**2))
    lower = (centre - adj) / denom
    upper = (centre + adj) / denom
    return (max(0.0, lower), min(1.0, upper))


def _annualization_factor(signals: list[Signal]) -> float:
    """Estima "trades/año" a partir de la duración de holding promedio de la muestra.

    Las señales son event-driven (no tienen periodicidad fija como barras
    diarias), así que no hay un "N por año" dado de antemano. Se estima como
    segundos_por_año / duración_promedio_de_holding_en_segundos. Con datos
    insuficientes para estimarlo, no se anualiza (factor 1.0).
    """
    holding_seconds = [
        (s.exit_time - s.entry_time).total_seconds() for s in signals if s.exit_time > s.entry_time
    ]
    if not holding_seconds:
        return 1.0
    avg_holding = statistics.mean(holding_seconds)
    if avg_holding <= 0:
        return 1.0
    return _SECONDS_PER_YEAR / avg_holding


def _sharpe_ratio(returns_pct: list[float], signals_chronological: list[Signal]) -> float:
    """Sharpe anualizado, risk_free=0. Requiere >=2 retornos para tener varianza."""
    if len(returns_pct) < 2:
        return 0.0
    mean = statistics.mean(returns_pct)
    stdev = statistics.stdev(returns_pct)
    if stdev == 0:
        return 0.0
    factor = _annualization_factor(signals_chronological)
    return (mean / stdev) * math.sqrt(factor)


def _max_drawdown(returns_pct_chronological: list[float]) -> float:
    """Drawdown máximo (fracción negativa, ej. -0.23 = -23%) sobre la curva de equity compuesta."""
    equity = 1.0
    peak = 1.0
    max_dd = 0.0
    for r in returns_pct_chronological:
        equity *= 1.0 + r / 100.0
        peak = max(peak, equity)
        dd = (equity - peak) / peak if peak > 0 else 0.0
        max_dd = min(max_dd, dd)
    return max_dd


def _compute_raw_metrics(
    returns_pct_chronological: list[float],
    pnls_chronological: list[Decimal],
    signals_chronological: list[Signal],
) -> dict[str, float]:
    """Métricas compartidas por la estrategia real y cada baseline simulado."""
    win_rate = sum(1 for r in returns_pct_chronological if r > 0) / len(returns_pct_chronological)
    avg_return = statistics.mean(returns_pct_chronological)
    sharpe = _sharpe_ratio(returns_pct_chronological, signals_chronological)
    max_dd = _max_drawdown(returns_pct_chronological)

    # Ratio estadístico, no un precio: se cae a float acá adrede.
    gross_profit = sum(float(p) for p in pnls_chronological if p > 0)
    gross_loss = abs(sum(float(p) for p in pnls_chronological if p < 0))
    if gross_loss > 0:
        profit_factor = gross_profit / gross_loss
    else:
        profit_factor = math.inf if gross_profit > 0 else 0.0

    return {
        "win_rate": win_rate,
        "avg_return": avg_return,
        "sharpe_ratio": sharpe,
        "max_drawdown": max_dd,
        "profit_factor": profit_factor,
    }


def _sharpe_is_defined(returns_pct: list[float]) -> bool:
    """Espejo de la condición de `_sharpe_ratio`: con <2 retornos o varianza cero
    el 0.0 que devuelve no es un Sharpe real, es un valor no comparable."""
    return len(returns_pct) >= 2 and statistics.stdev(returns_pct) != 0


def _beats_baseline(
    strategy_returns: list[float],
    strategy_metrics: dict[str, float],
    baseline_returns: list[float],
    baseline_metrics: dict[str, float],
) -> bool:
    """Criterio compuesto: "supera al baseline" exige LAS TRES, no una sola:
      1. avg_return estrictamente mayor.
      2. sharpe_ratio estrictamente mayor.
      3. max_drawdown estrictamente menor en valor absoluto.

    Si el Sharpe no está definido para alguno de los dos lados (varianza cero
    o <2 señales, ver `_sharpe_is_defined`), esa pata se omite del AND en vez
    de comparar un 0.0 degenerado que no es un Sharpe real — se evalúa con las
    métricas que sí están disponibles.
    """
    if strategy_metrics["avg_return"] <= baseline_metrics["avg_return"]:
        return False
    if abs(strategy_metrics["max_drawdown"]) >= abs(baseline_metrics["max_drawdown"]):
        return False
    if _sharpe_is_defined(strategy_returns) and _sharpe_is_defined(baseline_returns):
        if strategy_metrics["sharpe_ratio"] <= baseline_metrics["sharpe_ratio"]:
            return False
    return True


class Evaluator:
    """Evaluador/Juez: mide si una StrategyHypothesis tiene edge real."""

    PASS_MIN_N = 30
    PASS_MAX_P_VALUE = 0.05
    PASS_MIN_WILSON_LOWER = 0.50
    PASS_MIN_SHARPE = 1.0

    def __init__(self, seed: int = 42) -> None:
        self._seed = seed

    def evaluate(
        self,
        strategy_id: UUID,
        signals: list[Signal],
        historical_prices: list[float],
    ) -> EvalResult:
        n = len(signals)
        if n == 0:
            return EvalResult(
                strategy_id=strategy_id,
                n_signals=0,
                win_rate=0.0,
                avg_return=0.0,
                sharpe_ratio=0.0,
                max_drawdown=0.0,
                profit_factor=0.0,
                wilson_ci=(0.0, 1.0),
                p_value=1.0,
                vs_random={},
                vs_always_long={},
                vs_momentum={},
                passed=False,
                reason="insufficient_data",
            )

        mismatched = [s.signal_id for s in signals if s.strategy_id != strategy_id]
        if mismatched:
            raise ValueError(
                f"signals {mismatched} pertenecen a otro strategy_id, no a {strategy_id}"
            )

        ordered = sorted(signals, key=lambda s: s.entry_time)
        returns = _normalize(ordered)
        pnls = [s.pnl for s in ordered]

        raw = _compute_raw_metrics(returns, pnls, ordered)
        wins = sum(1 for r in returns if r > 0)
        ci = wilson_ci(wins, n)
        p_value = float(binomtest(wins, n, p=0.5, alternative="two-sided").pvalue)

        baselines: dict[str, BaselineStrategy] = {
            "vs_random": RandomBaseline(seed=self._seed),
            "vs_always_long": AlwaysLongBaseline(seed=self._seed),
            "vs_momentum": MomentumBaseline(seed=self._seed),
        }
        deltas: dict[str, dict[str, float]] = {}
        beats: dict[str, bool] = {}
        for key, baseline in baselines.items():
            baseline_metrics, baseline_returns = self._simulate_baseline(
                baseline, ordered, historical_prices
            )
            deltas[key] = {metric: raw[metric] - baseline_metrics[metric] for metric in raw}
            beats[key] = _beats_baseline(returns, raw, baseline_returns, baseline_metrics)

        failures: list[str] = []
        if n < self.PASS_MIN_N:
            failures.append("insufficient_data")
        if p_value >= self.PASS_MAX_P_VALUE:
            failures.append("not_significant")
        if ci[0] <= self.PASS_MIN_WILSON_LOWER:
            failures.append("wilson_ci_lower_bound_not_above_50")
        if raw["sharpe_ratio"] <= self.PASS_MIN_SHARPE:
            failures.append("sharpe_below_threshold")
        if not beats["vs_random"]:
            failures.append("does_not_beat_random_baseline")
        if not beats["vs_always_long"]:
            failures.append("does_not_beat_always_long_baseline")
        if not beats["vs_momentum"]:
            failures.append("does_not_beat_momentum_baseline")

        passed = not failures
        reason = "insufficient_data" if n < self.PASS_MIN_N else ("; ".join(failures) or None)

        return EvalResult(
            strategy_id=strategy_id,
            n_signals=n,
            win_rate=raw["win_rate"],
            avg_return=raw["avg_return"],
            sharpe_ratio=raw["sharpe_ratio"],
            max_drawdown=raw["max_drawdown"],
            profit_factor=raw["profit_factor"],
            wilson_ci=ci,
            p_value=p_value,
            vs_random=deltas["vs_random"],
            vs_always_long=deltas["vs_always_long"],
            vs_momentum=deltas["vs_momentum"],
            passed=passed,
            reason=reason,
        )

    def _simulate_baseline(
        self,
        baseline: BaselineStrategy,
        ordered_signals: list[Signal],
        historical_prices: list[float],
    ) -> tuple[dict[str, float], list[float]]:
        """Simula qué hubiera devuelto `baseline` en las mismas ventanas entrada/salida.

        Mismo trade (misma entrada/salida) que la señal real; el baseline solo
        decide el lado. Si coincide con el lado real, hereda su PnL; si es el
        lado opuesto, se espeja (-PnL) — aproximación que ignora fees/funding,
        deliberada para aislar "la dirección elegida tiene edge" del timing.

        Devuelve las métricas agregadas y también la serie de retornos cruda,
        porque `_beats_baseline` necesita esta última para decidir si el
        Sharpe del baseline está definido (ver `_sharpe_is_defined`).
        """
        baseline.reset()
        b_returns: list[float] = []
        b_pnls: list[Decimal] = []
        for i, s in enumerate(ordered_signals):
            context = historical_prices[: i + 1]
            same_side = baseline.signal(context) == s.direction
            b_returns.append(s.pnl_pct if same_side else -s.pnl_pct)
            b_pnls.append(s.pnl if same_side else -s.pnl)
        return _compute_raw_metrics(b_returns, b_pnls, ordered_signals), b_returns
