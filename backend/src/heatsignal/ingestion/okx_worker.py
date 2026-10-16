"""
Worker de ingesta de OKX V5 (perpetuos lineales USDT/USDC, instType SWAP) para HeatSignal.

Un solo worker multiplexa en una conexion publica:
  - liquidation-orders {instType: SWAP}   -> LiquidationEvent  [coverage PARTIAL]
  - trades        {instId}                -> TradeEvent
  - tickers       {instId}  (last)        -+
  - mark-price    {instId}  (markPx)       |-> TickerEvent (cache mergeado por simbolo)
  - funding-rate  {instId}  (fundingRate)  |
  - open-interest {instId}  (oiCcy/oiUsd) -+

Endpoint publico: wss://ws.okx.com/ws/v5/public (sin auth). REST: https://openapi.okx.com.

Notas de diseno (verificadas contra la doc oficial de OKX V5 y contra el WS en vivo, oct 2026):
  - Simbolos: el constructor acepta forma OKX (`BTC-USDT-SWAP`), interna (`BTC-USDT`) o
    Binance/Bybit (`BTCUSDT`); todo se lleva al instId `BTC-USDT-SWAP`. Los eventos salen con
    el simbolo interno `BTC-USDT` (instId sin el sufijo `-SWAP`). Solo perps lineales: los
    inversos (`BTC-USD-SWAP`, ctVal en USD) se rechazan al validar el contrato.
  - `liquidation-orders` se suscribe por instType (SWAP), no por instId: llega TODO el mercado
    SWAP y se filtra a los simbolos configurados. Doc: "This data doesn't represent the total
    number of liquidations on OKX." (historicamente: max 1 orden por contrato por segundo)
    -> Coverage.PARTIAL. Doc: "Liquidation data comes from different data sources, so the
    updated data is not necessarily in chronological order."
  - En `liquidation-orders`, `side` es "Order side" (lado de la ORDEN de liquidacion) y
    `posSide` es el modo/lado de la posicion ("long"/"short" en hedge, "net" en net mode).
    Ejemplo de la doc: posSide=short, side=buy. Nuestro esquema guarda el lado de la orden,
    asi que `side` se usa TAL CUAL (sell = se liquido un long). Distinto de Bybit allLiquidation.
  - `bkPx`: "Liquidation mark price. The price of the transaction with the system's
    liquidation account" (equivale al precio de quiebra de Bybit, no a un fill de mercado).
  - ⚠️ `sz` en liquidation-orders y trades: "For FUTURES/SWAP, the unit is contract". Se convierte
    a activo base con `ctVal` de `GET /api/v5/public/instruments?instType=SWAP` ("for linear
    contracts, ctVal is in the base currency (e.g., ctVal=0.01 BTC for BTC-USDT-SWAP)").
    qty_base = sz * ctVal. Los ctVal se cargan al arrancar (y se refrescan en cada reconexion);
    si un simbolo configurado no tiene ctVal valido -> `OkxContractError` y el worker se detiene
    (error de configuracion, nunca un qty silenciosamente mal escalado).
  - `trades` AGREGA: "The system will send only one message per taker order, filled price,
    source. The `count` field will be used to represent the number of aggregated matches."
    Equivale al aggTrade de Binance. `trades-all` (uno por match) vive en /ws/v5/business,
    no en /public, asi que no se usa aca.
  - `tickers` en SWAP NO trae mark/funding/OI (solo last/bid/ask/vol). Por eso el TickerEvent
    se arma mergeando 4 canales en un cache por instId: last <- tickers.last, mark_price <-
    mark-price.markPx (cada 200ms si cambia, 10s si no), funding_rate <- funding-rate.fundingRate
    (cada 30-90s), open_interest <- open-interest.oiCcy (base) y open_interest_usd <- oiUsd
    (cada 3s si hay cambios). Cada update emite un TickerEvent con el estado mergeado. El cache
    se limpia en cada reconexion.
  - Heartbeat: "The connection will break automatically if the subscription is not established
    or data has not been pushed for more than 30 seconds." Si no llega nada en N<30s se manda
    el string 'ping' y se espera 'pong'; si tampoco llega nada -> reconexion.
  - `{"event":"error"}` (p.ej. 60018 instId inexistente) se loguea como ERROR; `{"event":"notice"}`
    (64008, upgrade del servicio en 60s) dispara una reconexion proactiva.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import time
import urllib.request
from collections.abc import Awaitable, Callable, Iterable, Mapping
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

log = logging.getLogger("heatsignal.ingestion.okx")

PUBLIC_URL = "wss://ws.okx.com/ws/v5/public"
REST_BASE_URL = "https://openapi.okx.com"
INSTRUMENTS_PATH = "/api/v5/public/instruments?instType=SWAP"

_SWAP_SUFFIX = "-SWAP"
_PER_SYMBOL_CHANNELS = ("trades", "tickers", "mark-price", "funding-rate", "open-interest")

# (canal, campo crudo de OKX) -> campo de TickerEvent.
_TICKER_FIELDS: dict[tuple[str, str], str] = {
    ("tickers", "last"): "last_price",
    ("mark-price", "markPx"): "mark_price",
    ("funding-rate", "fundingRate"): "funding_rate",
    ("open-interest", "oiCcy"): "open_interest",
    ("open-interest", "oiUsd"): "open_interest_usd",
}
_TICKER_CHANNELS = frozenset(ch for ch, _ in _TICKER_FIELDS)

# Loader de instrumentos: devuelve el payload `data` crudo de GET /public/instruments?instType=SWAP.
InstrumentsLoader = Callable[[], Awaitable[list[dict[str, Any]]]]


class OkxContractError(RuntimeError):
    """Falta (o es invalido) el ctVal de un simbolo configurado. Error de configuracion: detiene
    el worker en vez de emitir cantidades mal escaladas."""


# ---- simbolos -------------------------------------------------------------------


def _to_inst_id(symbol: str) -> str:
    """'BTCUSDT' | 'BTC-USDT' | 'BTC-USDT-SWAP' (cualquier case) -> 'BTC-USDT-SWAP'."""
    raw = symbol.strip().upper()
    if raw.endswith(_SWAP_SUFFIX):
        return raw
    if "-" not in raw:
        raw = _norm_symbol(raw)  # 'BTCUSDT' -> 'BTC-USDT'
    if raw.count("-") != 1:
        raise ValueError(f"simbolo OKX no reconocido: {symbol!r}")
    return raw + _SWAP_SUFFIX


def _internal_symbol(inst_id: str) -> str:
    """instId de OKX 'BTC-USDT-SWAP' -> simbolo interno 'BTC-USDT'."""
    inst_id = inst_id.upper()
    return inst_id[: -len(_SWAP_SUFFIX)] if inst_id.endswith(_SWAP_SUFFIX) else inst_id


# ---- lados ----------------------------------------------------------------------


def _order_side(raw: str) -> Side:
    """`side` de OKX ("buy"/"sell") sin invertir.

    Sirve para trades ("Trade side of taker") y para liquidation-orders ("Order side": el lado
    de la orden de liquidacion, que es justo lo que guarda nuestro esquema).
    """
    value = str(raw).lower()
    if value == "buy":
        return Side.BUY
    if value == "sell":
        return Side.SELL
    raise ValueError(f"side invalido: {raw!r}")


def _opt_float(value: Any) -> float | None:
    return float(value) if value not in (None, "") else None


# ---- contract value (ctVal) -----------------------------------------------------


def _fetch_instruments_sync(base_url: str = REST_BASE_URL, timeout_s: float = 10.0) -> list[dict]:
    req = urllib.request.Request(base_url + INSTRUMENTS_PATH, headers={"User-Agent": "heatsignal"})
    with urllib.request.urlopen(req, timeout=timeout_s) as resp:  # noqa: S310 - URL fija https
        body = json.loads(resp.read())
    if str(body.get("code")) != "0":
        raise RuntimeError(f"OKX instruments respondio code={body.get('code')} msg={body.get('msg')}")
    return list(body.get("data", []))


async def fetch_swap_instruments() -> list[dict[str, Any]]:
    """Loader por defecto: REST publico de OKX, en un hilo para no bloquear el loop."""
    return await asyncio.to_thread(_fetch_instruments_sync)


def contract_values_from_instruments(
    instruments: Iterable[Mapping[str, Any]], inst_ids: Iterable[str]
) -> dict[str, float]:
    """Extrae y valida ctVal (activo base por contrato) para cada instId pedido.

    Falla con `OkxContractError` si algun instId falta, no es lineal, su ctValCcy no es el
    activo base, o el ctVal no es un numero positivo. Nunca devuelve un mapa incompleto.
    """
    by_id = {str(i.get("instId", "")).upper(): i for i in instruments}
    out: dict[str, float] = {}
    problems: list[str] = []
    for inst_id in inst_ids:
        spec = by_id.get(inst_id)
        if spec is None:
            problems.append(f"{inst_id}: no esta en /public/instruments SWAP")
            continue
        base = inst_id.split("-", 1)[0]
        if spec.get("ctType") != "linear":
            problems.append(f"{inst_id}: ctType={spec.get('ctType')!r} (solo se soportan lineales)")
            continue
        if str(spec.get("ctValCcy", "")).upper() != base:
            problems.append(f"{inst_id}: ctValCcy={spec.get('ctValCcy')!r} != base {base}")
            continue
        try:
            ct_val = float(spec["ctVal"]) * float(spec.get("ctMult") or 1)
        except (KeyError, TypeError, ValueError):
            problems.append(f"{inst_id}: ctVal invalido {spec.get('ctVal')!r}")
            continue
        if ct_val <= 0:
            problems.append(f"{inst_id}: ctVal no positivo {ct_val}")
            continue
        out[inst_id] = ct_val
    if problems:
        raise OkxContractError("ctVal desconocido/invalido: " + "; ".join(problems))
    return out


def _validate_static_contract_values(
    contract_values: Mapping[str, float], inst_ids: Iterable[str]
) -> dict[str, float]:
    """ctVal inyectados: deben cubrir todos los instIds y ser positivos (si no, falla)."""
    norm = {_to_inst_id(k): float(v) for k, v in contract_values.items()}
    missing = [i for i in inst_ids if norm.get(i, 0.0) <= 0]
    if missing:
        raise OkxContractError(f"ctVal inyectado faltante o no positivo para: {missing}")
    return norm


class OkxWorker:
    """Worker multiplexado de OKX V5 (perps lineales SWAP).

    Uso:
        worker = OkxWorker(symbols=["BTC-USDT-SWAP", "SOLUSDT"], sink=my_sink)
        await worker.run()   # carga ctVal por REST, conecta y reconecta solo

    `contract_values` ({instId: ctVal}) permite inyectar los ctVal (tests / cache externo) y
    saltear el REST; `instruments_loader` permite inyectar la fuente del REST.
    """

    def __init__(
        self,
        symbols: list[str],
        sink: EventSink,
        *,
        url: str = PUBLIC_URL,
        max_backoff_s: float = 60.0,
        base_backoff_s: float = 1.0,
        message_timeout_s: float = 40.0,
        ping_interval_s: float = 20.0,
        contract_values: Mapping[str, float] | None = None,
        instruments_loader: InstrumentsLoader | None = None,
    ) -> None:
        self._inst_ids = list(dict.fromkeys(_to_inst_id(s) for s in symbols))
        self._inst_set = frozenset(self._inst_ids)
        self._sink = sink
        self._url = url
        self._base_backoff = base_backoff_s
        self._max_backoff = max_backoff_s
        self._message_timeout_s = message_timeout_s
        self._ping_interval_s = ping_interval_s
        self._stop = asyncio.Event()
        self._ws: Any = None
        self._static_ct = contract_values is not None
        self._ct_val: dict[str, float] = (
            _validate_static_contract_values(contract_values, self._inst_ids)
            if contract_values is not None
            else {}
        )
        self._instruments_loader = instruments_loader or fetch_swap_instruments
        # Estado mergeado de los 4 canales de ticker por instId ("BTC-USDT-SWAP").
        self._tickers: dict[str, dict[str, float | None]] = {}

    # ---- suscripciones -------------------------------------------------------

    def _args(self) -> list[dict[str, str]]:
        args: list[dict[str, str]] = [{"channel": "liquidation-orders", "instType": "SWAP"}]
        for inst_id in self._inst_ids:
            args += [{"channel": ch, "instId": inst_id} for ch in _PER_SYMBOL_CHANNELS]
        return args

    def _subscribe_message(self) -> str:
        return json.dumps({"op": "subscribe", "args": self._args()})

    # ---- contract values ------------------------------------------------------

    async def _load_contract_values(self) -> None:
        """Carga/refresca ctVal. Si el REST falla pero ya hay cache, sigue con el cache."""
        if self._static_ct:
            return
        try:
            instruments = await self._instruments_loader()
        except Exception:
            if self._ct_val:
                log.warning("no se pudo refrescar ctVal de OKX; se usa el cache previo", exc_info=True)
                return
            raise
        self._ct_val = contract_values_from_instruments(instruments, self._inst_ids)
        log.info("ctVal OKX cargados: %s", self._ct_val)

    def _base_qty(self, inst_id: str, contracts: Any) -> float:
        try:
            ct_val = self._ct_val[inst_id]
        except KeyError:
            raise OkxContractError(f"sin ctVal para {inst_id}; no se puede convertir sz") from None
        return float(contracts) * ct_val

    # ---- normalizacion (adaptador OKX -> esquema comun) ----------------------

    def _normalize(self, msg: dict) -> list[IngestEvent]:
        """Traduce un mensaje de OKX a 0..N eventos del esquema comun.

        Los mensajes `event` (subscribe/error/notice/...) no producen eventos; un error se
        loguea como ERROR. `OkxContractError` NO se atrapa: es un error de configuracion.
        """
        if "event" in msg:
            event = msg.get("event")
            if event == "error":
                log.error("OKX error %s: %s", msg.get("code"), msg.get("msg"))
            elif event == "notice":
                log.warning("OKX notice %s: %s", msg.get("code"), msg.get("msg"))
            return []

        arg = msg.get("arg") or {}
        channel = str(arg.get("channel", ""))
        try:
            if channel == "liquidation-orders":
                return self._liquidations(msg.get("data", []))
            if channel == "trades":
                return [self._trade(d) for d in msg.get("data", []) if d["instId"] in self._inst_set]
            if channel in _TICKER_CHANNELS:
                return [
                    self._ticker(channel, d) for d in msg.get("data", []) if d["instId"] in self._inst_set
                ]
        except (KeyError, ValueError, TypeError) as exc:
            log.warning("no se pudo normalizar mensaje de %s: %s", channel, exc)
            return []
        if channel:
            log.debug("canal de OKX no manejado: %s", channel)
        return []

    def _liquidations(self, data: list[dict]) -> list[IngestEvent]:
        events: list[IngestEvent] = []
        for item in data:
            inst_id = str(item["instId"]).upper()
            if inst_id not in self._inst_set:
                continue  # el canal es SWAP-wide; nos quedamos con los simbolos configurados
            for det in item.get("details", []):
                price = float(det["bkPx"])
                qty = self._base_qty(inst_id, det["sz"])
                events.append(
                    LiquidationEvent(
                        source=Exchange.OKX,
                        symbol=_internal_symbol(inst_id),
                        side=_order_side(det["side"]),  # lado de la ORDEN, sin invertir
                        price=price,
                        qty=qty,
                        notional_usd=price * qty,
                        ts_event_ms=int(det["ts"]),
                        ts_ingest_ms=_now_ms(),
                        coverage=Coverage.PARTIAL,  # "doesn't represent the total number of liquidations"
                    )
                )
        return events

    def _trade(self, d: dict) -> TradeEvent:
        inst_id = str(d["instId"]).upper()
        return TradeEvent(
            source=Exchange.OKX,
            symbol=_internal_symbol(inst_id),
            side=_order_side(d["side"]),  # "Trade side of taker"
            price=float(d["px"]),
            qty=self._base_qty(inst_id, d["sz"]),
            ts_event_ms=int(d["ts"]),
            ts_ingest_ms=_now_ms(),
        )

    def _ticker(self, channel: str, d: dict) -> TickerEvent:
        inst_id = str(d["instId"]).upper()
        updates = {
            field: _opt_float(d.get(raw))
            for (ch, raw), field in _TICKER_FIELDS.items()
            if ch == channel and raw in d
        }
        state = self._tickers.setdefault(inst_id, {})
        state.update(updates)
        return TickerEvent(
            source=Exchange.OKX,
            symbol=_internal_symbol(inst_id),
            ts_event_ms=int(d.get("ts") or _now_ms()),
            ts_ingest_ms=_now_ms(),
            **state,
        )

    # ---- ciclo de vida -------------------------------------------------------

    async def _safe_write(self, event: IngestEvent) -> None:
        try:
            await self._sink.write(event)
        except Exception:  # noqa: BLE001 - un fallo de sink no debe tumbar la ingesta
            log.exception("sink.write fallo para evento %s", event.kind)

    async def _consume(self, ws: Any) -> None:
        """Lee mensajes hasta que muera la conexion, llegue un `notice` o haya silencio.

        Heartbeat segun la doc de OKX: si no llega nada en `ping_interval_s` (<30s) se manda
        'ping'; si pasan `message_timeout_s` sin ningun mensaje (ni 'pong') se fuerza la
        reconexion (rol del watchdog de socket zombie del worker de Binance).
        """
        last_msg = time.monotonic()
        pinged = False
        while not self._stop.is_set():
            idle = time.monotonic() - last_msg
            if idle >= self._message_timeout_s:
                log.warning(
                    "sin mensajes de OKX en %.0fs (ni pong) - posible socket zombie, forzando reconexion",
                    self._message_timeout_s,
                )
                return
            if not pinged and idle >= self._ping_interval_s:
                await ws.send("ping")
                pinged = True
            wait = (
                self._message_timeout_s - idle if pinged else min(self._ping_interval_s, self._message_timeout_s) - idle
            )
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=max(wait, 0.001))
            except asyncio.TimeoutError:
                continue
            last_msg = time.monotonic()
            pinged = False
            if raw == "pong":
                continue
            try:
                msg = json.loads(raw)
            except (ValueError, TypeError):
                continue
            if not isinstance(msg, dict):
                continue
            for event in self._normalize(msg):
                await self._safe_write(event)
            if msg.get("event") == "notice":
                log.warning("OKX anuncio corte por upgrade; reconectando de forma proactiva")
                return

    async def _run_once(self) -> None:
        await self._load_contract_values()
        async with websockets.connect(self._url, open_timeout=10, close_timeout=2, ping_interval=None) as ws:
            self._ws = ws
            self._tickers.clear()  # estado nuevo por conexion: nada viejo se mezcla
            await ws.send(self._subscribe_message())
            log.info(
                "suscripto a liquidation-orders SWAP + %d simbolos x %d canales",
                len(self._inst_ids), len(_PER_SYMBOL_CHANNELS),
            )
            try:
                await self._consume(ws)
            finally:
                self._ws = None

    async def run(self) -> None:
        """Corre para siempre: carga ctVal, conecta, consume y reconecta con backoff+jitter.

        `OkxContractError` (simbolo sin ctVal valido) se propaga: es configuracion, no red.
        """
        attempt = 0
        while not self._stop.is_set():
            started = time.monotonic()
            try:
                log.info("conectando worker OKX (intento %d)", attempt + 1)
                await self._run_once()
            except (asyncio.CancelledError, OkxContractError):
                raise
            except Exception:  # noqa: BLE001
                if self._stop.is_set():
                    # stop() cerro el socket a proposito: el ConnectionClosedOK que sigue no es un error.
                    log.debug("worker de OKX cerrado por stop()", exc_info=True)
                else:
                    log.exception("error en el worker de OKX")

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


# ---- ejemplo de ejecucion manual --------------------------------------------

async def _demo() -> None:
    from ..schema.sink import BufferedSink, ConsoleSink

    logging.basicConfig(level=logging.INFO)
    sink = BufferedSink(inner=ConsoleSink())
    worker = OkxWorker(symbols=["BTC-USDT-SWAP", "SOL-USDT-SWAP"], sink=sink)
    try:
        await worker.run()
    except KeyboardInterrupt:
        await worker.stop()


if __name__ == "__main__":
    asyncio.run(_demo())
