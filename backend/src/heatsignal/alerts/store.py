"""
Base de las alertas de oportunidad: `data/alerts.sqlite`, APPEND-ONLY (triggers bloquean UPDATE y DELETE).

    alert_runs     una fila por corrida del motor (ventana evaluada, estado, cobertura, duracion).
    alerts         una fila por cambio de objetivo detectado: entrada (0 -> +-x), salida (+-x -> 0) o giro (signo
                   cambia). UNIQUE (strategy_id, symbol, decision_ms, kind): re-correr una ventana no duplica.
    alert_actions  eventos sobre una alerta: 'enviada' (boton del panel), 'auto' (el interruptor de la estrategia
                   estaba prendido), 'descartada'; la fase 4 (ejecutor en la demo) suma 'ejecutada' / 'rechazada'.
    auto_settings  eventos del interruptor de automatico por estrategia; vale el ultimo (default apagado).

Sin pandas: la lee y escribe el panel (hilo aparte) y el motor.
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any

KINDS = ("entrada", "salida", "giro")
ACTIONS = ("enviada", "auto", "descartada", "ejecutada", "rechazada")

SCHEMA = """
CREATE TABLE IF NOT EXISTS alert_runs (
    run_id         TEXT    PRIMARY KEY,
    ts_ms          INTEGER NOT NULL,
    status         TEXT    NOT NULL,          -- ok | error | skipped
    window_from_ms INTEGER,
    window_to_ms   INTEGER,
    n_eval         INTEGER NOT NULL DEFAULT 0,
    n_alerts       INTEGER NOT NULL DEFAULT 0,
    duration_s     REAL,
    detail         TEXT                       -- JSON: salteadas, errores por estrategia, velas, hueco
);
CREATE TABLE IF NOT EXISTS alerts (
    alert_id     TEXT    PRIMARY KEY,
    run_id       TEXT    NOT NULL,
    created_ms   INTEGER NOT NULL,
    decision_ms  INTEGER NOT NULL,
    strategy_id  TEXT    NOT NULL,
    symbol       TEXT    NOT NULL,
    kind         TEXT    NOT NULL,
    direction    TEXT    NOT NULL,            -- long | short | flat (objetivo nuevo)
    target       REAL    NOT NULL,
    prev_target  REAL    NOT NULL,
    price        REAL,
    timeframe    TEXT,
    source       TEXT,                        -- catalogo | generada
    lab_status   TEXT,                        -- pasa | no pasa | sin probar
    lab_sharpe   REAL,
    paper_role   TEXT,                        -- rol en el bot paper (principal, seguimiento, ...) o NULL
    exit_plan    TEXT,
    UNIQUE (strategy_id, symbol, decision_ms, kind)
);
CREATE INDEX IF NOT EXISTS alerts_by_time ON alerts (decision_ms);
CREATE TABLE IF NOT EXISTS alert_actions (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    alert_id  TEXT    NOT NULL REFERENCES alerts(alert_id),
    ts_ms     INTEGER NOT NULL,
    action    TEXT    NOT NULL,
    origin    TEXT,
    detail    TEXT
);
CREATE INDEX IF NOT EXISTS actions_by_alert ON alert_actions (alert_id);
CREATE TABLE IF NOT EXISTS auto_settings (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    strategy_id  TEXT    NOT NULL,
    enabled      INTEGER NOT NULL,
    ts_ms        INTEGER NOT NULL,
    origin       TEXT
);
"""
_TRIGGERS = "".join(
    f"CREATE TRIGGER IF NOT EXISTS {t}_no_update BEFORE UPDATE ON {t} BEGIN SELECT RAISE(ABORT, '{t} es append-only'); END;"
    f"CREATE TRIGGER IF NOT EXISTS {t}_no_delete BEFORE DELETE ON {t} BEGIN SELECT RAISE(ABORT, '{t} es append-only'); END;"
    for t in ("alert_runs", "alerts", "alert_actions", "auto_settings"))

ALERT_COLS = ("alert_id", "run_id", "created_ms", "decision_ms", "strategy_id", "symbol", "kind", "direction", "target",
              "prev_target", "price", "timeframe", "source", "lab_status", "lab_sharpe", "paper_role", "exit_plan")


def _now_ms() -> int:
    return int(time.time() * 1000)


class AlertError(ValueError):
    pass


class AlertStore:
    def __init__(self, path: str | Path, *, timeout_s: float = 10.0, clock_ms=_now_ms) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.path), timeout=timeout_s, isolation_level=None)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.executescript(SCHEMA + _TRIGGERS)
        self._clock = clock_ms

    def close(self) -> None:
        self.conn.close()

    # ---- escritura (motor) ----------------------------------------------------------------------------

    def record_run(self, run_id: str, status: str, *, window: tuple[int | None, int | None] = (None, None),
                   n_eval: int = 0, alerts: list[dict[str, Any]] | None = None, duration_s: float | None = None,
                   detail: dict[str, Any] | None = None) -> int:
        """Corrida + sus alertas en UNA transaccion. Devuelve cuantas alertas nuevas entraron (las repetidas se
        ignoran). Las de estrategias con automatico prendido reciben la accion 'auto'."""
        alerts = alerts or []
        now = self._clock()
        auto = self.auto_map()
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            added = 0
            for a in alerts:
                row = tuple(a.get(c) for c in ALERT_COLS)
                cur = self.conn.execute(f"INSERT OR IGNORE INTO alerts ({', '.join(ALERT_COLS)}) VALUES "
                                        f"({', '.join('?' for _ in ALERT_COLS)})", row)
                if cur.rowcount:
                    added += 1
                    if auto.get(a["strategy_id"]):
                        self.conn.execute("INSERT INTO alert_actions (alert_id, ts_ms, action, origin, detail) "
                                          "VALUES (?, ?, 'auto', 'motor', NULL)", (a["alert_id"], now))
            self.conn.execute(
                "INSERT INTO alert_runs (run_id, ts_ms, status, window_from_ms, window_to_ms, n_eval, n_alerts, "
                "duration_s, detail) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (run_id, now, status, window[0], window[1], n_eval, added, duration_s,
                 None if detail is None else json.dumps(detail, ensure_ascii=False, default=str)))
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise
        return added

    # ---- acciones (panel) -------------------------------------------------------------------------------

    def act(self, alert_id: str, action: str, *, origin: str = "panel", detail: dict[str, Any] | None = None) -> dict[str, Any]:
        if action not in ACTIONS:
            raise AlertError(f"accion desconocida: {action}")
        alert = self.get(alert_id)
        if alert is None:
            raise AlertError(f"no existe la alerta {alert_id}")
        done = {x["action"] for x in alert["actions"]}
        if action in ("enviada", "descartada") and done & {"enviada", "auto", "descartada"}:
            raise AlertError(f"la alerta ya fue {sorted(done & {'enviada', 'auto', 'descartada'})[0]}")
        self.conn.execute("INSERT INTO alert_actions (alert_id, ts_ms, action, origin, detail) VALUES (?, ?, ?, ?, ?)",
                          (alert_id, self._clock(), action, origin, None if detail is None else json.dumps(detail)))
        return self.get(alert_id)  # type: ignore[return-value]

    def set_auto(self, strategy_id: str, enabled: bool, *, origin: str = "panel") -> None:
        self.conn.execute("INSERT INTO auto_settings (strategy_id, enabled, ts_ms, origin) VALUES (?, ?, ?, ?)",
                          (strategy_id, int(bool(enabled)), self._clock(), origin))

    # ---- lectura ----------------------------------------------------------------------------------------

    def auto_map(self) -> dict[str, bool]:
        rows = self.conn.execute("SELECT strategy_id, enabled FROM auto_settings ORDER BY id").fetchall()
        out: dict[str, bool] = {}
        for sid, en in rows:
            out[sid] = bool(en)
        return out

    def last_ok_window_end(self) -> int | None:
        r = self.conn.execute("SELECT MAX(window_to_ms) FROM alert_runs WHERE status = 'ok'").fetchone()
        return r[0] if r and r[0] is not None else None

    def runs(self, limit: int = 10) -> list[dict[str, Any]]:
        rows = self.conn.execute("SELECT run_id, ts_ms, status, window_from_ms, window_to_ms, n_eval, n_alerts, duration_s, "
                                 "detail FROM alert_runs ORDER BY ts_ms DESC LIMIT ?", (limit,)).fetchall()
        return [{"run_id": r[0], "ts_ms": r[1], "status": r[2], "window_from_ms": r[3], "window_to_ms": r[4],
                 "n_eval": r[5], "n_alerts": r[6], "duration_s": r[7], "detail": json.loads(r[8]) if r[8] else None}
                for r in rows]

    def _actions(self, ids: list[str]) -> dict[str, list[dict[str, Any]]]:
        out: dict[str, list[dict[str, Any]]] = {i: [] for i in ids}
        for i in range(0, len(ids), 500):
            chunk = ids[i:i + 500]
            for aid, ts, action, origin, detail in self.conn.execute(
                    f"SELECT alert_id, ts_ms, action, origin, detail FROM alert_actions WHERE alert_id IN "
                    f"({', '.join('?' for _ in chunk)}) ORDER BY id", chunk):
                out[aid].append({"ts_ms": ts, "action": action, "origin": origin,
                                 "detail": json.loads(detail) if detail else None})
        return out

    def _rows(self, where: str, params: list[Any], limit: int) -> list[dict[str, Any]]:
        rows = self.conn.execute(f"SELECT {', '.join(ALERT_COLS)} FROM alerts {where} ORDER BY decision_ms DESC, "
                                 f"alert_id LIMIT ?", [*params, limit]).fetchall()
        out = [dict(zip(ALERT_COLS, r)) for r in rows]
        acts = self._actions([a["alert_id"] for a in out])
        for a in out:
            a["actions"] = acts.get(a["alert_id"], [])
            last = next((x["action"] for x in reversed(a["actions"])), None)
            a["state"] = last or "nueva"
        return out

    def get(self, alert_id: str) -> dict[str, Any] | None:
        rows = self._rows("WHERE alert_id = ?", [alert_id], 1)
        return rows[0] if rows else None

    def recent(self, *, limit: int = 200, since_ms: int | None = None, strategy_id: str | None = None,
               symbol: str | None = None, kind: str | None = None) -> list[dict[str, Any]]:
        conds, params = [], []
        for col, val in (("strategy_id", strategy_id), ("symbol", symbol), ("kind", kind)):
            if val:
                conds.append(f"{col} = ?")
                params.append(val)
        if since_ms is not None:
            conds.append("decision_ms >= ?")
            params.append(since_ms)
        return self._rows(("WHERE " + " AND ".join(conds)) if conds else "", params, limit)

    def queued(self) -> list[dict[str, Any]]:
        """Alertas mandadas a la demo (a mano o automatico) que el ejecutor todavia no proceso (fase 4)."""
        ids = [r[0] for r in self.conn.execute(
            "SELECT alert_id FROM alert_actions GROUP BY alert_id HAVING "
            "SUM(action IN ('enviada', 'auto')) > 0 AND SUM(action IN ('ejecutada', 'rechazada', 'descartada')) = 0")]
        out = [a for i in ids if (a := self.get(i)) is not None]
        return sorted(out, key=lambda a: a["decision_ms"])
