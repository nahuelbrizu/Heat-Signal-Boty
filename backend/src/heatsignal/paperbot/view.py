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

from .params import ROLE_LABEL, ROLES
from .store import HOUR_MS, open_readonly

CAPITAL = 10_000.0
DISCLAIMER = ("SIMULADO: paper trading con dinero ficticio (10.000 USDT por estrategia, sin reinversion) y precios "
              "reales de Binance. No hay ordenes reales ni API keys. Ninguna de estas estrategias esta aprobada: el backtest "
              "o el holdout del laboratorio pueden descartarlas (ver el veredicto al lado de cada una); unas semanas de "
              "paper no prueban nada (muestra chica). Cada estrategia tiene su propio inicio: la que se sumo despues "
              "arranca con 10.000 US$ y su canasta desde su primera corrida.")
MAX_ROWS = 500
DAY_MS = 86_400_000

# ---- prueba hacia adelante (decisions.md, 2026-10-20): roles, Sharpe y criterio pre-registrado ---------------------
SEGUIMIENTO_MARCA = "exploratorio; corrección BH al evaluar"
CRITERIO_TEXTO = {
    "long": "Pre-registrado: a los 3 meses, Sharpe > 0 y > el de la canasta igual-ponderada del mismo período; a los "
            "6 meses, además p ≤ 0,05 de que le gana a la canasta (bootstrap pareado por bloques de 5 días).",
    "short": "Pre-registrado (simétrico, enmienda 2026-10-20): a los 3 meses, Sharpe > 0 y > el de la canasta en corto "
             "(> −Sharpe de la canasta); a los 6 meses, además p ≤ 0,05 (bootstrap pareado por bloques de 5 días).",
    "neta": "Pre-registrado (enmienda 2, 2026-10-20): a los 3 meses, Sharpe > 0 y > el de la canasta; a los 6 meses, "
            "además p ≤ 0,05 de que le gana a la canasta (bootstrap pareado por bloques de 5 días).",
}
LECTURA_CONJUNTA = ("Lectura conjunta pre-registrada (long y short): si las dos familias ganan cuando el mercado va a su "
                    "favor y pierden poco cuando va en contra → la regla tiene ventaja propia; si solo gana el lado del "
                    "mercado del período → es beta, no ventaja.")
BOOT_N = 2000
BOOT_BLOCK = 5
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


def _has_exits(conn: sqlite3.Connection) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='exit_reasons'").fetchone() is not None


def exit_counts(conn: sqlite3.Connection, sid: str, since_ms: int | None = None, until_ms: int | None = None) -> dict[str, Any]:
    """Trades cerrados por motivo (tabla `exit_reasons`, aditiva desde 2026-10-19). Los trades anteriores al registro
    quedan como `sin_dato` (no se reconstruyen hacia atras)."""
    where, args = "t.strategy_id=?", [sid]
    if since_ms is not None:
        where += " AND t.exit_ms >= ?"
        args.append(since_ms)
    if until_ms is not None:
        where += " AND t.exit_ms < ?"
        args.append(until_ms)
    if not _has_exits(conn):
        n = conn.execute(f"SELECT COUNT(*) FROM trades t WHERE {where}", args).fetchone()[0]
        return {"sin_dato": n} if n else {}
    out: dict[str, int] = {}
    for m, n in conn.execute(
            "SELECT COALESCE((SELECT e.motivo FROM exit_reasons e WHERE e.strategy_id=t.strategy_id AND e.symbol=t.symbol "
            f"AND e.fill_ms=t.exit_ms ORDER BY e.decision_ms DESC LIMIT 1), 'sin_dato'), COUNT(*) FROM trades t WHERE {where} "
            "GROUP BY 1", args):
        out[m] = n
    return out


def _ficha(sid: str) -> dict[str, Any] | None:
    from ..lab.exits import ficha_json  # sin pandas

    return ficha_json(sid)


def _add_months(ms: int, n: int) -> int:
    d = datetime.fromtimestamp(ms / 1000, tz=timezone.utc)
    y, m = divmod(d.month - 1 + n, 12)
    y, m = d.year + y, m + 1
    days = [31, 29 if (y % 4 == 0 and (y % 100 or y % 400 == 0)) else 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31][m - 1]
    return int(d.replace(year=y, month=m, day=min(d.day, days)).timestamp() * 1000)


def daily_returns(rows: list[tuple[int, float, float | None]], start_ms: int, end_ms: int) -> tuple[list[float], list[float]]:
    """Retornos diarios (cierre 00:00 UTC) de la estrategia y de su canasta entre su inicio (10.000) y `end`.
    `rows` = (hour_ms, equity, basket_equity) ordenadas. Una hora sin corrida arrastra el ultimo valor conocido."""
    pts = [(CAPITAL, CAPITAL)]
    d = start_ms - start_ms % DAY_MS + DAY_MS
    j, last = 0, None
    while d <= end_ms:
        while j < len(rows) and rows[j][0] <= d:
            last = rows[j]
            j += 1
        if last is not None:
            pts.append((last[1], last[2] if last[2] is not None else pts[-1][1]))
        d += DAY_MS
    rs = [b[0] / a[0] - 1 for a, b in zip(pts, pts[1:])]
    rb = [b[1] / a[1] - 1 for a, b in zip(pts, pts[1:])]
    return rs, rb


def sharpe(r: list[float]) -> float | None:
    """Sharpe anualizado (sqrt(365)) de retornos diarios; None con menos de 2 dias o sin variacion."""
    if len(r) < 2:
        return None
    m = sum(r) / len(r)
    sd = math.sqrt(sum((x - m) ** 2 for x in r) / (len(r) - 1))
    return None if sd <= 1e-15 else m / sd * math.sqrt(365)


def block_bootstrap_p(diff: list[float], block: int = BOOT_BLOCK, n_boot: int = BOOT_N, seed: int = 20261020) -> float | None:
    """p unilateral de que la media de `diff` (estrategia - referencia, pareado por dia) sea > 0: bootstrap circular por
    bloques de `block` dias sobre la serie centrada (H0: media 0). Determinista (semilla fija)."""
    import random

    n = len(diff)
    if n < 2 * block:
        return None
    obs = sum(diff) / n
    c = [x - obs for x in diff]
    rng = random.Random(seed)
    k = -(-n // block)
    hits = 0
    for _ in range(n_boot):
        tot, cnt = 0.0, 0
        for _b in range(k):
            i0 = rng.randrange(n)
            for i in range(block):
                if cnt == n:
                    break
                tot += c[(i0 + i) % n]
                cnt += 1
        hits += tot / n >= obs
    return (1 + hits) / (1 + n_boot)


def _side(sid: str) -> str:
    return "short" if "-short-" in sid else ("neta" if "-neta-" in sid else "long")


def criterio(rows: list[tuple[int, float, float | None]], sid: str, start_ms: int, last_ms: int) -> dict[str, Any]:
    """Criterio pre-registrado de una hipotesis principal: dias desde el inicio y, desde los 3 meses, el veredicto (las
    ventanas son fijas: inicio a inicio + 3 meses e inicio + 6 meses; no se mueven con el tiempo)."""
    side = _side(sid)
    h3, h6 = _add_months(start_ms, 3), _add_months(start_ms, 6)
    out: dict[str, Any] = {"lado": side, "texto": CRITERIO_TEXTO[side], "inicio_ms": start_ms, "inicio_at": _iso(start_ms),
                           "dias": (last_ms - start_ms) // DAY_MS, "hito_3m_ms": h3, "hito_3m_at": _iso(h3),
                           "hito_6m_ms": h6, "hito_6m_at": _iso(h6), "veredicto_3m": None, "veredicto_6m": None}

    def judge(end: int, with_p: bool) -> dict[str, Any]:
        rs, rb = daily_returns(rows, start_ms, end)
        ref = [-x for x in rb] if side == "short" else rb  # la canasta en corto para la familia short
        s, sb = sharpe(rs), sharpe(ref)
        checks = {"sharpe_positivo": s is not None and s > 0,
                  "le_gana_a_la_canasta": s is not None and sb is not None and s > sb}
        v: dict[str, Any] = {"dias": len(rs), "sharpe": s, "sharpe_referencia": sb, "checks": checks}
        if with_p:
            v["p"] = block_bootstrap_p([a - b for a, b in zip(rs, ref)])
            checks["p_menor_igual_005"] = v["p"] is not None and v["p"] <= 0.05
        v["pasa"] = all(checks.values())
        return v

    cur = judge(last_ms, False)
    out["en_curso"] = {k: cur[k] for k in ("dias", "sharpe", "sharpe_referencia")}
    if last_ms >= h3:
        out["veredicto_3m"] = judge(h3, False)
    if last_ms >= h6:
        out["veredicto_6m"] = judge(h6, True)
    out["estado"] = ("6m: " + ("pasa" if out["veredicto_6m"]["pasa"] else "no pasa")) if out["veredicto_6m"] else (
        ("3m: " + ("pasa" if out["veredicto_3m"]["pasa"] else "no pasa (se registra)")) if out["veredicto_3m"] else "en curso")
    return out


def _params_roles() -> dict[str, str]:
    try:
        from .params import load, role

        return {sid: role(e) for sid, e in load().items()}
    except Exception:  # noqa: BLE001 - sin params.json legible, el rol sale del detalle de la corrida
        return {}


def _rol(sid: str, info: dict[str, Any], roles: dict[str, str]) -> str:
    r = info.get("rol") or roles.get(sid) or "paper_previo"
    return r if r in ROLES else "paper_previo"


def _strategy_ids(conn: sqlite3.Connection) -> list[str]:
    return [r[0] for r in conn.execute("SELECT DISTINCT strategy_id FROM equity ORDER BY strategy_id")]


def _summary(conn: sqlite3.Connection, sid: str, last_ok_detail: dict[str, Any],
             roles: dict[str, str] | None = None) -> dict[str, Any] | None:
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
    rol = _rol(sid, info, roles if roles is not None else _params_roles())
    series = [(r[0], r[1], r[2]) for r in conn.execute(
        "SELECT hour_ms, equity, basket_equity FROM equity WHERE strategy_id=? ORDER BY hour_ms", (sid,))]
    rs, rb = daily_returns(series, first[0], e["hour_ms"])
    forward = {"rol": rol, "rol_label": ROLE_LABEL[rol], "estado": info.get("estado") or "ok", "motivo": info.get("motivo"),
               "votos": info.get("votos"),
               "sharpe": {"valor": sharpe(rs), "canasta": sharpe(rb), "dias": len(rs),
                          "marca": SEGUIMIENTO_MARCA if rol == "seguimiento" else None},
               "criterio": criterio(series, sid, first[0], e["hour_ms"]) if rol == "principal" else None}
    return {**forward,
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
        "exits": _ficha(sid), "exit_counts": exit_counts(conn, sid),
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
        roles = _params_roles()
        strategies = [s for s in (_summary(conn, sid, d, roles) for sid in _strategy_ids(conn)) if s is not None]
        have = {s["strategy_id"] for s in strategies}
        # sin fallos silenciosos: las que en la ultima corrida ok quedaron sin datos (y las que nunca tuvieron) se listan
        sin_datos = [{"strategy_id": sid, "rol": _rol(sid, v, roles), "motivo": v.get("motivo"),
                      "nunca_corrio": sid not in have}
                     for sid, v in (d.get("estrategias") or {}).items() if v.get("estado") == "sin_datos"]
    finally:
        conn.close()
    last = runs[0] if runs else None
    since_ok = None if last_ok is None else now - last_ok["hour_ms"]
    notes = []
    if last is not None and last["status"] == "error":
        notes.append("La ultima corrida termino en error: " + str(last["reason"]) + ". No se opero en esa hora.")
    if sin_datos:
        notes.append("Sin datos en la última corrida ok (no operaron esa hora): " + "; ".join(
            f"{x['strategy_id']}: {x['motivo']}" for x in sin_datos))
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
        "strategies": strategies, "notes": notes, "sin_datos": sin_datos,
        "roles": [{"rol": r, "label": ROLE_LABEL[r]} for r in ROLES],
        "lectura_conjunta": LECTURA_CONJUNTA, "marca_seguimiento": SEGUIMIENTO_MARCA,
    }


def strategy_detail(db_path: str | Path, strategy_id: str, *, now_ms: int | None = None) -> dict[str, Any] | None:
    """Detalle de una estrategia (None si no existe en la base)."""
    conn = open_readonly(db_path)
    try:
        last_ok = conn.execute("SELECT * FROM runs WHERE status='ok' ORDER BY hour_ms DESC LIMIT 1").fetchone()
        s = _summary(conn, strategy_id, _detail(last_ok), _params_roles())
        if s is None:
            return None
        eq = conn.execute("SELECT hour_ms, equity, basket_equity, realized, unrealized, funding, fees, slippage, exposure "
                          "FROM equity WHERE strategy_id=? ORDER BY hour_ms", (strategy_id,)).fetchall()
        pos = conn.execute("SELECT * FROM positions WHERE strategy_id=? AND hour_ms=? ORDER BY symbol",
                           (strategy_id, s["hour_ms"])).fetchall()
        orders = conn.execute("SELECT * FROM orders WHERE strategy_id=? ORDER BY fill_ms DESC, id DESC LIMIT ?",
                              (strategy_id, MAX_ROWS)).fetchall()
        motivo = ("(SELECT e.motivo FROM exit_reasons e WHERE e.strategy_id=t.strategy_id AND e.symbol=t.symbol AND "
                  "e.fill_ms=t.exit_ms ORDER BY e.decision_ms DESC LIMIT 1)") if _has_exits(conn) else "NULL"
        trades = conn.execute(f"SELECT t.*, {motivo} AS motivo FROM trades t WHERE t.strategy_id=? ORDER BY t.exit_ms DESC LIMIT ?",
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
