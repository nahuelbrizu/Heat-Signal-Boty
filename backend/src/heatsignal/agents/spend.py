"""
Gasto en LLM de lo que se analiza (grafo, lecciones, investigacion semanal) con tope semanal.

Pedido del usuario (2026-11-18): "que tenga un limite de 5 usd de lunes a viernes" para "lo que necesite ser
analizado". Regla:
  - El LLM solo se usa en los dias de HEATSIGNAL_LLM_DAYS (default lunes a viernes, UTC). Sabado y domingo el
    grafo calcula solo features.
  - Ventana semanal = lunes 00:00 UTC -> lunes siguiente. Si lo gastado en la ventana llega a
    HEATSIGNAL_LLM_WEEKLY_USD (default 5), no se hacen mas llamadas hasta el lunes. Nunca silencioso: el ciclo queda
    `no_llm` con el motivo y el panel muestra el gasto.
  - El gasto es a PRECIO DE LISTA (tokens x precio publico de Anthropic). Lo cobrado real puede ser algo distinto.
    La llamada que cruza el tope termina (se mide despues), asi que el total puede pasar el tope por una llamada.

Registro append-only en <data>/llm_spend.sqlite (una fila por llamada), escrito por un callback de LangChain que
`llm_factory.make_llms` cuelga de cada modelo. La investigacion semanal (Managed Agents) suma su list_cost al bajar
cada informe.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger("heatsignal.agents.spend")

# US$ por millon de tokens (entrada, salida), precios de lista de Anthropic (cache 2026-11-03). Lectura de cache
# = 0,1x entrada; escritura = 1,25x. Un modelo desconocido se cobra como Sonnet (conservador).
PRICES: dict[str, tuple[float, float]] = {
    "claude-haiku-5-5": (0.10, 0.50),
    "claude-sonnet-5-5": (2.00, 10.00),
    "claude-sonnet-5": (2.00, 10.00),
    "claude-opus-5-5": (4.00, 20.00),
}
FALLBACK_PRICE = (2.00, 10.00)
DAY_NAMES = ["lun", "mar", "mie", "jue", "vie", "sab", "dom"]

SCHEMA = """
CREATE TABLE IF NOT EXISTS llm_spend (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_ms INTEGER NOT NULL,
    source TEXT NOT NULL,
    model TEXT,
    input_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    cache_read_tokens INTEGER NOT NULL DEFAULT 0,
    cache_write_tokens INTEGER NOT NULL DEFAULT 0,
    cost_usd REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_llm_spend_ts ON llm_spend (ts_ms);
"""


def default_db() -> Path:
    if os.environ.get("HEATSIGNAL_LLM_SPEND_DB"):
        return Path(os.environ["HEATSIGNAL_LLM_SPEND_DB"]).expanduser()
    eval_db = os.environ.get("HEATSIGNAL_EVAL_DB", "data/heatsignal_eval.sqlite")
    return Path(eval_db).expanduser().parent / "llm_spend.sqlite"


def weekly_limit_usd() -> float:
    try:
        return float(os.environ.get("HEATSIGNAL_LLM_WEEKLY_USD", "5"))
    except ValueError:
        return 5.0


def allowed_days() -> set[int]:
    """Dias (0 = lunes) en que se usa el LLM. HEATSIGNAL_LLM_DAYS='0-4' (default) o '0,1,2,3,4,5,6'."""
    raw = os.environ.get("HEATSIGNAL_LLM_DAYS", "0-4").strip()
    out: set[int] = set()
    try:
        for part in raw.split(","):
            part = part.strip()
            if "-" in part:
                a, b = part.split("-", 1)
                out.update(range(int(a), int(b) + 1))
            elif part:
                out.add(int(part))
    except ValueError:
        out = set()
    return {d for d in out if 0 <= d <= 6} or {0, 1, 2, 3, 4}


def cost_usd(model: str | None, input_tokens: int, output_tokens: int, cache_read: int = 0, cache_write: int = 0) -> float:
    pin, pout = PRICES.get(str(model or "").strip(), FALLBACK_PRICE)
    uncached = max(0, input_tokens - cache_read - cache_write)
    return (uncached * pin + cache_read * pin * 0.1 + cache_write * pin * 1.25 + output_tokens * pout) / 1_000_000


def week_start(now: datetime) -> datetime:
    d = now.astimezone(timezone.utc)
    return (d - timedelta(days=d.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)


class SpendLedger:
    def __init__(self, path: Path | str | None = None, timeout_s: float = 5.0) -> None:
        self.path = Path(path) if path else default_db()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._timeout = timeout_s
        with self._conn() as c:
            c.executescript(SCHEMA)

    def _conn(self) -> sqlite3.Connection:
        return sqlite3.connect(str(self.path), timeout=self._timeout)

    def add(self, source: str, model: str | None, input_tokens: int = 0, output_tokens: int = 0, cache_read: int = 0,
            cache_write: int = 0, cost: float | None = None, ts_ms: int | None = None) -> float:
        c_usd = cost if cost is not None else cost_usd(model, input_tokens, output_tokens, cache_read, cache_write)
        with self._lock, self._conn() as c:
            c.execute("INSERT INTO llm_spend (ts_ms, source, model, input_tokens, output_tokens, cache_read_tokens,"
                      " cache_write_tokens, cost_usd) VALUES (?,?,?,?,?,?,?,?)",
                      (ts_ms or int(time.time() * 1000), source, model, input_tokens, output_tokens, cache_read,
                       cache_write, c_usd))
        return c_usd

    def spent_since(self, start_ms: int) -> tuple[float, dict[str, float]]:
        with self._conn() as c:
            rows = c.execute("SELECT source, SUM(cost_usd) FROM llm_spend WHERE ts_ms >= ? GROUP BY source", (start_ms,)).fetchall()
        by = {s: round(v or 0.0, 6) for s, v in rows}
        return round(sum(by.values()), 6), by


@dataclass
class BudgetState:
    allowed: bool
    reason: str | None  # None si se puede usar el LLM
    spent_usd: float
    limit_usd: float
    by_source: dict[str, float]
    window_start: str
    window_end: str
    days: str

    def as_dict(self) -> dict[str, Any]:
        return {"allowed": self.allowed, "reason": self.reason, "spent_usd": round(self.spent_usd, 4),
                "limit_usd": self.limit_usd, "by_source": self.by_source, "window_start": self.window_start,
                "window_end": self.window_end, "days": self.days}


def budget_state(ledger: SpendLedger | None = None, now: datetime | None = None) -> BudgetState:
    now = now or datetime.now(timezone.utc)
    ledger = ledger or SpendLedger()
    start = week_start(now)
    spent, by = ledger.spent_since(int(start.timestamp() * 1000))
    limit = weekly_limit_usd()
    days = allowed_days()
    days_txt = ",".join(DAY_NAMES[d] for d in sorted(days))
    reason = None
    if now.astimezone(timezone.utc).weekday() not in days:
        reason = f"el LLM solo se usa {days_txt} (UTC); hoy solo features"
    elif spent >= limit:
        reason = f"tope semanal alcanzado: US${spent:.2f} de US${limit:.2f} desde el lunes; vuelve el lunes 00:00 UTC"
    return BudgetState(reason is None, reason, spent, limit, by, start.isoformat(), (start + timedelta(days=7)).isoformat(),
                       days_txt)


def make_callback(source: str, ledger: SpendLedger | None = None) -> Any:
    """Callback de LangChain que anota cada llamada (tokens de usage_metadata) en el registro."""
    from langchain_core.callbacks import BaseCallbackHandler

    class SpendCallback(BaseCallbackHandler):
        def __init__(self) -> None:
            self.ledger = ledger

        def on_llm_end(self, response: Any, **kwargs: Any) -> None:
            try:
                led = self.ledger or SpendLedger()
                self.ledger = led
                for gens in response.generations or []:
                    for g in gens:
                        msg = getattr(g, "message", None)
                        usage = getattr(msg, "usage_metadata", None) or {}
                        if not usage:
                            continue
                        meta = getattr(msg, "response_metadata", None) or {}
                        model = meta.get("model") or meta.get("model_name")
                        det = usage.get("input_token_details") or {}
                        led.add(source, model, int(usage.get("input_tokens") or 0), int(usage.get("output_tokens") or 0),
                                int(det.get("cache_read") or 0), int(det.get("cache_creation") or 0))
            except Exception:  # noqa: BLE001 - anotar el gasto nunca rompe una llamada
                log.exception("no se pudo anotar el gasto de LLM")

    return SpendCallback()
