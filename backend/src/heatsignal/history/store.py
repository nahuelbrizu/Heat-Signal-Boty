"""
Almacen historico privado (etapa 1 del plan de herramienta personal, decisions.md "Datos de mercado
guardados para entrenar y simular").

Un archivo SQLite en la VM (`HEATSIGNAL_HISTORY_DB`) con:
  - bars_1m       agregados por minuto, exchange y simbolo (nuestros: no hay historia publica gratis de
                  liquidaciones, flujo taker por exchange, funding y OI a 1 minuto)
  - liquidations  cada liquidacion recibida (son pocas: cientos por dia)
  - news          cada noticia vista por primera vez
  - klines        velas historicas descargadas por REST (Binance 1m; 5m..1h se derivan exactas de 1m)

Las tablas de datos propios son de solo-agregar en la practica: un minuto que recibe eventos tarde se
fusiona (suma volumenes, extiende high/low) en vez de pisarse. Nada de esto se publica; el respaldo va a
la carpeta personal de Drive del usuario (`history/export.py`).
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable, Sequence
from dataclasses import astuple, dataclass, fields
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS bars_1m (
    exchange      TEXT    NOT NULL,
    symbol        TEXT    NOT NULL,
    minute_ms     INTEGER NOT NULL,     -- inicio del minuto (UTC, epoch ms)
    open          REAL,                 -- OHLC de trades (no block); NULL si no hubo trades
    high          REAL,
    low           REAL,
    close         REAL,
    buy_usd       REAL    NOT NULL DEFAULT 0,   -- volumen taker comprador
    sell_usd      REAL    NOT NULL DEFAULT 0,   -- volumen taker vendedor
    n_trades      INTEGER NOT NULL DEFAULT 0,
    block_usd     REAL    NOT NULL DEFAULT 0,   -- block trades (excluidos de buy/sell)
    liq_long_usd  REAL    NOT NULL DEFAULT 0,   -- longs liquidados (orden de liquidacion = SELL)
    liq_short_usd REAL    NOT NULL DEFAULT 0,   -- shorts liquidados (orden = BUY)
    n_liqs        INTEGER NOT NULL DEFAULT 0,
    mark          REAL,                 -- ultimo mark del minuto
    funding_8h    REAL,                 -- ultimo funding del minuto (equivalente 8 h)
    oi_usd        REAL,                 -- ultimo open interest del minuto, en USD
    PRIMARY KEY (exchange, symbol, minute_ms)
);
CREATE INDEX IF NOT EXISTS idx_bars_1m_symbol_minute ON bars_1m (symbol, minute_ms);

CREATE TABLE IF NOT EXISTS liquidations (
    exchange     TEXT    NOT NULL,
    symbol       TEXT    NOT NULL,
    ts_ms        INTEGER NOT NULL,
    side         TEXT    NOT NULL,      -- lado de la ORDEN de liquidacion
    price        REAL    NOT NULL,
    qty          REAL    NOT NULL,
    notional_usd REAL    NOT NULL,
    coverage     TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_liquidations_symbol_ts ON liquidations (symbol, ts_ms);

CREATE TABLE IF NOT EXISTS news (
    item_id      TEXT PRIMARY KEY,
    source       TEXT    NOT NULL,
    is_primary   INTEGER NOT NULL,
    title        TEXT    NOT NULL,
    summary      TEXT    NOT NULL,
    url          TEXT    NOT NULL,
    published_ms INTEGER NOT NULL,
    fetched_ms   INTEGER NOT NULL,
    symbols      TEXT    NOT NULL,      -- 'BTC,XRP'
    market_wide  INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_news_published ON news (published_ms);

CREATE TABLE IF NOT EXISTS klines (
    exchange        TEXT    NOT NULL,
    symbol          TEXT    NOT NULL,   -- formato interno 'XRP-USDT'
    interval        TEXT    NOT NULL,
    open_time_ms    INTEGER NOT NULL,
    open            REAL    NOT NULL,
    high            REAL    NOT NULL,
    low             REAL    NOT NULL,
    close           REAL    NOT NULL,
    volume          REAL    NOT NULL,   -- activo base
    quote_volume    REAL    NOT NULL,
    n_trades        INTEGER NOT NULL,
    taker_buy_quote REAL    NOT NULL,
    PRIMARY KEY (exchange, symbol, interval, open_time_ms)
);

-- Catalogo del almacenamiento por niveles (history/archive.py): que rangos ya NO estan en esta base porque se
-- archivaron en Drive (verificados). Vive en esta misma base para que el DELETE de las filas y el alta en el
-- catalogo sean UNA transaccion: un rango esta en el catalogo si y solo si se borro de aca. Solo-agregar.
CREATE TABLE IF NOT EXISTS archive_catalog (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    kind         TEXT    NOT NULL,      -- 'table' (filas borradas) | 'export_local' | 'checkpoints'
    table_name   TEXT    NOT NULL,      -- bars_1m | liquidations | news | klines_1m | export | checkpoints
    symbols      TEXT    NOT NULL,      -- 'BTC-USDT,XRP-USDT' ('' si no aplica)
    start_ms     INTEGER NOT NULL,      -- rango [start_ms, end_ms) UTC
    end_ms       INTEGER NOT NULL,
    rows         INTEGER NOT NULL,
    remote_path  TEXT    NOT NULL,      -- 'gdrive:archive/history/2026/08/17/bars_1m.csv.gz'
    bytes        INTEGER NOT NULL,
    md5          TEXT    NOT NULL,
    archived_ms  INTEGER NOT NULL,
    run_id       TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_archive_catalog_table ON archive_catalog (table_name, start_ms);
CREATE TRIGGER IF NOT EXISTS archive_catalog_no_update BEFORE UPDATE ON archive_catalog
BEGIN SELECT RAISE(ABORT, 'archive_catalog es solo-agregar'); END;
CREATE TRIGGER IF NOT EXISTS archive_catalog_no_delete BEFORE DELETE ON archive_catalog
BEGIN SELECT RAISE(ABORT, 'archive_catalog es solo-agregar'); END;

CREATE TABLE IF NOT EXISTS archive_runs (
    run_id          TEXT PRIMARY KEY,
    started_ms      INTEGER NOT NULL,
    finished_ms     INTEGER NOT NULL,
    status          TEXT    NOT NULL,   -- ok | error | partial
    threshold_pct   REAL    NOT NULL,
    target_pct      REAL    NOT NULL,
    used_pct_before REAL,
    used_pct_after  REAL,
    freed_bytes     INTEGER NOT NULL DEFAULT 0,
    detail          TEXT    NOT NULL    -- JSON: unidades archivadas, errores, vacuum
);
CREATE TRIGGER IF NOT EXISTS archive_runs_no_update BEFORE UPDATE ON archive_runs
BEGIN SELECT RAISE(ABORT, 'archive_runs es solo-agregar'); END;
CREATE TRIGGER IF NOT EXISTS archive_runs_no_delete BEFORE DELETE ON archive_runs
BEGIN SELECT RAISE(ABORT, 'archive_runs es solo-agregar'); END;
"""


@dataclass
class MinuteBar:
    exchange: str
    symbol: str
    minute_ms: int
    open: float | None = None
    high: float | None = None
    low: float | None = None
    close: float | None = None
    buy_usd: float = 0.0
    sell_usd: float = 0.0
    n_trades: int = 0
    block_usd: float = 0.0
    liq_long_usd: float = 0.0
    liq_short_usd: float = 0.0
    n_liqs: int = 0
    mark: float | None = None
    funding_8h: float | None = None
    oi_usd: float | None = None


BAR_COLUMNS = [f.name for f in fields(MinuteBar)]

# Fusion de un minuto que ya estaba guardado con eventos tardios del mismo minuto. `open` se conserva
# (el primero que se guardo), `close`/mark/funding/oi toman lo mas nuevo si llego algo.
_UPSERT_BAR = f"""
INSERT INTO bars_1m ({", ".join(BAR_COLUMNS)}) VALUES ({", ".join("?" * len(BAR_COLUMNS))})
ON CONFLICT (exchange, symbol, minute_ms) DO UPDATE SET
    open          = COALESCE(bars_1m.open, excluded.open),
    high          = CASE WHEN bars_1m.high IS NULL THEN excluded.high
                         WHEN excluded.high IS NULL THEN bars_1m.high
                         ELSE MAX(bars_1m.high, excluded.high) END,
    low           = CASE WHEN bars_1m.low IS NULL THEN excluded.low
                         WHEN excluded.low IS NULL THEN bars_1m.low
                         ELSE MIN(bars_1m.low, excluded.low) END,
    close         = COALESCE(excluded.close, bars_1m.close),
    buy_usd       = bars_1m.buy_usd + excluded.buy_usd,
    sell_usd      = bars_1m.sell_usd + excluded.sell_usd,
    n_trades      = bars_1m.n_trades + excluded.n_trades,
    block_usd     = bars_1m.block_usd + excluded.block_usd,
    liq_long_usd  = bars_1m.liq_long_usd + excluded.liq_long_usd,
    liq_short_usd = bars_1m.liq_short_usd + excluded.liq_short_usd,
    n_liqs        = bars_1m.n_liqs + excluded.n_liqs,
    mark          = COALESCE(excluded.mark, bars_1m.mark),
    funding_8h    = COALESCE(excluded.funding_8h, bars_1m.funding_8h),
    oi_usd        = COALESCE(excluded.oi_usd, bars_1m.oi_usd)
"""

KLINE_COLUMNS = ["exchange", "symbol", "interval", "open_time_ms", "open", "high", "low", "close",
                 "volume", "quote_volume", "n_trades", "taker_buy_quote"]


class HistoryStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # timeout: la ingesta y la descarga de velas pueden escribir a la vez (WAL + espera del lock)
        self._conn = sqlite3.connect(str(self.path), timeout=30)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    # ---- escritura ----------------------------------------------------------------------------

    def upsert_bars(self, bars: Iterable[MinuteBar]) -> int:
        rows = [astuple(b) for b in bars]
        if rows:
            self._conn.executemany(_UPSERT_BAR, rows)
            self._conn.commit()
        return len(rows)

    def add_liquidations(self, rows: Sequence[tuple]) -> None:
        if rows:
            self._conn.executemany(
                "INSERT INTO liquidations (exchange, symbol, ts_ms, side, price, qty, notional_usd, coverage) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)", rows)
            self._conn.commit()

    def add_news(self, rows: Sequence[tuple]) -> int:
        if not rows:
            return 0
        cur = self._conn.executemany(
            "INSERT OR IGNORE INTO news (item_id, source, is_primary, title, summary, url, published_ms, "
            "fetched_ms, symbols, market_wide) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", rows)
        self._conn.commit()
        return cur.rowcount

    def add_klines(self, rows: Sequence[tuple]) -> int:
        if not rows:
            return 0
        cur = self._conn.executemany(
            f"INSERT OR IGNORE INTO klines ({', '.join(KLINE_COLUMNS)}) "
            f"VALUES ({', '.join('?' * len(KLINE_COLUMNS))})", rows)
        self._conn.commit()
        return cur.rowcount

    # ---- lectura ------------------------------------------------------------------------------

    def query(self, sql: str, params: Sequence[Any] = ()) -> list[tuple]:
        return self._conn.execute(sql, params).fetchall()

    def last_kline_ms(self, exchange: str, symbol: str, interval: str) -> int | None:
        row = self._conn.execute(
            "SELECT MAX(open_time_ms) FROM klines WHERE exchange=? AND symbol=? AND interval=?",
            (exchange, symbol, interval)).fetchone()
        return row[0]

    def summary(self) -> dict[str, Any]:
        return summarize(self._conn, self.path)

    def close(self) -> None:
        self._conn.close()


def open_readonly(path: str | Path, timeout_s: float = 5.0) -> sqlite3.Connection:
    """Conexion de SOLO lectura a la base historica (modo WAL: no bloquea a la ingesta que escribe).

    Pensada para abrirse y usarse dentro de un hilo aparte (`asyncio.to_thread`): una conexion nueva por
    consulta, asi las lecturas pesadas del panel no corren en el hilo del event loop.
    """
    from urllib.parse import quote

    uri = f"file:{quote(str(Path(path).resolve()))}?mode=ro"
    return sqlite3.connect(uri, uri=True, timeout=timeout_s, check_same_thread=True)


def summarize(conn: sqlite3.Connection, path: str | Path) -> dict[str, Any]:
    """Resumen de lo guardado (COUNTs por tabla). Escanea tablas grandes: correrlo fuera del event loop."""
    path = Path(path)

    def one(sql: str) -> Any:
        return conn.execute(sql).fetchone()

    bars = one("SELECT COUNT(*), MIN(minute_ms), MAX(minute_ms) FROM bars_1m")
    liqs = one("SELECT COUNT(*), MAX(ts_ms), MIN(ts_ms) FROM liquidations")
    news = one("SELECT COUNT(*), MAX(fetched_ms), MIN(fetched_ms) FROM news")
    klines = conn.execute(
        "SELECT exchange, symbol, interval, COUNT(*), MIN(open_time_ms), MAX(open_time_ms) "
        "FROM klines GROUP BY exchange, symbol, interval ORDER BY symbol").fetchall()
    size = sum(p.stat().st_size for p in path.parent.glob(path.name + "*") if p.exists())
    return {
        "path": str(path),
        "size_bytes": size,
        "bars_1m": {"rows": bars[0], "first_ms": bars[1], "last_ms": bars[2]},
        "liquidations": {"rows": liqs[0], "first_ms": liqs[2], "last_ms": liqs[1]},
        "news": {"rows": news[0], "first_ms": news[2], "last_ms": news[1]},
        "klines": [
            {"exchange": e, "symbol": s, "interval": i, "rows": n, "first_ms": a, "last_ms": b}
            for e, s, i, n, a, b in klines
        ],
    }
