"""
Registro persistente de ciclos de analisis (append-only), para que el panel muestre el historial y el
ultimo razonamiento del grafo aunque el proceso se reinicie (antes vivia solo en memoria y cada deploy
lo borraba).

Vive en la base de evaluacion (`heatsignal_eval.sqlite`), tabla `cycle_log`: una fila por ciclo con
estado, detalle, decisiones por simbolo y el resumen del razonamiento (`run_view`). No guarda features.

    python -m heatsignal.pipeline.cycle_log --backfill CHECKPOINTS_DB [--db EVAL_DB]
        recupera los ciclos con grafo que ya estan en los checkpoints (una sola vez; idempotente)
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sqlite3
import sys
from collections.abc import Iterable
from pathlib import Path
from typing import Any

log = logging.getLogger("heatsignal.pipeline.cycle_log")

SCHEMA = """
CREATE TABLE IF NOT EXISTS cycle_log (
    run_id       TEXT PRIMARY KEY,
    ts_ms        INTEGER NOT NULL,
    status       TEXT    NOT NULL,
    n_hypotheses INTEGER NOT NULL,
    detail       TEXT    NOT NULL,
    decisions    TEXT    NOT NULL,   -- JSON
    report       TEXT                -- JSON (run_view) o NULL
);
CREATE INDEX IF NOT EXISTS idx_cycle_log_ts ON cycle_log (ts_ms);
CREATE TRIGGER IF NOT EXISTS trg_cycle_log_no_update BEFORE UPDATE ON cycle_log
BEGIN SELECT RAISE(ABORT, 'cycle_log es append-only'); END;
CREATE TRIGGER IF NOT EXISTS trg_cycle_log_no_delete BEFORE DELETE ON cycle_log
BEGIN SELECT RAISE(ABORT, 'cycle_log es append-only'); END;
"""


def ts_from_run_id(run_id: str) -> int:
    try:
        return int(run_id.rsplit("-", 1)[-1])
    except ValueError:
        return 0


class CycleLog:
    def __init__(self, db_path: str | Path) -> None:
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(db_path), timeout=30)
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    def append(self, run_id: str, status: str, n_hypotheses: int, detail: str,
               decisions: list[dict[str, Any]], report: dict[str, Any] | None) -> bool:
        cur = self._conn.execute(
            "INSERT OR IGNORE INTO cycle_log (run_id, ts_ms, status, n_hypotheses, detail, decisions, report) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (run_id, ts_from_run_id(run_id), status, n_hypotheses, detail, json.dumps(decisions),
             None if report is None else json.dumps(report)))
        self._conn.commit()
        return cur.rowcount == 1

    def _rows(self, where: str, limit: int) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT run_id, ts_ms, status, n_hypotheses, detail, decisions, report FROM cycle_log "
            f"{where} ORDER BY ts_ms DESC LIMIT ?", (limit,)).fetchall()
        return [
            {"run_id": r[0], "ts_ms": r[1], "status": r[2], "n_hypotheses": r[3], "detail": r[4],
             "decisions": json.loads(r[5]), "report": None if r[6] is None else json.loads(r[6])}
            for r in rows
        ]

    def latest(self, limit: int = 20, *, with_report: bool = False) -> list[dict[str, Any]]:
        rows = self._rows("", limit)
        if not with_report:
            for r in rows:
                r["report"] = None
        return rows

    def latest_ran(self) -> dict[str, Any] | None:
        rows = self._rows("WHERE status = 'ran'", 1)
        return rows[0] if rows else None

    def latest_per_symbol(self, scan: int = 200) -> dict[str, dict[str, Any]]:
        """Ultimo analisis del grafo de CADA simbolo (con rotacion, cada ciclo cubre solo algunos)."""
        out: dict[str, dict[str, Any]] = {}
        for row in self._rows("WHERE status = 'ran' AND report IS NOT NULL", scan):
            decisions = {d["symbol"]: d for d in row["decisions"]}
            for sym, analysis in (row["report"] or {}).get("symbols", {}).items():
                if sym not in out:
                    out[sym] = {"run_id": row["run_id"], "ts_ms": row["ts_ms"], "decision": decisions.get(sym),
                                "analysis": analysis}
        return out

    def counts(self) -> dict[str, int]:
        return dict(self._conn.execute("SELECT status, COUNT(*) FROM cycle_log GROUP BY status").fetchall())

    def close(self) -> None:
        self._conn.close()


async def backfill_from_checkpoints(cycle_log: CycleLog, checkpoints_db: str | Path) -> int:
    """Recupera los ciclos con grafo guardados en los checkpoints de LangGraph (estado final por run)."""
    from ..agents.checkpoint import sqlite_checkpointer
    from .analysis import run_view

    added = 0
    seen: set[str] = set()
    async with sqlite_checkpointer(checkpoints_db) as saver:
        async for cp in saver.alist(None):
            values = cp.checkpoint["channel_values"]
            run_id = values.get("run_id")
            if not run_id or run_id in seen or "report" not in values:
                continue  # solo el estado final (con reporte) de cada corrida
            seen.add(run_id)
            decisions = values.get("synthesis_decisions") or []
            views = [{"symbol": d.symbol, "outcome": d.outcome, "reason": d.reason, "strategy_id": d.strategy_id}
                     for d in decisions]
            detail = "; ".join(f"{d.symbol}={d.outcome}" for d in decisions) + " (recuperado de checkpoints)"
            n = len(values.get("pending_hypotheses") or [])
            if cycle_log.append(run_id, "ran", n, detail, views, run_view(values)):
                added += 1
    return added


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Historial de ciclos de analisis.")
    parser.add_argument("--db", default=os.environ.get("HEATSIGNAL_EVAL_DB", "data/heatsignal_eval.sqlite"))
    parser.add_argument("--backfill", metavar="CHECKPOINTS_DB", help="recuperar ciclos desde los checkpoints")
    args = parser.parse_args(list(argv) if argv is not None else None)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    cl = CycleLog(args.db)
    try:
        if args.backfill:
            print(f"ciclos recuperados: {asyncio.run(backfill_from_checkpoints(cl, args.backfill))}")
        for row in reversed(cl.latest(50)):
            print(row["run_id"], row["status"], row["n_hypotheses"], row["detail"][:120])
    finally:
        cl.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
