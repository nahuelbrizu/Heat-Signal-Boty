"""
Worker de ingesta de Bybit V5 (linear / USDT perps) para HeatSignal.

Un solo worker multiplexa tres topics por simbolo en una conexion:
  - allLiquidation.{symbol}  (liquidaciones)  -> LiquidationEvent  [coverage FULL]
  - publicTrade.{symbol}     (trades)         -> TradeEvent
  - tickers.{symbol}         (mark/funding/OI)-> TickerEvent

Endpoint publico: wss://stream.bybit.com/v5/public/linear (sin API key).

Notas de diseno (verificadas contra la doc oficial de Bybit V5, oct 2026):
  - `allLiquidation` empuja TODAS las liquidaciones (cada 500ms, en array) -> FULL.
    NO usar `liquidation.{symbol}` (deprecado, <=1/seg por simbolo).
  - ⚠️ En `allLiquidation`, `S` es el lado de la POSICION liquidada: "When you receive a
    `Buy` update, this means that a long position has been liquidated". En Binance
    (`forceOrder`) `S` es el lado de la ORDEN de liquidacion. Nuestro esquema usa el lado
    de la orden, asi que aca se INVIERTE: Buy -> SELL, Sell -> BUY.
  - `p` de `allLiquidation` es el precio de quiebra (bankruptcy price), no el de ejecucion.
  - `publicTrade`: `S` es el lado del taker, sin invertir. `BT` marca block trades.
  - `tickers` (linear) manda un `snapshot` y despues `delta`s: "If a response param is not
    found in the message, then its value has not changed". Se mantiene un cache por simbolo
    y cada delta se mergea sobre el; el cache se limpia en cada reconexion (el server manda
    un snapshot nuevo al re-suscribir).
  - Heartbeat: Bybit pide `{"op": "ping"}` a nivel aplicacion cada 20s; sin ping ni datos
    corta la conexion a los 10 min.
  - Referencias de implementacion: NautilusTrader `crates/adapters/bybit/src/websocket/`
    y Hummingbot `connector/derivative/bybit_perpetual/`.

A diferencia del worker de Binance (conector oficial con callbacks en otro hilo), este
worker es asyncio puro sobre `websockets`.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import time
from typing import Any

import websockets

from ..schema.events import (
    Coverage,
    Exchange,
    IngestEvent,
    LiquidationEvent,
    Side,
    TickerEvent,
    TradeEvent,
)
from ..schema.sink import EventSink
from .binance_worker import _norm_symbol, _now_ms

log = logging.getLogger("heatsignal.ingestion.bybit")

PUBLIC_LINEAR_URL = "wss://stream.bybit.com/v5/public/linear"

# Campos de `tickers` (linear) que usamos -> campo de TickerEvent.
_TICKER_FIELDS = {
    "markPrice": "mark_price",
    "lastPrice": "last_price",
    "fundingRate": "funding_rate",
    "openInterest": "open_interest",
    "openInterestValue": "open_interest_usd",
}


def _liquidation_order_side(position_side: str) -> Side:
    """Bybit informa el lado de la posicion liquidada; el esquema guarda el de la orden.

    Long liquidado (`Buy`) -> la liquidacion es una orden de VENTA, y viceversa.
    """
    raw = str(position_side).capitalize()
    if raw == "Buy":
        return Side.SELL
    if raw == "Sell":
        return Side.BUY
    raise ValueError(f"lado de posicion invalido: {position_side!r}")


def _taker_side(raw: str) -> Side:
    raw = str(raw).capitalize()
    if raw == "Buy":
        return Side.BUY
    if raw == "Sell":
        return Side.SELL
    raise ValueError(f"lado de taker invalido: {raw!r}")


def _opt_float(value: Any) -> float | None:
    return float(value) if value not in (None, "") else None


class BybitWorker:
    """Worker multiplexado de Bybit V5 linear.

    Uso:
        worker = BybitWorker(symbols=["BTCUSDT", "SOLUSDT"], sink=my_sink)
        await worker.run()   # corre para siempre, reconectando solo
    """

    def __init__(
        self,
        symbols: list[str],
        sink: EventSink,
        *,
        url: str = PUBLIC_LINEAR_URL,
        max_backoff_s: float = 60.0,
        base_backoff_s: float = 1.0,
        message_timeout_s: float = 60.0,
        ping_interval_s: float = 20.0,
    ) -> None:
        self._symbols = [s.upper() for s in symbols]
        self._sink = sink
        self._url = url
        self._base_backoff = base_backoff_s
        self._max_backoff = max_backoff_s
        self._message_timeout_s = message_timeout_s
        self._ping_interval_s = ping_interval_s
        self._stop = asyncio.Event()
        self._ws: Any = None
        # Estado mergeado de `tickers` por simbolo crudo de Bybit ("BTCUSDT").
        self._tickers: dict[str, dict[str, Any]] = {}

    # ---- suscripciones -------------------------------------------------------

    def _topics(self) -> list[str]:
        topics: list[str] = []
        for sym in self._symbols:
            topics += [f"allLiquidation.{sym}", f"publicTrade.{sym}", f"tickers.{sym}"]
        return topics

    def _subscribe_message(self) -> str:
        return json.dumps({"op": "subscribe", "args": self._topics()})

    # ---- normalizacion (adaptador Bybit -> esquema comun) --------------------

    def _normalize(self, msg: dict) -> list[IngestEvent]:
        """Traduce un mensaje de Bybit a 0..N eventos del esquema comun.

        Los mensajes de control (`op`: subscribe/ping/pong) no producen eventos; una
        suscripcion rechazada se loguea como error (no es un hueco silencioso).
        """
        if "op" in msg:
            if msg.get("op") == "subscribe" and not msg.get("success", False):
                log.error("Bybit rechazo la suscripcion: %s", msg.get("ret_msg"))
            return []

        topic = str(msg.get("topic", ""))
        channel = topic.split(".", 1)[0]
        try:
            if channel == "allLiquidation":
                return [self._liquidation(d) for d in msg.get("data", [])]
            if channel == "publicTrade":
                return [self._trade(d) for d in msg.get("data", [])]
            if channel == "tickers":
                event = self._ticker(msg)
                return [event] if event is not None else []
        except (KeyError, ValueError, TypeError) as exc:
            log.warning("no se pudo normalizar mensaje de %s: %s", topic, exc)
            return []
        if topic:
            log.debug("topic de Bybit no manejado: %s", topic)
        return []

    def _liquidation(self, d: dict) -> LiquidationEvent:
        price = float(d["p"])  # bankruptcy price
        qty = float(d["v"])
        return LiquidationEvent(
            source=Exchange.BYBIT,
            symbol=_norm_symbol(d["s"]),
            side=_liquidation_order_side(d["S"]),
            price=price,
            qty=qty,
            notional_usd=price * qty,
            ts_event_ms=int(d["T"]),
            ts_ingest_ms=_now_ms(),
            coverage=Coverage.FULL,  # allLiquidation empuja todas
        )

    def _trade(self, d: dict) -> TradeEvent:
        return TradeEvent(
            source=Exchange.BYBIT,
            symbol=_norm_symbol(d["s"]),
            side=_taker_side(d["S"]),
            price=float(d["p"]),
            qty=float(d["v"]),
            ts_event_ms=int(d["T"]),
            ts_ingest_ms=_now_ms(),
            is_block_trade=bool(d.get("BT", False)),
        )

    def _ticker(self, msg: dict) -> TickerEvent | None:
        data = msg["data"]
        sym = data["symbol"]
        if msg.get("type") == "snapshot":
            state: dict[str, Any] = {}
            self._tickers[sym] = state
        else:
            state = self._tickers.setdefault(sym, {})
        # Delta: un campo ausente significa "sin cambios" -> solo pisar los presentes.
        for raw_key in _TICKER_FIELDS:
            if raw_key in data:
                state[raw_key] = data[raw_key]
        return TickerEvent(
            source=Exchange.BYBIT,
            symbol=_norm_symbol(sym),
            ts_event_ms=int(msg.get("ts", _now_ms())),
            ts_ingest_ms=_now_ms(),
            **{field: _opt_float(state.get(raw)) for raw, field in _TICKER_FIELDS.items()},
        )

    # ---- ciclo de vida -------------------------------------------------------

    async def _safe_write(self, event: IngestEvent) -> None:
        try:
            await self._sink.write(event)
        except Exception:  # noqa: BLE001 - un fallo de sink no debe tumbar la ingesta
            log.exception("sink.write fallo para evento %s", event.kind)

    async def _ping_loop(self, ws: Any) -> None:
        while True:
            await asyncio.sleep(self._ping_interval_s)
            await ws.send(json.dumps({"op": "ping"}))

    async def _consume(self, ws: Any) -> None:
        """Lee mensajes hasta que la conexion muera o pase `message_timeout_s` sin datos.

        El timeout cumple el rol del watchdog de socket zombie del worker de Binance.
        """
        while not self._stop.is_set():
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=self._message_timeout_s)
            except asyncio.TimeoutError:
                log.warning(
                    "sin mensajes de Bybit en %.0fs - posible socket zombie, forzando reconexion",
                    self._message_timeout_s,
                )
                return
            try:
                msg = json.loads(raw)
            except (ValueError, TypeError):
                continue
            if not isinstance(msg, dict):
                continue
            for event in self._normalize(msg):
                await self._safe_write(event)

    async def _run_once(self) -> None:
        async with websockets.connect(
            self._url, open_timeout=10, close_timeout=2, ping_interval=None
        ) as ws:
            self._ws = ws
            self._tickers.clear()  # el server manda snapshots nuevos al re-suscribir
            await ws.send(self._subscribe_message())
            log.info("suscripto a %d simbolos x 3 topics", len(self._symbols))
            ping_task = asyncio.create_task(self._ping_loop(ws))
            try:
                await self._consume(ws)
            finally:
                ping_task.cancel()
                self._ws = None

    async def run(self) -> None:
        """Corre para siempre: conecta, consume y reconecta con backoff+jitter."""
        attempt = 0
        while not self._stop.is_set():
            started = time.monotonic()
            try:
                log.info("conectando worker Bybit (intento %d)", attempt + 1)
                await self._run_once()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                if self._stop.is_set():
                    # stop() cerro el socket a proposito: el ConnectionClosedOK que sigue no es un error.
                    log.debug("worker de Bybit cerrado por stop()", exc_info=True)
                else:
                    log.exception("error en el worker de Bybit")

            if self._stop.is_set():
                break

            # Una conexion que vivio un rato resetea el backoff.
            attempt = 0 if time.monotonic() - started > self._max_backoff else attempt + 1
            delay = min(self._max_backoff, self._base_backoff * (2 ** max(attempt - 1, 0)))
            delay = delay * (0.5 + random.random() / 2)  # jitter 50-100%
            log.info("reintentando en %.1fs", delay)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=delay)
            except asyncio.TimeoutError:
                pass

    async def stop(self) -> None:
        self._stop.set()
        if self._ws is not None:
            await self._ws.close()


# ---- ejemplo de ejecucion manual --------------------------------------------

async def _demo() -> None:
    from ..schema.sink import BufferedSink, ConsoleSink

    logging.basicConfig(level=logging.INFO)
    sink = BufferedSink(inner=ConsoleSink())
    worker = BybitWorker(symbols=["BTCUSDT", "SOLUSDT"], sink=sink)
    try:
        await worker.run()
    except KeyboardInterrupt:
        await worker.stop()


if __name__ == "__main__":
    asyncio.run(_demo())
