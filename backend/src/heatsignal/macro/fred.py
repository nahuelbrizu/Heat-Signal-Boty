"""
FRED / ALFRED point-in-time (St. Louis Fed). Necesita una API key gratuita en `FRED_API_KEY`.

Adaptado de TradingAgents (`tradingagents/dataflows/vendors/fred.py`, TauricResearch, Apache License 2.0): la idea
de fijar el vintage con `realtime_start`/`realtime_end`, el reloj de Chicago, el "." como valor faltante y el
mensaje de error de FRED en los 400. Cambios: en vez de pedir el vintage de UN dia, se pide la ventana completa de
vintages y se guarda cada uno con su fecha de publicacion; la key nunca aparece en logs ni errores; sin `requests`.

Doc oficial verificada el 2026-10-19:
  - https://fred.stlouisfed.org/docs/api/fred/series_observations.html
      GET https://api.stlouisfed.org/fred/series/observations
      api_key (obligatorio), file_type=json, series_id, realtime_start / realtime_end (YYYY-MM-DD, default hoy),
      observation_start / observation_end, sort_order (asc|desc), limit (1..100000), output_type (1 = por
      periodo de tiempo real, el default). Respuesta JSON: {"observations": [{"realtime_start", "realtime_end",
      "date", "value"}, ...]} con `value` como string.
  - https://fred.stlouisfed.org/docs/api/fred/realtime_period.html
      "The real-time period marks when facts were true or when information was known until it changed."
      Periodo cerrado en ambos extremos; 9999-12-31 = abierto.
  - https://fred.stlouisfed.org/docs/api/api_key.html (key gratuita, va como parametro `api_key`).

Point-in-time: con `realtime_start = inicio de ventana` y `realtime_end = 9999-12-31` FRED devuelve TODOS los
vintages de cada observacion; el `realtime_start` de cada fila es desde cuando se conocio ese valor. Si el vintage
empezo antes de la ventana, FRED lo recorta al inicio de la ventana: lo marcamos `realtime_clipped` (su fecha de
publicacion real es <= esa; tomarla como la del borde es conservador).
"""

from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Callable

from .store import FredObs, available_ms_for

log = logging.getLogger("heatsignal.macro.fred")

FRED_API_BASE = "https://api.stlouisfed.org/fred"
REQUEST_TIMEOUT_S = 30.0
OPEN_END = "9999-12-31"
DOC_URL = "https://fred.stlouisfed.org/docs/api/fred/series_observations.html"
KEY_URL = "https://fred.stlouisfed.org/docs/api/api_key.html"


@dataclass(frozen=True)
class FredSeries:
    series_id: str
    label: str        # corto, para el bloque del grafo
    name: str         # largo, para el panel
    units: str        # "%", "indice", ...
    frequency: str    # diaria | mensual
    note: str = ""


# Lista ACOTADA y documentada. Cambiarla es un cambio de codigo (version del bloque de contexto).
SERIES: tuple[FredSeries, ...] = (
    FredSeries("DFF", "fed funds", "Tasa efectiva de fondos federales (diaria)", "%", "diaria",
               "Se publica el dia habil siguiente."),
    FredSeries("CPIAUCSL", "CPI", "Indice de precios al consumidor, todos los items (desestacionalizado)", "indice",
               "mensual", "Lo publica el BLS ~2 semanas despues del mes; se revisa (factores estacionales en febrero)."),
    FredSeries("DGS10", "10 años", "Rendimiento del Tesoro a 10 años (constante)", "%", "diaria"),
    FredSeries("DTWEXBGS", "dólar amplio", "Indice dolar amplio nominal (Fed, base ene-2006=100)", "indice", "diaria",
               "Lo publica la Fed con rezago semanal (H.10)."),
    FredSeries("VIXCLS", "VIX", "VIX de CBOE (cierre)", "puntos", "diaria"),
    FredSeries("PAYEMS", "empleo NFP", "Empleo no agricola total (nominas, miles, desestacionalizado)", "miles",
               "mensual", "Lo publica el BLS el 1er viernes del mes siguiente (Employment Situation); revisa los dos "
               "meses previos. Sus vintages dan las fechas historicas de publicacion del NFP (laboratorio)."),
)
SERIES_BY_ID = {s.series_id: s for s in SERIES}

HttpGet = Callable[[str, float], tuple[int, bytes]]


class FredError(RuntimeError):
    """Fallo tipado de FRED (red, 4xx/5xx, JSON ilegible). El mensaje nunca incluye la key."""


class FredNotConfigured(FredError):
    """No hay FRED_API_KEY: la fuente queda APAGADA (se informa, no falla en silencio)."""


def api_key_from_env() -> str | None:
    key = (os.environ.get("FRED_API_KEY") or "").strip()
    return key or None


def _default_get(url: str, timeout_s: float) -> tuple[int, bytes]:
    req = urllib.request.Request(url, headers={"User-Agent": "heatsignal-macro/1"})
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        # sin la URL (lleva la key): solo el tipo y el motivo
        raise FredError(f"error de red contra api.stlouisfed.org: {type(exc).__name__}: {getattr(exc, 'reason', exc)}") from None


def parse_value(raw: str | None) -> float | None:
    """FRED codifica la observacion faltante como '.'."""
    if raw in (None, "", "."):
        return None
    try:
        return float(raw)
    except ValueError:
        return None


class FredClient:
    def __init__(self, api_key: str | None, http_get: HttpGet | None = None, timeout_s: float = REQUEST_TIMEOUT_S) -> None:
        self._key = api_key
        self._get = http_get or _default_get
        self._timeout = timeout_s

    @property
    def configured(self) -> bool:
        return bool(self._key)

    def _scrub(self, text: str) -> str:
        return text.replace(self._key, "***") if self._key else text

    def _request(self, path: str, params: dict[str, str]) -> dict:
        if not self._key:
            raise FredNotConfigured(f"falta FRED_API_KEY en .env (key gratuita: {KEY_URL})")
        url = f"{FRED_API_BASE}/{path}?" + urllib.parse.urlencode({**params, "api_key": self._key, "file_type": "json"})
        try:
            status, body = self._get(url, self._timeout)
        except FredError as exc:
            raise FredError(self._scrub(str(exc))) from None
        if status != 200:
            msg = ""
            try:
                msg = json.loads(body).get("error_message", "")
            except (ValueError, AttributeError):
                msg = body[:200].decode("utf-8", "replace") if isinstance(body, bytes) else str(body)[:200]
            raise FredError(self._scrub(f"FRED HTTP {status}: {msg}"))
        try:
            return json.loads(body)
        except ValueError:
            raise FredError("FRED devolvio un JSON ilegible") from None

    def vintages(self, series_id: str, realtime_start: str, observation_start: str,
                 realtime_end: str = OPEN_END) -> list[FredObs]:
        """Todos los vintages de las observaciones desde `observation_start` publicados en [realtime_start, end]."""
        data = self._request("series/observations", {
            "series_id": series_id, "realtime_start": realtime_start, "realtime_end": realtime_end,
            "observation_start": observation_start, "sort_order": "asc", "limit": "100000",
        })
        out: list[FredObs] = []
        for o in data.get("observations") or []:
            try:
                rs, d = o["realtime_start"], o["date"]
                date.fromisoformat(rs), date.fromisoformat(d)
            except (KeyError, TypeError, ValueError):
                log.warning("FRED %s: observacion ilegible descartada: %r", series_id, o)
                continue
            raw = str(o.get("value", ""))
            out.append(FredObs(series_id=series_id, obs_date=d, value=parse_value(raw), value_raw=raw,
                               realtime_start=rs, realtime_end=o.get("realtime_end"),
                               realtime_clipped=(rs == realtime_start), available_ms=available_ms_for(rs)))
        return out


def window_for(today: str, lookback_days: int) -> tuple[str, str]:
    """(realtime_start, observation_start) para una ventana de `lookback_days` hasta hoy (Chicago)."""
    start = (date.fromisoformat(today) - timedelta(days=lookback_days)).isoformat()
    return start, start
