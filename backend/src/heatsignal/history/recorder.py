"""
Grabador por minuto: un sink mas de la ingesta que agrega eventos en barras de 1 minuto por
(exchange, simbolo) y las guarda en `HistoryStore`.

Un minuto se escribe cuando ya cerro y paso un margen (`grace_s`) para eventos que llegan con algo de
demora. Si despues llega un evento de un minuto ya escrito, se vuelve a escribir y la base lo fusiona
(suma volumenes): nunca se pierde ni se pisa. Las liquidaciones tambien se guardan una por una.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass

from ..schema.events import IngestEvent, LiquidationEvent, Side, TickerEvent, TradeEvent
from .store import HistoryStore, MinuteBar

log = logging.getLogger("heatsignal.history.recorder")

MINUTE_MS = 60_000


def _now_ms() -> int:
    return int(time.time() * 1000)


@dataclass
class RecorderStats:
    events: int = 0
    bars_written: int = 0
    liquidations_written: int = 0
    late_events: int = 0  # llegaron despues de escribir su minuto (se fusionan igual)
    write_errors: int = 0
    last_flush_ms: int | None = None

    def as_dict(self) -> dict:
        return dict(self.__dict__)


class MinuteRecorder:
    """EventSink que agrega a 1 minuto. Se llama desde el event loop (mismo hilo que el store)."""

    def __init__(
        self,
        store: HistoryStore,
        *,
        grace_s: float = 10.0,
        flush_every_s: float = 5.0,
        clock_ms: Callable[[], int] = _now_ms,
    ) -> None:
        self._store = store
        self.store = store  # lo usan tambien el grabador de noticias y el panel (mismo hilo)
        self._grace_ms = int(grace_s * 1000)
        self._flush_every_ms = int(flush_every_s * 1000)
        self._clock_ms = clock_ms
        self._bars: dict[tuple[str, str, int], MinuteBar] = {}
        self._liqs: list[tuple] = []
        self._flushed_until_ms = 0  # minutos < este valor ya se escribieron al menos una vez
        self._last_flush_check_ms = 0
        self.stats = RecorderStats()

    def _bar(self, exchange: str, symbol: str, ts_ms: int) -> MinuteBar:
        minute = ts_ms - ts_ms % MINUTE_MS
        if minute < self._flushed_until_ms:
            self.stats.late_events += 1
        key = (exchange, symbol, minute)
        bar = self._bars.get(key)
        if bar is None:
            bar = self._bars[key] = MinuteBar(exchange, symbol, minute)
        return bar

    async def write(self, event: IngestEvent) -> None:
        self.stats.events += 1
        ex = event.source.value
        if isinstance(event, TradeEvent):
            bar = self._bar(ex, event.symbol, event.ts_event_ms)
            usd = event.price * event.qty
            if event.is_block_trade:
                bar.block_usd += usd
            else:
                if bar.open is None:
                    bar.open = bar.high = bar.low = event.price
                bar.high = max(bar.high or event.price, event.price)
                bar.low = min(bar.low or event.price, event.price)
                bar.close = event.price
                bar.n_trades += 1
                if event.side == Side.BUY:
                    bar.buy_usd += usd
                else:
                    bar.sell_usd += usd
        elif isinstance(event, LiquidationEvent):
            bar = self._bar(ex, event.symbol, event.ts_event_ms)
            if event.side == Side.SELL:
                bar.liq_long_usd += event.notional_usd
            else:
                bar.liq_short_usd += event.notional_usd
            bar.n_liqs += 1
            self._liqs.append((ex, event.symbol, event.ts_event_ms, event.side.value, event.price, event.qty,
                               event.notional_usd, event.coverage.value))
        elif isinstance(event, TickerEvent):
            # Hora de recepcion: algunos tickers (Hyperliquid) no traen hora del exchange.
            bar = self._bar(ex, event.symbol, event.ts_ingest_ms)
            if event.mark_price is not None:
                bar.mark = event.mark_price
            if event.funding_rate is not None:
                bar.funding_8h = event.funding_rate
            oi_usd = event.open_interest_usd
            if oi_usd is None and event.open_interest is not None:
                ref = event.mark_price if event.mark_price is not None else event.last_price
                oi_usd = event.open_interest * ref if ref is not None else None
            if oi_usd is not None:
                bar.oi_usd = oi_usd
        now = self._clock_ms()
        if now - self._last_flush_check_ms >= self._flush_every_ms:
            self.flush(now)

    def flush(self, now_ms: int | None = None, *, everything: bool = False) -> int:
        """Escribe los minutos cerrados (o todos, al cerrar). Devuelve cuantas barras escribio."""
        now = self._clock_ms() if now_ms is None else now_ms
        self._last_flush_check_ms = now
        cutoff = now - self._grace_ms
        cutoff -= cutoff % MINUTE_MS  # solo minutos cuyo fin + margen ya paso
        ready = [k for k in self._bars if everything or k[2] + MINUTE_MS <= cutoff]
        if not ready and not self._liqs:
            return 0
        bars = [self._bars[k] for k in ready]
        liqs = self._liqs
        try:
            self._store.upsert_bars(bars)
            self._store.add_liquidations(liqs)
        except Exception:  # noqa: BLE001 - un fallo de disco no tumba la ingesta; se reintenta
            self.stats.write_errors += 1
            log.exception("no se pudieron guardar %d barras / %d liquidaciones (se reintenta)", len(bars), len(liqs))
            return 0
        for k in ready:
            del self._bars[k]
        self._liqs = []
        self.stats.bars_written += len(bars)
        self.stats.liquidations_written += len(liqs)
        self.stats.last_flush_ms = now
        self._flushed_until_ms = max(self._flushed_until_ms, cutoff)
        return len(bars)

    def record_news(self, items: list) -> None:
        """Guarda noticias nuevas (callback de NewsWindow). Fallar aca no frena las noticias."""
        rows = [(it.item_id, it.source, int(it.primary), it.title, it.summary, it.url, it.published_ms,
                 it.fetched_ms, ",".join(it.symbols), int(it.market_wide)) for it in items]
        try:
            self._store.add_news(rows)
        except Exception:  # noqa: BLE001
            self.stats.write_errors += 1
            log.exception("no se pudieron guardar %d noticias", len(rows))

    async def close(self) -> None:
        self.flush(everything=True)
        self._store.close()
