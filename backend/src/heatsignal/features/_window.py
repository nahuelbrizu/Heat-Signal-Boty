"""
Utilidades comunes de ventana temporal para `features/`.

Convencion unica (point-in-time, sin lookahead): una ventana de `window_s` que
termina en `now_ms` es el intervalo semiabierto (now_ms - window_s*1000, now_ms].
Un evento con ts_event_ms > now_ms es "del futuro" respecto del snapshot y se
excluye siempre — un backtest que lo incluyera tendria lookahead.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import TypeVar

from ..schema.events import Exchange

E = TypeVar("E")

# Orden canonico de exchanges para desempates y listas ordenadas (deterministico).
EXCHANGE_ORDER: tuple[Exchange, ...] = (Exchange.BINANCE, Exchange.BYBIT, Exchange.OKX)


def exchange_rank(ex: Exchange) -> int:
    return EXCHANGE_ORDER.index(ex)


def sorted_exchanges(exs: Iterable[Exchange]) -> list[Exchange]:
    return sorted(set(exs), key=exchange_rank)


def window_start_ms(now_ms: int, window_s: int) -> int:
    if window_s <= 0:
        raise ValueError("window_s debe ser > 0")
    return now_ms - window_s * 1000


def in_window(ts_ms: int, now_ms: int, window_s: int) -> bool:
    """True si ts esta en (now - window, now]."""
    return window_start_ms(now_ms, window_s) < ts_ms <= now_ms


def select(events: Iterable[E], symbol: str, now_ms: int, window_s: int | None = None) -> list[E]:
    """Filtra por simbolo y por tiempo (<= now; y dentro de la ventana si window_s no es None).

    Devuelve los eventos ordenados por (ts_event_ms, exchange) para que todo calculo
    posterior sea deterministico independientemente del orden de llegada.
    """
    out = []
    for ev in events:
        if ev.symbol != symbol:  # type: ignore[attr-defined]
            continue
        ts = ev.ts_event_ms  # type: ignore[attr-defined]
        if ts > now_ms:
            continue
        if window_s is not None and not in_window(ts, now_ms, window_s):
            continue
        out.append(ev)
    out.sort(key=lambda e: (e.ts_event_ms, exchange_rank(e.source)))  # type: ignore[attr-defined]
    return out
