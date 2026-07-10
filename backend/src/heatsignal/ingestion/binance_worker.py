"""
Worker de ingesta de Binance USDⓈ-M Futures para HeatSignal.

Un solo worker multiplexa tres tipos de stream por conexion:
  - forceOrder  (liquidaciones)         -> LiquidationEvent  [coverage FULL]
  - aggTrade    (trades del agresor)    -> TradeEvent
  - markPrice   (funding + mark price)  -> TickerEvent

Usa el conector OFICIAL de Binance: `binance-futures-connector`
    pip install binance-futures-connector
    from binance.websocket.um_futures.websocket_client import UMFuturesWebsocketClient

Notas de diseno (verificadas contra la doc de Binance, jul 2026):
  - `forceOrder` empuja TODAS las liquidaciones -> coverage = FULL.
  - El conector responde el ping/pong del server automaticamente.
  - Una conexion admite hasta 1024 streams; multiplexar aca es correcto.
  - El callback del conector es SINCRONO y corre en su propio hilo. Para llegar al
    sink async usamos run_coroutine_threadsafe contra el loop principal.
  - Reconexion: el objetivo es que si la conexion muere, se reconstruya sola con
    backoff exponencial + jitter, re-suscribiendo todos los simbolos.

Este archivo es un esqueleto de produccion: la logica de normalizacion y el ciclo
de vida estan completos; ajustar detalles de campos si la doc del exchange cambia.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import time
from typing import Any

from binance.websocket.um_futures.websocket_client import UMFuturesWebsocketClient

from ..schema.events import (
    Coverage,
    Exchange,
    LiquidationEvent,
    Side,
    TickerEvent,
    TradeEvent,
)
from ..schema.sink import EventSink

log = logging.getLogger("heatsignal.ingestion.binance")


def _now_ms() -> int:
    return int(time.time() * 1000)


def _norm_symbol(raw: str) -> str:
    """Binance usa 'SOLUSDT'; el esquema interno usa 'SOL-USDT'.

    Heuristica simple para pares con quote USDT/USDC/USD. Para produccion,
    resolver contra el exchangeInfo en vez de adivinar el corte.
    """
    raw = raw.upper()
    for quote in ("USDT", "USDC", "USD"):
        if raw.endswith(quote) and len(raw) > len(quote):
            return f"{raw[:-len(quote)]}-{quote}"
    return raw


def _norm_side(raw: str) -> Side:
    return Side.BUY if str(raw).upper() == "BUY" else Side.SELL


class BinanceWorker:
    """Worker multiplexado de Binance USDⓈ-M.

    Uso:
        worker = BinanceWorker(symbols=["BTCUSDT", "SOLUSDT"], sink=my_sink)
        await worker.run()   # corre para siempre, reconectando solo
    """

    def __init__(
        self,
        symbols: list[str],
        sink: EventSink,
        *,
        max_backoff_s: float = 60.0,
        base_backoff_s: float = 1.0,
    ) -> None:
        self._symbols = [s.upper() for s in symbols]
        self._sink = sink
        self._base_backoff = base_backoff_s
        self._max_backoff = max_backoff_s
        self._loop: asyncio.AbstractEventLoop | None = None
        self._client: UMFuturesWebsocketClient | None = None
        self._stop = asyncio.Event()
        self._connection_dead = asyncio.Event()

    # ---- bridge sincrono -> async -------------------------------------------

    def _on_message(self, _client: Any, raw: str) -> None:
        """Callback del conector (hilo del websocket). Parsear y despachar al sink.

        Debe ser rapido y NO lanzar: cualquier excepcion aca puede matar el hilo del
        stream en silencio. Por eso todo va envuelto y logueado.
        """
        try:
            msg = json.loads(raw)
        except (ValueError, TypeError):
            return  # frames de control / no-JSON

        # Streams combinados llegan como {"stream": ..., "data": {...}}
        data = msg.get("data", msg) if isinstance(msg, dict) else msg
        event = self._normalize(data)
        if event is None:
            return

        loop = self._loop
        if loop is None:
            return
        # Empujar al sink async desde este hilo sincrono.
        asyncio.run_coroutine_threadsafe(self._safe_write(event), loop)

    async def _safe_write(self, event: Any) -> None:
        try:
            await self._sink.write(event)
        except Exception:  # noqa: BLE001 - un fallo de sink no debe tumbar la ingesta
            log.exception("sink.write fallo para evento %s", getattr(event, "kind", "?"))

    # ---- normalizacion (adaptador Binance -> esquema comun) -----------------

    def _normalize(self, d: dict) -> LiquidationEvent | TradeEvent | TickerEvent | None:
        etype = d.get("e")
        try:
            if etype == "forceOrder":
                o = d["o"]
                price = float(o["ap"] or o["p"])  # avg price; fallback a price
                qty = float(o["q"])
                return LiquidationEvent(
                    source=Exchange.BINANCE,
                    symbol=_norm_symbol(o["s"]),
                    side=_norm_side(o["S"]),
                    price=price,
                    qty=qty,
                    notional_usd=price * qty,
                    ts_event_ms=int(d.get("E", _now_ms())),
                    ts_ingest_ms=_now_ms(),
                    coverage=Coverage.FULL,  # forceOrder empuja todas
                )
            if etype == "aggTrade":
                price = float(d["p"])
                qty = float(d["q"])
                # En aggTrade, "m" = buyer is market maker -> el taker (agresor) es el lado opuesto.
                taker_side = Side.SELL if d.get("m") else Side.BUY
                return TradeEvent(
                    source=Exchange.BINANCE,
                    symbol=_norm_symbol(d["s"]),
                    side=taker_side,
                    price=price,
                    qty=qty,
                    ts_event_ms=int(d.get("E", _now_ms())),
                    ts_ingest_ms=_now_ms(),
                )
            if etype == "markPriceUpdate":
                return TickerEvent(
                    source=Exchange.BINANCE,
                    symbol=_norm_symbol(d["s"]),
                    mark_price=float(d["p"]) if d.get("p") is not None else None,
                    funding_rate=float(d["r"]) if d.get("r") not in (None, "") else None,
                    ts_event_ms=int(d.get("E", _now_ms())),
                    ts_ingest_ms=_now_ms(),
                )
        except (KeyError, ValueError, TypeError) as exc:
            log.warning("no se pudo normalizar evento %s: %s", etype, exc)
            return None
        return None  # tipo de evento no manejado

    # ---- ciclo de vida / suscripciones --------------------------------------

    def _subscribe_all(self, client: UMFuturesWebsocketClient) -> None:
        """(Re)suscribir todos los streams para todos los simbolos."""
        for sym in self._symbols:
            client.liquidation_order(symbol=sym)      # <symbol>@forceOrder
            client.agg_trade(symbol=sym)              # <symbol>@aggTrade
            client.mark_price(symbol=sym, speed=1)    # <symbol>@markPrice@1s
        log.info("suscripto a %d simbolos x 3 streams", len(self._symbols))

    def _open_client(self) -> UMFuturesWebsocketClient:
        # on_close marca la conexion como muerta para gatillar reconexion.
        def _on_close(*_a: Any) -> None:
            log.warning("conexion de Binance cerrada")
            loop = self._loop
            if loop is not None:
                loop.call_soon_threadsafe(self._connection_dead.set)

        client = UMFuturesWebsocketClient(
            on_message=self._on_message,
            on_close=_on_close,
            is_combined=True,
        )
        self._subscribe_all(client)
        return client

    async def run(self) -> None:
        """Corre para siempre: conecta, vigila, y reconecta con backoff+jitter."""
        self._loop = asyncio.get_running_loop()
        attempt = 0

        while not self._stop.is_set():
            self._connection_dead.clear()
            try:
                log.info("conectando worker Binance (intento %d)", attempt + 1)
                self._client = self._open_client()
                attempt = 0  # conexion abierta: resetear backoff

                # Vigilar hasta que la conexion muera o pidan stop.
                stop_task = asyncio.create_task(self._stop.wait())
                dead_task = asyncio.create_task(self._connection_dead.wait())
                await asyncio.wait(
                    {stop_task, dead_task}, return_when=asyncio.FIRST_COMPLETED
                )
                for t in (stop_task, dead_task):
                    t.cancel()

            except Exception:  # noqa: BLE001
                log.exception("error en el worker de Binance")
            finally:
                self._close_client()

            if self._stop.is_set():
                break

            # Backoff exponencial con jitter antes de reintentar.
            attempt += 1
            delay = min(self._max_backoff, self._base_backoff * (2 ** (attempt - 1)))
            delay = delay * (0.5 + random.random() / 2)  # jitter 50-100%
            log.info("reintentando en %.1fs", delay)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=delay)
            except asyncio.TimeoutError:
                pass

    def _close_client(self) -> None:
        if self._client is not None:
            try:
                self._client.stop()
            except Exception:  # noqa: BLE001
                log.debug("error cerrando cliente Binance", exc_info=True)
            self._client = None

    async def stop(self) -> None:
        self._stop.set()
        self._close_client()


# ---- ejemplo de ejecucion manual --------------------------------------------

async def _demo() -> None:
    from ..schema.sink import BufferedSink, ConsoleSink

    logging.basicConfig(level=logging.INFO)
    sink = BufferedSink(inner=ConsoleSink())
    worker = BinanceWorker(symbols=["BTCUSDT", "SOLUSDT"], sink=sink)
    try:
        await worker.run()
    except KeyboardInterrupt:
        await worker.stop()


if __name__ == "__main__":
    asyncio.run(_demo())
