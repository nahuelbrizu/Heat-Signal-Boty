"""
MemoryWindowSink: ventana deslizante de eventos SOLO en memoria (modo "pass-through").

Decision (ver decisions.md, "Datos de mercado solo en memoria"): en la etapa de prueba los
datos de mercado no se escriben a disco ni se exponen. Cada evento vive en RAM mientras
este dentro de la retencion de su tipo y despues se descarta. Lo unico que persiste son
las hipotesis y sus resoluciones (`evaluation/pending_store.py`).

Diseno:
  - Una deque por (tipo, simbolo). Se ordena por llegada y se poda por `ts_ingest_ms`,
    que es monotono por llegada. `ts_event_ms` puede venir desordenado: OKX manda
    liquidaciones tarde.
  - Retencion por tipo (trades, tickers y liquidaciones tienen volumenes muy distintos)
    y un tope duro de eventos por (tipo, simbolo) para acotar la RAM. Un TradeEvent ocupa
    ~1.1 KB: 2 simbolos x 3 exchanges ~ 80 trades/s -> 30 min ~ 160 MB.
  - Sin fallos silenciosos: si el tope expulsa eventos que todavia estaban dentro de la
    retencion, la ventana queda incompleta. Se cuenta, se loguea y `data_gaps()` lo declara.
  - Cada reinicio vacia la ventana (calentamiento). `data_gaps()` tambien lo declara
    mientras la ventana pedida sea mas larga que lo acumulado desde el arranque.
"""

from __future__ import annotations

import logging
import time
from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass, field

from ..schema.events import IngestEvent, LiquidationEvent, TickerEvent, TradeEvent

log = logging.getLogger("heatsignal.storage.memory")

KINDS = ("liquidation", "trade", "ticker")

DEFAULT_RETENTION_S: dict[str, float] = {
    "liquidation": 24 * 3600.0,  # pocas por minuto; el heatmap quiere ventanas largas
    "trade": 30 * 60.0,  # ATR (15 barras de 60s) + regimen (20 barras) entran holgados
    "ticker": 2 * 3600.0,  # throttled a 1/s por source+symbol
}
DEFAULT_MAX_EVENTS: dict[str, int] = {
    "liquidation": 200_000,
    "trade": 300_000,
    "ticker": 50_000,
}


def _now_ms() -> int:
    return int(time.time() * 1000)


@dataclass
class MemoryStats:
    written: int = 0
    expired: int = 0  # salieron por retencion (normal)
    evicted_by_cap: int = 0  # salieron por el tope ANTES de vencer (hueco)
    by_kind: dict[str, int] = field(default_factory=lambda: {k: 0 for k in KINDS})

    def as_dict(self) -> dict[str, object]:
        return {
            "written": self.written,
            "expired": self.expired,
            "evicted_by_cap": self.evicted_by_cap,
            "in_memory": dict(self.by_kind),
        }


class MemoryWindowSink:
    """Sink en RAM con ventana deslizante. Cumple `EventSink` y nunca lanza en `write`."""

    def __init__(
        self,
        *,
        retention_s: Mapping[str, float] | None = None,
        max_events: Mapping[str, int] | None = None,
        clock_ms=_now_ms,
    ) -> None:
        self._retention_ms = {
            k: int(1000 * (retention_s or {}).get(k, DEFAULT_RETENTION_S[k])) for k in KINDS
        }
        self._max = {k: int((max_events or {}).get(k, DEFAULT_MAX_EVENTS[k])) for k in KINDS}
        for k in KINDS:
            if self._retention_ms[k] <= 0 or self._max[k] <= 0:
                raise ValueError(f"retencion y tope de {k!r} deben ser > 0")
        self._clock_ms = clock_ms
        self.started_at_ms = clock_ms()
        self._buf: dict[tuple[str, str], deque[IngestEvent]] = {}
        # Hasta que ts_ingest_ms la ventana de (tipo, simbolo) tiene huecos por el tope.
        self._cap_hole_until_ms: dict[tuple[str, str], int] = {}
        self.stats = MemoryStats()

    # ---- escritura ------------------------------------------------------------------

    async def write(self, event: IngestEvent) -> None:
        kind = event.kind
        if kind not in self._retention_ms:
            log.warning("MemoryWindowSink: tipo de evento desconocido %r - descartado", kind)
            return
        key = (kind, event.symbol)
        buf = self._buf.setdefault(key, deque())
        buf.append(event)
        self.stats.written += 1
        self.stats.by_kind[kind] += 1
        self._expire(key, buf, self._clock_ms())
        if len(buf) > self._max[kind]:
            dropped = buf.popleft()
            self.stats.by_kind[kind] -= 1
            self.stats.evicted_by_cap += 1
            self._cap_hole_until_ms[key] = dropped.ts_ingest_ms
            if self.stats.evicted_by_cap == 1 or self.stats.evicted_by_cap % 10_000 == 0:
                log.warning(
                    "MemoryWindowSink: tope de %d %s para %s alcanzado - la ventana queda "
                    "incompleta (expulsados por tope: %d). Bajar la retencion o subir el tope.",
                    self._max[kind], kind, event.symbol, self.stats.evicted_by_cap,
                )

    def _expire(self, key: tuple[str, str], buf: deque[IngestEvent], now_ms: int) -> None:
        cutoff = now_ms - self._retention_ms[key[0]]
        while buf and buf[0].ts_ingest_ms < cutoff:
            buf.popleft()
            self.stats.expired += 1
            self.stats.by_kind[key[0]] -= 1

    def prune(self) -> None:
        """Poda por retencion todas las ventanas (las de simbolos sin trafico tambien)."""
        now = self._clock_ms()
        for key, buf in self._buf.items():
            self._expire(key, buf, now)

    async def close(self) -> None:
        # Pass-through: al cerrar no hay nada que volcar; los datos se descartan a proposito.
        self._buf.clear()
        self.stats.by_kind = {k: 0 for k in KINDS}

    # ---- lectura (para build_feature_set) ---------------------------------------------

    def _events(self, kind: str, symbol: str) -> list:
        self.prune()
        return list(self._buf.get((kind, symbol), ()))

    def liquidations(self, symbol: str) -> list[LiquidationEvent]:
        return self._events("liquidation", symbol)

    def trades(self, symbol: str) -> list[TradeEvent]:
        return self._events("trade", symbol)

    def tickers(self, symbol: str) -> list[TickerEvent]:
        return self._events("ticker", symbol)

    def summary(self) -> dict[str, dict[str, object]]:
        """Vista liviana por simbolo para el panel: conteos y ultimos valores (sin copiar ventanas)."""
        self.prune()
        out: dict[str, dict[str, object]] = {}
        for (kind, symbol), buf in self._buf.items():
            row = out.setdefault(symbol, {"counts": {k: 0 for k in KINDS}, "mark_by_source": {},
                                          "last_trade": None, "last_liquidation": None})
            row["counts"][kind] = len(buf)  # type: ignore[index]
            if not buf:
                continue
            if kind == "trade":
                t = buf[-1]
                row["last_trade"] = {"source": t.source.value, "price": t.price, "side": t.side.value,
                                     "ts_ms": t.ts_event_ms}
            elif kind == "liquidation":
                liq = buf[-1]
                row["last_liquidation"] = {"source": liq.source.value, "price": liq.price,
                                           "side": liq.side.value, "notional_usd": liq.notional_usd,
                                           "ts_ms": liq.ts_event_ms}
            elif kind == "ticker":
                marks: dict[str, float] = row["mark_by_source"]  # type: ignore[assignment]
                for ev in reversed(buf):
                    if ev.mark_price is not None and ev.source.value not in marks:
                        marks[ev.source.value] = ev.mark_price
                    if len(marks) == 3:
                        break
        return out

    def data_gaps(self, symbol: str, window_s: int) -> list[str]:
        """Huecos de la ventana en memoria, en texto, para `FeatureSet.data_gaps`."""
        now = self._clock_ms()
        gaps: list[str] = []
        have_s = (now - self.started_at_ms) / 1000
        if have_s < window_s:
            gaps.append(
                f"memoria: calentamiento tras reinicio, {have_s:.0f}s acumulados de {window_s}s pedidos"
            )
        for kind in KINDS:
            retention_s = self._retention_ms[kind] / 1000
            if retention_s < window_s:
                gaps.append(f"memoria: {kind} se retienen {retention_s:.0f}s (< ventana {window_s}s)")
            hole_until = self._cap_hole_until_ms.get((kind, symbol))
            if hole_until is not None and hole_until >= now - window_s * 1000:
                gaps.append(
                    f"memoria: {kind} incompletos por tope de {self._max[kind]} eventos "
                    f"(faltan anteriores a {hole_until} ms)"
                )
        return gaps
