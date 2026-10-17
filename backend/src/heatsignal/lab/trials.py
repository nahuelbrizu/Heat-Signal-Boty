"""
Registro APPEND-ONLY de intentos del laboratorio (plan-laboratorio.md, punto 5).

Cada corrida de backtest (cada combinacion de spec + parametros + ventana) queda registrada, incluidos
los fracasos: el conteo K por estrategia y total alimenta el Deflated Sharpe Ratio, y los Sharpe de los
intentos dan la varianza V[SR] del DSR. Igual que ``evaluation/strategy_registry.py``, el append-only se
fuerza en la base: triggers de SQLite bloquean UPDATE y DELETE.

El holdout se abre UNA sola vez por ``strategy_id``: un indice unico parcial sobre
``(strategy_id) WHERE split = 'holdout'`` impide registrar una segunda corrida de holdout.
"""

from __future__ import annotations

import json
import math
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

WINDOWS = ("train", "test", "holdout")


class HoldoutAlreadyOpenedError(Exception):
    """Ya hay una corrida de holdout registrada para ese strategy_id (el holdout se abre una vez)."""


@dataclass(frozen=True)
class Trial:
    id: int
    strategy_id: str
    spec_hash: str
    params: dict[str, Any]
    window: str
    window_start: str | None
    window_end: str | None
    symbols: list[str]
    metrics: dict[str, Any]
    created_at: datetime


def _json_default(obj: Any) -> Any:
    if hasattr(obj, "isoformat"):
        return obj.isoformat()
    if hasattr(obj, "item"):  # escalares numpy
        return obj.item()
    raise TypeError(f"no serializable: {type(obj).__name__}")


def _dumps(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, default=_json_default)


class TrialRegistry:
    """Persistencia append-only de intentos sobre SQLite."""

    def __init__(self, db_path: str | Path) -> None:
        self._conn = sqlite3.connect(str(db_path))
        self._ensure_schema()

    def _ensure_schema(self) -> None:
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS lab_trials (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                strategy_id  TEXT NOT NULL,
                spec_hash    TEXT NOT NULL,
                params       TEXT NOT NULL,
                split        TEXT NOT NULL CHECK (split IN ('train', 'test', 'holdout')),
                window_start TEXT,
                window_end   TEXT,
                symbols      TEXT NOT NULL,
                metrics      TEXT NOT NULL,
                created_at   TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_lab_trials_strategy ON lab_trials (strategy_id);

            CREATE UNIQUE INDEX IF NOT EXISTS uq_lab_trials_holdout_once
            ON lab_trials (strategy_id) WHERE split = 'holdout';

            CREATE TRIGGER IF NOT EXISTS trg_lab_trials_no_update
            BEFORE UPDATE ON lab_trials
            BEGIN
                SELECT RAISE(ABORT, 'lab_trials es append-only: UPDATE no permitido');
            END;

            CREATE TRIGGER IF NOT EXISTS trg_lab_trials_no_delete
            BEFORE DELETE ON lab_trials
            BEGIN
                SELECT RAISE(ABORT, 'lab_trials es append-only: DELETE no permitido');
            END;
            """
        )
        self._conn.commit()

    def register(
        self,
        strategy_id: str,
        spec_hash: str,
        params: dict[str, Any],
        window: str,
        symbols: list[str],
        metrics: dict[str, Any],
        window_start: datetime | str | None = None,
        window_end: datetime | str | None = None,
        created_at: datetime | None = None,
    ) -> int:
        """Registra un intento y devuelve su id. Los NaN/inf de las metricas se guardan como null."""
        if window not in WINDOWS:
            raise ValueError(f"window tiene que ser una de {WINDOWS}, no {window!r}")
        clean_metrics = {
            k: (None if isinstance(v, float) and not math.isfinite(v) else v) for k, v in metrics.items()
        }
        ws = window_start.isoformat() if hasattr(window_start, "isoformat") else window_start
        we = window_end.isoformat() if hasattr(window_end, "isoformat") else window_end
        when = created_at or datetime.now(timezone.utc)
        try:
            cur = self._conn.execute(
                """
                INSERT INTO lab_trials
                    (strategy_id, spec_hash, params, split, window_start, window_end,
                     symbols, metrics, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    strategy_id,
                    spec_hash,
                    _dumps(params),
                    window,
                    ws,
                    we,
                    _dumps(list(symbols)),
                    _dumps(clean_metrics),
                    when.isoformat(),
                ),
            )
        except sqlite3.IntegrityError as exc:
            self._conn.rollback()
            if window == "holdout":
                raise HoldoutAlreadyOpenedError(
                    f"el holdout de {strategy_id} ya se abrio; no se puede volver a correr"
                ) from exc
            raise
        self._conn.commit()
        return int(cur.lastrowid)

    def count(self, strategy_id: str | None = None, window: str | None = None) -> int:
        """K: cantidad de intentos de una estrategia (o de todas si ``strategy_id`` es None)."""
        sql, args = self._where(strategy_id, window)
        return int(self._conn.execute(f"SELECT COUNT(*) FROM lab_trials{sql}", args).fetchone()[0])

    def count_by_strategy(self, window: str | None = None) -> dict[str, int]:
        sql, args = self._where(None, window)
        rows = self._conn.execute(
            f"SELECT strategy_id, COUNT(*) FROM lab_trials{sql} GROUP BY strategy_id", args
        ).fetchall()
        return {r[0]: int(r[1]) for r in rows}

    def sharpes(
        self, strategy_id: str | None = None, window: str | None = None, metric: str = "sharpe"
    ) -> list[float]:
        """Sharpe (``metrics[metric]``) de cada intento, para la varianza V[SR] del DSR.

        Los intentos sin esa metrica (o con null) se omiten pero SIGUEN contando en ``count``.
        """
        out = []
        for t in self.trials(strategy_id, window):
            v = t.metrics.get(metric)
            if v is not None:
                out.append(float(v))
        return out

    def trials(self, strategy_id: str | None = None, window: str | None = None) -> list[Trial]:
        sql, args = self._where(strategy_id, window)
        rows = self._conn.execute(
            "SELECT id, strategy_id, spec_hash, params, split, window_start, window_end, symbols, "
            f"metrics, created_at FROM lab_trials{sql} ORDER BY id ASC",
            args,
        ).fetchall()
        return [
            Trial(
                id=r[0],
                strategy_id=r[1],
                spec_hash=r[2],
                params=json.loads(r[3]),
                window=r[4],
                window_start=r[5],
                window_end=r[6],
                symbols=json.loads(r[7]),
                metrics=json.loads(r[8]),
                created_at=datetime.fromisoformat(r[9]),
            )
            for r in rows
        ]

    @staticmethod
    def _where(strategy_id: str | None, window: str | None) -> tuple[str, tuple]:
        conds, args = [], []
        if strategy_id is not None:
            conds.append("strategy_id = ?")
            args.append(strategy_id)
        if window is not None:
            conds.append("split = ?")
            args.append(window)
        return ((" WHERE " + " AND ".join(conds)) if conds else ""), tuple(args)

    def close(self) -> None:
        self._conn.close()
