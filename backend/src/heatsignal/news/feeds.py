"""
Feeds RSS de noticias (verificados el 2026-10-17: responden 200 con RSS 2.0 y `item` con title, link,
guid, pubDate y description).

Ripple y XRPL no publican RSS (ripple.com/feed, /insights/feed, /rss.xml y xrpl.org/blog/rss.xml dan
404): no hay fuente primaria del emisor de XRP y se declara como hueco. La SEC exige un User-Agent
que identifique al cliente (sec.gov, "Accessing EDGAR Data"); se usa el de `USER_AGENT`.
"""

from __future__ import annotations

from dataclasses import dataclass


# Clase de feed. 'cripto': medios cripto y la SEC (van por simbolo al Analista de Noticias, como siempre).
# 'mundo' / 'economia' / 'tecnologia' (2026-10-27, pedido del usuario: "faltan cambios de presidentes, guerras, nuevas
# tecnologias"): feeds generalistas; solo se guardan los items con algun tema de `rss.TOPIC_PATTERNS` y van al
# Analista de Contexto como bloque comun "Mundo" (una vez por corrida, no repetido por simbolo).
KINDS = ("cripto", "mundo", "economia", "tecnologia")


@dataclass(frozen=True)
class Feed:
    name: str
    url: str
    primary: bool  # emisor o regulador (no un medio)
    crypto_only: bool = False  # True: descartar items sin palabras clave cripto (feed generalista)
    kind: str = "cripto"


# Feeds 'mundo' / 'economia' / 'tecnologia' verificados el 2026-10-27: responden 200 con RSS 2.0 e items
# (whitehouse.gov/feed da 404: sin fuente primaria del gobierno de EE.UU.).
FEEDS: tuple[Feed, ...] = (
    Feed("coindesk", "https://www.coindesk.com/arc/outboundfeeds/rss/", primary=False),
    Feed("cointelegraph", "https://cointelegraph.com/rss", primary=False),
    Feed("decrypt", "https://decrypt.co/feed", primary=False),
    Feed("sec", "https://www.sec.gov/news/pressreleases.rss", primary=True, crypto_only=True),
    Feed("bbc-world", "https://feeds.bbci.co.uk/news/world/rss.xml", primary=False, kind="mundo"),
    Feed("aljazeera", "https://www.aljazeera.com/xml/rss/all.xml", primary=False, kind="mundo"),
    Feed("guardian-world", "https://www.theguardian.com/world/rss", primary=False, kind="mundo"),
    Feed("nyt-world", "https://rss.nytimes.com/services/xml/rss/nyt/World.xml", primary=False, kind="mundo"),
    Feed("nyt-politics", "https://rss.nytimes.com/services/xml/rss/nyt/Politics.xml", primary=False, kind="mundo"),
    Feed("fed", "https://www.federalreserve.gov/feeds/press_all.xml", primary=True, kind="economia"),
    Feed("bbc-business", "https://feeds.bbci.co.uk/news/business/rss.xml", primary=False, kind="economia"),
    Feed("bbc-tech", "https://feeds.bbci.co.uk/news/technology/rss.xml", primary=False, kind="tecnologia"),
    Feed("nyt-tech", "https://rss.nytimes.com/services/xml/rss/nyt/Technology.xml", primary=False, kind="tecnologia"),
    Feed("arstechnica", "https://feeds.arstechnica.com/arstechnica/index", primary=False, kind="tecnologia"),
    Feed("techcrunch", "https://techcrunch.com/feed/", primary=False, kind="tecnologia"),
    Feed("mit-tech-review", "https://www.technologyreview.com/feed/", primary=False, kind="tecnologia"),
)
FEED_KIND: dict[str, str] = {f.name: f.kind for f in FEEDS}

USER_AGENT = "heatsignal-news/0.1 (proyecto de prueba; contacto en el repo)"

# Huecos conocidos de cobertura por activo base (se declaran en data_gaps del simbolo).
KNOWN_COVERAGE_GAPS: dict[str, str] = {
    "XRP": "noticias: Ripple/XRPL no publican RSS; sin fuente primaria del emisor (solo SEC + medios)",
}
