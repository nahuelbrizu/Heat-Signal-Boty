"""
Open interest de Binance USD-M por REST (el stream `markPrice` no trae OI).

Doc oficial (verificada 2026-10-19):
https://developers.binance.com/docs/derivatives/usds-margined-futures/market-data/rest-api/Open-Interest
    GET https://fapi.binance.com/fapi/v1/openInterest?symbol=BTCUSDT      peso IP 1, `symbol` obligatorio
    -> {"openInterest": "10659.509", "symbol": "BTCUSDT", "time": 1589437530011}   (OI en activo base)
7 simbolos cada 5 min = 7 de peso cada 5 min: despreciable frente al limite por IP.

Cada respuesta se emite como `TickerEvent(coverage="rest_poll")` al mismo sink que los workers WS, asi el
contexto (`context.open_interest_usd`, `oi_change_pct`), la memoria y la base historica lo tratan igual que
el OI de Bybit/OKX. `open_interest_usd` = OI base x ultimo mark de Binance que tenga la memoria (el WS
`markPrice`); sin mark se emite solo el OI base y se declara.

Sin fallos silenciosos: cada error queda en `last_error[simbolo]` y `data_gaps()` lo devuelve como hueco
real. Tras un ciclo con errores se reintenta antes (30 s, 60 s, ... hasta el intervalo); un HTTP 429/418
(limite de peso / ban de IP) espera el intervalo completo.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
import urllib.parse
from collections.abc import Callable, Sequence

from ..evaluation.price_source import HttpGet, _urllib_get, to_binance_symbol
from ..schema.events import Exchange, TickerEvent
from ..schema.sink import EventSink

log = logging.getLogger("heatsignal.ingestion.binance_oi")

BASE_URL = "https://fapi.binance.com"
PATH = "/fapi/v1/openInterest"
DEFAULT_INTERVAL_S = 300.0
RETRY_BASE_S = 30.0


def _now_ms() -> int:
    return int(time.time() * 1000)


def oi_url(symbol: str, base_url: str = BASE_URL) -> str:
    return f"{base_url}{PATH}?{urllib.parse.urlencode({'symbol': to_binance_symbol(symbol)})}"


def parse_open_interest(body: bytes, binance_symbol: str) -> tuple[float, int]:
    """(OI en activo base, time ms). ValueError si la respuesta no es la documentada."""
    d = json.loads(body)
    if not isinstance(d, dict) or "openInterest" not in d or "time" not in d:
        raise ValueError(f"respuesta inesperada: {str(d)[:120]}")
    if d.get("symbol") not in (None, binance_symbol):
        raise ValueError(f"simbolo {d.get('symbol')!r} != {binance_symbol!r}")
    oi = float(d["openInterest"])
    if oi < 0:
        raise ValueError(f"openInterest negativo: {oi}")
    return oi, int(d["time"])


class BinanceOiPoller:
    def __init__(
        self,
        symbols: Sequence[str],
        sink: EventSink,
        *,
        mark_lookup: Callable[[str], float | None] | None = None,
        interval_s: float = DEFAULT_INTERVAL_S,
        http_get: HttpGet | None = None,
        clock_ms: Callable[[], int] = _now_ms,
        timeout_s: float = 10.0,
        base_url: str = BASE_URL,
    ) -> None:
        self.symbols = list(symbols)  # formato interno, p.ej. "BTC-USDT"
        self._sink = sink
        self._mark_lookup = mark_lookup
        self.interval_s = interval_s
        self._http_get = http_get or _urllib_get
        self._clock_ms = clock_ms
        self._timeout_s = timeout_s
        self._base_url = base_url
        self.started_at_ms = clock_ms()
        self.last_ok_ms: dict[str, int] = {}
        self.last_error: dict[str, str] = {}
        self.no_mark: set[str] = set()
        self.failed_cycles = 0  # ciclos seguidos con algun error (backoff)
        self.rate_limited = False

    # ---- escritura ------------------------------------------------------------------------------

    async def poll_once(self) -> int:
        """Un GET por simbolo; devuelve cuantos se emitieron."""
        ok = 0
        self.rate_limited = False
        for sym in self.symbols:
            now = self._clock_ms()
            bsym = to_binance_symbol(sym)
            try:
                status, body = await asyncio.to_thread(self._http_get, oi_url(sym, self._base_url), self._timeout_s)
                if status in (418, 429):
                    self.rate_limited = True
                if status != 200:
                    raise ValueError(f"HTTP {status}: {body[:120]!r}")
                oi, ts = parse_open_interest(body, bsym)
            except Exception as exc:  # noqa: BLE001 - un simbolo caido no frena a los demas
                self.last_error[sym] = f"{type(exc).__name__}: {exc}"[:200]
                log.warning("OI binance %s: fallo el REST: %s", sym, self.last_error[sym])
                continue
            mark = self._mark_lookup(sym) if self._mark_lookup else None
            if mark is None:
                self.no_mark.add(sym)
            else:
                self.no_mark.discard(sym)
            event = TickerEvent(
                source=Exchange.BINANCE, symbol=sym, open_interest=oi,
                open_interest_usd=None if mark is None else oi * mark,
                ts_event_ms=ts, ts_ingest_ms=now, coverage="rest_poll",
            )
            try:
                await self._sink.write(event)
            except Exception as exc:  # noqa: BLE001
                self.last_error[sym] = f"sink: {type(exc).__name__}: {exc}"[:200]
                log.exception("OI binance %s: no se pudo emitir", sym)
                continue
            self.last_error.pop(sym, None)
            self.last_ok_ms[sym] = now
            ok += 1
        self.failed_cycles = self.failed_cycles + 1 if ok < len(self.symbols) else 0
        return ok

    def next_delay_s(self) -> float:
        if not self.failed_cycles or self.rate_limited:
            return self.interval_s
        return min(self.interval_s, RETRY_BASE_S * 2 ** (self.failed_cycles - 1))

    async def run_forever(self, stop: asyncio.Event) -> None:
        log.info("OI binance por REST: %d simbolos cada %.0fs (%s)", len(self.symbols), self.interval_s, PATH)
        while not stop.is_set():
            await self.poll_once()
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=self.next_delay_s())

    # ---- lectura --------------------------------------------------------------------------------

    def data_gaps(self, symbol: str, now_ms: int) -> list[str]:
        """Huecos REALES del OI de Binance para `FeatureSet.data_gaps` (features/gaps.py: clase real)."""
        if symbol not in self.symbols:
            return []
        gaps: list[str] = []
        ok = self.last_ok_ms.get(symbol)
        if symbol in self.last_error:
            since = "sin dato desde el arranque" if ok is None else f"ultimo dato hace {(now_ms - ok) // 60000} min"
            gaps.append(f"open_interest: fallo el REST de binance ({self.last_error[symbol]}); {since}")
        elif ok is None and now_ms - self.started_at_ms > 2 * self.interval_s * 1000:
            gaps.append("open_interest: REST de binance sin respuesta desde el arranque")
        if symbol in self.no_mark:
            gaps.append("open_interest: binance sin mark para pasar el OI a USD (no entra en la suma)")
        return gaps

    def status(self) -> dict:
        now = self._clock_ms()
        return {
            "endpoint": BASE_URL + PATH, "interval_s": self.interval_s,
            "symbols": {s: {"last_ok_ms": self.last_ok_ms.get(s), "error": self.last_error.get(s),
                            "age_s": None if s not in self.last_ok_ms else round((now - self.last_ok_ms[s]) / 1000, 1)}
                        for s in self.symbols},
        }
