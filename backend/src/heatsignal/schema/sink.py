"""
Sink: destino de los eventos normalizados. El worker NO conoce el backend concreto
(Redis, TimescaleDB, cola); solo llama a `write`. Esto permite testear el worker con
un sink de consola y en produccion inyectar el real sin tocar el worker.
"""

from __future__ import annotations

import logging
from typing import Protocol

from .events import IngestEvent

log = logging.getLogger("heatsignal.ingestion.sink")


class EventSink(Protocol):
    async def write(self, event: IngestEvent) -> None:
        ...


class ConsoleSink:
    """Sink de desarrollo: imprime el evento normalizado. Util para validar adaptadores."""

    async def write(self, event: IngestEvent) -> None:
        log.info("%s", event.model_dump_json())


class BufferedSink:
    """
    Envuelve otro sink y agrupa en memoria para inspeccion/tests.
    En produccion se reemplaza por un sink a Redis Streams / Timescale.
    """

    def __init__(self, inner: EventSink | None = None, keep_last: int = 1000) -> None:
        self._inner = inner
        self._keep_last = keep_last
        self.buffer: list[IngestEvent] = []

    async def write(self, event: IngestEvent) -> None:
        self.buffer.append(event)
        if len(self.buffer) > self._keep_last:
            self.buffer.pop(0)
        if self._inner is not None:
            await self._inner.write(event)
