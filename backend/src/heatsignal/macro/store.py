"""
Base macro propia, append-only (`data/heatsignal_macro.sqlite`, `HEATSIGNAL_MACRO_DB`).

Tablas (triggers de SQLite bloquean UPDATE/DELETE: nada se pisa ni se borra):

    fred_observations    una fila por (serie, fecha de la observacion, vintage). `realtime_start` es la fecha en
                         que FRED/ALFRED empezo a publicar ESE valor (fecha de Chicago). `available_ms` es cuando
                         lo damos por conocido: 00:00 de Chicago del dia SIGUIENTE a `realtime_start` (conservador:
                         a lo sumo un dia tarde, nunca antes). `realtime_clipped=1` marca un vintage cuyo inicio
                         real es anterior a la ventana pedida (FRED recorta `realtime_start` al inicio de la
                         ventana): su `available_ms` queda en el borde de la ventana, tambien conservador.
    polymarket_snapshots probabilidad implicita, volumen y liquidez de cada mercado con `fetched_ms` = cuando se
                         LEYO (no cuando lo fecha Polymarket). `source` = 'gamma' (lectura en vivo) o
                         'clob-history' (backfill de la serie de precios; sin volumen/liquidez).
    macro_runs           una fila por corrida y fuente: ok | partial | error | off (fuente apagada) con detalle.

Point-in-time: `fred_as_of(series, at_ms)` devuelve, para cada fecha de observacion, el ULTIMO vintage con
`available_ms <= at_ms`. Un valor no existe antes de su publicacion; una revision no reescribe el pasado.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence
from zoneinfo import ZoneInfo

DEFAULT_DB = "data/heatsignal_macro.sqlite"
FRED_TZ = ZoneInfo("America/Chicago")  # el reloj de FRED/ALFRED (St. Louis Fed) es el de Chicago

SCHEMA = """
CREATE TABLE IF NOT EXISTS fred_observations (
    series_id TEXT NOT NULL,
    obs_date TEXT NOT NULL,
    value REAL,
    value_raw TEXT NOT NULL,
    realtime_start TEXT NOT NULL,
    realtime_end TEXT,
    realtime_clipped INTEGER NOT NULL DEFAULT 0,
    available_ms INTEGER NOT NULL,
    fetched_ms INTEGER NOT NULL,
    PRIMARY KEY (series_id, obs_date, realtime_start)
);
CREATE INDEX IF NOT EXISTS idx_fred_avail ON fred_observations(series_id, available_ms);
CREATE TRIGGER IF NOT EXISTS trg_fred_no_update BEFORE UPDATE ON fred_observations
BEGIN SELECT RAISE(ABORT, 'fred_observations es append-only'); END;
CREATE TRIGGER IF NOT EXISTS trg_fred_no_delete BEFORE DELETE ON fred_observations
BEGIN SELECT RAISE(ABORT, 'fred_observations es append-only'); END;

CREATE TABLE IF NOT EXISTS polymarket_snapshots (
    market_id TEXT NOT NULL,
    fetched_ms INTEGER NOT NULL,
    source TEXT NOT NULL,
    slug TEXT,
    event_slug TEXT,
    config_key TEXT,
    question TEXT,
    outcome TEXT,
    prob REAL,
    volume REAL,
    volume_24h REAL,
    liquidity REAL,
    one_day_change REAL,
    best_bid REAL,
    best_ask REAL,
    end_date TEXT,
    closed INTEGER,
    token_id TEXT,
    PRIMARY KEY (market_id, fetched_ms, source)
);
CREATE INDEX IF NOT EXISTS idx_poly_time ON polymarket_snapshots(fetched_ms);
CREATE TRIGGER IF NOT EXISTS trg_poly_no_update BEFORE UPDATE ON polymarket_snapshots
BEGIN SELECT RAISE(ABORT, 'polymarket_snapshots es append-only'); END;
CREATE TRIGGER IF NOT EXISTS trg_poly_no_delete BEFORE DELETE ON polymarket_snapshots
BEGIN SELECT RAISE(ABORT, 'polymarket_snapshots es append-only'); END;

CREATE TABLE IF NOT EXISTS macro_runs (
    run_id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL,
    started_ms INTEGER NOT NULL,
    finished_ms INTEGER NOT NULL,
    status TEXT NOT NULL,
    n_rows INTEGER NOT NULL DEFAULT 0,
    detail TEXT
);
CREATE INDEX IF NOT EXISTS idx_macro_runs ON macro_runs(source, finished_ms);
CREATE TRIGGER IF NOT EXISTS trg_runs_no_update BEFORE UPDATE ON macro_runs
BEGIN SELECT RAISE(ABORT, 'macro_runs es append-only'); END;
CREATE TRIGGER IF NOT EXISTS trg_runs_no_delete BEFORE DELETE ON macro_runs
BEGIN SELECT RAISE(ABORT, 'macro_runs es append-only'); END;
"""

FRED_COLUMNS = ["series_id", "obs_date", "value", "value_raw", "realtime_start", "realtime_end", "realtime_clipped",
                "available_ms", "fetched_ms"]
POLY_COLUMNS = ["market_id", "fetched_ms", "source", "slug", "event_slug", "config_key", "question", "outcome", "prob",
                "volume", "volume_24h", "liquidity", "one_day_change", "best_bid", "best_ask", "end_date", "closed",
                "token_id"]


def available_ms_for(realtime_start: str) -> int:
    """00:00 de Chicago del dia siguiente a `realtime_start` (YYYY-MM-DD), en epoch ms UTC."""
    d = date.fromisoformat(realtime_start) + timedelta(days=1)
    return int(datetime(d.year, d.month, d.day, tzinfo=FRED_TZ).timestamp() * 1000)


@dataclass(frozen=True)
class FredObs:
    series_id: str
    obs_date: str
    value: float | None
    value_raw: str
    realtime_start: str
    realtime_end: str | None
    realtime_clipped: bool
    available_ms: int


def open_readonly(path: str | Path, timeout_s: float = 5.0) -> sqlite3.Connection:
    from urllib.parse import quote

    uri = f"file:{quote(str(Path(path).resolve()))}?mode=ro"
    return sqlite3.connect(uri, uri=True, timeout=timeout_s)


class MacroStore:
    def __init__(self, path: str | Path, *, readonly: bool = False) -> None:
        self.path = Path(path)
        self._lock = threading.Lock()
        if readonly:
            self._conn = open_readonly(self.path)
        else:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(self.path, timeout=30.0, check_same_thread=False)
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.executescript(SCHEMA)
            self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    # ------------------------------------------------------------------ escritura

    def add_fred(self, rows: Iterable[FredObs], fetched_ms: int) -> int:
        """Inserta vintages nuevos. Un vintage recortado (clipped) que repite el valor de un vintage ya guardado
        no se agrega (si no, cada corrida diaria sumaria una copia por el borde movil de la ventana)."""
        n = 0
        with self._lock, self._conn:
            for r in rows:
                if r.realtime_clipped:
                    prev = self._conn.execute(
                        "SELECT value_raw FROM fred_observations WHERE series_id=? AND obs_date=? AND realtime_start<=? "
                        "ORDER BY realtime_start DESC LIMIT 1", (r.series_id, r.obs_date, r.realtime_start)).fetchone()
                    if prev is not None and prev[0] == r.value_raw:
                        continue
                cur = self._conn.execute(
                    "INSERT OR IGNORE INTO fred_observations VALUES (?,?,?,?,?,?,?,?,?)",
                    (r.series_id, r.obs_date, r.value, r.value_raw, r.realtime_start, r.realtime_end,
                     int(r.realtime_clipped), r.available_ms, fetched_ms))
                n += cur.rowcount
        return n

    def add_polymarket(self, rows: Iterable[dict[str, Any]]) -> int:
        n = 0
        with self._lock, self._conn:
            for r in rows:
                cur = self._conn.execute(
                    f"INSERT OR IGNORE INTO polymarket_snapshots ({', '.join(POLY_COLUMNS)}) "
                    f"VALUES ({', '.join('?' * len(POLY_COLUMNS))})", [r.get(c) for c in POLY_COLUMNS])
                n += cur.rowcount
        return n

    def add_run(self, source: str, started_ms: int, finished_ms: int, status: str, n_rows: int = 0,
                detail: str | dict | None = None) -> None:
        if isinstance(detail, dict):
            detail = json.dumps(detail, ensure_ascii=False, default=str)
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO macro_runs (source, started_ms, finished_ms, status, n_rows, detail) VALUES (?,?,?,?,?,?)",
                (source, started_ms, finished_ms, status, n_rows, detail))

    # ------------------------------------------------------------------ lectura

    def fred_as_of(self, series_id: str, at_ms: int, n_obs: int = 15) -> list[FredObs]:
        """Ultimas `n_obs` observaciones conocidas en `at_ms`, cada una con su ultimo vintage publicado hasta ahi.

        Orden ascendente por fecha de observacion. Nada con `available_ms > at_ms` aparece.
        """
        with self._lock:
            rows = self._conn.execute(
                "SELECT o.series_id, o.obs_date, o.value, o.value_raw, o.realtime_start, o.realtime_end, "
                "o.realtime_clipped, o.available_ms FROM fred_observations o "
                "JOIN (SELECT obs_date, MAX(realtime_start) AS rs FROM fred_observations "
                "      WHERE series_id=? AND available_ms<=? GROUP BY obs_date "
                "      ORDER BY obs_date DESC LIMIT ?) k ON k.obs_date=o.obs_date AND k.rs=o.realtime_start "
                "WHERE o.series_id=? ORDER BY o.obs_date",
                (series_id, at_ms, int(n_obs), series_id)).fetchall()
        return [FredObs(r[0], r[1], r[2], r[3], r[4], r[5], bool(r[6]), r[7]) for r in rows]

    def fred_counts(self) -> dict[str, dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT series_id, COUNT(*), MAX(obs_date), MAX(realtime_start), MAX(fetched_ms) "
                "FROM fred_observations GROUP BY series_id").fetchall()
        return {r[0]: {"rows": r[1], "last_obs_date": r[2], "last_realtime_start": r[3], "last_fetched_ms": r[4]}
                for r in rows}

    def poly_latest(self, at_ms: int, *, source: str = "gamma") -> list[dict[str, Any]]:
        """Ultimo snapshot leido hasta `at_ms` de cada mercado (solo la ultima lectura de cada uno)."""
        with self._lock:
            rows = self._conn.execute(
                f"SELECT {', '.join('s.' + c for c in POLY_COLUMNS)} FROM polymarket_snapshots s "
                "JOIN (SELECT market_id, MAX(fetched_ms) AS f FROM polymarket_snapshots WHERE source=? AND fetched_ms<=? "
                "      GROUP BY market_id) k ON k.market_id=s.market_id AND k.f=s.fetched_ms AND s.source=? "
                "ORDER BY s.config_key, s.volume DESC", (source, at_ms, source)).fetchall()
        return [dict(zip(POLY_COLUMNS, r)) for r in rows]

    def poly_prob_at(self, market_id: str, at_ms: int, *, max_age_ms: int | None = None) -> tuple[int, float] | None:
        """(fetched_ms, prob) de la ultima lectura <= at_ms (cualquier fuente), o None."""
        with self._lock:
            row = self._conn.execute(
                "SELECT fetched_ms, prob FROM polymarket_snapshots WHERE market_id=? AND fetched_ms<=? AND prob IS NOT NULL "
                "ORDER BY fetched_ms DESC LIMIT 1", (market_id, at_ms)).fetchone()
        if row is None or (max_age_ms is not None and at_ms - row[0] > max_age_ms):
            return None
        return int(row[0]), float(row[1])

    def poly_series(self, market_id: str, since_ms: int, until_ms: int) -> list[tuple[int, float]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT fetched_ms, prob FROM polymarket_snapshots WHERE market_id=? AND fetched_ms>=? AND fetched_ms<=? "
                "AND prob IS NOT NULL ORDER BY fetched_ms", (market_id, since_ms, until_ms)).fetchall()
        return [(int(t), float(p)) for t, p in rows]

    def last_runs(self, limit_per_source: int = 5) -> dict[str, list[dict[str, Any]]]:
        out: dict[str, list[dict[str, Any]]] = {}
        with self._lock:
            sources = [r[0] for r in self._conn.execute("SELECT DISTINCT source FROM macro_runs").fetchall()]
            for s in sources:
                rows = self._conn.execute(
                    "SELECT run_id, source, started_ms, finished_ms, status, n_rows, detail FROM macro_runs "
                    "WHERE source=? ORDER BY finished_ms DESC, run_id DESC LIMIT ?", (s, limit_per_source)).fetchall()
                out[s] = [dict(zip(("run_id", "source", "started_ms", "finished_ms", "status", "n_rows", "detail"), r))
                          for r in rows]
        return out

    def last_ok_ms(self, source: str) -> int | None:
        with self._lock:
            row = self._conn.execute("SELECT MAX(finished_ms) FROM macro_runs WHERE source=? AND status IN ('ok','partial')",
                                     (source,)).fetchone()
        return None if row is None or row[0] is None else int(row[0])

    def rows_between(self, table: str, ts_col: str, since_ms: int, until_ms: int) -> tuple[list[str], list[tuple]]:
        cols = {"fred_observations": FRED_COLUMNS, "polymarket_snapshots": POLY_COLUMNS}[table]
        with self._lock:
            rows = self._conn.execute(
                f"SELECT {', '.join(cols)} FROM {table} WHERE {ts_col}>=? AND {ts_col}<? ORDER BY {ts_col}",
                (since_ms, until_ms)).fetchall()
        return cols, rows

    def min_ts(self, table: str, ts_col: str) -> int | None:
        with self._lock:
            row = self._conn.execute(f"SELECT MIN({ts_col}) FROM {table}").fetchone()
        return None if row is None or row[0] is None else int(row[0])


def fred_today(now: datetime | None = None) -> str:
    """Fecha de hoy en Chicago (FRED rechaza un realtime en su futuro)."""
    return (now or datetime.now(timezone.utc)).astimezone(FRED_TZ).strftime("%Y-%m-%d")


def ms_to_iso(ms: int | None) -> str | None:
    if ms is None:
        return None
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def chunks(seq: Sequence[Any], n: int) -> Iterable[Sequence[Any]]:
    for i in range(0, len(seq), n):
        yield seq[i:i + n]
