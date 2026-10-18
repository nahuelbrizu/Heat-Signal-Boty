"""
Base del bot de paper trading (`data/paperbot.sqlite`). Append-only: triggers rechazan UPDATE y DELETE en todas las
tablas; el estado (posiciones, PnL) se reconstruye siempre desde el log de ordenes y funding (`ledger.py`).

Una corrida = UNA transaccion: la fila de `runs` con status 'ok' se escribe junto con sus ordenes, funding,
equity, posiciones y trades. Si el proceso muere a la mitad no queda nada a medias (esa hora queda como hueco).
El indice unico parcial `runs_ok_hour` garantiza una sola corrida 'ok' por hora (idempotencia).

Sin pandas: este modulo lo importan el engine y el panel.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any

DEFAULT_DB = "data/paperbot.sqlite"
HOUR_MS = 3_600_000

TABLES = ("runs", "orders", "funding_events", "equity", "positions", "trades", "basket", "gaps", "funding_rates")

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id       TEXT    PRIMARY KEY,
    hour_ms      INTEGER NOT NULL,          -- hora de decision H (piso de la hora UTC en que corrio)
    started_ms   INTEGER NOT NULL,
    finished_ms  INTEGER NOT NULL,
    status       TEXT    NOT NULL CHECK (status IN ('ok', 'error', 'skipped')),
    reason       TEXT,                      -- motivo (error/skipped) o nota (ok)
    detail       TEXT,                      -- JSON: chequeos de datos, parametros, objetivos, memoria
    peak_rss_kb  INTEGER
);
CREATE UNIQUE INDEX IF NOT EXISTS runs_ok_hour ON runs (hour_ms) WHERE status = 'ok';
CREATE INDEX IF NOT EXISTS runs_hour ON runs (hour_ms);

CREATE TABLE IF NOT EXISTS orders (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id       TEXT    NOT NULL,
    strategy_id  TEXT    NOT NULL,
    symbol       TEXT    NOT NULL,
    decision_ms  INTEGER NOT NULL,          -- instante de la decision de la regla
    fill_ms      INTEGER NOT NULL,          -- apertura de la vela 1m del fill (o el instante del ultimo precio)
    price        REAL    NOT NULL,          -- precio crudo (el slippage va aparte, como en lab/sim.py)
    price_source TEXT    NOT NULL,          -- open_1m_siguiente | ultimo_precio
    weight_from  REAL    NOT NULL,
    weight_to    REAL    NOT NULL,
    units_from   REAL    NOT NULL,
    units_to     REAL    NOT NULL,
    notional     REAL    NOT NULL,
    fee          REAL    NOT NULL,
    slippage     REAL    NOT NULL,
    reason       TEXT    NOT NULL,          -- senal | resincronizacion | inicio | hueco
    UNIQUE (run_id, strategy_id, symbol, decision_ms)
);
CREATE INDEX IF NOT EXISTS orders_strategy ON orders (strategy_id, fill_ms);

CREATE TABLE IF NOT EXISTS funding_events (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id       TEXT    NOT NULL,
    strategy_id  TEXT    NOT NULL,
    symbol       TEXT    NOT NULL,
    funding_ms   INTEGER NOT NULL,
    rate         REAL    NOT NULL,
    units        REAL    NOT NULL,          -- posicion vigente justo antes de la liquidacion
    price        REAL    NOT NULL,
    cash         REAL    NOT NULL,          -- -units * price * rate (positivo = cobrado)
    UNIQUE (strategy_id, symbol, funding_ms)
);

CREATE TABLE IF NOT EXISTS equity (
    run_id        TEXT    NOT NULL,
    hour_ms       INTEGER NOT NULL,
    strategy_id   TEXT    NOT NULL,
    equity        REAL    NOT NULL,
    basket_equity REAL,
    realized      REAL    NOT NULL,         -- PnL de precio realizado (crudo)
    unrealized    REAL    NOT NULL,
    funding       REAL    NOT NULL,
    fees          REAL    NOT NULL,
    slippage      REAL    NOT NULL,
    exposure      REAL    NOT NULL,         -- nocional bruto / capital
    PRIMARY KEY (strategy_id, hour_ms)
);

CREATE TABLE IF NOT EXISTS positions (
    run_id       TEXT    NOT NULL,
    hour_ms      INTEGER NOT NULL,
    strategy_id  TEXT    NOT NULL,
    symbol       TEXT    NOT NULL,
    weight       REAL    NOT NULL,
    target       REAL    NOT NULL,          -- objetivo de la regla en H (debe coincidir con weight)
    units        REAL    NOT NULL,
    entry_price  REAL,
    mark_price   REAL    NOT NULL,
    unrealized   REAL    NOT NULL,
    realized     REAL    NOT NULL,
    funding      REAL    NOT NULL,
    fees         REAL    NOT NULL,
    slippage     REAL    NOT NULL,
    PRIMARY KEY (strategy_id, symbol, hour_ms)
);

CREATE TABLE IF NOT EXISTS trades (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id       TEXT    NOT NULL,
    strategy_id  TEXT    NOT NULL,
    symbol       TEXT    NOT NULL,
    direction    TEXT    NOT NULL,
    entry_ms     INTEGER NOT NULL,
    exit_ms      INTEGER NOT NULL,
    entry_price  REAL    NOT NULL,
    exit_price   REAL    NOT NULL,
    max_units    REAL    NOT NULL,
    pnl_price    REAL    NOT NULL,
    funding      REAL    NOT NULL,
    fees         REAL    NOT NULL,
    slippage     REAL    NOT NULL,
    pnl_net      REAL    NOT NULL,
    UNIQUE (strategy_id, symbol, entry_ms)
);

CREATE TABLE IF NOT EXISTS basket (
    strategy_id  TEXT    NOT NULL,
    symbol       TEXT    NOT NULL,
    start_ms     INTEGER NOT NULL,
    price        REAL    NOT NULL,
    units        REAL    NOT NULL,
    cost         REAL    NOT NULL,
    PRIMARY KEY (strategy_id, symbol)
);

CREATE TABLE IF NOT EXISTS gaps (
    run_id       TEXT    NOT NULL,
    from_ms      INTEGER NOT NULL,          -- primera hora sin corrida ok
    to_ms        INTEGER NOT NULL,          -- ultima hora sin corrida ok
    hours        INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS funding_rates (
    symbol       TEXT    NOT NULL,          -- BTCUSDT
    funding_ms   INTEGER NOT NULL,
    rate         REAL    NOT NULL,
    mark_price   REAL,
    fetched_ms   INTEGER NOT NULL,
    PRIMARY KEY (symbol, funding_ms)
);
""" + "".join(
    f"CREATE TRIGGER IF NOT EXISTS {t}_no_update BEFORE UPDATE ON {t} BEGIN SELECT RAISE(ABORT, 'paperbot: {t} es append-only'); END;\n"
    f"CREATE TRIGGER IF NOT EXISTS {t}_no_delete BEFORE DELETE ON {t} BEGIN SELECT RAISE(ABORT, 'paperbot: {t} es append-only'); END;\n"
    for t in TABLES)


def open_readonly(path: str | Path, timeout_s: float = 5.0) -> sqlite3.Connection:
    """Conexion de solo lectura (la usa el panel en un hilo aparte). FileNotFoundError si no existe."""
    from urllib.parse import quote

    p = Path(path)
    if not p.is_file():
        raise FileNotFoundError(str(p))
    conn = sqlite3.connect(f"file:{quote(str(p.resolve()))}?mode=ro", uri=True, timeout=timeout_s)
    conn.row_factory = sqlite3.Row
    return conn


class PaperbotStore:
    """Escritura (solo el proceso del bot) y lecturas que necesita el engine."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.path), timeout=30)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    # ---- lecturas -------------------------------------------------------------------------------

    def ok_run(self, hour_ms: int) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM runs WHERE status='ok' AND hour_ms=?", (hour_ms,)).fetchone()

    def last_ok_run(self, before_ms: int | None = None) -> sqlite3.Row | None:
        if before_ms is None:
            return self.conn.execute("SELECT * FROM runs WHERE status='ok' ORDER BY hour_ms DESC LIMIT 1").fetchone()
        return self.conn.execute("SELECT * FROM runs WHERE status='ok' AND hour_ms < ? ORDER BY hour_ms DESC LIMIT 1",
                                 (before_ms,)).fetchone()

    def orders(self, strategy_id: str) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM orders WHERE strategy_id=? ORDER BY fill_ms, id", (strategy_id,)).fetchall()

    def funding_events(self, strategy_id: str) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM funding_events WHERE strategy_id=? ORDER BY funding_ms, id",
                                 (strategy_id,)).fetchall()

    def basket(self, strategy_id: str) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM basket WHERE strategy_id=? ORDER BY symbol", (strategy_id,)).fetchall()

    def last_equity_hour(self, strategy_id: str) -> int | None:
        """Ultima hora con equity de la estrategia (None = nunca corrio: entra "desde hoy")."""
        return self.conn.execute("SELECT MAX(hour_ms) FROM equity WHERE strategy_id=?", (strategy_id,)).fetchone()[0]

    def first_equity_hour(self, strategy_id: str) -> int | None:
        return self.conn.execute("SELECT MIN(hour_ms) FROM equity WHERE strategy_id=?", (strategy_id,)).fetchone()[0]

    def first_ok_hour(self) -> int | None:
        row = self.conn.execute("SELECT MIN(hour_ms) FROM runs WHERE status='ok'").fetchone()
        return row[0]

    def funding_rates(self, symbol: str, since_ms: int, until_ms: int) -> list[tuple[int, float, float | None]]:
        return [tuple(r) for r in self.conn.execute(
            "SELECT funding_ms, rate, mark_price FROM funding_rates WHERE symbol=? AND funding_ms >= ? AND funding_ms <= ? "
            "ORDER BY funding_ms", (symbol, since_ms, until_ms))]

    def last_funding_ms(self, symbol: str) -> int | None:
        return self.conn.execute("SELECT MAX(funding_ms) FROM funding_rates WHERE symbol=?", (symbol,)).fetchone()[0]

    # ---- escrituras -----------------------------------------------------------------------------

    def add_funding_rates(self, rows: Sequence[tuple]) -> int:
        """(symbol, funding_ms, rate, mark_price, fetched_ms). INSERT OR IGNORE: una tasa liquidada no cambia."""
        if not rows:
            return 0
        cur = self.conn.executemany(
            "INSERT OR IGNORE INTO funding_rates (symbol, funding_ms, rate, mark_price, fetched_ms) VALUES (?, ?, ?, ?, ?)",
            rows)
        self.conn.commit()
        return cur.rowcount

    def record_run(self, run: dict[str, Any], *, orders: Iterable[dict] = (), funding: Iterable[dict] = (),
                   equity: Iterable[dict] = (), positions: Iterable[dict] = (), trades: Iterable[dict] = (),
                   basket: Iterable[dict] = (), gaps: Iterable[dict] = ()) -> None:
        """Todo en UNA transaccion. Si falla (p.ej. otra corrida ok de la misma hora), no queda nada escrito."""
        run = {**run, "detail": json.dumps(run.get("detail"), ensure_ascii=False, default=str)
               if not isinstance(run.get("detail"), str) else run["detail"]}
        with self.conn:
            _insert(self.conn, "runs", [run])
            _insert(self.conn, "orders", orders)
            _insert(self.conn, "funding_events", funding)
            _insert(self.conn, "equity", equity)
            _insert(self.conn, "positions", positions)
            _insert(self.conn, "trades", trades, ignore=True)  # un trade cerrado ya guardado no se duplica
            _insert(self.conn, "basket", basket)
            _insert(self.conn, "gaps", gaps)


def _insert(conn: sqlite3.Connection, table: str, rows: Iterable[dict], ignore: bool = False) -> None:
    rows = list(rows)
    if not rows:
        return
    cols = list(rows[0])
    conn.executemany(f"INSERT {'OR IGNORE ' if ignore else ''}INTO {table} ({', '.join(cols)}) "
                     f"VALUES ({', '.join('?' * len(cols))})", [tuple(r[c] for c in cols) for r in rows])
