"""
Datos para el analisis de sesiones (`features/sessions.py`) desde nuestra base historica. Sin pandas: SQL + Python.

Minutos de precio (vela de apertura y rango de la sesion anterior), por minuto y en este orden:
    1. `klines` 1m de Binance (base), 2. `bars_1m` propios del exchange binance (trades del WS),
    3. REST de Binance `GET /fapi/v1/klines?interval=1m` (verificado en `api/klines.py`) solo para los minutos que
       faltan en la ventana, un pedido por ventana. Lo que siga faltando se declara.
Volumen medio de la misma hora local en los 20 dias previos: solo base (sin REST), dias con los 60 minutos.
Datos propios del dia: `bars_1m` de TODOS los exchanges desde las 00:00 UTC hasta el disparo (CVD, liquidaciones,
ultimo funding y OI por exchange).

Costo por sesion y simbolo: ~4 consultas acotadas (<= 1 dia de 1m) + 1 consulta de 20 ventanas de 60 minutos.
Corre 4 veces por dia, en un hilo aparte.
"""

from __future__ import annotations

import json
import logging
import urllib.parse
from collections.abc import Callable, Sequence
from datetime import timedelta
from pathlib import Path
from typing import Any

from ..evaluation.price_source import HttpGet, _urllib_get, to_binance_symbol
from ..features.sessions import (
    DAY_MS,
    HOUR_MS,
    VOL_DAYS,
    SessionDef,
    _utc_date,
    analyze_symbol,
    build_analysis,
    opening_candle,
    prev_open,
)
from ..schema.sessions import DayOwnData, SessionAnalysis, SessionSymbolAnalysis

log = logging.getLogger("heatsignal.history.session_source")

EXCHANGE = "binance"
REST_BASE_URL = "https://fapi.binance.com"
MINUTE_MS = 60_000


def _minutes_db(conn: Any, symbol: str, start: int, end: int) -> dict[int, tuple]:
    out: dict[int, tuple] = {}
    for r in conn.execute(
        "SELECT open_time_ms, open, high, low, close, quote_volume FROM klines WHERE exchange=? AND symbol=? "
        "AND interval='1m' AND open_time_ms >= ? AND open_time_ms < ?", (EXCHANGE, symbol, start, end)):
        out[int(r[0])] = (*r, "klines")
    for r in conn.execute(
        "SELECT minute_ms, open, high, low, close, buy_usd + sell_usd + block_usd FROM bars_1m WHERE exchange=? "
        "AND symbol=? AND minute_ms >= ? AND minute_ms < ? AND open IS NOT NULL", (EXCHANGE, symbol, start, end)):
        out.setdefault(int(r[0]), (*r, "bars_1m"))
    return out


def own_day(conn: Any, symbol: str, start: int, end: int) -> DayOwnData:
    """Agregados propios (bars_1m, todos los exchanges) en [start, end)."""
    w = DayOwnData()
    rows = conn.execute(
        "SELECT exchange, COUNT(*), SUM(buy_usd), SUM(sell_usd), SUM(liq_long_usd), SUM(liq_short_usd), SUM(n_liqs) "
        "FROM bars_1m WHERE symbol=? AND minute_ms >= ? AND minute_ms < ? GROUP BY exchange", (symbol, start, end)).fetchall()
    for ex, n, b, s, ll, ls, nl in rows:
        w.exchanges.append(ex)
        w.minutes = max(w.minutes, int(n))
        w.buy_usd += b or 0.0
        w.sell_usd += s or 0.0
        w.liq_long_usd += ll or 0.0
        w.liq_short_usd += ls or 0.0
        w.n_liqs += int(nl or 0)
    w.cvd_usd = w.buy_usd - w.sell_usd
    # SQLite: con MAX()/MIN() las columnas "sueltas" salen de la fila del maximo/minimo (ultimo/primer dato).
    for ex, _, f in conn.execute(
        "SELECT exchange, MAX(minute_ms), funding_8h FROM bars_1m WHERE symbol=? AND minute_ms >= ? AND minute_ms < ? "
        "AND funding_8h IS NOT NULL GROUP BY exchange", (symbol, start, end)):
        w.funding_by_exchange[ex] = f
    if w.funding_by_exchange:
        w.funding_8h_mean = sum(w.funding_by_exchange.values()) / len(w.funding_by_exchange)
    last = {ex: oi for ex, _, oi in conn.execute(
        "SELECT exchange, MAX(minute_ms), oi_usd FROM bars_1m WHERE symbol=? AND minute_ms >= ? AND minute_ms < ? "
        "AND oi_usd IS NOT NULL GROUP BY exchange", (symbol, start, end))}
    first = {ex: oi for ex, _, oi in conn.execute(
        "SELECT exchange, MIN(minute_ms), oi_usd FROM bars_1m WHERE symbol=? AND minute_ms >= ? AND minute_ms < ? "
        "AND oi_usd IS NOT NULL GROUP BY exchange", (symbol, start, end))}
    if last:
        w.oi_usd = sum(last.values())
        common = [ex for ex in last if ex in first and first[ex]]
        if common:
            a, b = sum(first[ex] for ex in common), sum(last[ex] for ex in common)
            w.oi_change_pct = (b / a - 1) * 100 if a else None
    return w


class SessionDataSource:
    """Arma el `SessionAnalysis` de una sesion para todos los simbolos (sync: llamar con asyncio.to_thread)."""

    def __init__(self, history_db: str | Path | None, htf: Any, *, http_get: HttpGet | None = None, rest: bool = True,
                 rest_base_url: str = REST_BASE_URL, connect: Callable[[Path], Any] | None = None) -> None:
        self._db = None if history_db is None else Path(history_db)
        self._htf = htf  # history.hourly.HourlyCandleProvider (tendencia diaria + ATR de 1h)
        self._http_get = http_get or _urllib_get
        self._rest = rest
        self._base = rest_base_url.rstrip("/")
        self._connect = connect
        self.stats = {"rest_requests": 0, "rest_errors": 0}

    def _open(self) -> Any:
        if self._connect is not None:
            return self._connect(self._db)  # type: ignore[arg-type]
        from .store import open_readonly

        return open_readonly(self._db)  # type: ignore[arg-type]

    def _rest_minutes(self, symbol: str, start: int, end: int, gaps: list[str]) -> dict[int, tuple]:
        n = min(1000, max(1, (end - start) // MINUTE_MS))
        q = urllib.parse.urlencode({"symbol": to_binance_symbol(symbol), "interval": "1m", "startTime": start,
                                    "endTime": end - 1, "limit": n})
        self.stats["rest_requests"] += 1
        try:
            status, body = self._http_get(f"{self._base}/fapi/v1/klines?{q}", 10.0)
            if status != 200:
                raise ValueError(f"HTTP {status}")
            payload = json.loads(body)
            if not isinstance(payload, list):
                raise ValueError("respuesta no es lista")
            return {int(k[0]): (int(k[0]), float(k[1]), float(k[2]), float(k[3]), float(k[4]), float(k[7]), "rest")
                    for k in payload if start <= int(k[0]) < end}
        except Exception as exc:  # noqa: BLE001
            self.stats["rest_errors"] += 1
            gaps.append(f"sesion: fallo el REST de Binance para completar minutos ({exc})")
            return {}

    def _window(self, conn: Any, symbol: str, start: int, end: int, now_ms: int, gaps: list[str]) -> dict[int, tuple]:
        """Minutos CERRADOS de [start, end) con la cadena base -> REST."""
        end = min(end, now_ms - now_ms % MINUTE_MS)
        got = _minutes_db(conn, symbol, start, end) if conn is not None else {}
        expected = (end - start) // MINUTE_MS
        if len(got) < expected and self._rest:
            rest = self._rest_minutes(symbol, start, end, gaps)
            for k, v in rest.items():
                got.setdefault(k, v)
            n_rest = sum(1 for v in got.values() if v[6] == "rest")
            if n_rest:
                gaps.append(f"sesion: {n_rest} minuto(s) vinieron del REST de Binance (faltaban en la base)")
        if len(got) < expected:
            gaps.append(f"sesion: faltan {expected - len(got)} de {expected} minutos de precio en la ventana")
        return got

    def _vol_history(self, conn: Any, d: SessionDef, symbol: str, open_ms: int) -> list[float]:
        if conn is None:
            return []
        local = _utc_date(open_ms)
        ranges = []
        for k in range(1, VOL_DAYS + 3):
            o = d.open_utc_ms(local - timedelta(days=k))
            if o < open_ms:
                ranges.append(o)
            if len(ranges) == VOL_DAYS:
                break
        out = []
        for o in ranges:
            mins = _minutes_db(conn, symbol, o, o + HOUR_MS)
            if len(mins) == 60 and all(v[5] is not None for v in mins.values()):
                out.append(sum(v[5] for v in mins.values()))
        return out

    def analyze(self, defs: Sequence[SessionDef], d: SessionDef, open_ms: int, symbols: Sequence[str],
                now_ms: int) -> SessionAnalysis:
        trigger = open_ms + HOUR_MS
        conn = None
        base_gaps: list[str] = []
        if self._db is None or not self._db.exists():
            base_gaps.append("sesion: base historica no disponible (precio solo por REST, sin datos propios)")
        else:
            try:
                conn = self._open()
            except Exception as exc:  # noqa: BLE001
                base_gaps.append(f"sesion: no se pudo abrir la base historica ({exc})")
        out: dict[str, SessionSymbolAnalysis] = {}
        try:
            for sym in symbols:
                gaps = list(base_gaps)
                try:
                    out[sym] = self._one(conn, defs, d, sym, open_ms, trigger, now_ms, gaps)
                except Exception as exc:  # noqa: BLE001 - un simbolo no frena a los demas; queda declarado
                    log.exception("sesion %s %s: fallo el analisis", d.name, sym)
                    out[sym] = analyze_symbol(d, sym, open_ms, None, None, None, None,
                                              [*gaps, f"sesion: error en el analisis por codigo: {exc!r}"])
        finally:
            if conn is not None:
                conn.close()
        return build_analysis(d, open_ms, out)

    def _one(self, conn: Any, defs: Sequence[SessionDef], d: SessionDef, sym: str, open_ms: int, trigger: int,
             now_ms: int, gaps: list[str]) -> SessionSymbolAnalysis:
        htf = None
        if self._htf is not None:
            # Solo velas cerradas al momento del disparo (un analisis atrasado no ve lo que paso despues).
            htf = self._htf.context(sym, min(now_ms, trigger), day_start_ms=open_ms - open_ms % DAY_MS,
                                    real_now_ms=now_ms)
            gaps += [g for g in htf.data_gaps if g not in gaps]
        mins = self._window(conn, sym, open_ms, trigger, now_ms, gaps)
        sources: dict[str, int] = {}
        for v in mins.values():
            sources[v[6]] = sources.get(v[6], 0) + 1
        opening = opening_candle([v[:6] for v in mins.values()], open_ms, None if htf is None else htf.atr14_1h,
                                 self._vol_history(conn, d, sym, open_ms), sources)
        prev = None
        if d.role == "open":
            p = prev_open(defs, open_ms)
            if p is not None:
                pm = self._window(conn, sym, p[1], open_ms, now_ms, gaps)
                if pm:
                    prev = (p[0].name, max(v[2] for v in pm.values()), min(v[3] for v in pm.values()))
        own = None
        if conn is not None:
            day0 = open_ms - open_ms % DAY_MS
            own = own_day(conn, sym, day0, trigger)
            if not own.minutes:
                gaps.append("sesion: sin barras propias del dia (grabacion apagada o caida)")
        return analyze_symbol(d, sym, open_ms, opening, htf, prev, own, gaps)
