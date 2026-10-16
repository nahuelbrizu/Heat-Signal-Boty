"""
Metrica de uptime/salud por worker (criterio de salida de Fase 1: "metrica de uptime por worker").

Minimo y en-proceso, sin tocar las clases de los workers:
  - `CountingSink` envuelve el sink de cada worker y registra cada evento (ultimo mensaje, conteo por kind).
  - `connected` = "recibio datos en los ultimos `stale_after_s`". Es la definicion que importa aguas abajo:
    un socket abierto pero mudo (zombie) cuenta como CAIDO, no como sano.
  - `reconnects` se obtiene escuchando el log "conectando worker ... (intento N)" que ya emiten los workers
    (`ReconnectLogHandler`). Es un acople por convencion de log: si un worker cambia el mensaje, deja de
    contar. TODO: reemplazar por un hook explicito (`on_connect`) cuando se toquen los workers.
  - `uptime_ratio` = fraccion del tiempo muestreado en que el worker estuvo `connected`.

`HealthRegistry.run_sampler()` muestrea cada `sample_s` y loguea un resumen cada `log_every_s`.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from ..schema.events import IngestEvent
from ..schema.sink import EventSink

log = logging.getLogger("heatsignal.ingestion.health")


@dataclass
class WorkerHealth:
    name: str
    started_at: float
    connected: bool = False
    last_message_at: float | None = None      # reloj monotonic
    connect_attempts: int = 0
    events_by_kind: Counter[str] = field(default_factory=Counter)
    up_s: float = 0.0
    sampled_s: float = 0.0

    @property
    def reconnects(self) -> int:
        return max(0, self.connect_attempts - 1)

    @property
    def uptime_ratio(self) -> float | None:
        return None if self.sampled_s <= 0 else self.up_s / self.sampled_s

    def snapshot(self, now: float) -> dict[str, Any]:
        ratio = self.uptime_ratio
        return {
            "worker": self.name,
            "connected": self.connected,
            "last_message_age_s": None if self.last_message_at is None
            else round(now - self.last_message_at, 1),
            "reconnects": self.reconnects,
            "uptime_ratio": None if ratio is None else round(ratio, 4),
            "events": dict(self.events_by_kind),
        }


class HealthRegistry:
    def __init__(self, *, stale_after_s: float = 30.0, clock: Callable[[], float] = time.monotonic):
        self._stale_after_s = stale_after_s
        self._clock = clock
        self.workers: dict[str, WorkerHealth] = {}
        self._last_sample: float | None = None

    def register(self, name: str) -> WorkerHealth:
        if name not in self.workers:
            self.workers[name] = WorkerHealth(name=name, started_at=self._clock())
        return self.workers[name]

    def record_event(self, name: str, kind: str) -> None:
        h = self.register(name)
        h.last_message_at = self._clock()
        h.events_by_kind[kind] += 1

    def record_connect_attempt(self, name: str) -> None:
        self.register(name).connect_attempts += 1

    def sample(self) -> None:
        """Actualiza `connected` y acumula uptime desde la muestra anterior."""
        now = self._clock()
        dt = 0.0 if self._last_sample is None else now - self._last_sample
        self._last_sample = now
        for h in self.workers.values():
            was = h.connected
            h.connected = (
                h.last_message_at is not None and now - h.last_message_at <= self._stale_after_s
            )
            if dt > 0:
                h.sampled_s += dt
                if was and h.connected:
                    h.up_s += dt
            if was and not h.connected:
                log.warning("worker %s sin datos hace >%.0fs - marcado CAIDO", h.name, self._stale_after_s)
            elif h.connected and not was:
                log.info("worker %s recibiendo datos", h.name)

    def snapshot(self) -> list[dict[str, Any]]:
        now = self._clock()
        return [h.snapshot(now) for h in self.workers.values()]

    def log_summary(self) -> None:
        for snap in self.snapshot():
            level = logging.INFO if snap["connected"] else logging.WARNING
            log.log(level, "health %s", snap)

    async def run_sampler(self, *, sample_s: float = 5.0, log_every_s: float = 60.0) -> None:
        last_log = self._clock()
        while True:
            await asyncio.sleep(sample_s)
            self.sample()
            if self._clock() - last_log >= log_every_s:
                last_log = self._clock()
                self.log_summary()


class CountingSink:
    """Envuelve el sink de un worker y registra cada evento en el `HealthRegistry`."""

    def __init__(self, inner: EventSink, registry: HealthRegistry, worker: str) -> None:
        self._inner = inner
        self._registry = registry
        self._worker = worker
        registry.register(worker)

    async def write(self, event: IngestEvent) -> None:
        self._registry.record_event(self._worker, event.kind)
        await self._inner.write(event)


_CONNECT_RE = re.compile(r"conectando worker (\w+)", re.IGNORECASE)


class ReconnectLogHandler(logging.Handler):
    """Cuenta intentos de conexion a partir del log "conectando worker <Nombre>" de cada worker."""

    def __init__(self, registry: HealthRegistry, names: dict[str, str]) -> None:
        """`names`: nombre en el log (en minusculas, p.ej. "bybit") -> nombre en el registry."""
        super().__init__(level=logging.INFO)
        self._registry = registry
        self._names = names

    def emit(self, record: logging.LogRecord) -> None:
        try:
            m = _CONNECT_RE.search(record.getMessage())
        except Exception:  # noqa: BLE001 - un handler de log nunca debe lanzar
            return
        if m:
            name = self._names.get(m.group(1).lower())
            if name is not None:
                self._registry.record_connect_attempt(name)
