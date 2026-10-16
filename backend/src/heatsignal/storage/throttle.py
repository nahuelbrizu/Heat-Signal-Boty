"""
ThrottledSink: limita la frecuencia de ciertos tipos de evento antes de llegar a los sinks reales.

Motivo: Bybit empuja `tickers` cada 100ms; guardar 10 snapshots/seg por simbolo en Redis + Timescale no
aporta (funding/OI/mark cambian lento) y multiplica el storage.

Semantica (throttle con flanco de subida + bajada, "last-value-wins") por clave (kind, source, symbol):
  - Primer evento de una ventana -> pasa inmediatamente.
  - Eventos siguientes dentro de `min_interval_s` -> se guarda SOLO el ultimo (pisa al anterior).
  - Al terminar la ventana, el ultimo pendiente se emite -> el valor final nunca se pierde, solo se
    submuestrean los intermedios (se cuentan en `coalesced`, no es perdida silenciosa: es la politica).
  - `close()` emite los pendientes inmediatamente.
Los tipos que no estan en `min_interval_s_by_kind` (liquidaciones, trades) pasan sin tocar.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable

from ..schema.events import IngestEvent
from ..schema.sink import EventSink

log = logging.getLogger("heatsignal.storage.throttle")

_Key = tuple[str, str, str]


class ThrottledSink:
    def __init__(
        self,
        inner: EventSink,
        min_interval_s_by_kind: dict[str, float] | None = None,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._inner = inner
        self._intervals = dict(
            {"ticker": 1.0} if min_interval_s_by_kind is None else min_interval_s_by_kind
        )
        self._clock = clock
        self._last_emit: dict[_Key, float] = {}
        self._pending: dict[_Key, IngestEvent] = {}
        self._timers: dict[_Key, asyncio.Task[None]] = {}
        self.passed = 0      # eventos emitidos al sink interno (incluye no-throttleados)
        self.coalesced = 0   # eventos intermedios reemplazados por uno mas nuevo (submuestreo)

    @property
    def inner(self) -> EventSink:
        return self._inner

    @staticmethod
    def _key(event: IngestEvent) -> _Key:
        return (event.kind, event.source.value, event.symbol)

    async def write(self, event: IngestEvent) -> None:
        interval = self._intervals.get(event.kind)
        if not interval or interval <= 0:
            await self._emit(event)
            return
        key = self._key(event)
        now = self._clock()
        last = self._last_emit.get(key)
        if key not in self._pending and (last is None or now - last >= interval):
            self._last_emit[key] = now
            await self._emit(event)
            return
        # Dentro de la ventana: last-value-wins.
        if key in self._pending:
            self.coalesced += 1
        self._pending[key] = event
        if key not in self._timers:
            delay = max(0.0, (last if last is not None else now) + interval - now)
            self._timers[key] = asyncio.create_task(self._flush_after(key, delay))

    async def _flush_after(self, key: _Key, delay: float) -> None:
        try:
            await asyncio.sleep(delay)
        except asyncio.CancelledError:
            self._timers.pop(key, None)
            raise
        self._timers.pop(key, None)
        event = self._pending.pop(key, None)
        if event is not None:
            self._last_emit[key] = self._clock()
            await self._emit(event)

    async def _emit(self, event: IngestEvent) -> None:
        self.passed += 1
        try:
            await self._inner.write(event)
        except Exception:  # noqa: BLE001 - el throttle no debe tumbar al worker ni a su timer
            log.exception("ThrottledSink: fallo el sink interno para %s", event.kind)

    async def flush(self) -> None:
        """Emite ya todos los pendientes (sin esperar el fin de la ventana)."""
        for task in list(self._timers.values()):
            task.cancel()
        self._timers.clear()
        pending, self._pending = self._pending, {}
        for key, event in pending.items():
            self._last_emit[key] = self._clock()
            await self._emit(event)

    async def close(self) -> None:
        await self.flush()
        close = getattr(self._inner, "close", None)
        if close is not None:
            await close()
