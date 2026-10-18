"""
Polymarket: probabilidades implicitas de una lista ACOTADA de mercados (Gamma API publica, sin key).

Adaptado de TradingAgents (`tradingagents/dataflows/vendors/polymarket.py`, TauricResearch, Apache License 2.0):
`outcomes`/`outcomePrices` vienen como arrays JSON dentro de strings; `closed` es la marca confiable de resuelto
(`active` sigue en true en mercados liquidados); un `endDate` pasado = ya no mira hacia adelante; precio del "Yes" =
probabilidad implicita. Cambios: lista configurable en `config/polymarket_markets.json`, se guarda cada lectura con su
timestamp (append-only) en vez de devolver markdown, sin `requests`.

Endpoints (verificados el 2026-10-19 contra el SDK oficial `Polymarket/ts-sdk` y la guia oficial
`Polymarket/agent-skills/market-data.md`, y con una consulta real desde la VM: public-search, events/slug y
prices-history respondieron 200 con estos campos; `oneDayPriceChange` puede venir ausente en los mercados de la
busqueda, por eso la variacion de 24 h se calcula con lecturas propias. docs.polymarket.com y la API no resuelven
por DNS desde la red de la PC):
    Gamma (https://gamma-api.polymarket.com, sin auth)
      GET /public-search?q=...&limit_per_type=N     -> {"events": [Event, ...], "tags": [...], "pagination": {...}}
      GET /events/slug/{slug}                       -> Event (con "markets": [Market, ...])
      GET /markets/slug/{slug}                      -> Market
    Market: id, slug, question, outcomes (JSON string), outcomePrices (JSON string), volumeNum, volume24hr,
            liquidityNum, oneDayPriceChange, bestBid, bestAsk, lastTradePrice, endDate, closed, active,
            clobTokenIds (JSON string). Los numericos pueden venir como numero o como string decimal.
    CLOB (https://clob.polymarket.com, lectura sin auth; `Polymarket/clob-client` GET_PRICES_HISTORY)
      GET /prices-history?market=<token_id>&startTs=<s>&endTs=<s>&fidelity=<min>  -> {"history": [{"t", "p"}]}
      (solo para el backfill del laboratorio: no trae volumen ni liquidez).
"""

from __future__ import annotations

import json
import logging
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

log = logging.getLogger("heatsignal.macro.polymarket")

GAMMA_BASE = "https://gamma-api.polymarket.com"
CLOB_BASE = "https://clob.polymarket.com"
REQUEST_TIMEOUT_S = 20.0
CONFIG_PATH = Path(__file__).with_name("config") / "polymarket_markets.json"
DOC_URLS = (
    "https://github.com/Polymarket/agent-skills/blob/main/market-data.md",
    "https://github.com/Polymarket/ts-sdk (packages/client/src/actions/{search,events,markets}.ts)",
    "https://github.com/Polymarket/clob-client (src/endpoints.ts: /prices-history)",
)

HttpGet = Callable[[str, float], tuple[int, bytes]]


class PolymarketError(RuntimeError):
    """Fallo tipado (red, HTTP, JSON). Nunca en silencio: termina en `macro_runs`."""


def _default_get(url: str, timeout_s: float) -> tuple[int, bytes]:
    req = urllib.request.Request(url, headers={"User-Agent": "heatsignal-macro/1", "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise PolymarketError(f"error de red contra {urllib.parse.urlsplit(url).netloc}: "
                              f"{type(exc).__name__}: {getattr(exc, 'reason', exc)}") from None


@dataclass(frozen=True)
class Entry:
    key: str
    label: str
    kind: str                       # search | event | market
    q: str = ""
    slug: str = ""
    must_match: tuple[str, ...] = ()
    max_events: int = 1
    max_markets: int = 3
    end_within_days: float = 60.0
    prefer_uncertain: bool = False
    pick: str = "volume"            # volume | latest_end (el que vence mas tarde dentro de la ventana: "fin de mes")


@dataclass(frozen=True)
class MarketsConfig:
    config_id: str
    max_markets_total: int
    entries: tuple[Entry, ...] = field(default_factory=tuple)


def load_config(path: Path | str = CONFIG_PATH) -> MarketsConfig:
    """Lee y VALIDA la lista versionada. Un error de formato es una excepcion, no una lista vacia."""
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    entries = []
    keys: set[str] = set()
    for e in raw.get("entries") or []:
        kind = e.get("kind")
        if kind not in ("search", "event", "market"):
            raise ValueError(f"polymarket: kind invalido {kind!r} en {e.get('key')!r}")
        if kind == "search" and not e.get("q"):
            raise ValueError(f"polymarket: {e.get('key')!r} es search y no tiene q")
        if kind in ("event", "market") and not e.get("slug"):
            raise ValueError(f"polymarket: {e.get('key')!r} es {kind} y no tiene slug")
        if not e.get("key") or e["key"] in keys:
            raise ValueError(f"polymarket: key vacia o repetida {e.get('key')!r}")
        keys.add(e["key"])
        entries.append(Entry(key=e["key"], label=e.get("label") or e["key"], kind=kind, q=e.get("q", ""),
                             slug=e.get("slug", ""), must_match=tuple(_norm(w) for w in e.get("must_match") or []),
                             max_events=int(e.get("max_events", 1)), max_markets=int(e.get("max_markets", 3)),
                             end_within_days=float(e.get("end_within_days", 60)),
                             prefer_uncertain=bool(e.get("prefer_uncertain", False)),
                             pick=_pick_mode(e)))
    return MarketsConfig(config_id=str(raw.get("config_id") or "sin-id"),
                         max_markets_total=int(raw.get("max_markets_total", 12)), entries=tuple(entries))


def _pick_mode(e: dict) -> str:
    mode = e.get("pick", "volume")
    if mode not in ("volume", "latest_end"):
        raise ValueError(f"polymarket: pick invalido {mode!r} en {e.get('key')!r}")
    return mode


def _norm(s: str) -> str:
    d = unicodedata.normalize("NFKD", str(s).lower())
    return "".join(c for c in d if not unicodedata.combining(c))


def parse_json_list(value: Any) -> list:
    """Gamma codifica `outcomes`, `outcomePrices` y `clobTokenIds` como arrays JSON dentro de strings."""
    if isinstance(value, list):
        return value
    try:
        out = json.loads(value)
        return out if isinstance(out, list) else []
    except (TypeError, ValueError):
        return []


def num(value: Any) -> float | None:
    """Numerico o string decimal ('DecimalishSchema' del SDK) -> float; el resto -> None."""
    if value is None or isinstance(value, bool):
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f if f == f and abs(f) != float("inf") else None


def _end_ms(m: dict) -> int | None:
    end = m.get("endDate")
    if not end:
        return None
    try:
        return int(datetime.fromisoformat(str(end).replace("Z", "+00:00")).timestamp() * 1000)
    except ValueError:
        return None


def is_forward_looking(m: dict, now_ms: int) -> bool:
    """Abierto y que vence en el futuro (criterio de TradingAgents, ver docstring del modulo)."""
    if m.get("closed"):
        return False
    end = _end_ms(m)
    if end is not None and end < now_ms:
        return False
    return bool(parse_json_list(m.get("outcomePrices"))) and bool(parse_json_list(m.get("outcomes")))


def market_row(m: dict, *, fetched_ms: int, config_key: str, event_slug: str | None) -> dict[str, Any] | None:
    """Fila de `polymarket_snapshots` con la probabilidad del 'Yes' (o del primer resultado)."""
    outcomes = parse_json_list(m.get("outcomes"))
    prices = parse_json_list(m.get("outcomePrices"))
    tokens = parse_json_list(m.get("clobTokenIds"))
    if not outcomes or not prices or m.get("id") in (None, ""):
        return None
    idx = next((i for i, o in enumerate(outcomes) if _norm(o) in ("yes", "si")), 0)
    prob = num(prices[idx]) if idx < len(prices) else None
    if prob is None or not 0.0 <= prob <= 1.0:
        return None
    return {
        "market_id": str(m["id"]), "fetched_ms": fetched_ms, "source": "gamma", "slug": m.get("slug"),
        "event_slug": event_slug, "config_key": config_key, "question": (m.get("question") or "")[:300],
        "outcome": str(outcomes[idx]), "prob": prob, "volume": num(m.get("volumeNum") if m.get("volumeNum") is not None else m.get("volume")),
        "volume_24h": num(m.get("volume24hr")),
        "liquidity": num(m.get("liquidityNum") if m.get("liquidityNum") is not None else m.get("liquidity")),
        "one_day_change": num(m.get("oneDayPriceChange")), "best_bid": num(m.get("bestBid")),
        "best_ask": num(m.get("bestAsk")), "end_date": m.get("endDate"), "closed": int(bool(m.get("closed"))),
        "token_id": str(tokens[idx]) if idx < len(tokens) else None,
    }


class PolymarketClient:
    def __init__(self, http_get: HttpGet | None = None, timeout_s: float = REQUEST_TIMEOUT_S) -> None:
        self._get = http_get or _default_get
        self._timeout = timeout_s

    def _json(self, base: str, path: str, params: dict[str, Any] | None = None) -> Any:
        url = f"{base}{path}" + ("?" + urllib.parse.urlencode(params) if params else "")
        status, body = self._get(url, self._timeout)
        if status == 404:
            return None
        if status != 200:
            raise PolymarketError(f"{base}{path}: HTTP {status}")
        try:
            return json.loads(body)
        except ValueError:
            raise PolymarketError(f"{base}{path}: JSON ilegible") from None

    def search_events(self, q: str, limit: int = 20) -> list[dict]:
        data = self._json(GAMMA_BASE, "/public-search", {"q": q, "limit_per_type": limit})
        return list((data or {}).get("events") or [])

    def event_by_slug(self, slug: str) -> dict | None:
        return self._json(GAMMA_BASE, "/events/slug/" + urllib.parse.quote(slug, safe=""))

    def market_by_slug(self, slug: str) -> dict | None:
        return self._json(GAMMA_BASE, "/markets/slug/" + urllib.parse.quote(slug, safe=""))

    def price_history(self, token_id: str, start_s: int, end_s: int, fidelity_min: int = 60) -> list[tuple[int, float]]:
        """Serie (ms, prob) del CLOB para el backfill. Ventanas de <= 14 dias por pedido."""
        out: list[tuple[int, float]] = []
        step = 14 * 86400
        for a in range(start_s, end_s, step):
            data = self._json(CLOB_BASE, "/prices-history",
                              {"market": token_id, "startTs": a, "endTs": min(a + step, end_s), "fidelity": fidelity_min})
            pts = data.get("history") if isinstance(data, dict) else data
            for p in pts or []:
                t, v = num(p.get("t")), num(p.get("p"))
                if t is not None and v is not None and 0 <= v <= 1:
                    out.append((int(t * 1000) if t < 1e12 else int(t), v))
        return sorted(set(out))


def _pick_markets(markets: list[dict], e: Entry, now_ms: int) -> list[dict]:
    live = [m for m in markets if is_forward_looking(m, now_ms)]

    def informative(m: dict) -> bool:
        prices = parse_json_list(m.get("outcomePrices"))
        p = num(prices[0]) if prices else None
        return p is not None and 0.05 <= p <= 0.95

    vol = lambda m: num(m.get("volumeNum")) or num(m.get("volume")) or 0.0  # noqa: E731
    live.sort(key=lambda m: ((informative(m) if e.prefer_uncertain else True), vol(m)), reverse=True)
    return live[: e.max_markets]


def collect(client: PolymarketClient, cfg: MarketsConfig, now_ms: int | None = None
            ) -> tuple[list[dict[str, Any]], list[str]]:
    """Lee todas las entradas. Devuelve (filas, errores por entrada). Una entrada que falla no tumba al resto."""
    now_ms = now_ms or int(time.time() * 1000)
    rows: list[dict[str, Any]] = []
    errors: list[str] = []
    for e in cfg.entries:
        try:
            events: list[dict] = []
            if e.kind == "search":
                horizon = now_ms + int(e.end_within_days * 86_400_000)
                cands = []
                for ev in client.search_events(e.q):
                    title = _norm(ev.get("title") or ev.get("slug") or "")
                    if ev.get("closed") or any(w not in title for w in e.must_match):
                        continue
                    end = _end_ms(ev)
                    if end is not None and (end < now_ms or end > horizon):
                        continue
                    cands.append(ev)
                if e.pick == "latest_end":
                    cands.sort(key=lambda ev: (_end_ms(ev) or 0, num(ev.get("volume")) or 0.0), reverse=True)
                else:
                    cands.sort(key=lambda ev: num(ev.get("volume")) or 0.0, reverse=True)
                events = cands[: e.max_events]
                if not events:
                    errors.append(f"{e.key}: la busqueda {e.q!r} no encontro eventos abiertos que venzan en "
                                  f"{e.end_within_days:g} dias")
            elif e.kind == "event":
                ev = client.event_by_slug(e.slug)
                if ev is None:
                    errors.append(f"{e.key}: no existe el evento {e.slug!r}")
                else:
                    events = [ev]
            else:
                m = client.market_by_slug(e.slug)
                if m is None:
                    errors.append(f"{e.key}: no existe el mercado {e.slug!r}")
                elif not is_forward_looking(m, now_ms):
                    errors.append(f"{e.key}: el mercado {e.slug!r} esta cerrado o vencido")
                else:
                    r = market_row(m, fetched_ms=now_ms, config_key=e.key, event_slug=None)
                    rows += [r] if r else []
            for ev in events:
                picked = _pick_markets(list(ev.get("markets") or []), e, now_ms)
                if not picked:
                    errors.append(f"{e.key}: el evento {ev.get('slug')!r} no tiene mercados abiertos")
                for m in picked:
                    r = market_row(m, fetched_ms=now_ms, config_key=e.key, event_slug=ev.get("slug"))
                    if r:
                        rows.append(r)
        except PolymarketError as exc:
            errors.append(f"{e.key}: {exc}")
    if len(rows) > cfg.max_markets_total:
        errors.append(f"se leyeron {len(rows)} mercados; se guardan los primeros {cfg.max_markets_total} (tope)")
        rows = rows[: cfg.max_markets_total]
    return rows, errors


def labels(cfg: MarketsConfig) -> dict[str, str]:
    return {e.key: e.label for e in cfg.entries}


def utc_now_ms() -> int:
    return int(datetime.now(timezone.utc).timestamp() * 1000)
