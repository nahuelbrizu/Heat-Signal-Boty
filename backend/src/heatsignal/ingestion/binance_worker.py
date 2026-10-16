"""
Worker de ingesta de Binance USDⓈ-M Futures para HeatSignal.

Un solo worker multiplexa tres tipos de stream por conexion:
  - forceOrder  (liquidaciones)         -> LiquidationEvent  [coverage PARTIAL]
  - aggTrade    (trades del agresor)    -> TradeEvent
  - markPrice   (funding + mark price)  -> TickerEvent

Usa el conector OFICIAL de Binance: `binance-futures-connector`
    pip install binance-futures-connector
    from binance.websocket.um_futures.websocket_client import UMFuturesWebsocketClient

Notas de diseno (verificadas contra la doc de Binance, jul 2026):
  - `forceOrder` NO empuja todas: "for each symbol, only the latest one liquidation
    order within 1000ms will be pushed" (docstring del conector oficial, oct 2026)
    -> coverage = PARTIAL (<=1/seg por simbolo, igual que OKX).
  - Rutas WS (verificado oct 2026): Binance separo los streams de Futures en
    `/market` (aggTrade, markPrice, forceOrder) y `/public` (depth, bookTicker).
    El path legacy `/ws` / `/stream` acepta el handshake pero NO manda datos.
    Por eso `stream_url` apunta a `/market` (el conector le agrega `/stream`).
    Referencia: hummingbot `data_feed/liquidations_feed/binance/constants.py`.
  - El conector responde el ping/pong del server automaticamente.
  - Una conexion admite hasta 1024 streams; multiplexar aca es correcto.
  - El callback del conector es SINCRONO y corre en su propio hilo. Para llegar al
    sink async usamos run_coroutine_threadsafe contra el loop principal.
  - Reconexion: el objetivo es que si la conexion muere, se reconstruya sola con
    backoff exponencial + jitter, re-suscribiendo todos los simbolos.
  - Watchdog de socket zombie: `on_close` no dispara si el socket queda "vivo" a
    nivel TCP pero deja de recibir datos. Un watchdog aparte mide el tiempo desde
    el ultimo mensaje (`message_timeout_s`, default 60s) y fuerza reconexion si
    se excede.

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

# Ruta "market" de Binance USDⓈ-M Futures (aggTrade, markPrice, forceOrder).
MARKET_STREAM_URL = "wss://fstream.binance.com/market"


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
        message_timeout_s: float = 60.0,
        watchdog_poll_s: float = 5.0,
    ) -> None:
        self._symbols = [s.upper() for s in symbols]
        self._sink = sink
        self._base_backoff = base_backoff_s
        self._max_backoff = max_backoff_s
        self._message_timeout_s = message_timeout_s
        self._watchdog_poll_s = watchdog_poll_s
        self._loop: asyncio.AbstractEventLoop | None = None
        self._client: UMFuturesWebsocketClient | None = None
        self._stop = asyncio.Event()
        self._connection_dead = asyncio.Event()
        self._last_message_at: float = 0.0

    # ---- bridge sincrono -> async -------------------------------------------

    def _on_message(self, _client: Any, raw: str) -> None:
        """Callback del conector (hilo del websocket). Parsear y despachar al sink.

        Debe ser rapido y NO lanzar: cualquier excepcion aca puede matar el hilo del
        stream en silencio. Por eso todo va envuelto y logueado.
        """
        self._last_message_at = time.monotonic()  # cualquier frame cuenta como "vivo"
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
                    coverage=Coverage.PARTIAL,  # forceOrder: <=1 por simbolo cada 1000ms
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
            stream_url=MARKET_STREAM_URL,
            on_message=self._on_message,
            on_close=_on_close,
            is_combined=True,
        )
        self._subscribe_all(client)
        return client

    # ---- watchdog de socket zombie -------------------------------------------

    def _is_connection_stale(self) -> bool:
        """True si no llegó ningún mensaje en `_message_timeout_s` segundos.

        `on_close` del conector no cubre el caso de un socket zombie (la
        conexión TCP sigue "viva" pero Binance dejó de empujar datos, o el
        cierre no se propaga a `on_close` por lo que sea): este es el único
        chequeo que lo detecta.
        """
        return time.monotonic() - self._last_message_at > self._message_timeout_s

    async def _watchdog(self) -> None:
        """Sondea periódicamente y marca `_connection_dead` si detecta un socket zombie."""
        while not self._stop.is_set():
            await asyncio.sleep(self._watchdog_poll_s)
            if self._is_connection_stale():
                idle_s = time.monotonic() - self._last_message_at
                log.warning(
                    "sin mensajes de Binance en %.0fs (limite %.0fs) - posible socket "
                    "zombie, forzando reconexion",
                    idle_s,
                    self._message_timeout_s,
                )
                self._connection_dead.set()
                return

    async def run(self) -> None:
        """Corre para siempre: conecta, vigila, y reconecta con backoff+jitter."""
        self._loop = asyncio.get_running_loop()
        attempt = 0

        while not self._stop.is_set():
            self._connection_dead.clear()
            try:
                log.info("conectando worker Binance (intento %d)", attempt + 1)
                self._client = self._open_client()
                self._last_message_at = time.monotonic()  # arranca el reloj del watchdog aca
                attempt = 0  # conexion abierta: resetear backoff

                # Vigilar hasta que la conexion muera, el watchdog detecte un socket
                # zombie, o pidan stop.
                stop_task = asyncio.create_task(self._stop.wait())
                dead_task = asyncio.create_task(self._connection_dead.wait())
                watchdog_task = asyncio.create_task(self._watchdog())
                await asyncio.wait(
                    {stop_task, dead_task, watchdog_task}, return_when=asyncio.FIRST_COMPLETED
                )
                for t in (stop_task, dead_task, watchdog_task):
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
