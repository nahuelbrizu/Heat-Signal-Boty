"""
Funding liquidado de Binance USDS-M (REST publico, sin API key) -> cache `funding_rates` de `paperbot.sqlite`.

Endpoint (verificado contra la doc oficial vigente el 2026-10-18,
developers.binance.com/docs/derivatives/usds-margined-futures/market-data/rest-api/Get-Funding-Rate-History):

    GET https://fapi.binance.com/fapi/v1/fundingRate?symbol=BTCUSDT&startTime=<ms>&limit=1000
    [{"symbol": "BTCUSDT", "fundingRate": "-0.03750000", "fundingTime": 1570608000000,
      "markPrice": "34287.54619963", "rateType": "Regular"}, ...]

- `limit` default 100, max 1000; sin startTime/endTime devuelve las 200 mas recientes; orden ascendente.
- Peso: comparte 500 pedidos / 5 min / IP con `/fapi/v1/fundingInfo` (aca: 7 pedidos por hora).
- `fundingTime` puede traer milisegundos de mas (p.ej. ...000004): se redondea al minuto (igual que `lab/store.py`).
- Solo se guardan tasas YA liquidadas (fundingTime <= ahora); la del intervalo en curso no se conoce todavia.
"""

from __future__ import annotations

import json
import time
import urllib.parse
from collections.abc import Callable

from ..evaluation.price_source import HttpGet, _urllib_get
from .store import PaperbotStore

FUNDING_URL = "https://fapi.binance.com/fapi/v1/fundingRate"
PAGE = 1000
MINUTE_MS = 60_000


class FundingFetchError(Exception):
    pass


def _round_minute(ms: int) -> int:
    return (int(ms) + 30_000) // MINUTE_MS * MINUTE_MS


def update_funding(store: PaperbotStore, symbol: str, since_ms: int, now_ms: int, *, http_get: HttpGet | None = None,
                   sleep: Callable[[float], None] = time.sleep) -> int:
    """Baja el funding liquidado desde max(since, ultima guardada + 1) hasta ahora. Devuelve filas nuevas.
    Cualquier respuesta inesperada levanta FundingFetchError (la corrida queda en error, no se opera)."""
    get = http_get or _urllib_get
    last = store.last_funding_ms(symbol)
    start = since_ms if last is None else max(since_ms, last + 1)
    added = 0
    for _ in range(50):  # tope de paginas (100 dias de funding cada 1 h = 2400 filas = 3 paginas)
        q = urllib.parse.urlencode({"symbol": symbol, "startTime": start, "limit": PAGE})
        status, body = get(f"{FUNDING_URL}?{q}", 15.0)
        if status != 200:
            raise FundingFetchError(f"{symbol}: HTTP {status} de fundingRate: {body[:200]!r}")
        try:
            payload = json.loads(body)
        except ValueError as exc:
            raise FundingFetchError(f"{symbol}: respuesta no JSON de fundingRate") from exc
        if not isinstance(payload, list):
            raise FundingFetchError(f"{symbol}: respuesta inesperada de fundingRate: {body[:200]!r}")
        rows = []
        for r in payload:
            try:
                t = _round_minute(int(r["fundingTime"]))
                rate = float(r["fundingRate"])
                mark = r.get("markPrice")
                mark = float(mark) if mark not in (None, "") else None
            except (KeyError, TypeError, ValueError) as exc:
                raise FundingFetchError(f"{symbol}: fila de funding ilegible: {r!r}") from exc
            if t <= now_ms:
                rows.append((symbol, t, rate, mark, now_ms))
        added += store.add_funding_rates(rows)
        if len(payload) < PAGE:
            return added
        nxt = int(payload[-1]["fundingTime"]) + 1
        if nxt <= start:
            return added
        start = nxt
        sleep(0.2)
    raise FundingFetchError(f"{symbol}: demasiadas paginas de funding")
