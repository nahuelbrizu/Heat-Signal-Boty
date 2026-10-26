"""Parseo de RSS 2.0 a `NewsItem` y etiquetado por activo (codigo puro, sin red)."""

from __future__ import annotations

import hashlib
import html
import re
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime

from ..schema.news import NewsItem
from .feeds import Feed

SUMMARY_MAX_CHARS = 280
TITLE_MAX_CHARS = 200

# Activo base -> patron. Palabras completas para no confundir 'SOL' con 'solution' ni 'BTC' en una URL.
ASSET_PATTERNS: dict[str, re.Pattern[str]] = {
    "BTC": re.compile(r"\b(BTC|bitcoin)\b", re.IGNORECASE),
    "ETH": re.compile(r"\b(ETH|ether|ethereum)\b", re.IGNORECASE),
    "SOL": re.compile(r"\b(SOL|solana)\b", re.IGNORECASE),
    "XRP": re.compile(r"\b(XRP|XRPL|ripple)\b", re.IGNORECASE),
}
MARKET_WIDE = re.compile(
    r"\b(crypto\w*|digital assets?|stablecoins?|ETFs?|regulat\w*|SEC|CFTC|Federal Reserve|Fed|"
    r"interest rates?|tariffs?|liquidations?)\b",
    re.IGNORECASE,
)
CRYPTO_KEYWORDS = re.compile(
    r"\b(crypto\w*|digital assets?|bitcoin|ether(eum)?|ripple|XRP|solana|tokens?|blockchain|stablecoins?)\b",
    re.IGNORECASE,
)
# Temas (`newstopics-v1`, regla fija por palabras clave; cambiarla = v2). Orden = prioridad para el tema principal.
# Ingles y castellano. Falsos positivos posibles ("president" de una empresa): es un filtro, no un juicio.
NEWS_TOPICS_ID = "newstopics-v1"
TOPIC_LABEL = {"geopolitica": "guerras y geopolítica", "politica": "política y gobiernos", "economia": "economía",
               "tecnologia": "tecnología"}
TOPIC_PATTERNS: dict[str, re.Pattern[str]] = {
    "geopolitica": re.compile(
        r"\b(wars?|warfare|invasion|invade[sd]?|missiles?|air ?strikes?|drone strikes?|bombing|shelling|ceasefire|"
        r"truce|troops|military|armed forces|nuclear|sanctions?|NATO|hostages?|terror\w*|insurgen\w*|annex\w*|"
        r"blockade|coup|guerras?|invasi[oó]n|misiles?|bombardeos?|alto el fuego|tropas|ej[eé]rcito|sanciones|"
        r"golpe de estado)\b", re.IGNORECASE),
    "politica": re.compile(
        r"\b(elections?|elected|presidential|president[- ]elect|new president|prime minister|premier|chancellor|"
        r"resign\w*|impeach\w*|inaugurat\w*|cabinet|parliament\w*|referendum|government (collapse|shutdown)|"
        r"shutdown|regime|White House|Kremlin|elecci[oó]n|elecciones|presidente electo|primer ministro|"
        r"renuncia\w*|juicio pol[ií]tico|parlamento|gobierno)\b", re.IGNORECASE),
    "economia": re.compile(
        r"\b(inflation|interest rates?|rate (cut|hike)s?|central banks?|Federal Reserve|FOMC|ECB|Bank of Japan|"
        r"tariffs?|trade war|recession|GDP|jobs report|payrolls|unemployment|Treasury|bond yields?|oil prices?|"
        r"OPEC|sovereign default|debt ceiling|Wall Street|S&P 500|Nasdaq|inflaci[oó]n|tasas? de inter[eé]s|"
        r"aranceles|recesi[oó]n|banco central)\b", re.IGNORECASE),
    "tecnologia": re.compile(
        r"\b(artificial intelligence|OpenAI|Anthropic|Nvidia|TSMC|chips?|chipmakers?|semiconductors?|quantum|GPUs?|"
        r"data cent(er|re)s?|LLMs?|chatbots?|robot\w*|autonomous|cyber ?attacks?|hack(s|ed|ers?)|ransomware|"
        r"inteligencia artificial|cu[aá]ntic\w*|semiconductores?)\b", re.IGNORECASE),
}
# "AI" cuenta como tecnologia solo en mayusculas (sin distinguir, matchearia el "ai" suelto de otros textos).
_AI_UPPER = re.compile(r"\b(AI|A\.I\.)(?=\W|$)")
_TAGS = re.compile(r"<[^>]+>")
_SPACES = re.compile(r"\s+")
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


class FeedParseError(Exception):
    """El cuerpo no es un RSS 2.0 legible."""


def clean_text(raw: str | None, limit: int) -> str:
    """HTML -> texto plano de una linea, sin caracteres de control, recortado a `limit`."""
    text = html.unescape(_TAGS.sub(" ", raw or ""))
    text = _SPACES.sub(" ", _CONTROL.sub(" ", text)).strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def tag_assets(text: str) -> list[str]:
    return [asset for asset, pat in ASSET_PATTERNS.items() if pat.search(text)]


def classify_topics(text: str) -> list[str]:
    """Temas de `newstopics-v1` en orden de prioridad (vacio = ninguno)."""
    return [t for t, pat in TOPIC_PATTERNS.items()
            if pat.search(text) or (t == "tecnologia" and _AI_UPPER.search(text))]


def _item_id(feed: Feed, key: str) -> str:
    return hashlib.sha256(f"{feed.name}|{key}".encode()).hexdigest()[:16]


def parse_feed(feed: Feed, body: bytes, fetched_ms: int) -> list[NewsItem]:
    try:
        root = ET.fromstring(body)
    except ET.ParseError as exc:
        raise FeedParseError(f"{feed.name}: XML invalido: {exc}") from exc
    channel = root.find("channel")
    if root.tag != "rss" or channel is None:
        raise FeedParseError(f"{feed.name}: no es RSS 2.0 (raiz {root.tag!r})")
    out: list[NewsItem] = []
    for item in channel.findall("item"):
        title = clean_text(item.findtext("title"), TITLE_MAX_CHARS)
        link = (item.findtext("link") or "").strip()
        pub = item.findtext("pubDate")
        if not title or not link or not pub:
            continue  # sin titulo, link o fecha no se puede usar sin arriesgar look-ahead
        try:
            published_ms = int(parsedate_to_datetime(pub.strip()).timestamp() * 1000)
        except (TypeError, ValueError):
            continue
        summary = clean_text(item.findtext("description"), SUMMARY_MAX_CHARS)
        text = f"{title} {summary}"
        if feed.crypto_only and not CRYPTO_KEYWORDS.search(text):
            continue
        topics = classify_topics(text)
        if feed.kind != "cripto" and not topics:
            continue  # feed generalista: solo lo que toca guerras, gobiernos, economia o tecnologia
        out.append(NewsItem(
            item_id=_item_id(feed, (item.findtext("guid") or link).strip()),
            source=feed.name,
            primary=feed.primary,
            title=title,
            summary=summary,
            url=link,
            published_ms=published_ms,
            fetched_ms=fetched_ms,
            symbols=tag_assets(text),
            market_wide=feed.kind == "cripto" and bool(MARKET_WIDE.search(text)),
            kind=feed.kind,
            topics=topics,
        ))
    return out
