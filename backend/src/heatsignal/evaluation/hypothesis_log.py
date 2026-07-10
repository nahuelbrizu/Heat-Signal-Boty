"""
Log de resultados de evaluación, append-only (anti-p-hacking).

Cada llamada a `log_result` agrega una fila nueva — nunca se actualiza ni se
borra un resultado anterior, ni siquiera si la misma hipótesis se re-evalúa
con más datos más adelante. Es deliberado: el historial completo, incluidas
las corridas fallidas, es lo que hace auditable el ranking final. Podar
resultados es exactamente el mecanismo de p-hacking que este archivo existe
para impedir (ver CLAUDE.md, "Anti-p-hacking" y `agents.md`, "las pendientes
nunca se podan").
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID

from pydantic import BaseModel

from .evaluator import EvalResult
from .strategy_registry import StrategyHypothesis


class HypothesisLogEntry(BaseModel):
    """Una fila del log, ya hidratada."""

    strategy_id: UUID
    registered_at: datetime
    evaluated_at: datetime
    result: dict
    passed: bool
    n_signals: int
    p_value: float
    sharpe: float


class HypothesisLog:
    """Persistencia append-only de resultados de evaluación sobre SQLite."""

    def __init__(self, db_path: str | Path) -> None:
        self._conn = sqlite3.connect(str(db_path))
        self._ensure_schema()

    def _ensure_schema(self) -> None:
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS hypothesis_results (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                strategy_id   TEXT NOT NULL,
                registered_at TEXT NOT NULL,
                evaluated_at  TEXT NOT NULL,
                result        TEXT NOT NULL,
                passed        INTEGER NOT NULL,
                n_signals     INTEGER NOT NULL,
                p_value       REAL NOT NULL,
                sharpe        REAL NOT NULL
            );

            CREATE TRIGGER IF NOT EXISTS trg_hypothesis_results_no_update
            BEFORE UPDATE ON hypothesis_results
            BEGIN
                SELECT RAISE(ABORT, 'hypothesis_results es append-only: UPDATE no permitido');
            END;

            CREATE TRIGGER IF NOT EXISTS trg_hypothesis_results_no_delete
            BEFORE DELETE ON hypothesis_results
            BEGIN
                SELECT RAISE(ABORT, 'hypothesis_results es append-only: DELETE no permitido');
            END;
            """
        )
        self._conn.commit()

    def log_result(self, hypothesis: StrategyHypothesis, eval_result: EvalResult) -> None:
        """Agrega el resultado de evaluar `hypothesis`. Nunca modifica filas previas.

        Se loguea SIEMPRE, pase o no pase el filtro de aprobación: una
        hipótesis fallida es tan valiosa para el historial como una exitosa.
        """
        now = datetime.now(timezone.utc)
        self._conn.execute(
            """
            INSERT INTO hypothesis_results
                (strategy_id, registered_at, evaluated_at, result, passed, n_signals, p_value, sharpe)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(hypothesis.strategy_id),
                hypothesis.registered_at.isoformat(),
                now.isoformat(),
                eval_result.model_dump_json(),
                int(eval_result.passed),
                eval_result.n_signals,
                eval_result.p_value,
                eval_result.sharpe_ratio,
            ),
        )
        self._conn.commit()

    def get_history(self) -> list[HypothesisLogEntry]:
        """Todas las evaluaciones logueadas, ordenadas por registered_at ascendente."""
        rows = self._conn.execute(
            "SELECT strategy_id, registered_at, evaluated_at, result, passed, n_signals, p_value, sharpe "
            "FROM hypothesis_results ORDER BY registered_at ASC"
        ).fetchall()
        return [self._hydrate(row) for row in rows]

    def get_passed(self) -> list[HypothesisLogEntry]:
        """Solo las evaluaciones que pasaron el criterio de aprobación."""
        rows = self._conn.execute(
            "SELECT strategy_id, registered_at, evaluated_at, result, passed, n_signals, p_value, sharpe "
            "FROM hypothesis_results WHERE passed = 1 ORDER BY registered_at ASC"
        ).fetchall()
        return [self._hydrate(row) for row in rows]

    @staticmethod
    def _hydrate(row: tuple) -> HypothesisLogEntry:
        strategy_id, registered_at, evaluated_at, result, passed, n_signals, p_value, sharpe = row
        return HypothesisLogEntry(
            strategy_id=UUID(strategy_id),
            registered_at=datetime.fromisoformat(registered_at),
            evaluated_at=datetime.fromisoformat(evaluated_at),
            result=json.loads(result),
            passed=bool(passed),
            n_signals=n_signals,
            p_value=p_value,
            sharpe=sharpe,
        )

    def close(self) -> None:
        self._conn.close()
