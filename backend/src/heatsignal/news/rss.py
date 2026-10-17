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
            market_wide=bool(MARKET_WIDE.search(text)),
        ))
    return out
