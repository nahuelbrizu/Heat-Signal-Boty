"""
Minutos de `bars_1m` (los que muestra "Datos guardados") para las velas de 5m/15m en vivo (features/live.py).

Lectura read-only de `heatsignal_history.sqlite`, sin cache: ~1.500 filas por simbolo (lo que va del dia UTC o
`min_hours`, lo que sea mas largo), pocos ms. Lo llaman el ciclo de analisis y el panel desde `asyncio.to_thread`.
Un fallo de la base no tumba nada: el contexto sale `missing` con el motivo (sin fallos silenciosos).
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from ..features.htf import DAY_MS
from ..features.live import build_live_context, missing_live, monitor_hypothesis
from ..schema.features import LiveContext

log = logging.getLogger("heatsignal.history.live")

EXCHANGE = "binance"
MIN_HOURS = 8  # 15m x 20 velas para la EMA20 = 5 h; con margen


class LiveCandleProvider:
    def __init__(self, history_db: str | Path | None, *, min_hours: int = MIN_HOURS) -> None:
        self._db = None if history_db is None else Path(history_db)
        self._min_ms = min_hours * 3_600_000

    def minutes(self, symbol: str, start_ms: int, end_ms: int) -> list[tuple]:
        from .store import open_readonly

        conn = open_readonly(self._db)  # type: ignore[arg-type]
        try:
            return conn.execute(
                "SELECT minute_ms, open, high, low, close, buy_usd + sell_usd + block_usd FROM bars_1m "
                "WHERE exchange=? AND symbol=? AND minute_ms >= ? AND minute_ms < ? ORDER BY minute_ms",
                (EXCHANGE, symbol, start_ms, end_ms)).fetchall()
        finally:
            conn.close()

    def filled_minutes(self, symbol: str, start_ms: int, end_ms: int) -> tuple[list[tuple], list[str]]:
        """`bars_1m` + los minutos que faltan tomados de `klines` (1m de Binance por REST, ya bajados).

        Las velas REST traen precio y volumen pero no el flujo propio (buy/sell/bloques): sirven para las velas
        de 5m/15m y el VWAP, y se declaran en data_gaps (sin fallos silenciosos)."""
        from .store import open_readonly

        rows = self.minutes(symbol, start_ms, end_ms)
        have = {r[0] for r in rows}
        conn = open_readonly(self._db)  # type: ignore[arg-type]
        try:
            rest = conn.execute(
                "SELECT open_time_ms, open, high, low, close, quote_volume FROM klines "
                "WHERE exchange=? AND symbol=? AND interval='1m' AND open_time_ms >= ? AND open_time_ms < ? "
                "ORDER BY open_time_ms", (EXCHANGE, symbol, start_ms, end_ms)).fetchall()
        except Exception as exc:  # noqa: BLE001 - sin tabla klines: solo bars_1m, declarado
            return rows, [f"live: no se pudieron leer las velas REST para rellenar huecos ({type(exc).__name__})"]
        finally:
            conn.close()
        extra = [r for r in rest if r[0] not in have and r[0] + 60_000 <= end_ms]
        if not extra:
            return rows, []
        merged = sorted([*rows, *extra], key=lambda r: r[0])
        return merged, [f"live: {len(extra)} minuto(s) rellenados con velas 1m REST de Binance (precio y volumen, "
                        "sin flujo propio)"]

    def _start(self, now_ms: int, since_ms: int | None = None) -> int:
        start = min(now_ms - now_ms % DAY_MS, now_ms - self._min_ms)  # dia UTC completo (VWAP) y la EMA de 15m
        start -= start % (15 * 60_000)  # borde de vela de 15m: la primera no queda cortada (ni se descarta)
        return start if since_ms is None else min(start, since_ms - since_ms % 60_000)

    def context(self, symbol: str, now_ms: int) -> LiveContext:
        if self._db is None or not self._db.exists():
            return missing_live(symbol, now_ms, "base historica no disponible")
        try:
            rows, gaps = self.filled_minutes(symbol, self._start(now_ms), now_ms)
            return build_live_context(symbol, rows, now_ms, gaps)
        except Exception as exc:  # noqa: BLE001 - se declara en el contexto
            log.exception("live: fallo el contexto de %s", symbol)
            return missing_live(symbol, now_ms, f"error leyendo bars_1m: {exc!r}")

    def monitor(self, symbol: str, hyps: list[dict[str, Any]], now_ms: int) -> tuple[LiveContext, list[dict[str, Any]]]:
        """Contexto en vivo + seguimiento de las hipotesis abiertas de ese simbolo (una sola lectura)."""
        if self._db is None or not self._db.exists():
            live = missing_live(symbol, now_ms, "base historica no disponible")
            return live, [monitor_hypothesis(h, [], live, now_ms) for h in hyps]
        try:
            since = min([h["created_ms"] for h in hyps] or [now_ms])
            rows, gaps = self.filled_minutes(symbol, self._start(now_ms, since), now_ms)
            day = self._start(now_ms)
            live = build_live_context(symbol, [r for r in rows if r[0] >= day], now_ms, gaps)
            return live, [monitor_hypothesis(h, rows, live, now_ms) for h in hyps]
        except Exception as exc:  # noqa: BLE001
            log.exception("live: fallo el monitoreo de %s", symbol)
            live = missing_live(symbol, now_ms, f"error leyendo bars_1m: {exc!r}")
            return live, [monitor_hypothesis(h, [], live, now_ms) for h in hyps]
