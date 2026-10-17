"""
Cola de solicitudes al laboratorio ("Mandar al laboratorio" desde el panel).

El panel corre en la VM (sin pandas); el laboratorio corre en la PC. La VM solo guarda la cola y los
resultados; la PC los procesa con `python -m heatsignal.lab.worker`, que habla con esta CLI por SSH.

Vive en la base de evaluacion (`heatsignal_eval.sqlite`), mismo criterio append-only que `cycle_log` y
`strategy_registry`: triggers bloquean UPDATE/DELETE.
    lab_requests        una fila por solicitud (estrategias, simbolos, nota). Inmutable.
    lab_request_events  una fila por cambio de estado (pending -> running -> done | error). El estado actual
                        es el ultimo evento. Asi queda el historial completo (quien la tomo, cuando, el error).

CLI (corre EN LA VM; imprime JSON en stdout para el worker):

    python -m heatsignal.evaluation.lab_requests list [--status pending] [--limit 50]
    python -m heatsignal.evaluation.lab_requests claim-next [--worker pc]     # toma la mas vieja pendiente
    python -m heatsignal.evaluation.lab_requests done REQUEST_ID [--worker pc]  # exige data/lab_requests/<id>.json
    python -m heatsignal.evaluation.lab_requests fail REQUEST_ID --error "mensaje" [--worker pc]
        --db  base de evaluacion (default HEATSIGNAL_EVAL_DB o data/heatsignal_eval.sqlite)
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
import time
import uuid
from collections.abc import Iterable
from pathlib import Path
from typing import Any

STATUSES = ("pending", "running", "done", "error")
REQUEST_ID = re.compile(r"^[0-9a-f]{32}$")
RESULTS_DIRNAME = "lab_requests"  # data/lab_requests/<request_id>.json (lo sube el worker)
MAX_ERROR = 2000

SCHEMA = """
CREATE TABLE IF NOT EXISTS lab_requests (
    request_id   TEXT PRIMARY KEY,
    created_ms   INTEGER NOT NULL,
    strategy_ids TEXT    NOT NULL,   -- JSON (ya expandido si se pidio "*")
    symbols      TEXT    NOT NULL,   -- JSON, formato Binance 'XRPUSDT'
    note         TEXT    NOT NULL DEFAULT '',
    origin       TEXT    NOT NULL DEFAULT 'panel'
);
CREATE TABLE IF NOT EXISTS lab_request_events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    request_id TEXT    NOT NULL REFERENCES lab_requests (request_id),
    status     TEXT    NOT NULL CHECK (status IN ('pending', 'running', 'done', 'error')),
    ts_ms      INTEGER NOT NULL,
    worker     TEXT,
    detail     TEXT                -- JSON: {"error": ...} o {"result": {...}}
);
CREATE INDEX IF NOT EXISTS idx_lab_request_events_req ON lab_request_events (request_id, id);
CREATE TRIGGER IF NOT EXISTS trg_lab_requests_no_update BEFORE UPDATE ON lab_requests
BEGIN SELECT RAISE(ABORT, 'lab_requests es append-only'); END;
CREATE TRIGGER IF NOT EXISTS trg_lab_requests_no_delete BEFORE DELETE ON lab_requests
BEGIN SELECT RAISE(ABORT, 'lab_requests es append-only'); END;
CREATE TRIGGER IF NOT EXISTS trg_lab_request_events_no_update BEFORE UPDATE ON lab_request_events
BEGIN SELECT RAISE(ABORT, 'lab_request_events es append-only'); END;
CREATE TRIGGER IF NOT EXISTS trg_lab_request_events_no_delete BEFORE DELETE ON lab_request_events
BEGIN SELECT RAISE(ABORT, 'lab_request_events es append-only'); END;
"""

# estado actual = ultimo evento de cada solicitud
_CURRENT = """
SELECT r.request_id, r.created_ms, r.strategy_ids, r.symbols, r.note, r.origin,
       e.status, e.ts_ms, e.worker, e.detail
FROM lab_requests r
JOIN lab_request_events e
  ON e.id = (SELECT MAX(id) FROM lab_request_events x WHERE x.request_id = r.request_id)
"""

# transiciones validas (el resto es un error del llamador)
_ALLOWED = {"running": ("pending",), "done": ("running",), "error": ("pending", "running")}


class LabRequestError(Exception):
    """Transicion invalida o solicitud inexistente."""


def _now_ms() -> int:
    return int(time.time() * 1000)


def _row(r: tuple) -> dict[str, Any]:
    detail = json.loads(r[9]) if r[9] else {}
    return {"request_id": r[0], "created_ms": r[1], "strategy_ids": json.loads(r[2]), "symbols": json.loads(r[3]),
            "note": r[4], "origin": r[5], "status": r[6], "status_ms": r[7], "worker": r[8],
            "error": detail.get("error"), "result": detail.get("result")}


class LabRequestStore:
    def __init__(self, db_path: str | Path, *, timeout_s: float = 30.0, clock_ms=_now_ms) -> None:
        self.path = Path(db_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.path), timeout=timeout_s, isolation_level=None)
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.executescript(SCHEMA)
        self._clock_ms = clock_ms

    @property
    def results_dir(self) -> Path:
        return self.path.parent / RESULTS_DIRNAME

    # ---- escritura ------------------------------------------------------------------------------

    def create(self, strategy_ids: list[str], symbols: list[str], note: str = "", origin: str = "panel") -> dict[str, Any]:
        rid = uuid.uuid4().hex
        now = self._clock_ms()
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            self._conn.execute(
                "INSERT INTO lab_requests (request_id, created_ms, strategy_ids, symbols, note, origin) "
                "VALUES (?, ?, ?, ?, ?, ?)", (rid, now, json.dumps(strategy_ids), json.dumps(symbols), note, origin))
            self._conn.execute("INSERT INTO lab_request_events (request_id, status, ts_ms) VALUES (?, 'pending', ?)",
                               (rid, now))
            self._conn.execute("COMMIT")
        except Exception:
            self._conn.execute("ROLLBACK")
            raise
        return self.get(rid)  # type: ignore[return-value]

    def _transition(self, request_id: str, status: str, *, worker: str | None = None,
                    detail: dict[str, Any] | None = None) -> dict[str, Any]:
        """Agrega un evento de estado si la transicion es valida. BEGIN IMMEDIATE: dos workers no toman la
        misma solicitud."""
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            row = self._conn.execute(_CURRENT + " WHERE r.request_id = ?", (request_id,)).fetchone()
            if row is None:
                raise LabRequestError(f"solicitud {request_id} no existe")
            if row[6] not in _ALLOWED[status]:
                raise LabRequestError(f"solicitud {request_id} esta {row[6]}: no puede pasar a {status}")
            self._conn.execute(
                "INSERT INTO lab_request_events (request_id, status, ts_ms, worker, detail) VALUES (?, ?, ?, ?, ?)",
                (request_id, status, self._clock_ms(), worker, None if detail is None else json.dumps(detail)))
            self._conn.execute("COMMIT")
        except Exception:
            self._conn.execute("ROLLBACK")
            raise
        return self.get(request_id)  # type: ignore[return-value]

    def claim_next(self, worker: str = "pc") -> dict[str, Any] | None:
        """Toma la solicitud pendiente mas vieja (pasa a running) o None si no hay."""
        for req in reversed(self.list(status="pending", limit=50)[1]):
            try:
                return self._transition(req["request_id"], "running", worker=worker)
            except LabRequestError:  # otro worker la tomo justo
                continue
        return None

    def done(self, request_id: str, result: dict[str, Any], worker: str | None = None) -> dict[str, Any]:
        return self._transition(request_id, "done", worker=worker, detail={"result": result})

    def fail(self, request_id: str, error: str, worker: str | None = None) -> dict[str, Any]:
        return self._transition(request_id, "error", worker=worker, detail={"error": error[:MAX_ERROR]})

    # ---- lectura --------------------------------------------------------------------------------

    def get(self, request_id: str) -> dict[str, Any] | None:
        row = self._conn.execute(_CURRENT + " WHERE r.request_id = ?", (request_id,)).fetchone()
        return None if row is None else _row(row)

    def events(self, request_id: str) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT status, ts_ms, worker, detail FROM lab_request_events WHERE request_id = ? ORDER BY id",
            (request_id,)).fetchall()
        return [{"status": s, "ts_ms": t, "worker": w, **(json.loads(d) if d else {})} for s, t, w, d in rows]

    def list(self, *, status: str | None = None, limit: int = 50, offset: int = 0) -> tuple[int, list[dict[str, Any]]]:
        """(total con ese estado, pagina mas nueva primero)."""
        where, params = (" WHERE e.status = ?", [status]) if status else ("", [])
        total = int(self._conn.execute(f"SELECT COUNT(*) FROM ({_CURRENT}{where})", params).fetchone()[0])
        rows = self._conn.execute(f"{_CURRENT}{where} ORDER BY r.created_ms DESC, r.request_id LIMIT ? OFFSET ?",
                                  [*params, int(limit), int(offset)]).fetchall()
        return total, [_row(r) for r in rows]

    def count(self, status: str) -> int:
        return self.list(status=status, limit=1)[0]

    def close(self) -> None:
        self._conn.close()


def result_summary(path: Path) -> dict[str, Any]:
    """Resumen chico del archivo de resultados que subio el worker (lo que queda en el evento `done`)."""
    data = json.loads(path.read_text(encoding="utf-8"))
    items = data.get("strategies") or []
    return {
        "file": path.name,
        "strategies": len(items),
        "ok": sum(1 for s in items if s.get("status") == "ok"),
        "errors": sum(1 for s in items if s.get("status") == "error"),
        "passing": sum(1 for s in items if (s.get("summary") or {}).get("pasa")),
    }


# ---- CLI ------------------------------------------------------------------------------------------

def main(argv: Iterable[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Cola de solicitudes al laboratorio (corre en la VM).")
    p.add_argument("--db", default=os.environ.get("HEATSIGNAL_EVAL_DB", "data/heatsignal_eval.sqlite"))
    sub = p.add_subparsers(dest="cmd", required=True)
    ls = sub.add_parser("list")
    ls.add_argument("--status", choices=STATUSES)
    ls.add_argument("--limit", type=int, default=50)
    cl = sub.add_parser("claim-next")
    cl.add_argument("--worker", default="pc")
    dn = sub.add_parser("done")
    dn.add_argument("request_id")
    dn.add_argument("--worker", default="pc")
    fl = sub.add_parser("fail")
    fl.add_argument("request_id")
    fl.add_argument("--error", required=True)
    fl.add_argument("--worker", default="pc")
    args = p.parse_args(list(argv) if argv is not None else None)

    store = LabRequestStore(args.db)
    try:
        if args.cmd == "list":
            total, items = store.list(status=args.status, limit=args.limit)
            out: Any = {"total": total, "items": items}
        elif args.cmd == "claim-next":
            out = {"request": store.claim_next(args.worker)}
        else:
            if not REQUEST_ID.match(args.request_id):
                print(json.dumps({"error": f"request_id invalido: {args.request_id!r}"}))
                return 2
            if args.cmd == "done":
                path = store.results_dir / f"{args.request_id}.json"
                if not path.is_file():
                    print(json.dumps({"error": f"falta el archivo de resultados {path}"}))
                    return 1
                out = {"request": store.done(args.request_id, result_summary(path), args.worker)}
            else:
                out = {"request": store.fail(args.request_id, args.error, args.worker)}
    except LabRequestError as exc:
        print(json.dumps({"error": str(exc)}))
        return 1
    finally:
        store.close()
    print(json.dumps(out, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
