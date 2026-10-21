"""
Veredictos del CIO (graph-v2-cio) y su evaluacion descriptiva. Vive en `heatsignal_eval.sqlite`.

Tablas (append-only, triggers que abortan UPDATE/DELETE):
  - `cio_verdicts`: una fila por (ciclo, simbolo) en CADA ciclo con grafo, se opere o no. Postura ganadora del
    debate segun el CIO, `peso` (0-1, cuanto domina), decision (tomar candidato / no operar), razones, rechazo en codigo
    de la decision del CIO (setup fuera de los candidatos o direccion invertida), veto del CRO con su motivo, resultado
    final del registro y la version del grafo.
  - `cio_verdict_outcomes`: la evaluacion de cada veredicto con postura != empate (la escribe el job de evaluacion):
    retorno del simbolo en las 4 h siguientes con velas de 1h CERRADAS a mark price (entrada = cierre de la ultima vela
    cerrada al emitir, salida = cierre de la vela que termina 4 h despues), acierto de la postura y Brier del `peso`
    tomado como probabilidad de acierto.

Es DESCRIPTIVO: sirve para saber si el arbitro aporta. Ningun numero de aca vuelve al grafo ni cambia nada solo.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from .segments import wilson

logger = logging.getLogger(__name__)

HOUR_MS = 3_600_000
CIO_HORIZON_H = 4
NO_DATA_GRACE_MS = 6 * HOUR_MS  # sin vela tras 6 h de vencida la ventana -> 'sin_datos' (no se reintenta)

SCHEMA = """
CREATE TABLE IF NOT EXISTS cio_verdicts (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id          TEXT    NOT NULL,
    ts_ms           INTEGER NOT NULL,
    symbol          TEXT    NOT NULL,
    postura         TEXT    NOT NULL CHECK (postura IN ('alcista', 'bajista', 'empate')),
    peso            REAL    CHECK (peso IS NULL OR (peso >= 0 AND peso <= 1)),
    decision        TEXT    NOT NULL CHECK (decision IN ('tomar', 'no_operar')),
    strategy_id     TEXT,
    direction       TEXT    CHECK (direction IS NULL OR direction IN ('LONG', 'SHORT')),
    razones         TEXT    NOT NULL,   -- JSON: lista de frases
    candidates      TEXT    NOT NULL,   -- JSON: candidatos verificados por codigo que tenia el simbolo
    cio_rejection   TEXT,               -- motivo si el codigo rechazo la decision del CIO
    cro_veto        TEXT,               -- motivo del veto del CRO (NULL = sin veto)
    outcome         TEXT,               -- resultado del registro: hypothesis | no_hypothesis | rejected
    outcome_reason  TEXT,
    hypothesis_id   TEXT,
    session         TEXT,
    session_day     TEXT,
    graph_version   TEXT    NOT NULL,
    UNIQUE (run_id, symbol)
);
CREATE INDEX IF NOT EXISTS idx_cio_verdicts_ts ON cio_verdicts (ts_ms);
CREATE TRIGGER IF NOT EXISTS trg_cio_verdicts_no_update BEFORE UPDATE ON cio_verdicts
BEGIN SELECT RAISE(ABORT, 'cio_verdicts es append-only'); END;
CREATE TRIGGER IF NOT EXISTS trg_cio_verdicts_no_delete BEFORE DELETE ON cio_verdicts
BEGIN SELECT RAISE(ABORT, 'cio_verdicts es append-only'); END;

CREATE TABLE IF NOT EXISTS cio_verdict_outcomes (
    verdict_id     INTEGER PRIMARY KEY REFERENCES cio_verdicts (id),
    status         TEXT    NOT NULL CHECK (status IN ('ok', 'sin_datos')),
    entry_ts_ms    INTEGER NOT NULL,
    exit_ts_ms     INTEGER NOT NULL,
    entry_price    REAL,
    exit_price     REAL,
    return_pct     REAL,
    hit            INTEGER CHECK (hit IS NULL OR hit IN (0, 1)),
    brier          REAL,
    evaluated_ms   INTEGER NOT NULL
);
CREATE TRIGGER IF NOT EXISTS trg_cio_outcomes_no_update BEFORE UPDATE ON cio_verdict_outcomes
BEGIN SELECT RAISE(ABORT, 'cio_verdict_outcomes es append-only'); END;
CREATE TRIGGER IF NOT EXISTS trg_cio_outcomes_no_delete BEFORE DELETE ON cio_verdict_outcomes
BEGIN SELECT RAISE(ABORT, 'cio_verdict_outcomes es append-only'); END;
"""

_COLS = ("id, run_id, ts_ms, symbol, postura, peso, decision, strategy_id, direction, razones, candidates, "
         "cio_rejection, cro_veto, outcome, outcome_reason, hypothesis_id, session, session_day, graph_version")


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.commit()


def _row(r: tuple) -> dict[str, Any]:
    keys = [k.strip() for k in _COLS.split(",")]
    d = dict(zip(keys, r))
    d["razones"] = json.loads(d["razones"] or "[]")
    d["candidates"] = json.loads(d["candidates"] or "[]")
    return d


def _has_table(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None


class CioVerdictLog:
    """Escritura de `cio_verdicts` (la usa el nodo de registro del grafo)."""

    def __init__(self, db_path: str | Path) -> None:
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(db_path), timeout=30, check_same_thread=False)
        ensure_schema(self._conn)

    def append(self, rows: Iterable[dict[str, Any]]) -> int:
        """Inserta las filas del ciclo. Idempotente por (run_id, symbol): un reintento del nodo no duplica."""
        n = 0
        for r in rows:
            cur = self._conn.execute(
                "INSERT OR IGNORE INTO cio_verdicts (run_id, ts_ms, symbol, postura, peso, decision, strategy_id, "
                "direction, razones, candidates, cio_rejection, cro_veto, outcome, outcome_reason, hypothesis_id, "
                "session, session_day, graph_version) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (r["run_id"], int(r["ts_ms"]), r["symbol"], r["postura"], r.get("peso"), r["decision"],
                 r.get("strategy_id"), r.get("direction"), json.dumps(list(r.get("razones") or []), ensure_ascii=False),
                 json.dumps(list(r.get("candidates") or []), ensure_ascii=False), r.get("cio_rejection"),
                 r.get("cro_veto"), r.get("outcome"), r.get("outcome_reason"), r.get("hypothesis_id"),
                 r.get("session"), r.get("session_day"), r["graph_version"]))
            n += cur.rowcount
        self._conn.commit()
        return n

    def by_run(self, run_id: str) -> list[dict[str, Any]]:
        return by_run(self._conn, run_id)

    def close(self) -> None:
        self._conn.close()


# ---------------------------------------------------------------- lectura (conexion read-only del panel)


def by_run(conn: sqlite3.Connection, run_id: str) -> list[dict[str, Any]]:
    if not _has_table(conn, "cio_verdicts"):
        return []
    rows = conn.execute(f"SELECT {_COLS} FROM cio_verdicts WHERE run_id = ? ORDER BY symbol", (run_id,)).fetchall()
    out = [_row(r) for r in rows]
    if _has_table(conn, "cio_verdict_outcomes") and out:
        ev = {r[0]: r for r in conn.execute(
            "SELECT verdict_id, status, return_pct, hit, brier FROM cio_verdict_outcomes WHERE verdict_id IN "
            f"({','.join('?' * len(out))})", [d["id"] for d in out]).fetchall()}
        for d in out:
            e = ev.get(d["id"])
            d["evaluation"] = None if e is None else {"status": e[1], "return_pct": e[2],
                                                      "hit": None if e[3] is None else bool(e[3]), "brier": e[4]}
    return out


def accuracy(conn: sqlite3.Connection, recent: int = 20) -> dict[str, Any]:
    """Resumen de "¿acierta el CIO?": n, aciertos, tasa con IC de Wilson, Brier del peso y su referencia."""
    if not _has_table(conn, "cio_verdicts"):
        return {"available": False}
    total = int(conn.execute("SELECT COUNT(*) FROM cio_verdicts").fetchone()[0])
    by_post = dict(conn.execute("SELECT postura, COUNT(*) FROM cio_verdicts GROUP BY postura").fetchall())
    vetoes = int(conn.execute("SELECT COUNT(*) FROM cio_verdicts WHERE cro_veto IS NOT NULL").fetchone()[0])
    rejected = int(conn.execute("SELECT COUNT(*) FROM cio_verdicts WHERE cio_rejection IS NOT NULL").fetchone()[0])
    out: dict[str, Any] = {"available": True, "verdicts": total, "by_postura": by_post, "cro_vetoes": vetoes,
                           "cio_rejections": rejected, "horizon_h": CIO_HORIZON_H, "n": 0, "hits": 0, "rate": None,
                           "ci": None, "brier": None, "brier_ref": None, "mean_peso": None, "sin_datos": 0,
                           "pending": 0, "by_postura_eval": {}, "recent": []}
    if not _has_table(conn, "cio_verdict_outcomes"):
        return out
    rows = conn.execute(
        "SELECT v.postura, v.peso, o.hit, o.brier FROM cio_verdict_outcomes o JOIN cio_verdicts v ON v.id = o.verdict_id "
        "WHERE o.status = 'ok'").fetchall()
    out["sin_datos"] = int(conn.execute(
        "SELECT COUNT(*) FROM cio_verdict_outcomes WHERE status = 'sin_datos'").fetchone()[0])
    out["pending"] = int(conn.execute(
        "SELECT COUNT(*) FROM cio_verdicts v WHERE v.postura != 'empate' AND v.peso IS NOT NULL AND NOT EXISTS "
        "(SELECT 1 FROM cio_verdict_outcomes o WHERE o.verdict_id = v.id)").fetchone()[0])
    n = len(rows)
    if n:
        hits = sum(int(r[2]) for r in rows)
        p = hits / n
        out.update(n=n, hits=hits, rate=p, ci=wilson(hits, n), brier=sum(r[3] for r in rows) / n,
                   brier_ref=p * (1 - p), mean_peso=sum(r[1] for r in rows) / n)
        for post in ("alcista", "bajista"):
            sub = [r for r in rows if r[0] == post]
            if sub:
                k = sum(int(r[2]) for r in sub)
                out["by_postura_eval"][post] = {"n": len(sub), "hits": k, "rate": k / len(sub), "ci": wilson(k, len(sub))}
    out["recent"] = [
        {"run_id": r[0], "ts_ms": r[1], "symbol": r[2], "postura": r[3], "peso": r[4], "status": r[5],
         "return_pct": r[6], "hit": None if r[7] is None else bool(r[7]), "brier": r[8]}
        for r in conn.execute(
            "SELECT v.run_id, v.ts_ms, v.symbol, v.postura, v.peso, o.status, o.return_pct, o.hit, o.brier "
            "FROM cio_verdict_outcomes o JOIN cio_verdicts v ON v.id = o.verdict_id "
            "ORDER BY v.ts_ms DESC, v.symbol LIMIT ?", (int(recent),)).fetchall()]
    return out


# ---------------------------------------------------------------- evaluacion (job de evaluacion)


def _price(source: Any, symbol: str, end_ms: int) -> float | None:
    v = source.price_at(symbol, datetime.fromtimestamp(end_ms / 1000, tz=timezone.utc))
    return None if v is None else float(v)


def evaluate_missing(db_path: str | Path, price_source: Any, *, now: datetime | None = None,
                     limit: int = 500) -> dict[str, Any]:
    """Evalua los veredictos (postura != empate) cuya ventana de 4 h ya cerro y todavia no tienen fila.

    `price_source.price_at(symbol, ts)`: cierre de la vela de 1h que TERMINA en `ts` (`BinanceHourlyClosePriceSource`
    a mark). Entrada = vela que termina en floor_hora(ts del veredicto) (la ultima cerrada al emitir, sin look-ahead);
    salida = la que termina 4 h despues. Acierto: alcista y retorno > 0, o bajista y retorno < 0 (0 = fallo).
    Brier = (peso - acierto)^2. Sin vela -> se reintenta; pasada la gracia -> 'sin_datos'. Un error de la fuente
    deja el veredicto pendiente (no se escribe nada).
    """
    now_ms = int((now or datetime.now(timezone.utc)).timestamp() * 1000)
    conn = sqlite3.connect(str(db_path), timeout=30)
    try:
        ensure_schema(conn)
        rows = conn.execute(
            "SELECT v.id, v.symbol, v.ts_ms, v.postura, v.peso FROM cio_verdicts v WHERE v.postura != 'empate' "
            "AND v.peso IS NOT NULL AND NOT EXISTS (SELECT 1 FROM cio_verdict_outcomes o WHERE o.verdict_id = v.id) "
            "ORDER BY v.ts_ms LIMIT ?", (int(limit),)).fetchall()
        done = {"ok": 0, "sin_datos": 0, "waiting": 0, "errors": 0}
        for vid, symbol, ts_ms, postura, peso in rows:
            entry_ts = ts_ms - ts_ms % HOUR_MS
            exit_ts = entry_ts + CIO_HORIZON_H * HOUR_MS
            if exit_ts > now_ms:
                done["waiting"] += 1
                continue
            try:
                entry, exit_ = _price(price_source, symbol, entry_ts), _price(price_source, symbol, exit_ts)
            except Exception as exc:  # noqa: BLE001 - un fallo de la fuente deja el veredicto para la proxima
                logger.warning("cio: no se pudo leer el precio de %s (%s): %s", symbol, vid, exc)
                done["errors"] += 1
                continue
            if entry is None or exit_ is None or entry <= 0:
                if now_ms - exit_ts < NO_DATA_GRACE_MS:
                    done["waiting"] += 1
                    continue
                conn.execute(
                    "INSERT OR IGNORE INTO cio_verdict_outcomes (verdict_id, status, entry_ts_ms, exit_ts_ms, "
                    "entry_price, exit_price, evaluated_ms) VALUES (?, 'sin_datos', ?, ?, ?, ?, ?)",
                    (vid, entry_ts, exit_ts, entry, exit_, now_ms))
                done["sin_datos"] += 1
                continue
            ret = (exit_ / entry - 1) * 100
            hit = int((postura == "alcista" and ret > 0) or (postura == "bajista" and ret < 0))
            conn.execute(
                "INSERT OR IGNORE INTO cio_verdict_outcomes (verdict_id, status, entry_ts_ms, exit_ts_ms, entry_price, "
                "exit_price, return_pct, hit, brier, evaluated_ms) VALUES (?, 'ok', ?, ?, ?, ?, ?, ?, ?, ?)",
                (vid, entry_ts, exit_ts, entry, exit_, ret, hit, (float(peso) - hit) ** 2, now_ms))
            done["ok"] += 1
        conn.commit()
        return done
    finally:
        conn.close()


def now_ms() -> int:
    return int(time.time() * 1000)
