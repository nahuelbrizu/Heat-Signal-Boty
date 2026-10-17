"""
Consultas sobre el catalogo del almacenamiento por niveles (`archive_catalog` y `archive_runs` en la base
historica; esquema en `history/store.py`). Lo usan el archivador (`archive.py`), la lectura fria (`cold.py`) y
el panel (`GET /api/history/storage`). Todas toleran una base sin catalogo (nunca se archivo nada).
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from typing import Any

# tablas cuyas filas se borran de la base al archivarse (kind='table')
ROW_TABLES = ("bars_1m", "liquidations", "news", "klines_1m")


@dataclass(frozen=True)
class CatalogEntry:
    id: int
    kind: str
    table_name: str
    symbols: str
    start_ms: int
    end_ms: int
    rows: int
    remote_path: str
    bytes: int
    md5: str
    archived_ms: int
    run_id: str


_COLS = "id, kind, table_name, symbols, start_ms, end_ms, rows, remote_path, bytes, md5, archived_ms, run_id"


def _missing(exc: sqlite3.OperationalError) -> bool:
    return "no such table" in str(exc)


def entries(conn: sqlite3.Connection, table_name: str, since_ms: int | None, until_ms: int | None,
            *, kind: str = "table") -> list[CatalogEntry]:
    """Archivos frios de `table_name` que se superponen con [since, until) (None = sin limite), viejos primero."""
    sql = f"SELECT {_COLS} FROM archive_catalog WHERE kind = ? AND table_name = ?"
    params: list[Any] = [kind, table_name]
    if until_ms is not None:
        sql += " AND start_ms < ?"
        params.append(until_ms)
    if since_ms is not None:
        sql += " AND end_ms > ?"
        params.append(since_ms)
    try:
        rows = conn.execute(sql + " ORDER BY start_ms, id", params).fetchall()
    except sqlite3.OperationalError as exc:
        if _missing(exc):
            return []
        raise
    return [CatalogEntry(*r) for r in rows]


def summary(conn: sqlite3.Connection) -> dict[str, Any]:
    """Que hay en Drive por tabla: archivos, filas, bytes y rango cubierto."""
    try:
        rows = conn.execute(
            "SELECT kind, table_name, COUNT(*), SUM(rows), SUM(bytes), MIN(start_ms), MAX(end_ms), MAX(archived_ms) "
            "FROM archive_catalog GROUP BY kind, table_name ORDER BY kind, table_name").fetchall()
    except sqlite3.OperationalError as exc:
        if _missing(exc):
            return {}
        raise
    out: dict[str, Any] = {}
    for kind, table, files, n, size, a, b, last in rows:
        key = table if kind == "table" else ("export_local" if kind == "export_local" else table)
        out[key] = {"kind": kind, "files": files, "rows": n or 0, "bytes": size or 0, "first_ms": a, "end_ms": b,
                    "last_archived_ms": last}
    return out


def last_runs(conn: sqlite3.Connection, limit: int = 5) -> list[dict[str, Any]]:
    try:
        rows = conn.execute(
            "SELECT run_id, started_ms, finished_ms, status, threshold_pct, target_pct, used_pct_before, "
            "used_pct_after, freed_bytes, detail FROM archive_runs ORDER BY started_ms DESC LIMIT ?",
            (limit,)).fetchall()
    except sqlite3.OperationalError as exc:
        if _missing(exc):
            return []
        raise
    out = []
    for r in rows:
        try:
            detail = json.loads(r[9])
        except ValueError:
            detail = {"raw": r[9][:500]}
        out.append({"run_id": r[0], "started_ms": r[1], "finished_ms": r[2], "status": r[3], "threshold_pct": r[4],
                    "target_pct": r[5], "used_pct_before": r[6], "used_pct_after": r[7], "freed_bytes": r[8],
                    "detail": detail})
    return out
