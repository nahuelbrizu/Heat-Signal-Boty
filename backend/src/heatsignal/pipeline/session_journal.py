"""
Diario del dia (append-only): un analisis por (dia UTC, sesion, tipo) en la base de evaluacion.

    kind = 'code'  analisis por codigo de la sesion (siempre que la sesion se dispara, haya o no LLM)
    kind = 'llm'   resultado del grafo para esa sesion: llm_status 'con_llm' (resumen del Redactor), 'sin_llm'
                   (sin API key, sin estrategias o grafo apagado) o 'error' (el grafo fallo). Nunca se pisa el 'code'.

UNIQUE (day, session, kind) + triggers que bloquean UPDATE/DELETE: re-correr una sesion (reinicio, catch-up) no
duplica ni reescribe. Cada analisis siguiente del MISMO dia recibe los anteriores (`day()`), asi el razonamiento se
suma a lo largo del dia (Asia -> Europa -> EE.UU. -> cierre).
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS session_journal (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    day         TEXT    NOT NULL,          -- dia UTC 'YYYY-MM-DD' (de la apertura de la vela de la sesion)
    session     TEXT    NOT NULL,          -- asia | europa | eeuu | cierre | ...
    kind        TEXT    NOT NULL CHECK (kind IN ('code', 'llm')),
    label       TEXT    NOT NULL,
    open_ms     INTEGER NOT NULL,
    trigger_ms  INTEGER NOT NULL,
    created_ms  INTEGER NOT NULL,
    status      TEXT    NOT NULL,          -- code: ok | partial | missing ; llm: con_llm | sin_llm | error
    llm_status  TEXT,
    run_id      TEXT,
    summary     TEXT    NOT NULL,          -- texto (codigo o Redactor) que reciben los analisis siguientes del dia
    data        TEXT,                      -- JSON: SessionAnalysis (code) o reporte por simbolo (llm)
    UNIQUE (day, session, kind)
);
CREATE INDEX IF NOT EXISTS idx_session_journal_day ON session_journal (day, open_ms);
CREATE TRIGGER IF NOT EXISTS trg_session_journal_no_update BEFORE UPDATE ON session_journal
BEGIN SELECT RAISE(ABORT, 'session_journal es append-only: UPDATE no permitido'); END;
CREATE TRIGGER IF NOT EXISTS trg_session_journal_no_delete BEFORE DELETE ON session_journal
BEGIN SELECT RAISE(ABORT, 'session_journal es append-only: DELETE no permitido'); END;
"""

_COLS = "id, day, session, kind, label, open_ms, trigger_ms, created_ms, status, llm_status, run_id, summary, data"


class SessionJournal:
    def __init__(self, db_path: str | Path, timeout_s: float = 10.0) -> None:
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False, timeout=timeout_s)
        self._lock = threading.Lock()
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    def append(self, *, day: str, session: str, kind: str, label: str, open_ms: int, trigger_ms: int, status: str,
               summary: str, data: Any = None, llm_status: str | None = None, run_id: str | None = None) -> bool:
        """Agrega la fila. False (sin error) si ya existia (day, session, kind): no se duplica ni se pisa."""
        with self._lock:
            cur = self._conn.execute(
                f"INSERT OR IGNORE INTO session_journal ({_COLS.split(', ', 1)[1]}) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (day, session, kind, label, open_ms, trigger_ms, int(time.time() * 1000), status, llm_status, run_id,
                 summary, None if data is None else json.dumps(data, default=str)))
            self._conn.commit()
            return cur.rowcount == 1

    def has(self, day: str, session: str, kind: str = "code") -> bool:
        with self._lock:
            return self._conn.execute("SELECT 1 FROM session_journal WHERE day=? AND session=? AND kind=?",
                                      (day, session, kind)).fetchone() is not None

    @staticmethod
    def _row(r: tuple, with_data: bool) -> dict[str, Any]:
        keys = _COLS.split(", ")
        d = dict(zip(keys, r))
        d["data"] = json.loads(d["data"]) if with_data and d["data"] else None
        return d

    def day(self, day: str, *, before_open_ms: int | None = None, with_data: bool = False) -> list[dict[str, Any]]:
        """Entradas del dia en orden (por apertura y tipo). `before_open_ms`: solo sesiones anteriores o esta."""
        sql = f"SELECT {_COLS} FROM session_journal WHERE day=?"
        params: list[Any] = [day]
        if before_open_ms is not None:
            sql += " AND open_ms <= ?"
            params.append(before_open_ms)
        sql += " ORDER BY open_ms, CASE kind WHEN 'code' THEN 0 ELSE 1 END"
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [self._row(r, with_data) for r in rows]

    def days(self, limit: int = 30) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT day, COUNT(*), SUM(kind='code'), SUM(kind='llm' AND status='con_llm'), MAX(trigger_ms) "
                "FROM session_journal GROUP BY day ORDER BY day DESC LIMIT ?", (limit,)).fetchall()
        return [{"day": d, "entries": n, "sessions": int(c or 0), "with_llm": int(llm or 0), "last_trigger_ms": t}
                for d, n, c, llm, t in rows]

    def close(self) -> None:
        self._conn.close()
