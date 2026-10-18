"""
Vista de SOLO LECTURA de las hipotesis de 1h ejecutadas en el Freqtrade de hipotesis (dry-run), para el panel.

Cruza tres bases, todas abiertas en modo `?mode=ro`:
  - `ft_bridge.sqlite`        envios del puente (`paper/ft_bridge.py`): estado, trade_id, error;
  - `heatsignal_eval.sqlite`  la hipotesis y su resolucion (precio de referencia, precio de salida, PnL);
  - `freqtrade_hyp.sqlite`    el trade de Freqtrade (precio de entrada/salida reales del dry-run, comisiones,
                              funding, exit_reason), leido con `freqtrade_db.dump` (mismo esquema 2026.9).

Comparacion por operacion ("simulacion" = la resolucion de la hipotesis; "ejecucion" = el dry-run):
  entry_gap_bps   (open_rate de Freqtrade / precio de referencia de la hipotesis - 1) * 1e4   (precio crudo)
  exit_gap_bps    (close_rate de Freqtrade / precio de salida de la resolucion - 1) * 1e4
  entry_effect_bps / exit_effect_bps   los mismos gaps con el signo de la direccion: POSITIVO = a favor de
                  la operacion (entrar mas barato en un LONG, salir mas caro; al reves en un SHORT)
  ft_pnl_pct      close_profit de Freqtrade * 100 (neto de comisiones y funding, sobre el stake con leverage 1)
  ft_gross_pct    retorno de precio de Freqtrade en la direccion, sin costos
  pnl_diff_pp     ft_pnl_pct - pnl_pct de la hipotesis (neto con 10 bps): la distancia simulacion-ejecucion
  entry_delay_s   apertura del trade - emision de la hipotesis (el puente corre cada 5 min)
  exit_delay_s    cierre del trade - due_at (el bot revisa cada 60 s)

Solo libreria estandar (corre dentro del panel en la VM).
"""

from __future__ import annotations

import sqlite3
import time
import urllib.parse
from pathlib import Path
from typing import Any

from . import freqtrade_db as ftdb
from .ft_bridge import RETRY_MIN_LEFT_S, connect_ro, decode_tag, strategy_name

DISCLAIMER = ("SIMULADO: Freqtrade en DRY-RUN (sin plata real ni API keys de exchange), precios reales de Binance. "
              "Mide la distancia entre la resolucion de la hipotesis y una ejecucion simulada; no es una aprobacion "
              "de las estrategias.")
SMALL_N = 30
FINAL_LABEL = {"sent": "enviada", "skipped_pair_busy": "omitida: par ocupado", "skipped_late": "omitida: tarde",
               "skipped_unsupported": "omitida: par no soportado", "error": "error"}


def _f(v: Any) -> float | None:
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if x == x and x not in (float("inf"), float("-inf")) else None


def _mean(xs: list[float | None]) -> float | None:
    v = [x for x in xs if x is not None]
    return sum(v) / len(v) if v else None


def _bridge_rows(path: Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]], int | None]:
    p = Path(path).expanduser()
    if not p.is_file():
        raise FileNotFoundError(f"no existe la base del puente: {p}")
    conn = sqlite3.connect(f"file:{urllib.parse.quote(str(p.resolve()))}?mode=ro", uri=True, timeout=5.0)
    conn.row_factory = sqlite3.Row
    try:
        orders = [dict(r) for r in conn.execute("SELECT * FROM ft_bridge_orders ORDER BY id")]
        runs = [dict(r) for r in conn.execute("SELECT * FROM ft_bridge_runs ORDER BY ts_ms DESC LIMIT 20")]
        row = conn.execute("SELECT value FROM ft_bridge_meta WHERE key = 'first_run_ms'").fetchone()
    finally:
        conn.close()
    return orders, runs, int(row[0]) if row else None


def _eval_rows(eval_db: Path, ids: list[str]) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    """(estrategias de 1h, {hypothesis_id: hipotesis + resolucion})."""
    conn = connect_ro(eval_db)
    try:
        strategies = [dict(r) for r in conn.execute(
            "SELECT strategy_id, description, registered_at FROM strategy_hypotheses "
            "WHERE json_extract(exit_conditions, '$.timeframe') = '1h' ORDER BY registered_at, strategy_id")]
        hyps: dict[str, dict[str, Any]] = {}
        for i in range(0, len(ids), 400):
            chunk = ids[i:i + 400]
            q = ("SELECT p.hypothesis_id, p.entry_reference_price, p.created_ts, p.due_ts, p.confidence_score, "
                 "r.status, r.resolved_at, r.exit_price, r.pnl_pct, r.win, r.gross_pnl_pct, r.cost_bps, r.exit_price_ts "
                 "FROM pending_hypotheses p LEFT JOIN hypothesis_resolutions r ON r.hypothesis_id = p.hypothesis_id "
                 f"WHERE p.hypothesis_id IN ({','.join('?' * len(chunk))})")
            for r in conn.execute(q, chunk):
                hyps[r["hypothesis_id"]] = dict(r)
    finally:
        conn.close()
    return strategies, hyps


def _op(rows: list[dict[str, Any]], hyp: dict[str, Any] | None, trades: dict[int, dict[str, Any]],
        live: dict[Any, dict[str, Any]], now_ms: int) -> dict[str, Any]:
    final = next((r for r in rows if r["status"] != "error"), None)
    last = final or rows[-1]
    errors = [r for r in rows if r["status"] == "error"]
    sign = 1.0 if last["direction"] == "LONG" else -1.0
    due_ms = int(last["due_ts"]) * 1000
    if final is None:
        state = "error_reintenta" if (due_ms - now_ms) / 1000 > RETRY_MIN_LEFT_S else "error"
    else:
        state = final["status"]
    out: dict[str, Any] = {
        "hypothesis_id": last["hypothesis_id"], "id8": str(last["hypothesis_id"]).replace("-", "")[:8],
        "strategy_id": last["strategy_id"], "strategy_name": last["strategy_name"], "symbol": last["symbol"],
        "pair": last["pair"], "direction": last["direction"], "entry_tag": last["entry_tag"],
        "created_ms": int(last["created_ts"] * 1000), "due_ms": due_ms, "stake_usdt": last["stake_usdt"],
        "status": state, "status_label": FINAL_LABEL.get(state, "error (se reintenta)"),
        "attempts": len(rows), "errors": len(errors), "detail": last["detail"],
        "last_error": errors[-1]["detail"] if errors else None,
        "sent_ms": final["ts_ms"] if final and final["status"] == "sent" else None,
        "trade_id": final["trade_id"] if final else None,
    }
    h = hyp or {}
    ref = _f(h.get("entry_reference_price"))
    out["hyp"] = {
        "status": h.get("status") or ("pending" if hyp else None), "entry_ref": ref, "exit_price": _f(h.get("exit_price")),
        "pnl_pct": _f(h.get("pnl_pct")), "gross_pnl_pct": _f(h.get("gross_pnl_pct")), "cost_bps": _f(h.get("cost_bps")),
        "win": None if h.get("win") is None else bool(h["win"]), "resolved_at": h.get("resolved_at"),
        "confidence": _f(h.get("confidence_score")),
    }
    t = trades.get(out["trade_id"]) if out["trade_id"] is not None else None
    if t is not None:
        dec = decode_tag(t.get("enter_tag"))
        if dec is None or dec[0] != out["id8"]:
            out["trade_mismatch"] = f"el trade {t['id']} de Freqtrade tiene otro tag ({t.get('enter_tag')})"
            t = None
    ft: dict[str, Any] | None = None
    cmp: dict[str, Any] = {}
    if t is not None:
        lv = live.get(t["id"]) or {}
        o, c = t["open_rate"], t["close_rate"]
        fees = (t["fee_open_cost"] or 0.0) + (t["fee_close_cost"] or 0.0)
        ft = {
            "is_open": t["is_open"], "is_short": t["is_short"], "open_ms": t["open_ms"], "open_rate": o,
            "close_ms": t["close_ms"], "close_rate": c, "stake": t["stake_amount"], "leverage": t["leverage"],
            "pnl_abs": t["close_profit_abs"], "pnl_pct": None if t["close_profit"] is None else t["close_profit"] * 100,
            "gross_pct": sign * (c / o - 1) * 100 if o and c else None, "fees": fees, "funding": t["funding_fees"] or 0.0,
            "exit_reason": t["exit_reason"], "current_rate": _f(lv.get("current_rate")),
            "unrealized_pct": None if _f(lv.get("profit_ratio")) is None else _f(lv.get("profit_ratio")) * 100,
            "unrealized_abs": _f(lv.get("profit_abs")),
        }
        exit_px = out["hyp"]["exit_price"]
        if o and ref:
            cmp["entry_gap_bps"] = (o / ref - 1) * 1e4
            cmp["entry_effect_bps"] = -sign * cmp["entry_gap_bps"]
        if c and exit_px:
            cmp["exit_gap_bps"] = (c / exit_px - 1) * 1e4
            cmp["exit_effect_bps"] = sign * cmp["exit_gap_bps"]
        if ft["pnl_pct"] is not None and out["hyp"]["pnl_pct"] is not None:
            cmp["pnl_diff_pp"] = ft["pnl_pct"] - out["hyp"]["pnl_pct"]
        if ft["gross_pct"] is not None and out["hyp"]["gross_pnl_pct"] is not None:
            cmp["gross_diff_pp"] = ft["gross_pct"] - out["hyp"]["gross_pnl_pct"]
        if t["open_ms"]:
            cmp["entry_delay_s"] = (t["open_ms"] - out["created_ms"]) / 1000
        if t["close_ms"]:
            cmp["exit_delay_s"] = (t["close_ms"] - due_ms) / 1000
        if t["close_profit_abs"] is not None and out["hyp"]["win"] is not None:
            cmp["same_outcome"] = (t["close_profit_abs"] > 0) == out["hyp"]["win"]
        cmp["safety_stop"] = t["exit_reason"] == "stop_loss"
    out["ft"] = ft
    out["cmp"] = cmp or None
    return out


def overview(bridge_db: str | Path, eval_db: str | Path, ft_db: str | Path, *, api: dict[str, Any] | None = None,
             now_ms: int | None = None) -> dict[str, Any]:
    """Todo lo que muestra el panel. FileNotFoundError si falta la base del puente o la de evaluacion."""
    now_ms = now_ms or int(time.time() * 1000)
    orders, runs, first_run = _bridge_rows(Path(bridge_db))
    by_hyp: dict[str, list[dict[str, Any]]] = {}
    for r in orders:
        by_hyp.setdefault(r["hypothesis_id"], []).append(r)
    strategies, hyps = _eval_rows(Path(eval_db), list(by_hyp))
    notes: list[str] = []
    ft_ok, ft_error = True, None
    try:
        d = ftdb.dump(ft_db)
    except FileNotFoundError:
        d, ft_ok, ft_error = {"trades": []}, False, f"no existe la base del Freqtrade de hipotesis ({ft_db})"
    except sqlite3.Error as exc:
        d, ft_ok, ft_error = {"trades": []}, False, f"no se pudo leer la base del Freqtrade de hipotesis: {exc}"
    if ft_error:
        notes.append(ft_error + ": sin datos de ejecucion (solo los envios del puente).")
    trades = {t["id"]: t for t in d.get("trades", [])}
    api = api or {}
    live = {t.get("trade_id"): t for t in (api.get("status") or []) if isinstance(t, dict)}
    ops = [_op(rows, hyps.get(hid), trades, live, now_ms) for hid, rows in by_hyp.items()]
    ops.sort(key=lambda o: o["created_ms"], reverse=True)

    known = {o["trade_id"] for o in ops if o["trade_id"] is not None}
    orphans = [t["id"] for t in d.get("trades", []) if t["id"] not in known]
    if orphans:
        notes.append(f"{len(orphans)} trade(s) en Freqtrade sin envio del puente (ids {orphans[:10]}): no deberian "
                     "existir (la estrategia rechaza entradas sin tag del puente). Revisar.")

    names = {s["strategy_id"]: strategy_name(s["description"]) or s["strategy_id"] for s in strategies}
    for o in ops:
        names.setdefault(o["strategy_id"], o["strategy_name"] or o["strategy_id"])
    out_strats = []
    for sid, name in names.items():
        mine = [o for o in ops if o["strategy_id"] == sid]
        sent = [o for o in mine if o["status"] == "sent"]
        compared = [o for o in sent if o["cmp"] and o["cmp"].get("pnl_diff_pp") is not None]
        closed = [o for o in sent if o["ft"] and not o["ft"]["is_open"]]
        ft_wins = sum(1 for o in closed if (o["ft"]["pnl_abs"] or 0) > 0)
        out_strats.append({
            "strategy_id": sid, "name": name, "hypotheses": len(mine), "sent": len(sent),
            "pair_busy": sum(1 for o in mine if o["status"] == "skipped_pair_busy"),
            "late": sum(1 for o in mine if o["status"] == "skipped_late"),
            "unsupported": sum(1 for o in mine if o["status"] == "skipped_unsupported"),
            "errors": sum(1 for o in mine if o["status"].startswith("error")),
            "open": sum(1 for o in sent if o["ft"] and o["ft"]["is_open"]),
            "closed": len(closed), "ft_wins": ft_wins, "ft_ci95": ftdb.wilson(ft_wins, len(closed)),
            "ft_pnl_abs": sum(o["ft"]["pnl_abs"] or 0.0 for o in closed),
            "compared": len(compared),
            "hyp_wins": sum(1 for o in compared if o["hyp"]["win"]),
            "same_outcome": sum(1 for o in compared if o["cmp"].get("same_outcome")),
            "mean_pnl_diff_pp": _mean([o["cmp"]["pnl_diff_pp"] for o in compared]),
            "mean_entry_effect_bps": _mean([o["cmp"].get("entry_effect_bps") for o in compared]),
            "mean_exit_effect_bps": _mean([o["cmp"].get("exit_effect_bps") for o in compared]),
            "mean_entry_delay_s": _mean([(o["cmp"] or {}).get("entry_delay_s") for o in sent]),
            "mean_exit_delay_s": _mean([o["cmp"].get("exit_delay_s") for o in compared]),
            "safety_stops": sum(1 for o in sent if o["cmp"] and o["cmp"].get("safety_stop")),
            "ops": mine,
        })
    total_closed = sum(s["closed"] for s in out_strats)
    if total_closed < SMALL_N:
        notes.append(f"Muestra chica: {total_closed} operaciones cerradas en total. Con menos de {SMALL_N} por "
                     "estrategia las diferencias promedio no permiten sacar conclusiones.")
    if api and not api.get("ok"):
        notes.append("La API del Freqtrade de hipotesis no respondio: falta el precio actual de las abiertas."
                     + (f" ({api.get('error')})" if api.get("error") else ""))
    last = runs[0] if runs else None
    return {
        "disclaimer": DISCLAIMER,
        "now_ms": now_ms,
        "source": {"bridge_db": str(bridge_db), "ft_db": str(ft_db), "ft_available": ft_ok, "ft_error": ft_error,
                   "api": {"ok": bool(api.get("ok")), "url": api.get("url"), "error": api.get("error")} if api else None},
        "bridge": {"first_run_ms": first_run, "last_run": last, "runs": runs[:10],
                   "api_down_runs": sum(1 for r in runs if r["status"] == "api_down")},
        "totals": {"hypotheses": len(ops), "sent": sum(s["sent"] for s in out_strats),
                   "skipped": sum(s["pair_busy"] + s["late"] + s["unsupported"] for s in out_strats),
                   "errors": sum(s["errors"] for s in out_strats), "open": sum(s["open"] for s in out_strats),
                   "closed": total_closed, "ft_pnl_abs": sum(s["ft_pnl_abs"] for s in out_strats)},
        "strategies": out_strats,
        "notes": notes,
    }
