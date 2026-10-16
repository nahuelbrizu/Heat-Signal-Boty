"""
Fuentes de precio para resolver hipotesis (Fase 3).

Contrato `PriceSource.price_at(symbol, ts) -> Decimal | None`:
    - Devuelve el precio observado en `ts` o inmediatamente ANTES (nunca despues:
      usar un precio posterior a `ts` seria look-ahead), siempre que esa observacion
      no sea mas vieja que la tolerancia de staleness de la fuente.
    - `None` significa "la fuente respondio y NO hay un precio confiable" (hueco de
      datos, simbolo inexistente, observacion stale). El Resolver lo registra como
      `expired_no_data` — nunca se descarta en silencio.
    - Un fallo de transporte (red caida, 5xx, rate limit) NO es "no hay datos": se
      levanta `PriceSourceError` y la hipotesis queda pendiente para reintentar. Si
      lo tratasemos como None, un corte de red se convertiria en un outcome falso.

`symbol` usa el formato interno ('BTC-USDT'); cada fuente lo traduce al suyo.
"""

from __future__ import annotations

import bisect
import json
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Callable, Protocol


class PriceSourceError(Exception):
    """Fallo transitorio de la fuente (red, 5xx, 429). La hipotesis queda pendiente."""


class PriceSource(Protocol):
    def price_at(self, symbol: str, ts: datetime) -> Decimal | None: ...


def _to_utc(ts: datetime) -> datetime:
    if ts.tzinfo is None or ts.tzinfo.utcoffset(ts) is None:
        raise ValueError(f"ts debe ser timezone-aware, recibido naive: {ts!r}")
    return ts.astimezone(timezone.utc)


def _valid_price(value: Any) -> Decimal | None:
    """Decimal finito y > 0, o None. Un precio <= 0 o no numerico no es confiable."""
    try:
        price = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None
    if not price.is_finite() or price <= 0:
        return None
    return price


class StaticPriceSource:
    """Fuente en memoria: serie de (timestamp, precio) por simbolo.

    Util para tests y como adaptador de cualquier serie ya cargada (p.ej. desde
    TimescaleDB). Devuelve la ultima observacion con timestamp <= ts, si no es mas
    vieja que `max_staleness`.
    """

    def __init__(
        self,
        observations: dict[str, list[tuple[datetime, Decimal]]],
        max_staleness: timedelta = timedelta(minutes=2),
    ) -> None:
        self._max_staleness = max_staleness
        self._series: dict[str, tuple[list[datetime], list[Decimal]]] = {}
        for symbol, obs in observations.items():
            ordered = sorted(((_to_utc(t), p) for t, p in obs), key=lambda x: x[0])
            self._series[symbol] = ([t for t, _ in ordered], [p for _, p in ordered])

    def price_at(self, symbol: str, ts: datetime) -> Decimal | None:
        series = self._series.get(symbol)
        if series is None:
            return None
        times, prices = series
        target = _to_utc(ts)
        idx = bisect.bisect_right(times, target) - 1
        if idx < 0:
            return None
        if target - times[idx] > self._max_staleness:
            return None
        return _valid_price(prices[idx])


def to_binance_symbol(symbol: str) -> str:
    """'BTC-USDT' (formato interno) -> 'BTCUSDT' (Binance USDS-M)."""
    return symbol.replace("-", "").replace("/", "").upper()


HttpGet = Callable[[str, float], tuple[int, bytes]]
"""(url, timeout_s) -> (status_code, body). Inyectable para tests sin red."""


def _urllib_get(url: str, timeout_s: float) -> tuple[int, bytes]:
    request = urllib.request.Request(url, headers={"User-Agent": "heatsignal-evaluator/1"})
    try:
        with urllib.request.urlopen(request, timeout=timeout_s) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise PriceSourceError(f"error de red consultando {url}: {exc}") from exc


class BinanceKlinePriceSource:
    """Precio desde klines REST de Binance USDS-M Futures.

    Endpoint (verificado contra la doc oficial, enlazada desde
    `binance-futures-connector` 4.2.0 `um_futures.market.klines`, y contra la
    respuesta real el 2026-10-17):
        GET https://fapi.binance.com/fapi/v1/klines
        params: symbol (obligatorio), interval (obligatorio, p.ej. '1m'),
                startTime / endTime (ms, opcionales), limit (default 500, max 1000).
        Respuesta: lista de arrays
            [openTime, open, high, low, close, volume, closeTime, quoteVolume,
             trades, takerBuyBase, takerBuyQuote, ignore]
        Las klines se identifican por su openTime. Con `endTime=T&limit=1` devuelve
        la kline de openTime mas reciente <= T (la vela que contiene T), o `[]` si no
        hay ninguna. Simbolo invalido -> HTTP 400 {"code": -1121, ...}.
    `price_kind="mark"` usa `GET /fapi/v1/markPriceKlines` (mismos params y formato),
    consistente con `entry_reference_price` (que es mark price).

    Precio en `ts`: el OPEN de la vela que contiene `ts` (observado en su openTime,
    a lo sumo `interval` antes de `ts`). No se usa el close: si la vela todavia esta
    abierta su close cambia, y ademas el close es posterior a `ts` (look-ahead).
    Staleness: si openTime de la vela devuelta queda a mas de `max_staleness` de
    `ts` (hueco por mantenimiento/delisting), se devuelve None.
    """

    BASE_URL = "https://fapi.binance.com"
    _PATHS = {"last": "/fapi/v1/klines", "mark": "/fapi/v1/markPriceKlines"}
    _INVALID_SYMBOL_CODE = -1121

    def __init__(
        self,
        *,
        interval: str = "1m",
        max_staleness: timedelta = timedelta(minutes=2),
        price_kind: str = "last",
        timeout_s: float = 10.0,
        http_get: HttpGet | None = None,
        base_url: str | None = None,
    ) -> None:
        if price_kind not in self._PATHS:
            raise ValueError(f"price_kind debe ser uno de {sorted(self._PATHS)}, recibido {price_kind!r}")
        self._interval = interval
        self._max_staleness = max_staleness
        self._path = self._PATHS[price_kind]
        self._timeout_s = timeout_s
        self._http_get = http_get or _urllib_get
        self._base_url = (base_url or self.BASE_URL).rstrip("/")

    def build_url(self, symbol: str, ts: datetime) -> str:
        end_ms = int(_to_utc(ts).timestamp() * 1000)
        query = urllib.parse.urlencode(
            {
                "symbol": to_binance_symbol(symbol),
                "interval": self._interval,
                "endTime": end_ms,
                "limit": 1,
            }
        )
        return f"{self._base_url}{self._path}?{query}"

    def price_at(self, symbol: str, ts: datetime) -> Decimal | None:
        target = _to_utc(ts)
        url = self.build_url(symbol, target)
        status, body = self._http_get(url, self._timeout_s)

        if status == 400:
            payload = _safe_json(body)
            if isinstance(payload, dict) and payload.get("code") == self._INVALID_SYMBOL_CODE:
                return None  # simbolo inexistente: no hay dato, no es transitorio
            raise PriceSourceError(f"HTTP 400 inesperado de Binance: {body[:200]!r}")
        if status != 200:
            raise PriceSourceError(f"HTTP {status} de Binance: {body[:200]!r}")

        payload = _safe_json(body)
        if not isinstance(payload, list):
            raise PriceSourceError(f"respuesta de klines no es una lista: {body[:200]!r}")
        if not payload:
            return None
        kline = payload[-1]
        if not isinstance(kline, list) or len(kline) < 5:
            raise PriceSourceError(f"kline con formato inesperado: {kline!r}")
        try:
            open_time = datetime.fromtimestamp(int(kline[0]) / 1000, tz=timezone.utc)
        except (TypeError, ValueError, OverflowError) as exc:
            raise PriceSourceError(f"openTime invalido en kline: {kline!r}") from exc
        if open_time > target:
            return None  # nunca usar una observacion posterior a ts
        if target - open_time > self._max_staleness:
            return None
        return _valid_price(kline[1])


def _safe_json(body: bytes) -> Any:
    try:
        return json.loads(body)
    except (ValueError, UnicodeDecodeError):
        return None
