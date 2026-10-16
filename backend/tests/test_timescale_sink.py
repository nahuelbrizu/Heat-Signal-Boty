"""Tests de TimescaleSink con un pool asyncpg falso (y opcionalmente contra Postgres real).

Integracion real: exportar `HEATSIGNAL_TEST_DATABASE_URL=postgresql://...` (usa tablas reales en esa DB;
apuntar a una base descartable). Sin esa variable, el test de integracion se saltea.
"""

from __future__ import annotations

import asyncio
import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone

import asyncpg
import pytest

from heatsignal.schema.events import (
    Coverage,
    Exchange,
    LiquidationEvent,
    Side,
    TickerEvent,
    TradeEvent,
)
from heatsignal.storage.timescale import (
    TABLE_COLUMNS,
    TimescaleSink,
    _redact,
    event_to_record,
)


def _liq(i: int = 1) -> LiquidationEvent:
    return LiquidationEvent(
        source=Exchange.BYBIT, symbol="BTC-USDT", side=Side.SELL, price=100.0, qty=2.0,
        notional_usd=200.0, ts_event_ms=1_700_000_000_000 + i, ts_ingest_ms=1_700_000_000_050 + i,
        coverage=Coverage.FULL,
    )


def _trade(i: int = 1, block: bool = False) -> TradeEvent:
    return TradeEvent(
        source=Exchange.BINANCE, symbol="SOL-USDT", side=Side.BUY, price=150.0, qty=1.0,
        ts_event_ms=1_700_000_000_000 + i, ts_ingest_ms=1_700_000_000_000 + i, is_block_trade=block,
    )


def _ticker(i: int = 1) -> TickerEvent:
    return TickerEvent(
        source=Exchange.BYBIT, symbol="BTC-USDT", mark_price=1.0, funding_rate=0.0001,
        ts_event_ms=1_700_000_000_000 + i, ts_ingest_ms=1_700_000_000_000 + i,
    )


# ---- doble de asyncpg -----------------------------------------------------------------------------


class FakeConn:
    def __init__(self, pool: FakePool) -> None:
        self.pool = pool

    async def execute(self, sql: str, *args) -> str:  # noqa: ANN002
        self.pool.executed.append(" ".join(sql.split()))
        return "OK"

    async def fetchval(self, sql: str, *args):  # noqa: ANN002, ANN201
        if "pg_available_extensions" in sql:
            return 1 if self.pool.timescale_available else 0
        if "pg_extension" in sql:
            return 1 if self.pool.timescale_available else 0
        return None

    @asynccontextmanager
    async def transaction(self):  # noqa: ANN201
        staged: list = []
        self.pool._staged = staged
        yield
        self.pool.rows.extend(staged)  # commit solo si no hubo excepcion

    async def copy_records_to_table(self, table: str, *, records, columns) -> None:  # noqa: ANN001
        if self.pool.fail_with is not None:
            raise self.pool.fail_with
        assert list(columns) == TABLE_COLUMNS[table]
        for r in records:
            assert len(r) == len(columns)
            self.pool._staged.append((table, r))


class FakePool:
    def __init__(self, *, timescale_available: bool = False) -> None:
        self.timescale_available = timescale_available
        self.executed: list[str] = []
        self.rows: list[tuple[str, tuple]] = []
        self.fail_with: BaseException | None = None
        self.closed = False
        self._staged: list = []

    @asynccontextmanager
    async def acquire(self):  # noqa: ANN201
        yield FakeConn(self)

    async def close(self) -> None:
        self.closed = True


# ---- conversion -----------------------------------------------------------------------------------


def test_event_to_record_mirrors_schema() -> None:
    table, row = event_to_record(_liq())
    assert table == "liquidations"
    rec = dict(zip(TABLE_COLUMNS[table], row))
    assert rec["source"] == "bybit" and rec["coverage"] == "full" and rec["side"] == "sell"
    assert rec["ts"] == datetime.fromtimestamp(1_700_000_000.001, tz=timezone.utc)

    table, row = event_to_record(_trade(block=True))
    assert table == "trades"
    assert dict(zip(TABLE_COLUMNS[table], row))["is_block_trade"] is True

    table, row = event_to_record(_ticker())
    assert table == "tickers"
    rec = dict(zip(TABLE_COLUMNS[table], row))
    assert rec["last_price"] is None and rec["funding_rate"] == 0.0001


def test_redact_hides_password() -> None:
    assert _redact("postgresql://u:secret@h:5432/db") == "postgresql://u:***@h:5432/db"
    assert "secret" not in _redact("postgresql://u:secret@h/db")


# ---- esquema --------------------------------------------------------------------------------------


async def test_ensure_schema_plain_mode_skips_hypertables() -> None:
    pool = FakePool(timescale_available=False)
    sink = TimescaleSink(pool)
    assert await sink.ensure_schema() == "plain"
    sql = "\n".join(pool.executed)
    for t in TABLE_COLUMNS:
        assert f"CREATE TABLE IF NOT EXISTS {t}" in sql
    assert "create_hypertable" not in sql
    assert "add_retention_policy" not in sql


async def test_ensure_schema_timescale_mode_creates_hypertables_and_policies() -> None:
    pool = FakePool(timescale_available=True)
    sink = TimescaleSink(pool)
    assert await sink.ensure_schema() == "timescale"
    sql = "\n".join(pool.executed)
    assert sql.count("create_hypertable") == 3
    assert "if_not_exists => TRUE" in sql
    assert "add_retention_policy('trades', INTERVAL '30 days'" in sql
    assert "add_retention_policy('tickers', INTERVAL '90 days'" in sql
    assert "add_retention_policy('liquidations'" not in sql  # liquidaciones no se borran
    assert sql.count("add_compression_policy") == 3


# ---- batching -------------------------------------------------------------------------------------


async def test_flush_by_size_writes_all_tables_in_one_transaction() -> None:
    pool = FakePool()
    sink = TimescaleSink(pool, batch_size=3, flush_interval_s=60.0, max_buffer=100)
    sink.start()
    await sink.write(_liq())
    await sink.write(_trade())
    assert pool.rows == []  # todavia no llego al tamanio de lote
    await sink.write(_ticker())
    await asyncio.sleep(0.05)
    assert sorted(t for t, _ in pool.rows) == ["liquidations", "tickers", "trades"]
    assert sink.stats.written == 3 and sink.stats.buffered == 0
    await sink.close()
    assert pool.closed


async def test_flush_by_interval() -> None:
    pool = FakePool()
    sink = TimescaleSink(pool, batch_size=1000, flush_interval_s=0.05, max_buffer=1000)
    sink.start()
    await sink.write(_trade())
    await asyncio.sleep(0.15)
    assert len(pool.rows) == 1
    await sink.close()


async def test_transient_failure_keeps_buffer_and_retries() -> None:
    pool = FakePool()
    sink = TimescaleSink(pool, batch_size=10, max_buffer=100)
    for i in range(5):
        await sink.write(_trade(i))
    pool.fail_with = ConnectionRefusedError("db caida")
    assert await sink.flush_once() is False
    assert sink.stats.failed_flushes == 1
    assert sink.stats.buffered == 5 and sink.stats.dropped_total == 0
    pool.fail_with = None
    assert await sink.flush_once() is True
    # orden preservado tras el reintento
    assert [r[1][6] for r in pool.rows] == [1_700_000_000_000 + i for i in range(5)]
    assert sink.stats.written == 5


async def test_overflow_drops_oldest_and_counts_explicitly() -> None:
    pool = FakePool()
    sink = TimescaleSink(pool, batch_size=2, max_buffer=3)
    pool.fail_with = OSError("sin red")
    for i in range(5):
        await sink.write(_trade(i))
    assert sink.stats.dropped_overflow == 2
    assert sink.stats.buffered == 3
    await sink.flush_once()  # falla, re-encola sin superar el tope
    assert sink.stats.buffered == 3
    pool.fail_with = None
    while sink.stats.buffered:
        await sink.flush_once()
    # sobrevivieron los 3 mas nuevos
    assert [r[1][6] - 1_700_000_000_000 for r in pool.rows] == [2, 3, 4]
    assert sink.stats.enqueued == 5 and sink.stats.written == 3


async def test_data_error_drops_batch_and_counts_rejected() -> None:
    pool = FakePool()
    sink = TimescaleSink(pool, batch_size=10, max_buffer=100)
    await sink.write(_trade())
    pool.fail_with = asyncpg.exceptions.DataError("valor invalido")
    assert await sink.flush_once() is False
    assert sink.stats.dropped_rejected == 1 and sink.stats.buffered == 0


async def test_close_flushes_remaining_and_counts_unflushable() -> None:
    pool = FakePool()
    sink = TimescaleSink(pool, batch_size=100, flush_interval_s=60.0, max_buffer=1000)
    sink.start()
    await sink.write(_liq())
    await sink.close()
    assert len(pool.rows) == 1 and sink.stats.dropped_on_close == 0

    pool2 = FakePool()
    pool2.fail_with = OSError("caida")
    sink2 = TimescaleSink(pool2, batch_size=100, max_buffer=1000, close_timeout_s=0.1)
    await sink2.write(_liq())
    await sink2.write(_trade())
    await sink2.close()
    assert sink2.stats.dropped_on_close == 2
    await sink2.write(_trade())  # escritura tardia: se cuenta, no se pierde en silencio
    assert sink2.stats.dropped_on_close == 3


async def test_connect_returns_none_when_db_unreachable() -> None:
    sink = await TimescaleSink.connect(
        "postgresql://nobody:x@127.0.0.1:1/none", connect_timeout_s=2.0
    )
    assert sink is None


# ---- integracion real (opcional) ------------------------------------------------------------------

DB_URL = os.environ.get("HEATSIGNAL_TEST_DATABASE_URL")


@pytest.mark.skipif(not DB_URL, reason="HEATSIGNAL_TEST_DATABASE_URL no configurada")
async def test_roundtrip_against_real_postgres() -> None:
    sink = await TimescaleSink.connect(DB_URL, batch_size=10, flush_interval_s=0.05)
    assert sink is not None
    await sink.ensure_schema()  # idempotente: segunda vez no falla
    marker = 4_000_000_000_000
    await sink.write(_trade(marker - 1_700_000_000_000, block=True))
    await asyncio.sleep(0.3)
    await sink.close()
    conn = await asyncpg.connect(DB_URL)
    try:
        n = await conn.fetchval(
            "SELECT count(*) FROM trades WHERE ts_event_ms = $1 AND is_block_trade", marker
        )
        assert n >= 1
        await conn.execute("DELETE FROM trades WHERE ts_event_ms = $1", marker)
    finally:
        await conn.close()
