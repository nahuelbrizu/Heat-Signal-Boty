"""
Sink de persistencia a PostgreSQL / TimescaleDB (asyncpg).

Tres tablas, una por tipo de evento, con columnas que espejan `schema/events.py`:
  - `liquidations` (incluye `coverage`: full/partial — la cobertura NO es uniforme entre exchanges)
  - `trades`       (incluye `is_block_trade`)
  - `tickers`
Todas llevan `source` (exchange) y una columna `ts timestamptz` derivada de `ts_event_ms`, que es la
dimension temporal de la hypertable. Se guardan tambien `ts_event_ms`/`ts_ingest_ms` crudos (epoch ms UTC)
para no perder precision ni la latencia de ingesta.

Modos (se loguea cual se uso en `ensure_schema()`):
  - **timescale**: la extension `timescaledb` esta disponible -> `create_hypertable(..., if_not_exists =>
    TRUE)` + politicas de compresion y retencion.
  - **plain**: Postgres sin TimescaleDB -> tablas comunes con indice (symbol, ts). Funciona igual; la
    retencion queda manual (`prune_plain()`), ver `RETENTION_NOTE`.

Escritura: `write()` NUNCA toca la red — solo encola en memoria. Una tarea de fondo vacia la cola por
tamanio (`batch_size`) o por tiempo (`flush_interval_s`) con `COPY` dentro de una transaccion (todo el
lote entra o no entra nada -> sin duplicados al reintentar).

Sin perdida silenciosa (principio 5 de CLAUDE.md):
  - Si la DB falla (conexion), el lote vuelve al frente de la cola y se reintenta en el proximo ciclo.
  - La cola esta acotada (`max_buffer`): si se llena, se descartan los eventos MAS VIEJOS y se cuentan en
    `stats.dropped_overflow`, con log de error.
  - Si la DB rechaza el lote por datos (error no transitorio), se descarta y se cuenta en
    `stats.dropped_rejected` (si no, un registro envenenado frenaria la cola para siempre).
  - Lo que quede en cola al cerrar sin poder escribirse se cuenta en `stats.dropped_on_close`.
"""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any

from ..schema.events import IngestEvent

log = logging.getLogger("heatsignal.storage.timescale")


# ---- DDL ----------------------------------------------------------------------------------------

TABLE_COLUMNS: dict[str, list[str]] = {
    "liquidations": [
        "ts", "source", "symbol", "side", "price", "qty", "notional_usd",
        "ts_event_ms", "ts_ingest_ms", "coverage",
    ],
    "trades": [
        "ts", "source", "symbol", "side", "price", "qty",
        "ts_event_ms", "ts_ingest_ms", "is_block_trade",
    ],
    "tickers": [
        "ts", "source", "symbol", "mark_price", "last_price", "funding_rate",
        "open_interest", "open_interest_usd", "ts_event_ms", "ts_ingest_ms",
    ],
}

KIND_TO_TABLE = {"liquidation": "liquidations", "trade": "trades", "ticker": "tickers"}

CREATE_TABLES_SQL = [
    """
    CREATE TABLE IF NOT EXISTS liquidations (
        ts            TIMESTAMPTZ      NOT NULL,
        source        TEXT             NOT NULL,
        symbol        TEXT             NOT NULL,
        side          TEXT             NOT NULL,
        price         DOUBLE PRECISION NOT NULL,
        qty           DOUBLE PRECISION NOT NULL,
        notional_usd  DOUBLE PRECISION NOT NULL,
        ts_event_ms   BIGINT           NOT NULL,
        ts_ingest_ms  BIGINT           NOT NULL,
        coverage      TEXT             NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS trades (
        ts              TIMESTAMPTZ      NOT NULL,
        source          TEXT             NOT NULL,
        symbol          TEXT             NOT NULL,
        side            TEXT             NOT NULL,
        price           DOUBLE PRECISION NOT NULL,
        qty             DOUBLE PRECISION NOT NULL,
        ts_event_ms     BIGINT           NOT NULL,
        ts_ingest_ms    BIGINT           NOT NULL,
        is_block_trade  BOOLEAN          NOT NULL DEFAULT FALSE
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS tickers (
        ts                 TIMESTAMPTZ      NOT NULL,
        source             TEXT             NOT NULL,
        symbol             TEXT             NOT NULL,
        mark_price         DOUBLE PRECISION,
        last_price         DOUBLE PRECISION,
        funding_rate       DOUBLE PRECISION,
        open_interest      DOUBLE PRECISION,
        open_interest_usd  DOUBLE PRECISION,
        ts_event_ms        BIGINT           NOT NULL,
        ts_ingest_ms       BIGINT           NOT NULL
    )
    """,
]

CREATE_INDEXES_SQL = [
    f"CREATE INDEX IF NOT EXISTS {t}_symbol_ts_idx ON {t} (symbol, ts DESC)" for t in TABLE_COLUMNS
] + [
    f"CREATE INDEX IF NOT EXISTS {t}_source_symbol_ts_idx ON {t} (source, symbol, ts DESC)"
    for t in TABLE_COLUMNS
]

# Retencion por defecto (dias). None = no borrar nunca.
# - liquidations: volumen bajo y es el nucleo del producto (heatmap propio) -> se guarda todo.
# - trades: tick-by-tick, es lo que mas crece -> 30 dias crudos (los agregados OHLCV/CVD se derivan
#   antes y viven aparte cuando existan).
# - tickers: ya throttleados a 1/seg por (source, symbol) -> 90 dias.
DEFAULT_RETENTION_DAYS: dict[str, int | None] = {
    "liquidations": None,
    "trades": 30,
    "tickers": 90,
}
DEFAULT_COMPRESS_AFTER_DAYS = 7

RETENTION_NOTE = """
Politica de retencion (checklist de higiene, phase-0-blockers.md):
  liquidations -> sin retencion (bajo volumen, nucleo del producto)
  trades       -> 30 dias crudos, compresion a los 7 dias
  tickers      -> 90 dias, compresion a los 7 dias (ya vienen throttleados a 1/seg)

Con TimescaleDB, `ensure_schema()` aplica:
  ALTER TABLE <t> SET (timescaledb.compress, timescaledb.compress_segmentby = 'source, symbol');
  SELECT add_compression_policy('<t>', INTERVAL '7 days', if_not_exists => TRUE);
  SELECT add_retention_policy('<t>', INTERVAL '<N> days', if_not_exists => TRUE);

Sin TimescaleDB (Postgres plano) no hay jobs de fondo: correr periodicamente (cron / pg_cron):
  DELETE FROM trades  WHERE ts < now() - INTERVAL '30 days';
  DELETE FROM tickers WHERE ts < now() - INTERVAL '90 days';
(`TimescaleSink.prune_plain()` hace exactamente eso.)
"""


# ---- conversion evento -> fila ------------------------------------------------------------------

def _ts(ms: int) -> datetime:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc)


def event_to_record(event: IngestEvent) -> tuple[str, tuple[Any, ...]]:
    """Devuelve (tabla, fila) en el orden de `TABLE_COLUMNS[tabla]`."""
    if event.kind == "liquidation":
        return "liquidations", (
            _ts(event.ts_event_ms), event.source.value, event.symbol, event.side.value,
            event.price, event.qty, event.notional_usd,
            event.ts_event_ms, event.ts_ingest_ms, event.coverage.value,
        )
    if event.kind == "trade":
        return "trades", (
            _ts(event.ts_event_ms), event.source.value, event.symbol, event.side.value,
            event.price, event.qty, event.ts_event_ms, event.ts_ingest_ms, event.is_block_trade,
        )
    if event.kind == "ticker":
        return "tickers", (
            _ts(event.ts_event_ms), event.source.value, event.symbol,
            event.mark_price, event.last_price, event.funding_rate,
            event.open_interest, event.open_interest_usd,
            event.ts_event_ms, event.ts_ingest_ms,
        )
    raise ValueError(f"tipo de evento desconocido: {getattr(event, 'kind', '?')!r}")


# ---- clasificacion de errores -------------------------------------------------------------------

def _is_transient(exc: BaseException) -> bool:
    """True si el error es de conexion/disponibilidad (reintentar); False si es de datos (descartar)."""
    if isinstance(exc, (OSError, asyncio.TimeoutError, ConnectionError)):
        return True
    try:
        import asyncpg
    except ImportError:  # pragma: no cover - asyncpg es dependencia declarada
        return True
    transient = (
        asyncpg.exceptions.InterfaceError,
        asyncpg.exceptions.PostgresConnectionError,
        asyncpg.exceptions.CannotConnectNowError,
        asyncpg.exceptions.TooManyConnectionsError,
        asyncpg.exceptions.AdminShutdownError,
        asyncpg.exceptions.UndefinedTableError,  # tabla borrada por fuera: reintentar tras re-crear
    )
    if isinstance(exc, transient):
        return True
    if isinstance(exc, asyncpg.exceptions.PostgresError):
        return False  # DataError, constraint, etc.: el lote no va a entrar reintentando
    return True  # desconocido: ser conservador y no tirar datos


# ---- sink ---------------------------------------------------------------------------------------

@dataclass
class TimescaleStats:
    """Contadores expuestos (sin perdida silenciosa). Todos acumulativos desde el arranque."""
    enqueued: int = 0
    written: int = 0
    dropped_overflow: int = 0
    dropped_rejected: int = 0
    dropped_on_close: int = 0
    failed_flushes: int = 0
    buffered: int = 0

    @property
    def dropped_total(self) -> int:
        return self.dropped_overflow + self.dropped_rejected + self.dropped_on_close

    def as_dict(self) -> dict[str, int]:
        return {**asdict(self), "dropped_total": self.dropped_total}


class TimescaleSink:
    """EventSink que persiste en Postgres/TimescaleDB por lotes.

    Uso recomendado (no crashea si la DB no esta):
        sink = await TimescaleSink.connect(os.environ["DATABASE_URL"])   # None si no conecta
        ...
        await sink.close()   # flush final

    `pool` es cualquier objeto con la API de `asyncpg.Pool` (`acquire()` como context manager async y
    `close()`), lo que permite testear con un doble.
    """

    def __init__(
        self,
        pool: Any,
        *,
        batch_size: int = 500,
        flush_interval_s: float = 1.0,
        max_buffer: int = 100_000,
        retention_days: dict[str, int | None] | None = None,
        compress_after_days: int | None = DEFAULT_COMPRESS_AFTER_DAYS,
        close_timeout_s: float = 10.0,
    ) -> None:
        if batch_size <= 0 or max_buffer < batch_size:
            raise ValueError("batch_size > 0 y max_buffer >= batch_size")
        self._pool = pool
        self._batch_size = batch_size
        self._flush_interval_s = flush_interval_s
        self._max_buffer = max_buffer
        self._retention = dict(DEFAULT_RETENTION_DAYS if retention_days is None else retention_days)
        self._compress_after_days = compress_after_days
        self._close_timeout_s = close_timeout_s
        self._buffer: deque[tuple[str, tuple[Any, ...]]] = deque()
        self._wake = asyncio.Event()
        self._flusher: asyncio.Task[None] | None = None
        self._closed = False
        self._late_logged = False
        self._flush_lock = asyncio.Lock()
        self.stats = TimescaleStats()
        self.mode: str | None = None  # "timescale" | "plain", se fija en ensure_schema()

    # ---- arranque ------------------------------------------------------------------------------

    @classmethod
    async def connect(
        cls,
        dsn: str,
        *,
        connect_timeout_s: float = 5.0,
        min_size: int = 1,
        max_size: int = 4,
        **kwargs: Any,
    ) -> TimescaleSink | None:
        """Crea pool + esquema + flusher. Si la DB no responde devuelve None con warning (no crashea)."""
        try:
            import asyncpg

            pool = await asyncio.wait_for(
                asyncpg.create_pool(dsn, min_size=min_size, max_size=max_size),
                timeout=connect_timeout_s,
            )
        except Exception:  # noqa: BLE001 - cualquier fallo de arranque -> sin persistencia, con aviso
            log.warning("PostgreSQL no disponible (%s) - SIN persistencia", _redact(dsn), exc_info=True)
            return None
        sink = cls(pool, **kwargs)
        try:
            await sink.ensure_schema()
        except Exception:  # noqa: BLE001
            log.warning("no se pudo crear el esquema en PostgreSQL - SIN persistencia", exc_info=True)
            await pool.close()
            return None
        sink.start()
        return sink

    def start(self) -> None:
        """Arranca la tarea de flush periodico (idempotente)."""
        if self._flusher is None or self._flusher.done():
            self._flusher = asyncio.create_task(self._flush_loop(), name="timescale-flusher")

    async def ensure_schema(self) -> str:
        """Crea tablas/indices de forma idempotente; hypertables + politicas solo si hay TimescaleDB.

        Devuelve y loguea el modo: "timescale" o "plain".
        """
        async with self._pool.acquire() as conn:
            for sql in CREATE_TABLES_SQL:
                await conn.execute(sql)
            timescale = await self._enable_timescale(conn)
            if timescale:
                for table in TABLE_COLUMNS:
                    await conn.execute(
                        "SELECT create_hypertable($1::regclass, 'ts', if_not_exists => TRUE, "
                        "migrate_data => TRUE)",
                        table,
                    )
            for sql in CREATE_INDEXES_SQL:
                await conn.execute(sql)
            if timescale:
                await self._apply_policies(conn)
        self.mode = "timescale" if timescale else "plain"
        if timescale:
            log.info("TimescaleSink en modo TIMESCALE (hypertables + compresion/retencion)")
        else:
            log.warning(
                "TimescaleSink en modo PLAIN (Postgres sin extension timescaledb): tablas comunes, "
                "sin hypertables ni retencion automatica - ver RETENTION_NOTE / prune_plain()"
            )
        return self.mode

    @staticmethod
    async def _enable_timescale(conn: Any) -> bool:
        available = await conn.fetchval(
            "SELECT count(*) FROM pg_available_extensions WHERE name = 'timescaledb'"
        )
        if not available:
            return False
        try:
            await conn.execute("CREATE EXTENSION IF NOT EXISTS timescaledb")
        except Exception:  # noqa: BLE001 - p.ej. sin permisos: seguimos en modo plain
            log.warning("timescaledb disponible pero no se pudo habilitar", exc_info=True)
        installed = await conn.fetchval(
            "SELECT count(*) FROM pg_extension WHERE extname = 'timescaledb'"
        )
        return bool(installed)

    async def _apply_policies(self, conn: Any) -> None:
        """Compresion + retencion (solo TimescaleDB). Cada sentencia aislada: un fallo no aborta el resto."""
        for table in TABLE_COLUMNS:
            stmts: list[str] = []
            if self._compress_after_days is not None:
                stmts += [
                    f"ALTER TABLE {table} SET (timescaledb.compress, "
                    f"timescaledb.compress_segmentby = 'source, symbol')",
                    f"SELECT add_compression_policy('{table}', "
                    f"INTERVAL '{int(self._compress_after_days)} days', if_not_exists => TRUE)",
                ]
            days = self._retention.get(table)
            if days is not None:
                stmts.append(
                    f"SELECT add_retention_policy('{table}', INTERVAL '{int(days)} days', "
                    f"if_not_exists => TRUE)"
                )
            for sql in stmts:
                try:
                    await conn.execute(sql)
                except Exception:  # noqa: BLE001
                    log.warning("politica no aplicada (%s)", sql, exc_info=True)

    async def prune_plain(self) -> dict[str, str]:
        """Retencion manual para modo plain (en timescale la hace el job de la extension)."""
        out: dict[str, str] = {}
        async with self._pool.acquire() as conn:
            for table, days in self._retention.items():
                if days is None:
                    continue
                out[table] = await conn.execute(
                    f"DELETE FROM {table} WHERE ts < now() - make_interval(days => $1)", int(days)
                )
        return out

    # ---- escritura -----------------------------------------------------------------------------

    async def write(self, event: IngestEvent) -> None:
        if self._closed:
            self.stats.dropped_on_close += 1
            if not self._late_logged:  # loguear una vez; el total queda en stats
                self._late_logged = True
                log.error("TimescaleSink cerrado: llegan eventos tarde (%s), se descartan", event.kind)
            return
        self._buffer.append(event_to_record(event))
        self.stats.enqueued += 1
        self._trim_overflow()
        self.stats.buffered = len(self._buffer)
        if len(self._buffer) >= self._batch_size:
            self._wake.set()

    def _trim_overflow(self) -> None:
        overflow = len(self._buffer) - self._max_buffer
        if overflow <= 0:
            return
        for _ in range(overflow):
            self._buffer.popleft()
        self.stats.dropped_overflow += overflow
        log.error(
            "TimescaleSink: cola llena (%d) - %d eventos MAS VIEJOS descartados (total %d)",
            self._max_buffer, overflow, self.stats.dropped_overflow,
        )

    async def _flush_loop(self) -> None:
        while not self._closed:
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=self._flush_interval_s)
            except asyncio.TimeoutError:
                pass
            self._wake.clear()
            # Vaciar en lotes mientras haya y la DB responda.
            while self._buffer and await self.flush_once():
                if len(self._buffer) < self._batch_size:
                    break

    async def flush_once(self) -> bool:
        """Intenta escribir un lote. True si escribio (o no habia nada); False si fallo y hay que esperar."""
        async with self._flush_lock:
            if not self._buffer:
                return True
            n = min(self._batch_size, len(self._buffer))
            batch = [self._buffer.popleft() for _ in range(n)]
            by_table: dict[str, list[tuple[Any, ...]]] = {}
            for table, row in batch:
                by_table.setdefault(table, []).append(row)
            try:
                async with self._pool.acquire() as conn:
                    async with conn.transaction():
                        for table, rows in by_table.items():
                            await conn.copy_records_to_table(
                                table, records=rows, columns=TABLE_COLUMNS[table]
                            )
            except Exception as exc:  # noqa: BLE001
                self.stats.failed_flushes += 1
                if _is_transient(exc):
                    # Devolver el lote al frente (orden preservado) y respetar el tope de la cola.
                    self._buffer.extendleft(reversed(batch))
                    self._trim_overflow()
                    log.error(
                        "TimescaleSink: fallo escribiendo lote de %d (se reintenta, en cola %d): %r",
                        n, len(self._buffer), exc,
                    )
                else:
                    self.stats.dropped_rejected += n
                    log.error(
                        "TimescaleSink: la DB rechazo un lote de %d eventos (DESCARTADO, total %d): %r",
                        n, self.stats.dropped_rejected, exc,
                    )
                self.stats.buffered = len(self._buffer)
                return False
            self.stats.written += n
            self.stats.buffered = len(self._buffer)
            return True

    # ---- cierre --------------------------------------------------------------------------------

    async def close(self) -> None:
        """Detiene el flusher, intenta un flush final acotado en tiempo y cierra el pool."""
        if self._closed:
            return
        self._closed = True
        self._wake.set()
        if self._flusher is not None:
            self._flusher.cancel()
            try:
                await self._flusher
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass

        async def _drain() -> None:
            while self._buffer:
                if not await self.flush_once():
                    return

        try:
            await asyncio.wait_for(_drain(), timeout=self._close_timeout_s)
        except asyncio.TimeoutError:
            log.error("TimescaleSink: timeout en el flush final")
        if self._buffer:
            lost = len(self._buffer)
            self.stats.dropped_on_close += lost
            self._buffer.clear()
            log.error("TimescaleSink: %d eventos sin persistir al cerrar (DESCARTADOS)", lost)
        self.stats.buffered = 0
        try:
            await self._pool.close()
        except Exception:  # noqa: BLE001
            log.debug("error cerrando pool", exc_info=True)
        log.info("TimescaleSink cerrado: %s", self.stats.as_dict())


def _redact(dsn: str) -> str:
    """Oculta la password del DSN para logs."""
    if "@" not in dsn or "://" not in dsn:
        return dsn
    scheme, rest = dsn.split("://", 1)
    creds, host = rest.rsplit("@", 1)
    user = creds.split(":", 1)[0]
    return f"{scheme}://{user}:***@{host}"
