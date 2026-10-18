"""
JSON del bot de paper trading para el panel (`GET /api/paperbot` y `/api/paperbot/{strategy_id}`).

Sin pandas ni nada de `heatsignal.lab`: corre dentro del proceso de ingesta de la VM. Lee `paperbot.sqlite` con una
conexion de solo lectura (llamar desde `asyncio.to_thread`).
"""

from __future__ import annotations

import json
import math
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .store import HOUR_MS, open_readonly

CAPITAL = 10_000.0
DISCLAIMER = ("SIMULADO: paper trading con dinero ficticio (10.000 USDT por estrategia, sin reinversion) y precios "
              "reales de Binance. No hay ordenes reales ni API keys. Ninguna de estas estrategias esta aprobada: el backtest "
              "o el holdout del laboratorio pueden descartarlas (ver el veredicto al lado de cada una); unas semanas de "
              "paper no prueban nada (muestra chica). Cada estrategia tiene su propio inicio: la que se sumo despues "
              "arranca con 10.000 US$ y su canasta desde su primera corrida.")
MAX_ROWS = 500
STALE_AFTER_MS = 2 * HOUR_MS + 10 * 60_000


def _iso(ms: int | None) -> str | None:
    return None if ms is None else datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def _num(v: Any) -> Any:
    return None if isinstance(v, float) and not math.isfinite(v) else v


def _row(r: sqlite3.Row) -> dict[str, Any]:
    return {k: _num(r[k]) for k in r.keys()}


def wilson(k: int, n: int, z: float = 1.959964) -> list[float] | None:
    if n <= 0:
        return None
    p = k / n
    den = 1 + z * z / n
    c = (p + z * z / (2 * n)) / den
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / den
    return [max(0.0, c - h), min(1.0, c + h)]


def run_json(r: sqlite3.Row | None, with_detail: bool = False) -> dict[str, Any] | None:
    if r is None:
        return None
    out = {"run_id": r["run_id"], "hour_ms": r["hour_ms"], "hour_at": _iso(r["hour_ms"]), "started_ms": r["started_ms"],
           "started_at": _iso(r["started_ms"]), "finished_ms": r["finished_ms"], "status": r["status"],
           "reason": r["reason"], "peak_rss_kb": r["peak_rss_kb"]}
    try:
        d = json.loads(r["detail"] or "{}")
    except ValueError:
        d = {}
    out["duration_s"] = d.get("duracion_s")
    out["mode"] = d.get("modo")
    if with_detail:
        d.pop("traceback", None)  # no se expone al panel (queda en la base y en el journal)
        out["detail"] = d
    return out


def _detail(r: sqlite3.Row | None) -> dict[str, Any]:
    if r is None:
        return {}
    try:
        return json.loads(r["detail"] or "{}")
    except ValueError:
        return {}


def _strategy_ids(conn: sqlite3.Connection) -> list[str]:
    return [r[0] for r in conn.execute("SELECT DISTINCT strategy_id FROM equity ORDER BY strategy_id")]


def _summary(conn: sqlite3.Connection, sid: str, last_ok_detail: dict[str, Any]) -> dict[str, Any] | None:
    e = conn.execute("SELECT * FROM equity WHERE strategy_id=? ORDER BY hour_ms DESC LIMIT 1", (sid,)).fetchone()
    if e is None:
        return None
    pos = conn.execute("SELECT * FROM positions WHERE strategy_id=? AND hour_ms=? ORDER BY symbol", (sid, e["hour_ms"])).fetchall()
    n_orders = conn.execute("SELECT COUNT(*), MAX(fill_ms) FROM orders WHERE strategy_id=?", (sid,)).fetchone()
    tr = conn.execute("SELECT COUNT(*), COALESCE(SUM(pnl_net > 0), 0), COALESCE(SUM(pnl_net), 0) FROM trades WHERE strategy_id=?",
                      (sid,)).fetchone()
    first = conn.execute("SELECT MIN(hour_ms), COUNT(*) FROM equity WHERE strategy_id=?", (sid,)).fetchone()
    spark = conn.execute("SELECT hour_ms, equity, basket_equity FROM (SELECT * FROM equity WHERE strategy_id=? "
                         "ORDER BY hour_ms DESC LIMIT 168) ORDER BY hour_ms", (sid,)).fetchall()
    info = (last_ok_detail.get("estrategias") or {}).get(sid) or {}
    eq, beq = e["equity"], e["basket_equity"]
    return {
        "strategy_id": sid, "spec_hash": info.get("spec_hash"), "params": info.get("params"), "origen": info.get("origen"),
        "universe": info.get("universe") or [p["symbol"] for p in pos], "capital": CAPITAL,
        "hour_ms": e["hour_ms"], "hour_at": _iso(e["hour_ms"]),
        "started_ms": first[0], "started_at": _iso(first[0]), "hours": first[1], "desde": info.get("desde"),
        "equity": eq, "return": eq / CAPITAL - 1, "basket_equity": beq,
        "basket_return": None if beq is None else beq / CAPITAL - 1,
        "realized": e["realized"], "unrealized": e["unrealized"], "funding": e["funding"], "fees": e["fees"],
        "slippage": e["slippage"], "exposure": e["exposure"],
        "positions": [{"symbol": p["symbol"], "weight": p["weight"], "target": p["target"], "units": p["units"],
                       "entry_price": p["entry_price"], "mark_price": p["mark_price"],
                       "notional": p["units"] * p["mark_price"], "unrealized": p["unrealized"]} for p in pos],
        "n_orders": n_orders[0], "last_order_ms": n_orders[1], "last_order_at": _iso(n_orders[1]),
        "n_trades": tr[0], "wins": tr[1], "trades_pnl": tr[2], "win_ci95": wilson(tr[1], tr[0]),
        "spark": [[r["hour_ms"], r["equity"], r["basket_equity"]] for r in spark],
    }


def overview(db_path: str | Path, *, now_ms: int | None = None) -> dict[str, Any]:
    """Resumen de todas las estrategias. FileNotFoundError si el bot no corrio nunca."""
    now = int(now_ms if now_ms is not None else time.time() * 1000)
    conn = open_readonly(db_path)
    try:
        runs = conn.execute("SELECT * FROM runs ORDER BY started_ms DESC LIMIT 72").fetchall()
        last_ok = conn.execute("SELECT * FROM runs WHERE status='ok' ORDER BY hour_ms DESC LIMIT 1").fetchone()
        counts = {r[0]: r[1] for r in conn.execute("SELECT status, COUNT(*) FROM runs GROUP BY status")}
        first = conn.execute("SELECT MIN(hour_ms) FROM runs WHERE status='ok'").fetchone()[0]
        gaps = conn.execute("SELECT * FROM gaps ORDER BY from_ms DESC LIMIT 50").fetchall()
        d = _detail(last_ok)
        strategies = [s for s in (_summary(conn, sid, d) for sid in _strategy_ids(conn)) if s is not None]
    finally:
        conn.close()
    last = runs[0] if runs else None
    since_ok = None if last_ok is None else now - last_ok["hour_ms"]
    notes = []
    if last is not None and last["status"] == "error":
        notes.append("La ultima corrida termino en error: " + str(last["reason"]) + ". No se opero en esa hora.")
    if since_ok is not None and since_ok > STALE_AFTER_MS:
        notes.append(f"Hace {since_ok // HOUR_MS} h que no hay una corrida ok: revisar heatsignal-paperbot.timer "
                     "(sudo journalctl -u heatsignal-paperbot -n 50).")
    return {
        "disclaimer": DISCLAIMER, "simulated": True, "read_at": _iso(now), "db": str(db_path),
        "started_ms": first, "started_at": _iso(first),
        "last_run": run_json(last, with_detail=True), "last_ok": run_json(last_ok),
        "stale": since_ok is None or since_ok > STALE_AFTER_MS,
        "counts": {"ok": counts.get("ok", 0), "error": counts.get("error", 0), "skipped": counts.get("skipped", 0)},
        "runs": [run_json(r) for r in runs],
        "gaps": [{"from_ms": g["from_ms"], "from_at": _iso(g["from_ms"]), "to_ms": g["to_ms"], "to_at": _iso(g["to_ms"]),
                  "hours": g["hours"], "run_id": g["run_id"]} for g in gaps],
        "strategies": strategies, "notes": notes,
    }


def strategy_detail(db_path: str | Path, strategy_id: str, *, now_ms: int | None = None) -> dict[str, Any] | None:
    """Detalle de una estrategia (None si no existe en la base)."""
    conn = open_readonly(db_path)
    try:
        last_ok = conn.execute("SELECT * FROM runs WHERE status='ok' ORDER BY hour_ms DESC LIMIT 1").fetchone()
        s = _summary(conn, strategy_id, _detail(last_ok))
        if s is None:
            return None
        eq = conn.execute("SELECT hour_ms, equity, basket_equity, realized, unrealized, funding, fees, slippage, exposure "
                          "FROM equity WHERE strategy_id=? ORDER BY hour_ms", (strategy_id,)).fetchall()
        pos = conn.execute("SELECT * FROM positions WHERE strategy_id=? AND hour_ms=? ORDER BY symbol",
                           (strategy_id, s["hour_ms"])).fetchall()
        orders = conn.execute("SELECT * FROM orders WHERE strategy_id=? ORDER BY fill_ms DESC, id DESC LIMIT ?",
                              (strategy_id, MAX_ROWS)).fetchall()
        trades = conn.execute("SELECT * FROM trades WHERE strategy_id=? ORDER BY exit_ms DESC LIMIT ?",
                              (strategy_id, MAX_ROWS)).fetchall()
        funding = conn.execute("SELECT * FROM funding_events WHERE strategy_id=? ORDER BY funding_ms DESC LIMIT ?",
                               (strategy_id, MAX_ROWS)).fetchall()
        basket = conn.execute("SELECT * FROM basket WHERE strategy_id=? ORDER BY symbol", (strategy_id,)).fetchall()
    finally:
        conn.close()
    by_symbol = []
    for p in pos:
        price = p["realized"] + p["unrealized"]
        by_symbol.append({"symbol": p["symbol"], "precio": price, "realizado": p["realized"], "no_realizado": p["unrealized"],
                          "funding": p["funding"], "comisiones": p["fees"], "slippage": p["slippage"],
                          "total": price + p["funding"] - p["fees"] - p["slippage"]})
    price = s["realized"] + s["unrealized"]
    s.update({
        "disclaimer": DISCLAIMER, "simulated": True,
        "equity_hourly": [[r["hour_ms"], r["equity"], r["basket_equity"]] for r in eq],
        "breakdown": {"precio": price, "realizado": s["realized"], "no_realizado": s["unrealized"], "funding": s["funding"],
                      "comisiones": s["fees"], "slippage": s["slippage"],
                      "total": price + s["funding"] - s["fees"] - s["slippage"]},
        "breakdown_by_symbol": by_symbol,
        "orders": [{**_row(o), "fill_at": _iso(o["fill_ms"]), "decision_at": _iso(o["decision_ms"])} for o in orders],
        "trades": [{**_row(t), "entry_at": _iso(t["entry_ms"]), "exit_at": _iso(t["exit_ms"])} for t in trades],
        "funding_events": [{**_row(f), "funding_at": _iso(f["funding_ms"])} for f in funding],
        "basket": [_row(b) for b in basket],
        "max_drawdown": _max_dd([r["equity"] for r in eq]),
    })
    return s


LAB_FIELDS = ("run", "oos_range", "oos_days", "sharpe", "sharpe_ci95", "dsr", "psr", "total_return", "max_drawdown",
              "trades", "hit_rate", "breakeven_bps", "basket_sharpe", "random_p", "holdout")


def lab_verdict(summary: Any, strategy_id: str, spec_hash: str | None) -> dict[str, Any] | None:
    """Veredicto del backtest del laboratorio (lab_summary.json publicado) para mostrar junto al paper, con el
    resultado del holdout si se abrio (`holdout_result`: Sharpe, retorno, trades, `confirma` y sus checks).

    `same_spec` = False si el backtest es de otra version de la regla (hash distinto): se avisa, no se esconde."""
    if not isinstance(summary, dict) or "error" in summary:
        return None
    for e in summary.get("strategies") or []:
        if isinstance(e, dict) and e.get("strategy_id") == strategy_id:
            v = e.get("verdict") or {}
            out = {k: e.get(k) for k in LAB_FIELDS}
            out.update({"spec_hash": e.get("spec_hash"), "pasa": v.get("pasa"), "checks": v.get("checks"),
                        "holdout_result": e.get("holdout_result"),
                        "criterios": v.get("criterios"), "generated_at": summary.get("generated_at"),
                        "same_spec": spec_hash is None or e.get("spec_hash") == spec_hash})
            return out
    return None


def _max_dd(values: list[float]) -> float | None:
    if not values:
        return None
    peak, dd = values[0], 0.0
    for v in values:
        peak = max(peak, v)
        dd = min(dd, v / peak - 1 if peak else 0.0)
    return dd
