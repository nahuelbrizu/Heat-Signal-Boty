"""
Ventana de noticias en memoria (48 h) y poller de los feeds RSS.

Mismo criterio que la ventana de mercado: nada se guarda en disco. Un item se identifica por su
`item_id`; si un feed lo vuelve a mandar, se conserva el primer `fetched_ms` (cuando lo vimos por primera
vez), que es lo que define si una corrida del grafo podia conocerlo (anti look-ahead).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Callable, Sequence

from ..evaluation.price_source import HttpGet
from ..schema.news import NewsItem
from .feeds import FEEDS, KNOWN_COVERAGE_GAPS, USER_AGENT, Feed
from .rss import FeedParseError, parse_feed

log = logging.getLogger("heatsignal.news")

DEFAULT_RETENTION_S = 48 * 3600
DEFAULT_INTERVAL_S = 300.0
MAX_SYMBOL_ITEMS = 8
MAX_MARKET_ITEMS = 4


def _now_ms() -> int:
    return int(time.time() * 1000)


def base_asset(symbol: str) -> str:
    """'XRP-USDT' / 'XRPUSDT' -> 'XRP'."""
    s = symbol.upper().replace("/", "-")
    if "-" in s:
        return s.split("-", 1)[0]
    for quote in ("USDT", "USDC", "USD"):
        if s.endswith(quote) and len(s) > len(quote):
            return s[: -len(quote)]
    return s


def _urllib_get_with_ua(url: str, timeout_s: float) -> tuple[int, bytes]:
    import urllib.error
    import urllib.request

    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=timeout_s) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


class NewsWindow:
    def __init__(
        self,
        feeds: Sequence[Feed] = FEEDS,
        *,
        retention_s: int = DEFAULT_RETENTION_S,
        interval_s: float = DEFAULT_INTERVAL_S,
        http_get: HttpGet | None = None,
        clock_ms: Callable[[], int] = _now_ms,
        timeout_s: float = 15.0,
        on_new: Callable[[list[NewsItem]], None] | None = None,
    ) -> None:
        self._feeds = list(feeds)
        self._retention_ms = retention_s * 1000
        self.interval_s = interval_s
        self._http_get = http_get or _urllib_get_with_ua
        self._clock_ms = clock_ms
        self._timeout_s = timeout_s
        self._items: dict[str, NewsItem] = {}
        self._on_new = on_new
        self.last_ok_ms: dict[str, int] = {}
        self.last_error: dict[str, str] = {}
        self.started_at_ms = clock_ms()

    # ---- escritura ------------------------------------------------------------------------------

    def ingest(self, items: Sequence[NewsItem]) -> int:
        """Agrega items nuevos (por item_id); devuelve cuantos eran nuevos."""
        fresh = [it for it in dict((it.item_id, it) for it in items).values() if it.item_id not in self._items]
        for it in fresh:
            self._items[it.item_id] = it
        self.prune()
        if fresh and self._on_new is not None:
            self._on_new(fresh)
        return len(fresh)

    def prune(self) -> None:
        cutoff = self._clock_ms() - self._retention_ms
        for k in [k for k, it in self._items.items() if it.published_ms < cutoff]:
            del self._items[k]

    async def poll_once(self) -> None:
        for feed in self._feeds:
            now = self._clock_ms()
            try:
                status, body = await asyncio.to_thread(self._http_get, feed.url, self._timeout_s)
                if status != 200:
                    raise FeedParseError(f"HTTP {status}")
                items = parse_feed(feed, body, now)
            except Exception as exc:  # noqa: BLE001 - un feed caido no frena a los demas
                self.last_error[feed.name] = f"{type(exc).__name__}: {exc}"[:200]
                log.warning("noticias: feed %s fallo: %s", feed.name, self.last_error[feed.name])
                continue
            self.last_error.pop(feed.name, None)
            self.last_ok_ms[feed.name] = now
            new = self.ingest(items)
            log.debug("noticias: %s -> %d items, %d nuevos", feed.name, len(items), new)

    async def run_forever(self, stop: asyncio.Event) -> None:
        log.info("noticias: %d feeds cada %.0fs (%s)", len(self._feeds), self.interval_s,
                 ", ".join(f.name for f in self._feeds))
        while not stop.is_set():
            await self.poll_once()
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=self.interval_s)

    # ---- lectura --------------------------------------------------------------------------------

    def items(self) -> list[NewsItem]:
        """Todo lo que hay en la ventana (copia), del mas nuevo al mas viejo por `published_ms`."""
        return sorted(self._items.values(), key=lambda it: it.published_ms, reverse=True)

    def items_for(self, symbol: str, now_ms: int) -> list[NewsItem]:
        """Noticias que una corrida en `now_ms` podia conocer: del activo primero, luego del mercado."""
        asset = base_asset(symbol)
        cutoff = now_ms - self._retention_ms
        visible = [
            it for it in self._items.values()
            if it.fetched_ms <= now_ms and it.published_ms <= now_ms and it.published_ms >= cutoff
        ]
        visible.sort(key=lambda it: it.published_ms, reverse=True)
        own = [it for it in visible if asset in it.symbols][:MAX_SYMBOL_ITEMS]
        market = [it for it in visible if it.market_wide and asset not in it.symbols][:MAX_MARKET_ITEMS]
        return own + market

    def data_gaps(self, symbol: str, now_ms: int) -> list[str]:
        gaps: list[str] = []
        stale_ms = int(3 * self.interval_s * 1000)
        for feed in self._feeds:
            ok = self.last_ok_ms.get(feed.name)
            if ok is None:
                if now_ms - self.started_at_ms > stale_ms or feed.name in self.last_error:
                    gaps.append(f"noticias: feed {feed.name} sin respuesta valida desde el arranque"
                                + (f" ({self.last_error[feed.name]})" if feed.name in self.last_error else ""))
            elif now_ms - ok > stale_ms:
                gaps.append(f"noticias: feed {feed.name} sin actualizar hace {(now_ms - ok) // 60000} min")
        if now_ms - self.started_at_ms < self._retention_ms and not self._items:
            gaps.append("noticias: ventana vacia (recien arrancado o feeds caidos)")
        if (known := KNOWN_COVERAGE_GAPS.get(base_asset(symbol))) is not None:
            gaps.append(known)
        return gaps

    def summary(self) -> dict:
        return {
            "items": len(self._items),
            "feeds": {
                f.name: {"primary": f.primary, "last_ok_ms": self.last_ok_ms.get(f.name),
                         "error": self.last_error.get(f.name)}
                for f in self._feeds
            },
        }
