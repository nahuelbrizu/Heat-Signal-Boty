"""
FanoutSink: escribe cada evento en varios sinks (p.ej. Redis Streams + TimescaleDB).

Los fallos se aislan por sink: si uno lanza, los demas igual reciben el evento. Cada fallo se loguea y se
cuenta por sink en `errors` (sin fallos silenciosos), y nunca se propaga al worker de ingesta.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from ..schema.events import IngestEvent
from ..schema.sink import EventSink

log = logging.getLogger("heatsignal.storage.fanout")


def sink_name(sink: Any) -> str:
    return type(sink).__name__


class FanoutSink:
    def __init__(self, *sinks: EventSink) -> None:
        if not sinks:
            raise ValueError("FanoutSink necesita al menos un sink")
        self.sinks: tuple[EventSink, ...] = sinks
        self.errors: dict[int, int] = {i: 0 for i in range(len(sinks))}

    async def write(self, event: IngestEvent) -> None:
        if len(self.sinks) == 1:
            results: list[Any] = []
            try:
                await self.sinks[0].write(event)
            except Exception as exc:  # noqa: BLE001
                results = [exc]
        else:
            results = await asyncio.gather(
                *(s.write(event) for s in self.sinks), return_exceptions=True
            )
        for i, res in enumerate(results):
            if isinstance(res, BaseException):
                if isinstance(res, asyncio.CancelledError):
                    raise res
                self.errors[i] += 1
                log.error(
                    "FanoutSink: %s fallo escribiendo %s (fallos acumulados %d): %r",
                    sink_name(self.sinks[i]), event.kind, self.errors[i], res,
                )

    def error_counts(self) -> dict[str, int]:
        return {f"{i}:{sink_name(s)}": self.errors[i] for i, s in enumerate(self.sinks)}

    async def close(self) -> None:
        """Cierra cada sink que tenga `close()`, aislando fallos."""
        for s in self.sinks:
            close = getattr(s, "close", None)
            if close is None:
                continue
            try:
                await close()
            except Exception:  # noqa: BLE001
                log.exception("FanoutSink: error cerrando %s", sink_name(s))
