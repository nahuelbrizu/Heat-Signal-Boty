"""
Calendario de eventos macro: decisiones del FOMC, CPI y empleo (NFP). Archivo versionado `config/calendar.json`.

No hay una API oficial estable y sin key para estos calendarios, asi que las fechas se copian a mano de las
fuentes oficiales (verificadas el 2026-10-19) y quedan en el repo:
  - FOMC: https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm (comunicado 14:00 ET del 2º dia; las de
    2027 son tentativas hasta que se confirman en la reunion anterior).
  - CPI: https://www.bls.gov/schedule/news_release/cpi.htm (08:30 ET).
  - Empleo: https://www.bls.gov/schedule/news_release/empsit.htm (08:30 ET).
Las horas se guardan en hora de Nueva York y se pasan a UTC con DST (zoneinfo). Si el archivo se queda sin fechas
futuras, `coverage_warning` lo dice (sin fallos silenciosos): hay que actualizarlo con el calendario nuevo.

`config/calendar_historico.json` (aparte, inmutable, solo lo usa el laboratorio): FOMC 2021-2025 verificado contra
la misma pagina de la Fed, con `scheduled` (false = accion no programada, que no se conocia de antemano). Va en un
archivo separado para que el vigente siga chico y con su propio `verified_at`/aviso de cobertura, y para que el vivo
no cambie de comportamiento. CPI/NFP historicos no se copian a mano: salen de los vintages de FRED (lab/meta).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

CALENDAR_PATH = Path(__file__).with_name("config") / "calendar.json"
HISTORICAL_PATH = Path(__file__).with_name("config") / "calendar_historico.json"
KINDS = ("FOMC", "CPI", "NFP")
WARN_IF_COVERAGE_BELOW_DAYS = 45


@dataclass(frozen=True)
class MacroEvent:
    kind: str
    label: str
    ts_ms: int          # instante de la publicacion, UTC
    tentative: bool
    source: str
    scheduled: bool = True  # False = no programada (no se conocia de antemano)

    def as_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "label": self.label, "ts_ms": self.ts_ms,
                "ts_at": datetime.fromtimestamp(self.ts_ms / 1000, tz=timezone.utc).isoformat().replace("+00:00", "Z"),
                "tentative": self.tentative, "source": self.source}


@dataclass(frozen=True)
class Calendar:
    calendar_id: str
    verified_at: str
    events: tuple[MacroEvent, ...]
    sources: dict[str, str]

    def upcoming(self, now_ms: int, hours: float = 48.0) -> list[MacroEvent]:
        end = now_ms + int(hours * 3_600_000)
        return [e for e in self.events if now_ms <= e.ts_ms < end]

    def next_events(self, now_ms: int, n: int = 8) -> list[MacroEvent]:
        return [e for e in self.events if e.ts_ms >= now_ms][:n]

    def next_by_kind(self, now_ms: int) -> dict[str, MacroEvent | None]:
        return {k: next((e for e in self.events if e.kind == k and e.ts_ms >= now_ms), None) for k in KINDS}

    def coverage_warning(self, now_ms: int) -> str | None:
        last = {k: max((e.ts_ms for e in self.events if e.kind == k), default=None) for k in KINDS}
        short = [k for k, ms in last.items() if ms is None or ms - now_ms < WARN_IF_COVERAGE_BELOW_DAYS * 86_400_000]
        if short:
            return (f"el calendario versionado ({self.calendar_id}) tiene fechas para menos de "
                    f"{WARN_IF_COVERAGE_BELOW_DAYS} dias de {', '.join(short)}: actualizar macro/config/calendar.json")
        return None


def parse_calendar(raw: dict[str, Any]) -> Calendar:
    """Valida el archivo: tipo conocido, fecha y hora legibles, fuente para cada tipo. Error -> ValueError."""
    tz = ZoneInfo(raw.get("timezone") or "America/New_York")
    sources = dict(raw.get("sources") or {})
    events = []
    for e in raw.get("events") or []:
        kind = e.get("kind")
        if kind not in KINDS:
            raise ValueError(f"calendario: tipo desconocido {kind!r}")
        if kind not in sources:
            raise ValueError(f"calendario: falta la fuente oficial de {kind}")
        try:
            local = datetime.fromisoformat(f"{e['date']}T{e.get('time', '00:00')}").replace(tzinfo=tz)
        except (KeyError, ValueError) as exc:
            raise ValueError(f"calendario: fecha ilegible en {e!r}") from exc
        events.append(MacroEvent(kind=kind, label=str(e.get("label") or kind), ts_ms=int(local.timestamp() * 1000),
                                 tentative=bool(e.get("tentative")), source=sources[kind],
                                 scheduled=bool(e.get("scheduled", True))))
    events.sort(key=lambda x: x.ts_ms)
    return Calendar(calendar_id=str(raw.get("calendar_id") or "sin-id"), verified_at=str(raw.get("verified_at") or ""),
                    events=tuple(events), sources=sources)


@lru_cache(maxsize=4)
def _load_cached(path: str, mtime: float) -> Calendar:
    return parse_calendar(json.loads(Path(path).read_text(encoding="utf-8")))


def load_calendar(path: Path | str = CALENDAR_PATH) -> Calendar:
    p = Path(path)
    return _load_cached(str(p), p.stat().st_mtime)


def load_historical(path: Path | str = HISTORICAL_PATH) -> Calendar:
    """Calendario historico (laboratorio). Mismo formato y validacion que `calendar.json`."""
    p = Path(path)
    return _load_cached(str(p), p.stat().st_mtime)
