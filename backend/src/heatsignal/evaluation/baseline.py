"""
Baselines pasivos: la referencia contra la que se mide el edge de cualquier
StrategyHypothesis real (Bloqueante C). Un win-rate crudo no significa nada
sin esto — un agente "long-only" en un mercado con sesgo alcista estructural
supera el 50% sin tener edge real.

Convención de `signal()`: recibe la serie de precios de cierre conocida hasta
el momento de decidir (orden cronológico, último elemento = precio más
reciente disponible). No hay un tipo `Candle` (OHLC) en el proyecto todavía,
así que "vela verde/roja" se aproxima comparando los dos últimos precios de
cierre de esa serie. Si más adelante se define un tipo OHLC real, reemplazar
esa aproximación por el open/close real de la vela.
"""

from __future__ import annotations

import random
from abc import ABC, abstractmethod

from ..schema.signals import Direction


class BaselineStrategy(ABC):
    """Estrategia pasiva de referencia. Reproducible: mismo seed -> misma secuencia."""

    def __init__(self, seed: int = 42) -> None:
        self._seed = seed
        self._rng = random.Random(seed)

    @abstractmethod
    def signal(self, price_history: list[float]) -> Direction:
        """Dada la serie de cierres conocida hasta ahora, devuelve LONG o SHORT."""
        ...

    def reset(self) -> None:
        """Reinicia el generador reproducible al seed original."""
        self._rng = random.Random(self._seed)


class RandomBaseline(BaselineStrategy):
    """Long/short al azar, 50/50. Seed fijo -> misma secuencia siempre."""

    def signal(self, price_history: list[float]) -> Direction:
        return Direction.LONG if self._rng.random() < 0.5 else Direction.SHORT


class AlwaysLongBaseline(BaselineStrategy):
    """Siempre long. Captura el sesgo alcista estructural de cripto."""

    def signal(self, price_history: list[float]) -> Direction:
        return Direction.LONG


class MomentumBaseline(BaselineStrategy):
    """Sigue la última vela: cierre sube (verde) -> LONG, cierre baja (roja) -> SHORT.

    Con menos de 2 precios conocidos no hay vela anterior que seguir; cae a LONG.
    """

    def signal(self, price_history: list[float]) -> Direction:
        if len(price_history) < 2:
            return Direction.LONG
        return Direction.LONG if price_history[-1] >= price_history[-2] else Direction.SHORT
