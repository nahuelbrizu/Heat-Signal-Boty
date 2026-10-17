"""
Pre-registro de hipótesis de estrategia (Bloqueante C, anti-p-hacking).

Toda estrategia debe registrarse ACÁ, con sus condiciones de entrada/salida
congeladas, ANTES de que el Evaluador vea un solo resultado (ver CLAUDE.md,
principio "Anti-p-hacking"). El registro es append-only a nivel de base de
datos: triggers de SQLite bloquean físicamente UPDATE/DELETE sobre
`strategy_hypotheses`, no es solo una convención de la capa de aplicación.

Los cambios de estado (pending -> running -> evaluated) NO se modelan como un
UPDATE de la fila original — eso violaría el append-only y perdería el
historial de transiciones. Se modelan como eventos nuevos en la tabla
`strategy_status_events`; el estado "actual" de una hipótesis es simplemente
el último evento de esa tabla para su `strategy_id`.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from pydantic import BaseModel, Field


class StrategyStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    EVALUATED = "evaluated"


class StrategyHypothesis(BaseModel):
    """Una hipótesis de estrategia pre-registrada. Inmutable una vez guardada."""

    strategy_id: UUID = Field(default_factory=uuid4)
    description: str
    entry_conditions: dict[str, Any]
    exit_conditions: dict[str, Any]
    registered_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    author: str
    status: StrategyStatus = StrategyStatus.PENDING


class StrategyAlreadyRegisteredError(Exception):
    """Se intentó re-registrar un strategy_id existente. El pre-registro es inmutable."""


class UnknownStrategyError(Exception):
    """Se intentó operar sobre un strategy_id que no fue pre-registrado."""


class StrategyRegistry:
    """Persistencia append-only de hipótesis de estrategia sobre SQLite."""

    def __init__(self, db_path: str | Path) -> None:
        self._conn = sqlite3.connect(str(db_path))
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._ensure_schema()

    def _ensure_schema(self) -> None:
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS strategy_hypotheses (
                strategy_id      TEXT PRIMARY KEY,
                description      TEXT NOT NULL,
                entry_conditions TEXT NOT NULL,
                exit_conditions  TEXT NOT NULL,
                registered_at    TEXT NOT NULL,
                author           TEXT NOT NULL
            );

            CREATE TRIGGER IF NOT EXISTS trg_strategy_hypotheses_no_update
            BEFORE UPDATE ON strategy_hypotheses
            BEGIN
                SELECT RAISE(ABORT, 'strategy_hypotheses es append-only: UPDATE no permitido');
            END;

            CREATE TRIGGER IF NOT EXISTS trg_strategy_hypotheses_no_delete
            BEFORE DELETE ON strategy_hypotheses
            BEGIN
                SELECT RAISE(ABORT, 'strategy_hypotheses es append-only: DELETE no permitido');
            END;

            CREATE TABLE IF NOT EXISTS strategy_status_events (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                strategy_id TEXT NOT NULL,
                status      TEXT NOT NULL,
                changed_at  TEXT NOT NULL,
                FOREIGN KEY (strategy_id) REFERENCES strategy_hypotheses (strategy_id)
            );

            CREATE TRIGGER IF NOT EXISTS trg_strategy_status_events_no_update
            BEFORE UPDATE ON strategy_status_events
            BEGIN
                SELECT RAISE(ABORT, 'strategy_status_events es append-only: UPDATE no permitido');
            END;

            CREATE TRIGGER IF NOT EXISTS trg_strategy_status_events_no_delete
            BEFORE DELETE ON strategy_status_events
            BEGIN
                SELECT RAISE(ABORT, 'strategy_status_events es append-only: DELETE no permitido');
            END;
            """
        )
        self._conn.commit()

    def register(self, hypothesis: StrategyHypothesis) -> None:
        """Pre-registra una hipótesis. Falla si el strategy_id ya existe."""
        try:
            self._conn.execute(
                """
                INSERT INTO strategy_hypotheses
                    (strategy_id, description, entry_conditions, exit_conditions, registered_at, author)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    str(hypothesis.strategy_id),
                    hypothesis.description,
                    json.dumps(hypothesis.entry_conditions),
                    json.dumps(hypothesis.exit_conditions),
                    hypothesis.registered_at.isoformat(),
                    hypothesis.author,
                ),
            )
        except sqlite3.IntegrityError as exc:
            self._conn.rollback()
            raise StrategyAlreadyRegisteredError(
                f"strategy_id {hypothesis.strategy_id} ya está registrado"
            ) from exc
        self._append_status_event(hypothesis.strategy_id, hypothesis.status, hypothesis.registered_at)
        self._conn.commit()

    def set_status(self, strategy_id: UUID, status: StrategyStatus) -> None:
        """Registra una transición de estado como evento nuevo (nunca UPDATE)."""
        try:
            self._append_status_event(strategy_id, status, datetime.now(timezone.utc))
            self._conn.commit()
        except sqlite3.IntegrityError as exc:
            self._conn.rollback()
            raise UnknownStrategyError(
                f"strategy_id {strategy_id} no está pre-registrado"
            ) from exc

    def _append_status_event(self, strategy_id: UUID, status: StrategyStatus, when: datetime) -> None:
        self._conn.execute(
            "INSERT INTO strategy_status_events (strategy_id, status, changed_at) VALUES (?, ?, ?)",
            (str(strategy_id), status.value, when.isoformat()),
        )

    def get(self, strategy_id: UUID) -> StrategyHypothesis | None:
        row = self._conn.execute(
            "SELECT strategy_id, description, entry_conditions, exit_conditions, registered_at, author "
            "FROM strategy_hypotheses WHERE strategy_id = ?",
            (str(strategy_id),),
        ).fetchone()
        if row is None:
            return None
        return self._hydrate(row, self._current_status(strategy_id))

    def get_all(self) -> list[StrategyHypothesis]:
        rows = self._conn.execute(
            "SELECT strategy_id, description, entry_conditions, exit_conditions, registered_at, author "
            "FROM strategy_hypotheses ORDER BY registered_at ASC"
        ).fetchall()
        return [self._hydrate(row, self._current_status(UUID(row[0]))) for row in rows]

    def status_history(self, strategy_id: UUID) -> list[tuple[StrategyStatus, datetime]]:
        """Todas las transiciones de estado de una estrategia, en orden (append-only)."""
        rows = self._conn.execute(
            "SELECT status, changed_at FROM strategy_status_events WHERE strategy_id = ? ORDER BY id ASC",
            (str(strategy_id),),
        ).fetchall()
        return [(StrategyStatus(s), datetime.fromisoformat(t)) for s, t in rows]

    def _current_status(self, strategy_id: UUID) -> StrategyStatus:
        row = self._conn.execute(
            "SELECT status FROM strategy_status_events WHERE strategy_id = ? ORDER BY id DESC LIMIT 1",
            (str(strategy_id),),
        ).fetchone()
        if row is None:
            return StrategyStatus.PENDING
        return StrategyStatus(row[0])

    @staticmethod
    def _hydrate(row: tuple, status: StrategyStatus) -> StrategyHypothesis:
        strategy_id, description, entry_conditions, exit_conditions, registered_at, author = row
        return StrategyHypothesis(
            strategy_id=UUID(strategy_id),
            description=description,
            entry_conditions=json.loads(entry_conditions),
            exit_conditions=json.loads(exit_conditions),
            registered_at=datetime.fromisoformat(registered_at),
            author=author,
            status=status,
        )

    def close(self) -> None:
        self._conn.close()
