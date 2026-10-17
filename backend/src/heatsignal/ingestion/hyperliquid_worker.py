"""
Worker de ingesta de Hyperliquid (perps) para HeatSignal.

Una conexion multiplexa dos suscripciones por activo:
  - trades          (trades)                    -> TradeEvent
  - activeAssetCtx  (mark, funding, OI, oracle)  -> TickerEvent

Endpoint publico: wss://api.hyperliquid.xyz/ws (sin API key).

Verificado contra la doc oficial (hyperliquid.gitbook.io, "Websocket > Subscriptions", "Notation",
"Timeouts and heartbeats", "Rate limits") y contra la respuesta real el 2026-10-17:
  - Suscripcion: {"method": "subscribe", "subscription": {"type": "trades", "coin": "XRP"}}.
    El server confirma con channel "subscriptionResponse"; un rechazo llega como channel "error".
  - `trades` -> lista de WsTrade {coin, side, px, sz, time, hash, tid, users}. "B = Bid = Buy, A = Ask =
    Short" y "Side is aggressing side for trades": es el lado del taker, sin invertir.
    Al suscribir llega un primer lote con trades recientes: en una reconexion pueden repetirse, asi
    que se descartan por (time, tid) con un set acotado.
  - `activeAssetCtx` (perps) -> {coin, ctx: {funding, openInterest, markPx, oraclePx, midPx, ...}}.
    No trae timestamp: se usa la hora de recepcion. `openInterest` va en unidades del activo base.
  - ⚠️ FUNDING POR HORA: "The funding rate on Hyperliquid is paid every hour" y `fundingHistory` trae una
    entrada por hora del mismo orden que `ctx.funding`. Binance/Bybit/OKX informan la tasa del
    intervalo de 8 h, asi que aca se multiplica por 8 (equivalente 8 h) para que sean comparables.
  - Sin stream publico de liquidaciones: solo existen por usuario (`userEvents`, `userFills`). Este
    worker no emite LiquidationEvent; el builder lo declara como cobertura no disponible.
  - Heartbeat: "The server will close any connection if it hasn't sent a message to it in the last 60
    seconds"; se manda {"method": "ping"} (responde {"channel": "pong"}) cada 30 s.
  - Limites: 10 conexiones, 1000 suscripciones, 2000 mensajes/min por IP. Aca: 1 conexion, 2 x simbolo.
  - Los perps de Hyperliquid cotizan en USD con colateral USDC; se mapean al simbolo interno
    'XRP-USDT' para agregarlos con los demas exchanges (la diferencia USDC/USDT es despreciable aca).
  - Referencia de implementacion: Hummingbot `connector/derivative/hyperliquid_perpetual/`.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import time
from collections import deque
from typing import Any

import websockets

from ..schema.events import Exchange, IngestEvent, Side, TickerEvent, TradeEvent
from ..schema.sink import EventSink
from .binance_worker import _now_ms

log = logging.getLogger("heatsignal.ingestion.hyperliquid")

PUBLIC_WS_URL = "wss://api.hyperliquid.xyz/ws"
FUNDING_TO_8H = 8.0  # Hyperliquid informa la tasa horaria
_DEDUP_MAX = 5000


def coin_for(symbol: str) -> str:
    """'XRPUSDT' / 'XRP-USDT' -> 'XRP' (nombre del activo en Hyperliquid)."""
    s = symbol.upper().replace("/", "-")
    if "-" in s:
        return s.split("-", 1)[0]
    for quote in ("USDT", "USDC", "USD"):
        if s.endswith(quote) and len(s) > len(quote):
            return s[: -len(quote)]
    return s


def _taker_side(raw: str) -> Side:
    if raw == "B":
        return Side.BUY
    if raw == "A":
        return Side.SELL
    raise ValueError(f"lado de trade invalido: {raw!r}")


def _opt_float(value: Any) -> float | None:
    return float(value) if value not in (None, "") else None


class HyperliquidWorker:
    """Worker de Hyperliquid perps. Misma API de constructor y ciclo de vida que BybitWorker."""

    def __init__(
        self,
        symbols: list[str],
        sink: EventSink,
        *,
        url: str = PUBLIC_WS_URL,
        max_backoff_s: float = 60.0,
        base_backoff_s: float = 1.0,
        message_timeout_s: float = 60.0,
        ping_interval_s: float = 30.0,
    ) -> None:
        # coin de Hyperliquid -> simbolo interno ("XRP" -> "XRP-USDT")
        self._internal = {coin_for(s): f"{coin_for(s)}-USDT" for s in symbols}
        self._sink = sink
        self._url = url
        self._base_backoff = base_backoff_s
        self._max_backoff = max_backoff_s
        self._message_timeout_s = message_timeout_s
        self._ping_interval_s = ping_interval_s
        self._stop = asyncio.Event()
        self._ws: Any = None
        self._seen: set[tuple[int, int]] = set()
        self._seen_order: deque[tuple[int, int]] = deque()

    # ---- suscripciones -------------------------------------------------------

    def _subscribe_messages(self) -> list[str]:
        out = []
        for coin in self._internal:
            for kind in ("trades", "activeAssetCtx"):
                out.append(json.dumps({"method": "subscribe", "subscription": {"type": kind, "coin": coin}}))
        return out

    # ---- normalizacion -------------------------------------------------------

    def _normalize(self, msg: dict) -> list[IngestEvent]:
        channel = msg.get("channel")
        if channel in ("subscriptionResponse", "pong"):
            return []
        if channel == "error":
            log.error("Hyperliquid rechazo un mensaje: %s", msg.get("data"))
            return []
        try:
            if channel == "trades":
                return [ev for d in msg.get("data", []) if (ev := self._trade(d)) is not None]
            if channel == "activeAssetCtx":
                event = self._ticker(msg["data"])
                return [event] if event is not None else []
        except (KeyError, ValueError, TypeError) as exc:
            log.warning("no se pudo normalizar mensaje de %s: %s", channel, exc)
            return []
        if channel:
            log.debug("canal de Hyperliquid no manejado: %s", channel)
        return []

    def _is_duplicate(self, key: tuple[int, int]) -> bool:
        if key in self._seen:
            return True
        self._seen.add(key)
        self._seen_order.append(key)
        if len(self._seen_order) > _DEDUP_MAX:
            self._seen.discard(self._seen_order.popleft())
        return False

    def _trade(self, d: dict) -> TradeEvent | None:
        symbol = self._internal.get(d["coin"])
        if symbol is None:
            return None
        ts = int(d["time"])
        if self._is_duplicate((ts, int(d["tid"]))):
            return None
        return TradeEvent(
            source=Exchange.HYPERLIQUID,
            symbol=symbol,
            side=_taker_side(d["side"]),
            price=float(d["px"]),
            qty=float(d["sz"]),
            ts_event_ms=ts,
            ts_ingest_ms=_now_ms(),
        )

    def _ticker(self, data: dict) -> TickerEvent | None:
        symbol = self._internal.get(data["coin"])
        if symbol is None:
            return None
        ctx = data["ctx"]
        mark = _opt_float(ctx.get("markPx"))
        oi = _opt_float(ctx.get("openInterest"))
        funding = _opt_float(ctx.get("funding"))
        now = _now_ms()
        return TickerEvent(
            source=Exchange.HYPERLIQUID,
            symbol=symbol,
            mark_price=mark,
            last_price=_opt_float(ctx.get("midPx")),
            funding_rate=None if funding is None else funding * FUNDING_TO_8H,
            open_interest=oi,
            open_interest_usd=None if oi is None or mark is None else oi * mark,
            ts_event_ms=now,
            ts_ingest_ms=now,
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
            await ws.send(json.dumps({"method": "ping"}))

    async def _consume(self, ws: Any) -> None:
        while not self._stop.is_set():
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=self._message_timeout_s)
            except asyncio.TimeoutError:
                log.warning(
                    "sin mensajes de Hyperliquid en %.0fs - posible socket zombie, forzando reconexion",
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
            for m in self._subscribe_messages():
                await ws.send(m)
            log.info("suscripto a %d activos x 2 canales", len(self._internal))
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
                log.info("conectando worker Hyperliquid (intento %d)", attempt + 1)
                await self._run_once()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                if self._stop.is_set():
                    log.debug("worker de Hyperliquid cerrado por stop()", exc_info=True)
                else:
                    log.exception("error en el worker de Hyperliquid")

            if self._stop.is_set():
                break

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
