"""
Sink: destino de los eventos normalizados. El worker NO conoce el backend concreto
(Redis, TimescaleDB, cola); solo llama a `write`. Esto permite testear el worker con
un sink de consola y en produccion inyectar el real sin tocar el worker.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Protocol

from redis.asyncio import Redis

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


class RedisStreamSink:
    """Sink de producción: un Redis Stream por símbolo + tipo de evento.

    Reemplaza a `BufferedSink` sin que `BinanceWorker` (ni ningún otro worker)
    se entere — mismo protocolo `EventSink`. Stream key: `heatsignal:{symbol}:
    {event.kind}` (ej. `heatsignal:BTC-USDT:liquidation`) — separado por símbolo
    y tipo para que cada consumidor (Analista de Liquidaciones, Order Flow,
    Contexto de Mercado) lea solo lo que le importa, sin filtrar del lado del
    consumer ni cruzar símbolos en un stream compartido.

    Esto es el buffer de ventana de `phase-1-ingestion.md` ("Buffers/agregación
    en ventanas → Redis"), no la persistencia final — por eso se recorta a
    `maxlen` entradas aprox. por stream (`XADD ... MAXLEN ~`). La persistencia
    de largo plazo (TimescaleDB) sigue sin implementar.

    `write()` nunca propaga excepciones: un fallo de Redis en operación (cae la
    conexión, timeout, lo que sea) se loguea y se descarta — no debe tumbar la
    ingesta, mismo principio que el resto de los sinks de este módulo. Para
    arrancar, usar `RedisStreamSink.connect()` en vez del constructor directo:
    prueba conectividad primero y cae a `ConsoleSink` si Redis no responde, en
    vez de crashear el proceso de ingesta.
    """

    def __init__(self, redis_client: Redis, *, maxlen: int = 10_000) -> None:
        self._redis = redis_client
        self._maxlen = maxlen

    @classmethod
    async def connect(
        cls,
        url: str,
        *,
        maxlen: int = 10_000,
        max_connections: int = 10,
        connect_timeout_s: float = 2.0,
    ) -> EventSink:
        """Punto de entrada recomendado para producción.

        Prueba conectividad con `PING` antes de comprometerse a Redis como
        sink. Si no responde a tiempo (Redis caído, URL mal configurada, lo
        que sea), loguea un warning y devuelve `ConsoleSink` de fallback en
        vez de crashear el worker de ingesta al arrancar.
        """
        client = Redis.from_url(url, max_connections=max_connections)
        try:
            await asyncio.wait_for(client.ping(), timeout=connect_timeout_s)
        except Exception:  # noqa: BLE001 - cualquier fallo de arranque cae a ConsoleSink
            log.warning("Redis no disponible en %s - usando ConsoleSink de fallback", url, exc_info=True)
            await client.aclose()
            return ConsoleSink()
        return cls(client, maxlen=maxlen)

    @staticmethod
    def _stream_name(event: IngestEvent) -> str:
        return f"heatsignal:{event.symbol}:{event.kind}"

    async def write(self, event: IngestEvent) -> None:
        stream = self._stream_name(event)
        payload = json.dumps(event.model_dump(mode="json"))
        try:
            await self._redis.xadd(stream, {"data": payload}, maxlen=self._maxlen, approximate=True)
        except Exception:  # noqa: BLE001 - un fallo de Redis en operación no debe tumbar la ingesta
            log.error("RedisStreamSink.write fallo para stream %s", stream, exc_info=True)

    async def close(self) -> None:
        await self._redis.aclose()
