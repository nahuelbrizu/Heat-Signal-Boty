"""
Velas para el panel interno, de paso: se piden a Binance en el momento, se devuelven y no se guardan
(decisions.md, "Datos de mercado solo en memoria").

    GET https://fapi.binance.com/fapi/v1/klines?symbol=XRPUSDT&interval=1h&limit=500
    Respuesta: [[openTime, open, high, low, close, volume, closeTime, ...], ...]
    `limit` maximo 1500 (verificado contra la API real el 2026-10-17: 1501 -> code -1130).

Mismo endpoint que `evaluation/price_source.py`, pero velas de ultimo precio (no mark) porque es lo
que se mira en un grafico; la entrada de las hipotesis es mark price, la diferencia es chica.
"""

from __future__ import annotations

import asyncio
import json
import urllib.parse
from typing import Any

from ..evaluation.price_source import HttpGet, _urllib_get, to_binance_symbol

BASE_URL = "https://fapi.binance.com"
INTERVALS = ("1m", "5m", "15m", "1h", "4h", "1d", "1w")
MAX_LIMIT = 1500


class KlinesError(Exception):
    """Binance no devolvio velas validas (red, HTTP no 200, formato inesperado)."""


def build_url(symbol: str, interval: str, limit: int, base_url: str = BASE_URL) -> str:
    query = urllib.parse.urlencode({"symbol": to_binance_symbol(symbol), "interval": interval, "limit": limit})
    return f"{base_url}/fapi/v1/klines?{query}"


def parse_klines(body: bytes) -> list[dict[str, Any]]:
    try:
        payload = json.loads(body)
    except (ValueError, UnicodeDecodeError) as exc:
        raise KlinesError(f"respuesta no es JSON: {body[:200]!r}") from exc
    if not isinstance(payload, list):
        raise KlinesError(f"respuesta de klines no es una lista: {body[:200]!r}")
    out = []
    for k in payload:
        if not isinstance(k, list) or len(k) < 6:
            raise KlinesError(f"kline con formato inesperado: {k!r}")
        try:
            out.append({
                "t": int(k[0]) // 1000,  # segundos UTC, lo que espera lightweight-charts
                "o": float(k[1]), "h": float(k[2]), "l": float(k[3]), "c": float(k[4]), "v": float(k[5]),
            })
        except (TypeError, ValueError) as exc:
            raise KlinesError(f"kline con valores invalidos: {k!r}") from exc
    return out


async def fetch_klines(
    symbol: str, interval: str, limit: int, *, http_get: HttpGet | None = None, timeout_s: float = 10.0
) -> list[dict[str, Any]]:
    if interval not in INTERVALS:
        raise ValueError(f"interval debe ser uno de {INTERVALS}, recibido {interval!r}")
    if not 1 <= limit <= MAX_LIMIT:
        raise ValueError(f"limit debe estar entre 1 y {MAX_LIMIT}, recibido {limit}")
    url = build_url(symbol, interval, limit)
    try:
        status, body = await asyncio.to_thread(http_get or _urllib_get, url, timeout_s)
    except Exception as exc:  # noqa: BLE001 - PriceSourceError u otro fallo de red
        raise KlinesError(str(exc)) from exc
    if status != 200:
        raise KlinesError(f"HTTP {status} de Binance: {body[:200]!r}")
    return parse_klines(body)
