"""
Registro APPEND-ONLY de intentos del laboratorio (plan-laboratorio.md, punto 5).

Cada corrida de backtest (cada combinacion de spec + parametros + ventana) queda registrada, incluidos
los fracasos: el conteo K por estrategia y total alimenta el Deflated Sharpe Ratio, y los Sharpe de los
intentos dan la varianza V[SR] del DSR. Igual que ``evaluation/strategy_registry.py``, el append-only se
fuerza en la base: triggers de SQLite bloquean UPDATE y DELETE.

El holdout se abre UNA sola vez por ``strategy_id``: un indice unico parcial sobre
``(strategy_id) WHERE split = 'holdout'`` impide registrar una segunda corrida de holdout.

Estados por intento (regla del liston del programa, decisions.md 2026-10-19)
--------------------------------------------------------------------------
Un intento (= una combinacion ``strategy_id`` + ``params``, la unidad que cuenta K) puede estar:
  - ``valido`` (por defecto: todo intento sin eventos es valido);
  - ``invalido`` (invalido tecnico: un bug, datos rotos; el motivo es obligatorio);
  - ``sin_resultado`` (la corrida no produjo nada medible).
El estado NO se guarda en ``lab_trials`` (que es append-only y no admite UPDATE): cada cambio es un EVENTO nuevo en
la tabla ``lab_trial_status``, tambien append-only (triggers). Vale el ultimo evento que aplica a la combinacion
(``params`` exactos o ``'*'`` = todas las combinaciones de la estrategia). Volver a ``valido`` es otro evento.
Un intento no valido SIGUE contando en K (el conteo real no cambia nunca); solo sale de la dispersion y de la
matriz de retornos del liston del programa (``lab.program``).

    python -m heatsignal.lab.trials marcar <strategy_id> --estado invalido --motivo "bug en ..." [--params '{...}']
    python -m heatsignal.lab.trials estados
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
STATUSES = ("valido", "invalido", "sin_resultado")
DEFAULT_STATUS = "valido"
ALL_PARAMS = "*"  # el evento aplica a todas las combinaciones de la estrategia
# si una estrategia tiene combinaciones en estados distintos, manda el peor
_SEVERITY = {"valido": 0, "sin_resultado": 1, "invalido": 2}


class HoldoutAlreadyOpenedError(Exception):
    """Ya hay una corrida de holdout registrada para ese strategy_id (el holdout se abre una vez)."""


STATUS_SCHEMA = """
            CREATE TABLE IF NOT EXISTS lab_trial_status (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                strategy_id  TEXT NOT NULL,
                params       TEXT NOT NULL,
                status       TEXT NOT NULL CHECK (status IN ('valido', 'invalido', 'sin_resultado')),
                motivo       TEXT NOT NULL CHECK (length(trim(motivo)) > 0),
                created_at   TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_lab_trial_status_strategy ON lab_trial_status (strategy_id);

            CREATE TRIGGER IF NOT EXISTS trg_lab_trial_status_no_update
            BEFORE UPDATE ON lab_trial_status
            BEGIN
                SELECT RAISE(ABORT, 'lab_trial_status es append-only: UPDATE no permitido');
            END;

            CREATE TRIGGER IF NOT EXISTS trg_lab_trial_status_no_delete
            BEFORE DELETE ON lab_trial_status
            BEGIN
                SELECT RAISE(ABORT, 'lab_trial_status es append-only: DELETE no permitido');
            END;
"""


@dataclass(frozen=True)
class StatusEvent:
    id: int
    strategy_id: str
    params: str  # JSON canonico de la combinacion o '*'
    status: str
    motivo: str
    created_at: str


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
        self._conn = sqlite3.connect(str(db_path), timeout=60)  # varias corridas en paralelo
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
            + STATUS_SCHEMA
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

    # ------------------------------------------------------------------------ estados (eventos append-only)

    def combinations(self, strategy_id: str | None = None) -> dict[str, list[str]]:
        """{strategy_id: [params JSON canonico, ...]} de las combinaciones registradas (sin el holdout): la unidad de K."""
        return combinations_from_conn(self._conn, strategy_id)

    def mark(
        self,
        strategy_id: str,
        status: str,
        motivo: str,
        params: dict[str, Any] | str | None = None,
        created_at: datetime | None = None,
    ) -> int:
        """Agrega un evento de estado (nunca modifica filas). ``params`` None = todas las combinaciones (``'*'``).

        El motivo es obligatorio. La estrategia (y la combinacion, si se pasa) tiene que existir en ``lab_trials``:
        un typo no puede crear un estado huerfano."""
        if status not in STATUSES:
            raise ValueError(f"estado tiene que ser uno de {STATUSES}, no {status!r}")
        if not motivo or not str(motivo).strip():
            raise ValueError("el motivo es obligatorio")
        combos = self.combinations(strategy_id).get(strategy_id)
        if not combos:
            raise ValueError(f"{strategy_id!r} no tiene intentos registrados")
        if params is None or params == ALL_PARAMS:
            key = ALL_PARAMS
        else:
            key = params if isinstance(params, str) else _dumps(params)
            key = _dumps(json.loads(key))  # canonico, igual que lab_trials.params
            if key not in combos:
                raise ValueError(f"{strategy_id}: la combinacion {key} no esta registrada; hay {combos}")
        when = created_at or datetime.now(timezone.utc)
        cur = self._conn.execute(
            "INSERT INTO lab_trial_status (strategy_id, params, status, motivo, created_at) VALUES (?, ?, ?, ?, ?)",
            (strategy_id, key, status, str(motivo).strip(), when.isoformat()),
        )
        self._conn.commit()
        return int(cur.lastrowid)

    def status_events(self, strategy_id: str | None = None) -> list[StatusEvent]:
        return status_events_from_conn(self._conn, strategy_id)

    def statuses(self) -> dict[str, dict[str, Any]]:
        """Estado vigente por estrategia (ver ``resolve_statuses``)."""
        return resolve_statuses(self.combinations(), self.status_events())

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


# ---------------------------------------------------------------------------- estados: lectura sin escribir


def combinations_from_conn(conn: sqlite3.Connection, strategy_id: str | None = None) -> dict[str, list[str]]:
    sql = "SELECT DISTINCT strategy_id, params FROM lab_trials WHERE split != 'holdout'"
    args: tuple = ()
    if strategy_id is not None:
        sql += " AND strategy_id = ?"
        args = (strategy_id,)
    out: dict[str, list[str]] = {}
    for sid, params in conn.execute(sql + " ORDER BY strategy_id, params", args).fetchall():
        out.setdefault(sid, []).append(params)
    return out


def status_events_from_conn(conn: sqlite3.Connection, strategy_id: str | None = None) -> list[StatusEvent]:
    """Eventos de estado en orden de alta. Una base anterior a los estados (sin la tabla) = sin eventos."""
    has = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='lab_trial_status'").fetchone()
    if not has:
        return []
    sql = "SELECT id, strategy_id, params, status, motivo, created_at FROM lab_trial_status"
    args: tuple = ()
    if strategy_id is not None:
        sql += " WHERE strategy_id = ?"
        args = (strategy_id,)
    return [StatusEvent(*r) for r in conn.execute(sql + " ORDER BY id ASC", args).fetchall()]


def resolve_statuses(combos: dict[str, list[str]], events: list[StatusEvent]) -> dict[str, dict[str, Any]]:
    """Estado vigente de cada combinacion (ultimo evento que la toca: sus params exactos o '*'; sin eventos =
    valido) y de cada estrategia (el peor de sus combinaciones: invalido > sin_resultado > valido).

    {strategy_id: {"status", "motivo", "n_combinations", "combinations": {params: {"status", "motivo"}}}}"""
    out: dict[str, dict[str, Any]] = {}
    for sid, plist in combos.items():
        per: dict[str, dict[str, Any]] = {p: {"status": DEFAULT_STATUS, "motivo": None} for p in plist}
        for ev in events:  # en orden de id: el ultimo pisa
            if ev.strategy_id != sid:
                continue
            targets = plist if ev.params == ALL_PARAMS else [ev.params] if ev.params in per else []
            for p in targets:
                per[p] = {"status": ev.status, "motivo": ev.motivo}
        worst = max(per.values(), key=lambda x: _SEVERITY[x["status"]])
        out[sid] = {"status": worst["status"], "motivo": worst["motivo"], "n_combinations": len(plist),
                    "combinations": per}
    return out


def read_statuses(db_path: str | Path) -> dict[str, dict[str, Any]]:
    """Estados vigentes leyendo la base en SOLO LECTURA (para publish/program: no crea tablas)."""
    p = Path(db_path)
    if not p.exists():
        return {}
    conn = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
    try:
        return resolve_statuses(combinations_from_conn(conn), status_events_from_conn(conn))
    finally:
        conn.close()


def main(argv: list[str] | None = None) -> int:
    import argparse
    import os
    import sys

    default_db = Path(os.environ.get("HEATSIGNAL_LAB_HOME", Path.home() / "heatsignal-lab")) / "trials.sqlite"
    ap = argparse.ArgumentParser(description="Estados de los intentos del laboratorio (eventos append-only).")
    ap.add_argument("--registry", default=str(default_db))
    sub = ap.add_subparsers(dest="cmd", required=True)
    m = sub.add_parser("marcar", help="agregar un evento de estado (no borra ni modifica nada)")
    m.add_argument("strategy_id")
    m.add_argument("--estado", required=True, choices=STATUSES)
    m.add_argument("--motivo", required=True, help="obligatorio: por que (bug, datos rotos, ...)")
    m.add_argument("--params", default=None, help="JSON de UNA combinacion; sin esto aplica a todas ('*')")
    sub.add_parser("estados", help="estado vigente de cada estrategia y sus eventos")
    args = ap.parse_args(argv)
    if args.cmd == "marcar":
        if not args.motivo.strip():
            ap.error("--motivo no puede estar vacio")
        reg = TrialRegistry(args.registry)
        try:
            eid = reg.mark(args.strategy_id, args.estado, args.motivo, args.params)
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        finally:
            reg.close()
        print(f"evento {eid}: {args.strategy_id} {args.params or ALL_PARAMS} -> {args.estado} ({args.motivo.strip()})")
        return 0
    for sid, st in sorted(read_statuses(args.registry).items()):
        print(f"{sid:40s} {st['status']:14s} {st['n_combinations']} comb." + (f"  motivo: {st['motivo']}" if st["motivo"] else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
