"""
Store append-only de hipotesis pendientes y sus resoluciones (Fase 3).

Mismo estilo anti-p-hacking que `strategy_registry.py` / `hypothesis_log.py`:
triggers de SQLite bloquean UPDATE/DELETE a nivel de motor sobre las dos tablas,
no es una convencion de la capa de aplicacion.

Modelo:
    - `pending_hypotheses`: una fila por `PendingHypothesis` emitida por el grafo.
      Nunca se modifica ni se borra (las pendientes nunca se podan).
    - `hypothesis_resolutions`: una fila NUEVA por resolucion. `hypothesis_id` es
      UNIQUE: una hipotesis se resuelve como maximo una vez. Re-resolver (p.ej. con
      un precio "mas conveniente") es exactamente el p-hacking que esto impide.

El estado "actual" de una hipotesis se deriva: sin fila en resoluciones -> pending;
con fila -> el `status` de esa fila (`resolved` o `expired_no_data`).

Rechaza hipotesis cuyo `strategy_id` no este pre-registrado en `StrategyRegistry`
(chequeo explicito via el registry inyectado: el registry puede vivir en otra base,
asi que una FK de SQLite no alcanza).

Idempotencia por corrida: si `run_id` no es None, `add(h)` es idempotente sobre
(run_id, symbol, strategy_id). El Sintetizador del grafo puede re-ejecutarse al
reanudar desde un checkpoint despues de haber llamado a `add()`, y emitiria la
"misma" hipotesis con un `hypothesis_id` nuevo. Ese duplicado es un no-op logueado
(no un error, no una segunda fila): contarla dos veces inflaria n y sesgaria el
win-rate. Lo garantiza tambien un indice UNIQUE parcial a nivel de motor. Con
`run_id=None` no hay clave de corrida y cada `add` es una hipotesis nueva.

Todas las fechas deben ser timezone-aware; se persisten en UTC (ISO) y ademas como
epoch en segundos para poder consultar "vencidas" con aritmetica simple.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Protocol
from uuid import UUID

from pydantic import BaseModel

from ..schema.hypotheses import HypothesisStatus, PendingHypothesis, Resolution
from ..schema.signals import Direction
from .strategy_registry import UnknownStrategyError

logger = logging.getLogger(__name__)


class StrategyLookup(Protocol):
    """Lo minimo que el store necesita del `StrategyRegistry`."""

    def get(self, strategy_id: UUID) -> Any | None: ...


class HypothesisSink(Protocol):
    """Protocolo que consume el grafo LangGraph para emitir hipotesis."""

    def add(self, h: PendingHypothesis) -> None: ...


class DuplicateHypothesisError(Exception):
    """Se intento agregar un `hypothesis_id` que ya existe."""


class UnknownHypothesisError(Exception):
    """Se intento resolver un `hypothesis_id` que nunca fue agregado."""


class HypothesisAlreadyResolvedError(Exception):
    """La hipotesis ya tiene una resolucion. Se resuelve como maximo una vez."""


class ResolutionMetrics(BaseModel):
    """Detalle de una resolucion `resolved`, mas alla del contrato `Resolution`.

    Todo en puntos porcentuales (2.5 = +2.5%).
    - gross_pnl_pct: retorno de la direccion elegida, sin costos.
    - cost_bps: costo round-trip descontado (bps sobre el notional de entrada).
    - benchmark_pnl_pct: retorno buy-and-hold (long pasivo) en la misma ventana.
    - alpha_pct: pnl_pct neto - benchmark_pnl_pct.
    - exit_price_ts: momento de la observacion de precio usada como salida.
    """

    gross_pnl_pct: float
    cost_bps: float
    benchmark_pnl_pct: float
    alpha_pct: float
    exit_price_ts: datetime | None = None


class HypothesisRecord(BaseModel):
    """Una hipotesis hidratada con su resolucion (si la tiene)."""

    hypothesis: PendingHypothesis
    resolution: Resolution | None = None
    metrics: ResolutionMetrics | None = None

    @property
    def status(self) -> HypothesisStatus:
        return HypothesisStatus.PENDING if self.resolution is None else self.resolution.status

    @property
    def due_at(self) -> datetime:
        return due_at(self.hypothesis)


def _require_aware(dt: datetime, field: str) -> datetime:
    if dt.tzinfo is None or dt.tzinfo.utcoffset(dt) is None:
        raise ValueError(f"{field} debe ser timezone-aware, recibido naive: {dt!r}")
    return dt.astimezone(timezone.utc)


def due_at(h: PendingHypothesis) -> datetime:
    """Momento en que vence la ventana de la hipotesis (created_at + horizon_s)."""
    return _require_aware(h.created_at, "created_at") + timedelta(seconds=h.horizon_s)


_HYP_COLUMNS = (
    "hypothesis_id, strategy_id, symbol, created_at, created_ts, due_ts, direction, "
    "confidence_score, entry_reference_price, horizon_s, atr_normalized_trigger, rationale, "
    "heatmap_model_id, run_id"
)
_RES_COLUMNS = (
    "r.status, r.resolved_at, r.exit_price, r.pnl_pct, r.win, r.gross_pnl_pct, r.cost_bps, "
    "r.benchmark_pnl_pct, r.alpha_pct, r.exit_price_ts"
)


class PendingHypothesisStore:
    """Persistencia append-only de `PendingHypothesis` + `Resolution` sobre SQLite."""

    def __init__(self, db_path: str | Path, registry: StrategyLookup) -> None:
        self._registry = registry
        # check_same_thread=False + lock: el grafo puede llamar add() desde otro hilo.
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._lock = threading.Lock()
        self._ensure_schema()

    def _ensure_schema(self) -> None:
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS pending_hypotheses (
                hypothesis_id          TEXT PRIMARY KEY,
                strategy_id            TEXT NOT NULL,
                symbol                 TEXT NOT NULL,
                created_at             TEXT NOT NULL,
                created_ts             REAL NOT NULL,
                due_ts                 REAL NOT NULL,
                direction              TEXT NOT NULL CHECK (direction IN ('LONG', 'SHORT')),
                confidence_score       REAL NOT NULL CHECK (confidence_score BETWEEN 0 AND 1),
                entry_reference_price  TEXT NOT NULL,
                horizon_s              INTEGER NOT NULL CHECK (horizon_s > 0),
                atr_normalized_trigger REAL,
                rationale              TEXT NOT NULL,
                heatmap_model_id       TEXT,
                run_id                 TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_pending_hypotheses_strategy
                ON pending_hypotheses (strategy_id, created_ts);
            CREATE INDEX IF NOT EXISTS idx_pending_hypotheses_due
                ON pending_hypotheses (due_ts);

            -- Idempotencia por corrida del grafo (ver docstring del modulo).
            CREATE UNIQUE INDEX IF NOT EXISTS uq_pending_hypotheses_run_key
                ON pending_hypotheses (run_id, symbol, strategy_id)
                WHERE run_id IS NOT NULL;

            CREATE TRIGGER IF NOT EXISTS trg_pending_hypotheses_no_update
            BEFORE UPDATE ON pending_hypotheses
            BEGIN
                SELECT RAISE(ABORT, 'pending_hypotheses es append-only: UPDATE no permitido');
            END;

            CREATE TRIGGER IF NOT EXISTS trg_pending_hypotheses_no_delete
            BEFORE DELETE ON pending_hypotheses
            BEGIN
                SELECT RAISE(ABORT, 'pending_hypotheses es append-only: DELETE no permitido');
            END;

            CREATE TABLE IF NOT EXISTS hypothesis_resolutions (
                id                INTEGER PRIMARY KEY AUTOINCREMENT,
                hypothesis_id     TEXT NOT NULL UNIQUE,
                status            TEXT NOT NULL CHECK (status IN ('resolved', 'expired_no_data')),
                resolved_at       TEXT NOT NULL,
                exit_price        TEXT,
                pnl_pct           REAL,
                win               INTEGER,
                gross_pnl_pct     REAL,
                cost_bps          REAL,
                benchmark_pnl_pct REAL,
                alpha_pct         REAL,
                exit_price_ts     TEXT,
                CHECK (
                    (status = 'resolved'
                        AND exit_price IS NOT NULL AND pnl_pct IS NOT NULL AND win IS NOT NULL)
                    OR
                    (status = 'expired_no_data'
                        AND exit_price IS NULL AND pnl_pct IS NULL AND win IS NULL)
                ),
                FOREIGN KEY (hypothesis_id) REFERENCES pending_hypotheses (hypothesis_id)
            );

            CREATE TRIGGER IF NOT EXISTS trg_hypothesis_resolutions_no_update
            BEFORE UPDATE ON hypothesis_resolutions
            BEGIN
                SELECT RAISE(ABORT, 'hypothesis_resolutions es append-only: UPDATE no permitido');
            END;

            CREATE TRIGGER IF NOT EXISTS trg_hypothesis_resolutions_no_delete
            BEFORE DELETE ON hypothesis_resolutions
            BEGIN
                SELECT RAISE(ABORT, 'hypothesis_resolutions es append-only: DELETE no permitido');
            END;
            """
        )
        self._conn.commit()

    # ------------------------------------------------------------------ escritura

    def add(self, h: PendingHypothesis) -> None:
        """Agrega una hipotesis pendiente. Rechaza estrategias no pre-registradas.

        Idempotente sobre (run_id, symbol, strategy_id) si `run_id` no es None: un
        duplicado de la misma corrida es un no-op logueado (WARNING). Un
        `hypothesis_id` repetido sin esa clave sigue siendo `DuplicateHypothesisError`.
        """
        if self._registry.get(h.strategy_id) is None:
            raise UnknownStrategyError(
                f"strategy_id {h.strategy_id} no esta pre-registrado: el Estratega no inventa estrategias"
            )
        created = _require_aware(h.created_at, "created_at")
        if not h.entry_reference_price.is_finite() or h.entry_reference_price <= 0:
            raise ValueError(
                f"entry_reference_price debe ser > 0 y finito, recibido {h.entry_reference_price}"
            )
        due = created + timedelta(seconds=h.horizon_s)
        with self._lock:
            if self._run_key_exists(h):
                return
            try:
                self._conn.execute(
                    f"INSERT INTO pending_hypotheses ({_HYP_COLUMNS}) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        str(h.hypothesis_id),
                        str(h.strategy_id),
                        h.symbol,
                        created.isoformat(),
                        created.timestamp(),
                        due.timestamp(),
                        h.direction.value,
                        h.confidence_score,
                        str(h.entry_reference_price),
                        h.horizon_s,
                        h.atr_normalized_trigger,
                        h.rationale,
                        h.heatmap_model_id,
                        h.run_id,
                    ),
                )
                self._conn.commit()
            except sqlite3.IntegrityError as exc:
                self._conn.rollback()
                # Carrera con otro proceso que inserto la misma clave de corrida.
                if self._run_key_exists(h):
                    return
                raise DuplicateHypothesisError(
                    f"hypothesis_id {h.hypothesis_id} ya existe"
                ) from exc

    def _run_key_exists(self, h: PendingHypothesis) -> bool:
        """True (y loguea) si ya hay una hipotesis con la misma (run_id, symbol, strategy_id)."""
        if h.run_id is None:
            return False
        row = self._conn.execute(
            "SELECT hypothesis_id FROM pending_hypotheses "
            "WHERE run_id = ? AND symbol = ? AND strategy_id = ?",
            (h.run_id, h.symbol, str(h.strategy_id)),
        ).fetchone()
        if row is None:
            return False
        logger.warning(
            "add() duplicado ignorado: run_id=%s symbol=%s strategy_id=%s ya registrado como "
            "hypothesis_id=%s (descartado hypothesis_id=%s)",
            h.run_id,
            h.symbol,
            h.strategy_id,
            row[0],
            h.hypothesis_id,
        )
        return True

    def resolve(self, resolution: Resolution, metrics: ResolutionMetrics | None = None) -> None:
        """Agrega la resolucion de una hipotesis. Como maximo una por hipotesis.

        Valida coherencia: `resolved` exige exit_price/pnl_pct/win (y `win` debe ser
        `pnl_pct > 0`); `expired_no_data` exige que los tres sean None. No se puede
        resolver antes de que venza la ventana (`resolved_at >= due_at`).
        """
        status = resolution.status
        if status == HypothesisStatus.PENDING:
            raise ValueError("una Resolution no puede tener status=pending")
        resolved_at = _require_aware(resolution.resolved_at, "resolved_at")
        if status == HypothesisStatus.RESOLVED:
            if resolution.exit_price is None or resolution.pnl_pct is None or resolution.win is None:
                raise ValueError("status=resolved exige exit_price, pnl_pct y win")
            if resolution.win != (resolution.pnl_pct > 0):
                raise ValueError(
                    f"win={resolution.win} incoherente con pnl_pct={resolution.pnl_pct} (win = pnl_pct > 0)"
                )
        else:
            if not (resolution.exit_price is None and resolution.pnl_pct is None and resolution.win is None):
                raise ValueError("status=expired_no_data exige exit_price, pnl_pct y win en None")
            if metrics is not None:
                raise ValueError("status=expired_no_data no lleva metricas de PnL")

        with self._lock:
            row = self._conn.execute(
                "SELECT due_ts FROM pending_hypotheses WHERE hypothesis_id = ?",
                (str(resolution.hypothesis_id),),
            ).fetchone()
            if row is None:
                raise UnknownHypothesisError(f"hypothesis_id {resolution.hypothesis_id} no existe")
            if resolved_at.timestamp() < row[0]:
                raise ValueError(
                    f"hypothesis {resolution.hypothesis_id} todavia no vencio: no se puede resolver antes de due_at"
                )
            exit_ts = (
                _require_aware(metrics.exit_price_ts, "exit_price_ts").isoformat()
                if metrics is not None and metrics.exit_price_ts is not None
                else None
            )
            try:
                self._conn.execute(
                    """
                    INSERT INTO hypothesis_resolutions
                        (hypothesis_id, status, resolved_at, exit_price, pnl_pct, win,
                         gross_pnl_pct, cost_bps, benchmark_pnl_pct, alpha_pct, exit_price_ts)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        str(resolution.hypothesis_id),
                        status.value,
                        resolved_at.isoformat(),
                        None if resolution.exit_price is None else str(resolution.exit_price),
                        resolution.pnl_pct,
                        None if resolution.win is None else int(resolution.win),
                        None if metrics is None else metrics.gross_pnl_pct,
                        None if metrics is None else metrics.cost_bps,
                        None if metrics is None else metrics.benchmark_pnl_pct,
                        None if metrics is None else metrics.alpha_pct,
                        exit_ts,
                    ),
                )
                self._conn.commit()
            except sqlite3.IntegrityError as exc:
                self._conn.rollback()
                raise HypothesisAlreadyResolvedError(
                    f"hypothesis_id {resolution.hypothesis_id} ya fue resuelta"
                ) from exc

    # ------------------------------------------------------------------ lectura

    def due(self, now: datetime) -> list[PendingHypothesis]:
        """Pendientes cuya ventana ya vencio (due_at <= now) y sin resolucion, por due_at."""
        now_ts = _require_aware(now, "now").timestamp()
        with self._lock:
            rows = self._conn.execute(
                f"SELECT {_HYP_COLUMNS} FROM pending_hypotheses p "
                "WHERE p.due_ts <= ? AND NOT EXISTS "
                "(SELECT 1 FROM hypothesis_resolutions r WHERE r.hypothesis_id = p.hypothesis_id) "
                "ORDER BY p.due_ts ASC, p.hypothesis_id ASC",
                (now_ts,),
            ).fetchall()
        return [self._hydrate_hypothesis(row) for row in rows]

    def list_by_strategy(self, strategy_id: UUID) -> list[HypothesisRecord]:
        """Todas las hipotesis de una estrategia (con su resolucion), por created_at."""
        with self._lock:
            rows = self._conn.execute(
                f"SELECT {', '.join('p.' + c.strip() for c in _HYP_COLUMNS.split(','))}, {_RES_COLUMNS} "
                "FROM pending_hypotheses p "
                "LEFT JOIN hypothesis_resolutions r ON r.hypothesis_id = p.hypothesis_id "
                "WHERE p.strategy_id = ? ORDER BY p.created_ts ASC, p.hypothesis_id ASC",
                (str(strategy_id),),
            ).fetchall()
        return [self._hydrate_record(row) for row in rows]

    def strategy_ids(self) -> list[UUID]:
        """Estrategias con al menos una hipotesis, en orden de primera aparicion."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT strategy_id, MIN(created_ts) AS first_ts FROM pending_hypotheses "
                "GROUP BY strategy_id ORDER BY first_ts ASC"
            ).fetchall()
        return [UUID(r[0]) for r in rows]

    def open_positions(self) -> set[tuple[str, str]]:
        """Pares (strategy_id, symbol) con al menos una hipotesis sin resolucion.

        El Sintetizador no abre otra hipotesis de la misma estrategia en el mismo simbolo mientras
        haya una abierta: hipotesis solapadas sobre el mismo movimiento inflarian n y el intervalo
        de confianza (decisions.md, "Una hipotesis abierta por estrategia y simbolo").
        """
        with self._lock:
            rows = self._conn.execute(
                "SELECT DISTINCT p.strategy_id, p.symbol FROM pending_hypotheses p WHERE NOT EXISTS "
                "(SELECT 1 FROM hypothesis_resolutions r WHERE r.hypothesis_id = p.hypothesis_id)"
            ).fetchall()
        return {(str(sid).lower(), sym) for sid, sym in rows}

    def pending_count(self) -> int:
        """Cantidad de hipotesis sin resolucion (vencidas o no)."""
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) FROM pending_hypotheses p WHERE NOT EXISTS "
                "(SELECT 1 FROM hypothesis_resolutions r WHERE r.hypothesis_id = p.hypothesis_id)"
            ).fetchone()
        return int(row[0])

    # ------------------------------------------------------------------ hidratacion

    @staticmethod
    def _hydrate_hypothesis(row: tuple) -> PendingHypothesis:
        (
            hypothesis_id,
            strategy_id,
            symbol,
            created_at,
            _created_ts,
            _due_ts,
            direction,
            confidence_score,
            entry_reference_price,
            horizon_s,
            atr_normalized_trigger,
            rationale,
            heatmap_model_id,
            run_id,
        ) = row
        return PendingHypothesis(
            hypothesis_id=UUID(hypothesis_id),
            strategy_id=UUID(strategy_id),
            symbol=symbol,
            created_at=datetime.fromisoformat(created_at),
            direction=Direction(direction),
            confidence_score=confidence_score,
            entry_reference_price=Decimal(entry_reference_price),
            horizon_s=horizon_s,
            atr_normalized_trigger=atr_normalized_trigger,
            rationale=rationale,
            heatmap_model_id=heatmap_model_id,
            run_id=run_id,
        )

    @classmethod
    def _hydrate_record(cls, row: tuple) -> HypothesisRecord:
        hypothesis = cls._hydrate_hypothesis(row[:14])
        (
            status,
            resolved_at,
            exit_price,
            pnl_pct,
            win,
            gross_pnl_pct,
            cost_bps,
            benchmark_pnl_pct,
            alpha_pct,
            exit_price_ts,
        ) = row[14:]
        if status is None:
            return HypothesisRecord(hypothesis=hypothesis)
        resolution = Resolution(
            hypothesis_id=hypothesis.hypothesis_id,
            status=HypothesisStatus(status),
            resolved_at=datetime.fromisoformat(resolved_at),
            exit_price=None if exit_price is None else Decimal(exit_price),
            pnl_pct=pnl_pct,
            win=None if win is None else bool(win),
        )
        metrics = None
        if gross_pnl_pct is not None:
            metrics = ResolutionMetrics(
                gross_pnl_pct=gross_pnl_pct,
                cost_bps=cost_bps,
                benchmark_pnl_pct=benchmark_pnl_pct,
                alpha_pct=alpha_pct,
                exit_price_ts=None if exit_price_ts is None else datetime.fromisoformat(exit_price_ts),
            )
        return HypothesisRecord(hypothesis=hypothesis, resolution=resolution, metrics=metrics)

    def close(self) -> None:
        self._conn.close()
