"""
Velas de 1h CERRADAS desde nuestra base historica, con cache incremental, para el contexto multi-temporalidad
(`features/htf.py`). Corre en el proceso de ingesta, cada ciclo de analisis y en el panel: sin pandas.

Cadena de resolucion por hora (explicita, sin fallback silencioso):
    1. `klines` 1m de Binance en `heatsignal_history.sqlite` si la hora tiene sus 60 minutos (fuente "klines").
       La descarga es diaria (00:20 UTC): para las horas de hoy casi nunca estan.
    2. `bars_1m` propios del exchange binance (trades del WS) si hay >= 50 minutos con precio (fuente "bars_1m";
       si son < 60 se declara en data_gaps de la vela/contexto).
    3. REST de Binance `GET /fapi/v1/klines?interval=1h` (verificado en `api/klines.py`), solo para las horas que
       faltan, en UN pedido por simbolo, y como mucho una vez por `retry_ms` por hora faltante (fuente "rest").
    4. Lo que siga faltando queda como hueco declarado.

Costo: la primera llamada por simbolo lee `lookback_h` horas (~35 dias, ~50k filas de 1m: < 0.1 s en la VM); las
siguientes solo piden las horas nuevas. Una hora recien cerrada se toma de la base recien `db_grace_ms` despues de
cerrar (el grabador escribe el minuto ~15 s despues); antes de eso va a REST.
"""

from __future__ import annotations

import json
import logging
import threading
import urllib.parse
from collections.abc import Callable
from pathlib import Path

from ..evaluation.price_source import HttpGet, _urllib_get, to_binance_symbol
from ..features.htf import HOUR_MS, build_htf_context, missing_context
from ..schema.features import Candle1h, HtfContext

log = logging.getLogger("heatsignal.history.hourly")

DEFAULT_LOOKBACK_H = 24 * 35
MIN_BAR_MINUTES = 50
DEFAULT_DB_GRACE_MS = 60_000
DEFAULT_RETRY_MS = 3_600_000
REST_BASE_URL = "https://fapi.binance.com"
REST_MAX_LIMIT = 1500
EXCHANGE = "binance"


def _hour(ms: int) -> int:
    return ms - ms % HOUR_MS


def _vwap_parts(g: list[tuple], quote: bool) -> tuple[float | None, float | None]:
    """Sumas para el VWAP (`htf-levels-v1`): precio tipico (H+L+C)/3 x volumen BASE de cada vela 1m.

    `rows[5]` es el volumen: base (klines.volume) o USD (bars_1m: buy+sell+block; base = USD / precio tipico).
    Sin columna de volumen -> (None, None) (el VWAP queda sin_datos, nunca inventado)."""
    if not g or len(g[0]) < 6:
        return None, None
    pv = vol = 0.0
    for r in g:
        if r[5] is None:
            return None, None
        tp = (float(r[2]) + float(r[3]) + float(r[4])) / 3
        v = float(r[5]) / tp if quote and tp > 0 else (0.0 if quote else float(r[5]))
        pv += tp * v
        vol += v
    return pv, vol


def aggregate_minutes(rows: list[tuple], source: str, min_minutes: int, quote_volume: bool = False) -> dict[int, Candle1h]:
    """(minute_ms, o, h, l, c[, volumen]) ordenadas -> velas de 1h. Minutos sin precio (o NULL) no cuentan.

    Con la columna de volumen la vela trae ademas las sumas para el VWAP del dia (`vwap_pv`, `vwap_vol`)."""
    groups: dict[int, list[tuple]] = {}
    for r in rows:
        if r[1] is None or r[2] is None or r[3] is None or r[4] is None:
            continue
        groups.setdefault(_hour(int(r[0])), []).append(r)
    out: dict[int, Candle1h] = {}
    for h, g in groups.items():
        if len(g) < min_minutes:
            continue
        g.sort(key=lambda r: r[0])
        pv, vol = _vwap_parts(g, quote_volume)
        out[h] = Candle1h(open_ms=h, open=float(g[0][1]), high=max(float(r[2]) for r in g),
                          low=min(float(r[3]) for r in g), close=float(g[-1][4]), source=source,  # type: ignore[arg-type]
                          minutes=min(60, len(g)), vwap_pv=pv, vwap_vol=vol)
    return out


def parse_rest_1h(body: bytes, now_ms: int) -> dict[int, Candle1h]:
    """Respuesta de /fapi/v1/klines (interval=1h) -> velas CERRADAS (closeTime < now)."""
    payload = json.loads(body)
    if not isinstance(payload, list):
        raise ValueError(f"respuesta de klines no es una lista: {body[:200]!r}")
    out: dict[int, Candle1h] = {}
    for k in payload:
        if not isinstance(k, list) or len(k) < 7:
            raise ValueError(f"kline con formato inesperado: {k!r}")
        if int(k[6]) >= now_ms:
            continue  # vela abierta
        t = int(k[0])
        out[t] = Candle1h(open_ms=t, open=float(k[1]), high=float(k[2]), low=float(k[3]), close=float(k[4]),
                          source="rest", minutes=60)
    return out


class HourlyCandleProvider:
    """Thread-safe (un lock): lo llaman el ciclo de analisis y el panel, cada uno desde `asyncio.to_thread`."""

    def __init__(
        self,
        history_db: str | Path | None,
        *,
        lookback_h: int = DEFAULT_LOOKBACK_H,
        http_get: HttpGet | None = None,
        rest: bool = True,
        db_grace_ms: int = DEFAULT_DB_GRACE_MS,
        retry_ms: int = DEFAULT_RETRY_MS,
        rest_base_url: str = REST_BASE_URL,
        connect: Callable[[Path], object] | None = None,
    ) -> None:
        self._db = None if history_db is None else Path(history_db)
        self._lookback_h = lookback_h
        self._http_get = http_get or _urllib_get
        self._rest = rest
        self._grace = db_grace_ms
        self._retry = retry_ms
        self._base = rest_base_url.rstrip("/")
        self._connect = connect
        self._lock = threading.Lock()
        self._cache: dict[str, dict[int, Candle1h]] = {}
        self._tried: dict[str, dict[int, int]] = {}  # hora faltante -> ultimo intento (ms)
        self.stats = {"db_queries": 0, "rest_requests": 0, "rest_errors": 0}

    # ---- lectura de la base -----------------------------------------------------------------------

    def _open(self):  # noqa: ANN202
        if self._connect is not None:
            return self._connect(self._db)  # type: ignore[arg-type]
        from .store import open_readonly

        return open_readonly(self._db)  # type: ignore[arg-type]

    def _from_db(self, symbol: str, start_ms: int, end_ms: int, gaps: list[str]) -> dict[int, Candle1h]:
        if self._db is None or not self._db.exists():
            gaps.append("htf: base historica no disponible (se usa REST de Binance)")
            return {}
        try:
            conn = self._open()
        except Exception as exc:  # noqa: BLE001
            gaps.append(f"htf: no se pudo abrir la base historica ({exc}); se usa REST")
            return {}
        try:
            self.stats["db_queries"] += 1
            kl = conn.execute(  # type: ignore[attr-defined]
                "SELECT open_time_ms, open, high, low, close, volume FROM klines WHERE exchange=? AND symbol=? "
                "AND interval='1m' AND open_time_ms >= ? AND open_time_ms < ? ORDER BY open_time_ms",
                (EXCHANGE, symbol, start_ms, end_ms)).fetchall()
            out = {h: c for h, c in aggregate_minutes(kl, "klines", 60).items()}
            bars = conn.execute(  # type: ignore[attr-defined]
                "SELECT minute_ms, open, high, low, close, buy_usd + sell_usd + block_usd FROM bars_1m WHERE exchange=? AND symbol=? "
                "AND minute_ms >= ? AND minute_ms < ? ORDER BY minute_ms",
                (EXCHANGE, symbol, start_ms, end_ms)).fetchall()
            for h, c in aggregate_minutes(bars, "bars_1m", MIN_BAR_MINUTES, quote_volume=True).items():
                out.setdefault(h, c)
            return out
        except Exception as exc:  # noqa: BLE001 - tabla ausente, base bloqueada: se declara y sigue con REST
            gaps.append(f"htf: fallo la lectura de la base historica ({exc}); se usa REST")
            return {}
        finally:
            conn.close()  # type: ignore[attr-defined]

    def _from_rest(self, symbol: str, start_ms: int, end_ms: int, now_ms: int, gaps: list[str]) -> dict[int, Candle1h]:
        n = min(REST_MAX_LIMIT, (end_ms - start_ms) // HOUR_MS)
        q = urllib.parse.urlencode({"symbol": to_binance_symbol(symbol), "interval": "1h", "startTime": start_ms,
                                    "endTime": end_ms - 1, "limit": max(1, n)})
        self.stats["rest_requests"] += 1
        try:
            status, body = self._http_get(f"{self._base}/fapi/v1/klines?{q}", 10.0)
            if status != 200:
                raise ValueError(f"HTTP {status}: {body[:120]!r}")
            return parse_rest_1h(body, now_ms)
        except Exception as exc:  # noqa: BLE001 - red, rate limit, formato: hueco declarado
            self.stats["rest_errors"] += 1
            gaps.append(f"htf: fallo el REST de Binance para completar velas de 1h ({exc})")
            return {}

    # ---- API ----------------------------------------------------------------------------------------

    def candles(self, symbol: str, now_ms: int, real_now_ms: int | None = None) -> tuple[list[Candle1h], list[str]]:
        """Velas de 1h cerradas de las ultimas `lookback_h` horas + huecos declarados en esta llamada.

        `now_ms` es el "hasta" del calculo (solo velas cerradas a ese momento). `real_now_ms` (reloj real, si se
        calcula hacia atras, p.ej. una sesion ya pasada) decide si una hora ya esta escrita en la base.
        """
        real = now_ms if real_now_ms is None else max(now_ms, real_now_ms)
        gaps: list[str] = []
        last = _hour(now_ms) - HOUR_MS
        first = last - (self._lookback_h - 1) * HOUR_MS
        first -= first % (24 * HOUR_MS)  # dias UTC completos: el primer dia no queda cortado (medias diarias)
        with self._lock:
            cache = self._cache.setdefault(symbol, {})
            tried = self._tried.setdefault(symbol, {})
            for k in [k for k in cache if k < first]:
                del cache[k]
            for k in [k for k in tried if k < first]:
                del tried[k]
            needed = [h for h in range(first, last + HOUR_MS, HOUR_MS)
                      if h not in cache and real - tried.get(h, -10**15) >= self._retry]
            if needed:
                lo = needed[0]
                db_end = min(_hour(real - self._grace), last + HOUR_MS)  # horas cerradas hace > grace
                if db_end > lo:
                    for h, c in self._from_db(symbol, lo, db_end, gaps).items():
                        if h not in cache and first <= h <= last:
                            cache[h] = c
                still = [h for h in needed if h not in cache]
                if still and self._rest:
                    got = self._from_rest(symbol, still[0], still[-1] + HOUR_MS, real, gaps)
                    for h in still:
                        if h in got:
                            cache[h] = got[h]
                for h in needed:
                    if h not in cache:
                        tried[h] = real
            out = [cache[k] for k in sorted(cache) if k <= last]
        recent = out[-48:]
        n_rest = sum(1 for c in recent if c.source == "rest")
        if n_rest:
            gaps.append(f"htf: {n_rest} de las ultimas {len(recent)} velas de 1h vinieron del REST de Binance "
                        "(faltaban en la base propia)")
        missing_recent = sum(1 for h in range(last - 47 * HOUR_MS, last + HOUR_MS, HOUR_MS) if h not in {c.open_ms for c in recent})
        if missing_recent:
            gaps.append(f"htf: faltan {missing_recent} de las ultimas 48 velas de 1h (ni base ni REST)")
        return out, gaps

    def context(self, symbol: str, now_ms: int, day_start_ms: int | None = None,
                real_now_ms: int | None = None) -> HtfContext:
        try:
            cs, gaps = self.candles(symbol, now_ms, real_now_ms)
            return build_htf_context(symbol, cs, now_ms, gaps, day_start_ms=day_start_ms)
        except Exception as exc:  # noqa: BLE001 - un fallo del contexto no tumba el ciclo, pero se declara
            log.exception("htf: fallo el contexto de %s", symbol)
            return missing_context(symbol, now_ms, f"error armando el contexto de 1h: {exc!r}")
