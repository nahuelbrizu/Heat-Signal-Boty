"""
MAE / MFE de cada hipotesis resuelta: cuanto fue en contra y cuanto a favor entre la emision y el vencimiento.

Pedido del usuario (2026-10-19, decisions.md "Stop loss y take profit"). Las hipotesis del grafo salen por TIEMPO
(pre-registradas asi: el Resolver no tiene stop ni objetivo). Esto NO cambia eso: es un dato DESCRIPTIVO, calculado
despues de resolver, que no vuelve al grafo ni a ningun criterio de evaluacion.

Definiciones (en % del precio de entrada y en ATR; siempre >= 0):
    entry = entry_reference_price (mark al emitir)
    LONG : MAE = max(0, entry - min(low)) / entry     MFE = max(0, max(high) - entry) / entry
    SHORT: MAE = max(0, max(high) - entry) / entry    MFE = max(0, entry - min(low)) / entry
    en ATR: (MAE% / 100) * entry / ATR14(1h)
con las velas entre la emision y el vencimiento:
  - 1m de la base historica (`klines`, Binance) si estan TODAS las de [piso del minuto de emision, vencimiento):
    fuente `klines_1m`;
  - si no, velas de 1h de Binance (REST `GET /fapi/v1/klines?interval=1h`, el mismo pedido verificado en
    `history/hourly.py`) que se solapan con la ventana: fuente `rest_1h`. Si la emision no cae en un cierre de vela,
    la primera vela incluye precio ANTERIOR a la emision: se marca `parcial_antes_de_emision`.
  Las velas son de precio "last" y la entrada es mark: la diferencia (centavos de bps) se declara en `nota`.
ATR = `atr14_1h` del contexto 1h guardado con la hipotesis (`htf_snapshot`, el que se conocia al emitir); si no esta,
ATR(14) de Wilder con las 1h cerradas antes de la emision (misma fuente de velas). Sin ATR -> solo %.

Corte descriptivo "¿un stop de X ATR la hubiera sacado antes?" (X en `DESCRIPTIVE_STOPS_ATR`): para cada X, si el MAE
llego a X ATR antes del vencimiento (y cuando). NO PRE-REGISTRADO, NO SE USA PARA DECIDIR NADA: agregar un stop a una
estrategia ya registrada seria una -v2 con su propio pre-registro.

Persistencia: tabla `hypothesis_excursions` en la base de evaluacion, APPEND-ONLY (triggers); una fila por hipotesis
(UNIQUE), calculada una sola vez. Sin datos de velas todavia -> no se escribe nada y se reintenta en la proxima corrida.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import urllib.parse
from collections.abc import Callable, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..lab.exits import DESCRIPTIVE_STOPS_ATR

log = logging.getLogger("heatsignal.evaluation.excursion")

MODEL_ID = "excursion-v1"
MIN_MS = 60_000
HOUR_MS = 3_600_000
ATR_PERIOD = 14
REST_BASE_URL = "https://fapi.binance.com"
NOTE_PRICE = ("Velas de precio 'last' de Binance; la entrada de la hipotesis es mark price (la diferencia suele ser de "
              "pocos bps).")
NOT_PREREGISTERED = ("Corte descriptivo NO pre-registrado: no se uso para decidir nada. La hipotesis sale por tiempo; "
                     "agregar un stop seria una estrategia -v2 con su propio pre-registro.")

SCHEMA = """
CREATE TABLE IF NOT EXISTS hypothesis_excursions (
    hypothesis_id   TEXT    PRIMARY KEY,
    model_id        TEXT    NOT NULL,
    computed_at     TEXT    NOT NULL,
    source          TEXT    NOT NULL CHECK (source IN ('klines_1m', 'rest_1h')),
    n_candles       INTEGER NOT NULL,
    window_start_ms INTEGER NOT NULL,
    window_end_ms   INTEGER NOT NULL,
    entry           REAL    NOT NULL,
    atr             REAL,
    atr_source      TEXT,
    mae_pct         REAL    NOT NULL,
    mfe_pct         REAL    NOT NULL,
    mae_atr         REAL,
    mfe_atr         REAL,
    mae_ms          INTEGER,
    mfe_ms          INTEGER,
    stops           TEXT    NOT NULL,       -- JSON: [{"atr": 1.0, "hit": true, "hit_ms": ...}, ...] (descriptivo)
    partial_before  INTEGER NOT NULL DEFAULT 0,
    note            TEXT
);
CREATE TRIGGER IF NOT EXISTS trg_hypothesis_excursions_no_update BEFORE UPDATE ON hypothesis_excursions
BEGIN SELECT RAISE(ABORT, 'hypothesis_excursions es append-only: UPDATE no permitido'); END;
CREATE TRIGGER IF NOT EXISTS trg_hypothesis_excursions_no_delete BEFORE DELETE ON hypothesis_excursions
BEGIN SELECT RAISE(ABORT, 'hypothesis_excursions es append-only: DELETE no permitido'); END;
"""

Candle = tuple[int, float, float, float, float]  # open_ms, open, high, low, close


def excursion(direction: str, entry: float, candles: Sequence[Candle], atr: float | None,
              stops: Sequence[float] = DESCRIPTIVE_STOPS_ATR) -> dict[str, Any]:
    """MAE/MFE de una direccion sobre velas ya recortadas a la ventana. ValueError sin velas o con entrada <= 0."""
    if not candles:
        raise ValueError("sin velas en la ventana")
    if not entry or entry <= 0:
        raise ValueError(f"entrada invalida: {entry}")
    long = direction.upper() == "LONG"
    mae = mfe = 0.0
    mae_ms = mfe_ms = None
    first_hit: dict[float, int | None] = {x: None for x in stops}
    for t, _o, h, lo, _c in sorted(candles):
        adverse = (entry - lo) if long else (h - entry)
        favor = (h - entry) if long else (entry - lo)
        if adverse > mae:
            mae, mae_ms = adverse, t
        if favor > mfe:
            mfe, mfe_ms = favor, t
        if atr:
            for x in stops:
                if first_hit[x] is None and adverse >= x * atr:
                    first_hit[x] = t
    ok_atr = atr is not None and atr > 0
    return {
        "mae_pct": mae / entry * 100, "mfe_pct": mfe / entry * 100,
        "mae_atr": mae / atr if ok_atr else None, "mfe_atr": mfe / atr if ok_atr else None,
        "mae_ms": mae_ms, "mfe_ms": mfe_ms, "n_candles": len(candles),
        "stops": [{"atr": x, "hit": first_hit[x] is not None, "hit_ms": first_hit[x]} for x in stops] if ok_atr else [],
    }


def wilder_atr(candles: Sequence[Candle], period: int = ATR_PERIOD) -> float | None:
    cs = sorted(candles)
    if len(cs) < period + 1:
        return None
    trs = [max(c[2] - c[3], abs(c[2] - p[4]), abs(c[3] - p[4])) for p, c in zip(cs, cs[1:])]
    atr = sum(trs[:period]) / period
    for tr in trs[period:]:
        atr = (atr * (period - 1) + tr) / period
    return atr


# ---- velas ---------------------------------------------------------------------------------------------------


class CandleSource:
    """1m de la base historica (si estan completas) o 1h del REST de Binance. `http_get(url, timeout) -> (status, body)`."""

    def __init__(self, history_db: str | Path | None, *, http_get: Callable[[str, float], tuple[int, bytes]] | None = None,
                 rest_base_url: str = REST_BASE_URL) -> None:
        self._db = None if history_db is None else Path(history_db)
        if http_get is None:
            from .price_source import _urllib_get

            http_get = _urllib_get
        self._get = http_get
        self._base = rest_base_url.rstrip("/")

    def minutes(self, symbol: str, start_ms: int, end_ms: int) -> list[Candle] | None:
        """Velas 1m [start, end) si estan TODAS en la base historica; None si falta alguna o no hay base."""
        if self._db is None or not self._db.exists():
            return None
        from ..history.klines import EXCHANGE, internal_symbol
        from ..history.store import open_readonly

        try:
            conn = open_readonly(self._db)
        except Exception:  # noqa: BLE001
            return None
        try:
            rows = conn.execute("SELECT open_time_ms, open, high, low, close FROM klines WHERE exchange=? AND symbol=? "
                                "AND interval='1m' AND open_time_ms >= ? AND open_time_ms < ? ORDER BY open_time_ms",
                                (EXCHANGE, internal_symbol(symbol), start_ms, end_ms)).fetchall()
        except sqlite3.Error:
            return None
        finally:
            conn.close()
        expected = (end_ms - start_ms + MIN_MS - 1) // MIN_MS
        if len(rows) < expected or expected <= 0:
            return None
        return [(int(r[0]), float(r[1]), float(r[2]), float(r[3]), float(r[4])) for r in rows]

    def hours(self, symbol: str, start_ms: int, end_ms: int) -> list[Candle]:
        """Velas de 1h CERRADAS con apertura en [start, end) del REST de Binance. Propaga el error de red."""
        from .price_source import to_binance_symbol

        n = max(1, min(1500, (end_ms - start_ms + HOUR_MS - 1) // HOUR_MS))
        q = urllib.parse.urlencode({"symbol": to_binance_symbol(symbol), "interval": "1h", "startTime": start_ms,
                                    "endTime": end_ms - 1, "limit": n})
        status, body = self._get(f"{self._base}/fapi/v1/klines?{q}", 10.0)
        if status != 200:
            raise RuntimeError(f"Binance HTTP {status}: {body[:120]!r}")
        out = []
        for k in json.loads(body):
            t = int(k[0])
            if start_ms <= t < end_ms:
                out.append((t, float(k[1]), float(k[2]), float(k[3]), float(k[4])))
        return out


def compute(h: Any, source: CandleSource, *, now_ms: int) -> dict[str, Any] | None:
    """Fila de `hypothesis_excursions` para una hipotesis (PendingHypothesis). None = todavia sin datos."""
    created = int(h.created_at.timestamp() * 1000)
    due = created + int(h.horizon_s) * 1000
    if due > now_ms:
        return None
    start_1m = created - created % MIN_MS
    candles = source.minutes(h.symbol, start_1m, due)
    partial = False
    if candles is not None:
        src = "klines_1m"
        partial = start_1m < created
    else:
        first_h = created - created % HOUR_MS
        candles = source.hours(h.symbol, first_h, due)
        candles = [c for c in candles if c[0] + HOUR_MS <= now_ms]
        expected = (due - first_h + HOUR_MS - 1) // HOUR_MS
        if len(candles) < expected:
            return None
        src = "rest_1h"
        partial = first_h < created
    atr, atr_src = None, None
    snap = getattr(h, "htf_snapshot", None) or {}
    if isinstance(snap, dict) and isinstance(snap.get("atr14_1h"), (int, float)) and snap["atr14_1h"] > 0:
        atr, atr_src = float(snap["atr14_1h"]), "htf_snapshot (ATR14 1h al emitir)"
    else:
        last_closed = created - created % HOUR_MS
        try:
            before = source.hours(h.symbol, last_closed - (ATR_PERIOD + 6) * HOUR_MS, last_closed)
        except Exception as exc:  # noqa: BLE001 - sin ATR igual se guarda el % (declarado)
            before, atr_src = [], f"sin ATR: {exc}"
        atr = wilder_atr(before)
        if atr is not None:
            atr_src = "ATR14 de las velas de 1h cerradas antes de la emision"
        elif atr_src is None:
            atr_src = "sin ATR: faltan velas de 1h previas"
    entry = float(h.entry_reference_price)
    ex = excursion(h.direction.value if hasattr(h.direction, "value") else str(h.direction), entry, candles, atr)
    notes = [NOTE_PRICE]
    if partial:
        notes.append("La primera vela empieza antes de la emision: el extremo puede incluir precio previo.")
    return {
        "hypothesis_id": str(h.hypothesis_id), "model_id": MODEL_ID,
        "computed_at": datetime.fromtimestamp(now_ms / 1000, tz=timezone.utc).isoformat(), "source": src,
        "n_candles": ex["n_candles"], "window_start_ms": created, "window_end_ms": due, "entry": entry,
        "atr": atr, "atr_source": atr_src, "mae_pct": ex["mae_pct"], "mfe_pct": ex["mfe_pct"],
        "mae_atr": ex["mae_atr"], "mfe_atr": ex["mfe_atr"], "mae_ms": ex["mae_ms"], "mfe_ms": ex["mfe_ms"],
        "stops": json.dumps(ex["stops"]), "partial_before": int(partial), "note": " ".join(notes),
    }


# ---- persistencia --------------------------------------------------------------------------------------------


class ExcursionStore:
    def __init__(self, db_path: str | Path) -> None:
        self._conn = sqlite3.connect(str(db_path), timeout=30)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    def add(self, row: dict[str, Any]) -> bool:
        cols = list(row)
        cur = self._conn.execute(f"INSERT OR IGNORE INTO hypothesis_excursions ({', '.join(cols)}) VALUES "
                                 f"({', '.join('?' * len(cols))})", [row[c] for c in cols])
        self._conn.commit()
        return cur.rowcount == 1

    def missing(self, limit: int = 200) -> list[str]:
        """Hipotesis resueltas (status resolved) sin fila de excursion, las mas viejas primero."""
        try:
            rows = self._conn.execute(
                "SELECT p.hypothesis_id FROM pending_hypotheses p JOIN hypothesis_resolutions r ON r.hypothesis_id = "
                "p.hypothesis_id LEFT JOIN hypothesis_excursions e ON e.hypothesis_id = p.hypothesis_id "
                "WHERE r.status = 'resolved' AND e.hypothesis_id IS NULL ORDER BY p.due_ts LIMIT ?", (limit,)).fetchall()
        except sqlite3.OperationalError:  # base sin hipotesis todavia
            return []
        return [r[0] for r in rows]


def get(conn: sqlite3.Connection, hypothesis_id: str) -> dict[str, Any] | None:
    """Lectura para el panel (conexion de solo lectura). None si no hay fila o no existe la tabla."""
    try:
        cur = conn.execute("SELECT * FROM hypothesis_excursions WHERE hypothesis_id = ?", (hypothesis_id,))
        r = cur.fetchone()
    except sqlite3.OperationalError:
        return None
    if r is None:
        return None
    out = dict(zip([d[0] for d in cur.description], tuple(r)))
    out["stops"] = json.loads(out.get("stops") or "[]")
    out["partial_before"] = bool(out.get("partial_before"))
    out["descriptivo"] = NOT_PREREGISTERED
    return out


def write_missing(db_path: str | Path, pending_store: Any, source: CandleSource, *, now: datetime,
                  limit: int = 200) -> dict[str, Any]:
    """Calcula y guarda la excursion de las resueltas que no la tienen. Nunca levanta: los errores van al resumen."""
    from uuid import UUID

    st = ExcursionStore(db_path)
    out: dict[str, Any] = {"written": 0, "waiting": 0, "errors": []}
    now_ms = int(now.timestamp() * 1000)
    try:
        for hid in st.missing(limit):
            rec = pending_store.get_record(UUID(hid))
            if rec is None:
                continue
            try:
                row = compute(rec.hypothesis, source, now_ms=now_ms)
            except Exception as exc:  # noqa: BLE001 - red / datos: se reintenta en la proxima corrida
                out["errors"].append({"hypothesis_id": hid, "error": f"{type(exc).__name__}: {exc}"})
                continue
            if row is None:
                out["waiting"] += 1
            elif st.add(row):
                out["written"] += 1
    finally:
        st.close()
    out["errors"] = out["errors"][:20]
    return out


def summary(conn: sqlite3.Connection, strategy_id: str | None = None) -> list[dict[str, Any]]:
    """Por estrategia: n con excursion, MAE/MFE medianos en ATR y, para cada stop descriptivo, cuantas hubiera sacado
    antes y cuantas de esas terminaron acertando (el stop las habria cortado igual). Solo descriptivo."""
    q = ("SELECT p.strategy_id, r.win, e.mae_atr, e.mfe_atr, e.mae_pct, e.mfe_pct, e.stops FROM hypothesis_excursions e "
         "JOIN pending_hypotheses p ON p.hypothesis_id = e.hypothesis_id "
         "JOIN hypothesis_resolutions r ON r.hypothesis_id = e.hypothesis_id WHERE r.status = 'resolved'")
    args: list[Any] = []
    if strategy_id:
        q += " AND p.strategy_id = ?"
        args.append(strategy_id)
    try:
        rows = conn.execute(q, args).fetchall()
    except sqlite3.OperationalError:
        return []

    def med(xs: list[float]) -> float | None:
        xs = sorted(x for x in xs if x is not None)
        if not xs:
            return None
        m = len(xs) // 2
        return xs[m] if len(xs) % 2 else (xs[m - 1] + xs[m]) / 2

    by: dict[str, list[tuple]] = {}
    for r in rows:
        by.setdefault(r[0], []).append(tuple(r))
    out = []
    for sid, rs in sorted(by.items()):
        stops: dict[float, dict[str, Any]] = {}
        for r in rs:
            for s in json.loads(r[6] or "[]"):
                d = stops.setdefault(float(s["atr"]), {"atr": float(s["atr"]), "n": 0, "hit": 0, "hit_won": 0, "hit_lost": 0})
                d["n"] += 1
                if s.get("hit"):
                    d["hit"] += 1
                    d["hit_won" if r[1] else "hit_lost"] += 1
        out.append({"strategy_id": sid, "n": len(rs), "mae_atr_median": med([r[2] for r in rs]),
                    "mfe_atr_median": med([r[3] for r in rs]), "mae_pct_median": med([r[4] for r in rs]),
                    "mfe_pct_median": med([r[5] for r in rs]), "stops": [stops[k] for k in sorted(stops)]})
    return out
