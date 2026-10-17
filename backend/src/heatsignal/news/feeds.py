"""
Feeds RSS de noticias (verificados el 2026-10-17: responden 200 con RSS 2.0 y `item` con title, link,
guid, pubDate y description).

Ripple y XRPL no publican RSS (ripple.com/feed, /insights/feed, /rss.xml y xrpl.org/blog/rss.xml dan
404): no hay fuente primaria del emisor de XRP y se declara como hueco. La SEC exige un User-Agent
que identifique al cliente (sec.gov, "Accessing EDGAR Data"); se usa el de `USER_AGENT`.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Feed:
    name: str
    url: str
    primary: bool  # emisor o regulador (no un medio)
    crypto_only: bool = False  # True: descartar items sin palabras clave cripto (feed generalista)


FEEDS: tuple[Feed, ...] = (
    Feed("coindesk", "https://www.coindesk.com/arc/outboundfeeds/rss/", primary=False),
    Feed("cointelegraph", "https://cointelegraph.com/rss", primary=False),
    Feed("decrypt", "https://decrypt.co/feed", primary=False),
    Feed("sec", "https://www.sec.gov/news/pressreleases.rss", primary=True, crypto_only=True),
)

USER_AGENT = "heatsignal-news/0.1 (proyecto de prueba; contacto en el repo)"

# Huecos conocidos de cobertura por activo base (se declaran en data_gaps del simbolo).
KNOWN_COVERAGE_GAPS: dict[str, str] = {
    "XRP": "noticias: Ripple/XRPL no publican RSS; sin fuente primaria del emisor (solo SEC + medios)",
}
