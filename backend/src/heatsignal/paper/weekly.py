"""
Revision semanal automatica (cierre de la semana UTC lunes 00:00 -> lunes 00:00), por codigo y sin LLM.

    python -m heatsignal.paper.weekly run                 # cierra la ultima semana completa (heatsignal-weekly.timer,
                                                          # lunes 00:30 UTC); si ya estaba cerrada no hace nada
    python -m heatsignal.paper.weekly run --week 2026-W42 # una semana puntual (tiene que estar terminada)
    python -m heatsignal.paper.weekly run --dry-run       # calcula e imprime sin escribir (medir en la VM)
    python -m heatsignal.paper.weekly list | show 2026-W42

Junta cinco fuentes, todas abiertas en SOLO LECTURA (`?mode=ro`):
  (a) bot propio de paper (`paperbot.sqlite`): PnL de la semana y acumulado por estrategia contra su canasta, trades,
      costos y funding, corridas con error y huecos;
  (b) Freqtrade de Donchian (`freqtrade_dryrun.sqlite`) + la ultima comparacion paper vs backtest publicada
      (`paper_summary.json`);
  (c) Freqtrade de hipotesis (`freqtrade_hyp.sqlite` + `ft_bridge.sqlite`, via `ft_bridge_view.overview`): enviadas,
      omitidas, PnL y la diferencia con la resolucion de la hipotesis;
  (d) hipotesis del grafo (`heatsignal_eval.sqlite`): emitidas y resueltas en la semana, tasa con IC de Wilson, alfa
      contra mantener, cortes por estrategia, tendencia y sesion (`evaluation.segments.trend_segments`, el mismo
      corte que usa `evaluation/report.py`) y el ultimo estado shadow/publicable del reporte (`strategy_reports`);
  (e) salud: minutos de `bars_1m` y `klines` 1m que faltan, ciclos del grafo por estado, sesiones con/sin LLM.
      Las llamadas al LLM NO se pueden contar: `cycle_log` no guarda cuantas hubo por ciclo (se declara).

Reglas: lenguaje historico ("en la semana", nunca "va a"); toda tasa con n e IC 95 % de Wilson; con menos de
SMALL_N casos se marca "muestra chica"; siempre el aviso de que una semana no prueba nada.

Persistencia: tabla `weekly_reviews` en `heatsignal_eval.sqlite` (append-only con triggers; PRIMARY KEY = semana ISO:
una sola fila por semana, `run` sobre una semana ya cerrada no escribe nada). Se eligio esa base y no una propia
porque ya es la base de "resultados" (hipotesis, reportes, diario de sesiones), entra en el respaldo semanal, el panel
ya la tiene abierta y una fila por semana es despreciable. Si una fuente falla, la fila queda con status 'partial' y el
error de esa fuente escrito (sin fallos silenciosos); NO se reintenta ni se reescribe: la revision es la foto que se
pudo sacar ese lunes.

Solo libreria estandar (corre en el venv de la ingesta de la VM, sin pandas); la vista del panel usa `list_reviews` y
`get_review` de este mismo modulo.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import sqlite3
import sys
import time
import urllib.parse
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from ..evaluation.segments import trend_segments, wilson
from ..paperbot.view import LECTURA_CONJUNTA
from . import freqtrade_db as ftdb

MODEL_ID = "weekly-review-v1"
WEEK_MS = 7 * 86_400_000
HOUR_MS = 3_600_000
MIN_MS = 60_000
SMALL_N = 30
CAPITAL = 10_000.0          # capital ficticio por estrategia del paperbot (paperbot/ledger.py)
FT_WALLET = 10_000.0        # dry_run_wallet de los dos Freqtrade
WEEK_RE = re.compile(r"^(\d{4})-W(\d{2})$")
DEFAULT_SYMBOLS = ("BTCUSDT", "SOLUSDT", "XRPUSDT", "ETHUSDT", "XLMUSDT", "BNBUSDT", "TRXUSDT")
ONE_WEEK_NOTE = ("Una semana no prueba nada: son resultados historicos de 7 dias, con muestras chicas. Sirven para ver "
                 "que todo corre y para comparar semana contra semana, no para aprobar ni descartar una estrategia.")

SCHEMA = """
CREATE TABLE IF NOT EXISTS weekly_reviews (
    week          TEXT    PRIMARY KEY,      -- semana ISO '2026-W42' (lunes 00:00 UTC a lunes 00:00 UTC)
    week_start_ms INTEGER NOT NULL,
    week_end_ms   INTEGER NOT NULL,
    generated_ms  INTEGER NOT NULL,
    model_id      TEXT    NOT NULL,         -- version del calculo (weekly-review-v1)
    status        TEXT    NOT NULL CHECK (status IN ('ok', 'partial')),
    summary       TEXT    NOT NULL,         -- JSON con las 5 fuentes, metricas planas y avisos
    text          TEXT    NOT NULL          -- resumen redactado por codigo (sin LLM)
);
CREATE TRIGGER IF NOT EXISTS trg_weekly_reviews_no_update BEFORE UPDATE ON weekly_reviews
BEGIN SELECT RAISE(ABORT, 'weekly_reviews es append-only: UPDATE no permitido'); END;
CREATE TRIGGER IF NOT EXISTS trg_weekly_reviews_no_delete BEFORE DELETE ON weekly_reviews
BEGIN SELECT RAISE(ABORT, 'weekly_reviews es append-only: DELETE no permitido'); END;
"""


# ---- semana UTC ------------------------------------------------------------------------------------------


def iso(ms: int | float | None) -> str | None:
    if ms is None:
        return None
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def week_id(start_ms: int) -> str:
    y, w, _ = datetime.fromtimestamp(start_ms / 1000, tz=timezone.utc).isocalendar()
    return f"{y}-W{w:02d}"


def week_bounds(week: str) -> tuple[int, int]:
    """'2026-W42' -> (lunes 00:00 UTC, lunes siguiente 00:00 UTC) en epoch ms. ValueError si no es valida."""
    m = WEEK_RE.match(week or "")
    if not m:
        raise ValueError(f"semana invalida: {week!r} (formato 2026-W42)")
    try:
        start = datetime.fromisocalendar(int(m.group(1)), int(m.group(2)), 1).replace(tzinfo=timezone.utc)
    except ValueError as exc:
        raise ValueError(f"semana invalida: {week!r} ({exc})") from None
    s = int(start.timestamp() * 1000)
    return s, s + WEEK_MS


def last_complete_week(now_ms: int) -> str:
    """La ultima semana UTC (lunes a domingo) que ya termino en `now`."""
    now = datetime.fromtimestamp(now_ms / 1000, tz=timezone.utc)
    monday = (now - timedelta(days=now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
    return week_id(int((monday - timedelta(days=7)).timestamp() * 1000))


# ---- utilidades ------------------------------------------------------------------------------------------


def _ro(path: str | Path) -> sqlite3.Connection:
    p = Path(path).expanduser()
    if not p.is_file():
        raise FileNotFoundError(f"no existe {p}")
    conn = sqlite3.connect(f"file:{urllib.parse.quote(str(p.resolve()))}?mode=ro", uri=True, timeout=10.0)
    conn.row_factory = sqlite3.Row
    return conn


def _has_table(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None


def _f(v: Any) -> float | None:
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) else None


def _mean(xs: list[Any]) -> float | None:
    v = [x for x in (_f(x) for x in xs) if x is not None]
    return sum(v) / len(v) if v else None


def _rate(wins: int, n: int) -> dict[str, Any]:
    """Tasa honesta: siempre con n, IC 95 % de Wilson y la marca de muestra chica."""
    return {"n": n, "wins": wins, "rate": wins / n if n else None, "ci95": wilson(wins, n), "small_sample": n < SMALL_N}


def _parse_iso_ms(s: Any) -> int | None:
    if not s:
        return None
    try:
        d = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except ValueError:
        return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return int(d.timestamp() * 1000)


def _source(fn, *args, **kw) -> dict[str, Any]:
    """Corre una fuente; si falla queda {available: False, error} (la revision sigue con las demas)."""
    try:
        return {"available": True, **fn(*args, **kw)}
    except FileNotFoundError as exc:
        return {"available": False, "error": str(exc)}
    except (sqlite3.Error, OSError, ValueError, KeyError, TypeError) as exc:
        return {"available": False, "error": f"{type(exc).__name__}: {exc}"}


def _max_dd(values: list[float]) -> float | None:
    if not values:
        return None
    peak, dd = values[0], 0.0
    for v in values:
        peak = max(peak, v)
        dd = min(dd, v / peak - 1 if peak else 0.0)
    return dd


# ---- (a) bot propio de paper -----------------------------------------------------------------------------


def paperbot_week(db: str | Path, ws: int, we: int) -> dict[str, Any]:
    conn = _ro(db)
    try:
        sids = [r[0] for r in conn.execute("SELECT DISTINCT strategy_id FROM equity WHERE hour_ms <= ? ORDER BY 1", (we,))]
        # rol de la prueba hacia adelante (decisions.md 2026-10-20): del detalle de la ultima corrida ok de la semana
        from ..paperbot import view as pbv

        last = conn.execute("SELECT detail FROM runs WHERE status='ok' AND hour_ms < ? ORDER BY hour_ms DESC LIMIT 1",
                            (we,)).fetchone()
        try:
            info_all = (json.loads(last[0] or "{}").get("estrategias") or {}) if last else {}
        except ValueError:
            info_all = {}
        roles = pbv._params_roles()
        sin_datos = sorted(k for k, v in info_all.items() if v.get("estado") == "sin_datos")
        strategies = []
        for sid in sids:
            first = conn.execute("SELECT MIN(hour_ms) FROM equity WHERE strategy_id=?", (sid,)).fetchone()[0]
            start = conn.execute("SELECT * FROM equity WHERE strategy_id=? AND hour_ms <= ? ORDER BY hour_ms DESC LIMIT 1",
                                 (sid, ws)).fetchone()
            end = conn.execute("SELECT * FROM equity WHERE strategy_id=? AND hour_ms <= ? ORDER BY hour_ms DESC LIMIT 1",
                               (sid, we)).fetchone()
            series = [r[0] for r in conn.execute(
                "SELECT equity FROM equity WHERE strategy_id=? AND hour_ms > ? AND hour_ms <= ? ORDER BY hour_ms", (sid, ws, we))]
            zero = {"equity": CAPITAL, "basket_equity": CAPITAL, "realized": 0.0, "unrealized": 0.0, "funding": 0.0,
                    "fees": 0.0, "slippage": 0.0}
            s0 = dict(start) if start is not None else zero
            e1 = dict(end)

            def d(k: str, s0=s0, e1=e1) -> float | None:
                a, b = _f(s0.get(k)), _f(e1.get(k))
                return None if a is None or b is None else b - a

            tr = conn.execute("SELECT COUNT(*), COALESCE(SUM(pnl_net > 0), 0), COALESCE(SUM(pnl_net), 0), "
                              "COALESCE(SUM(fees + slippage), 0), COALESCE(SUM(funding), 0) FROM trades "
                              "WHERE strategy_id=? AND exit_ms >= ? AND exit_ms < ?", (sid, ws, we)).fetchone()
            tr_all = conn.execute("SELECT COUNT(*), COALESCE(SUM(pnl_net > 0), 0) FROM trades WHERE strategy_id=? AND exit_ms < ?",
                                  (sid, we)).fetchone()
            orders = {r[0]: r[1] for r in conn.execute(
                "SELECT reason, COUNT(*) FROM orders WHERE strategy_id=? AND fill_ms >= ? AND fill_ms < ? GROUP BY reason",
                (sid, ws, we))}
            from ..paperbot.view import exit_counts

            pnl = d("equity")
            bpnl = d("basket_equity")
            rol = pbv._rol(sid, info_all.get(sid) or {}, roles)
            eq_rows = [(r[0], r[1], r[2]) for r in conn.execute(
                "SELECT hour_ms, equity, basket_equity FROM equity WHERE strategy_id=? AND hour_ms <= ? ORDER BY hour_ms", (sid, we))]
            rs, rb = pbv.daily_returns(eq_rows, first, e1["hour_ms"])
            strategies.append({
                "strategy_id": sid, "rol": rol, "rol_label": pbv.ROLE_LABEL[rol],
                "sharpe_cum": {"valor": pbv.sharpe(rs), "canasta": pbv.sharpe(rb), "dias": len(rs),
                               "marca": pbv.SEGUIMIENTO_MARCA if rol == "seguimiento" else None},
                "criterio": pbv.criterio(eq_rows, sid, first, e1["hour_ms"]) if rol == "principal" else None,
                "started_ms": first, "started_at": iso(first), "started_in_week": first > ws,
                "end_hour_ms": e1["hour_ms"], "end_hour_at": iso(e1["hour_ms"]), "hours_in_week": len(series),
                "pnl_week": pnl, "return_week": None if pnl is None else pnl / CAPITAL,
                "basket_pnl_week": bpnl, "basket_return_week": None if bpnl is None else bpnl / CAPITAL,
                "vs_basket_week": None if pnl is None or bpnl is None else pnl - bpnl,
                "price_pnl_week": None if d("realized") is None or d("unrealized") is None else d("realized") + d("unrealized"),
                "funding_week": d("funding"), "fees_week": d("fees"), "slippage_week": d("slippage"),
                "costs_week": None if d("fees") is None or d("slippage") is None else d("fees") + d("slippage"),
                "max_dd_week": _max_dd([s0["equity"], *series]) if series else None,
                "equity_end": e1["equity"], "pnl_cum": e1["equity"] - CAPITAL, "return_cum": e1["equity"] / CAPITAL - 1,
                "basket_cum": None if e1["basket_equity"] is None else e1["basket_equity"] - CAPITAL,
                "vs_basket_cum": None if e1["basket_equity"] is None else e1["equity"] - e1["basket_equity"],
                "exposure_end": e1["exposure"],
                "orders_week": sum(orders.values()), "orders_by_reason": orders,
                "trades_week": {**_rate(tr[1], tr[0]), "pnl_net": tr[2], "costs": tr[3], "funding": tr[4]},
                "exits_week": exit_counts(conn, sid, ws, we),  # cierres por motivo (stop/objetivo/tiempo/senal/...)
                "trades_cum": _rate(tr_all[1], tr_all[0]),
            })
        runs = {r[0]: r[1] for r in conn.execute(
            "SELECT status, COUNT(*) FROM runs WHERE hour_ms >= ? AND hour_ms < ? GROUP BY status", (ws, we))}
        errors = [{"hour_ms": r["hour_ms"], "hour_at": iso(r["hour_ms"]), "reason": str(r["reason"] or "")[:240]}
                  for r in conn.execute("SELECT hour_ms, reason FROM runs WHERE status='error' AND hour_ms >= ? AND "
                                        "hour_ms < ? ORDER BY hour_ms", (ws, we))]
        ok_hours = conn.execute("SELECT COUNT(DISTINCT hour_ms) FROM runs WHERE status='ok' AND hour_ms >= ? AND hour_ms < ?",
                                (ws, we)).fetchone()[0]
        first_ok = conn.execute("SELECT MIN(hour_ms) FROM runs WHERE status='ok'").fetchone()[0]
        gaps = [dict(g) for g in conn.execute("SELECT from_ms, to_ms, hours FROM gaps WHERE from_ms < ? AND to_ms >= ? "
                                              "ORDER BY from_ms", (we, ws))]
    finally:
        conn.close()
    expected = 0 if first_ok is None else max(0, (we - max(ws, first_ok)) // HOUR_MS)
    return {
        "strategies": strategies,
        "runs": {"ok": runs.get("ok", 0), "error": runs.get("error", 0), "skipped": runs.get("skipped", 0),
                 "ok_hours": ok_hours, "expected_hours": expected, "missing_hours": max(0, expected - ok_hours)},
        "errors": errors[:20], "n_errors": len(errors),
        "gaps": [{**g, "from_at": iso(g["from_ms"]), "to_at": iso(g["to_ms"])} for g in gaps],
        "capital": CAPITAL, "sin_datos_ultima_corrida": sin_datos,
    }


# ---- (b) Freqtrade de Donchian ---------------------------------------------------------------------------


def _ft_closed(trades: list[dict[str, Any]], ws: int | None, we: int) -> list[dict[str, Any]]:
    return [t for t in trades if not t["is_open"] and t["close_ms"] is not None and t["close_ms"] < we
            and (ws is None or t["close_ms"] >= ws)]


def _ft_block(closed: list[dict[str, Any]]) -> dict[str, Any]:
    wins = sum(1 for t in closed if (t["close_profit_abs"] or 0) > 0)
    return {**_rate(wins, len(closed)), "pnl": sum(t["close_profit_abs"] or 0.0 for t in closed),
            "fees": sum((t["fee_open_cost"] or 0.0) + (t["fee_close_cost"] or 0.0) for t in closed),
            "funding": sum(t["funding_fees"] or 0.0 for t in closed)}


def freqtrade_week(db: str | Path, ws: int, we: int, summary_path: str | Path | None = None) -> dict[str, Any]:
    d = ftdb.dump(db)
    trades = d.get("trades") or []
    week = _ft_closed(trades, ws, we)
    cum = _ft_closed(trades, None, we)
    open_end = [t for t in trades if t["open_ms"] is not None and t["open_ms"] < we
                and (t["is_open"] or (t["close_ms"] is not None and t["close_ms"] >= we))]
    opened = [t for t in trades if t["open_ms"] is not None and ws <= t["open_ms"] < we]
    bw, bc = _ft_block(week), _ft_block(cum)
    return {
        "exits_week": _count(week, "exit_reason"),  # exit_reason de Freqtrade (exit_signal, stop_loss, ...)
        "wallet": FT_WALLET, "bot_start_ms": (d.get("kv") or {}).get("bot_start_time"),
        "week": {**bw, "return": bw["pnl"] / FT_WALLET, "opened": len(opened)},
        "cum": {**bc, "return": bc["pnl"] / FT_WALLET},
        "open_at_end": len(open_end),
        "unrealized_note": "El PnL no realizado de las abiertas al cierre de la semana no se puede reconstruir desde la base "
                           "(Freqtrade no guarda el precio de marca): la semana cuenta solo lo cerrado.",
        "compare": paper_compare_brief(summary_path, ws, we),
    }


def paper_compare_brief(path: str | Path | None, ws: int, we: int) -> dict[str, Any] | None:
    """Ultimo paper vs backtest publicado (`paper_summary.json`, lo sube lab/paper_compare.py desde la PC)."""
    if path is None or not Path(path).is_file():
        return None
    try:
        cmp = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return {"error": f"{Path(path).name} ilegible: {exc}"}
    latest = cmp.get("latest") or {}
    tot = latest.get("totals") or {}
    gen_ms = _parse_iso_ms(cmp.get("generated_at"))
    return {"generated_at": cmp.get("generated_at"), "status": latest.get("status"), "window": latest.get("window"),
            "paper_total": (tot.get("paper") or {}).get("total"), "sim_total": (tot.get("sim") or {}).get("total"),
            "diff": tot.get("diff"), "runs": len(cmp.get("history") or []),
            "published_in_week": gen_ms is not None and ws <= gen_ms < we + 2 * HOUR_MS}


# ---- (c) Freqtrade de hipotesis --------------------------------------------------------------------------


def hyp_bridge_week(bridge_db: str | Path, eval_db: str | Path, ft_db: str | Path, ws: int, we: int) -> dict[str, Any]:
    from . import ft_bridge_view as fbv

    ov = fbv.overview(bridge_db, eval_db, ft_db, api=None, now_ms=we)
    per = []
    tot = {"emitted": 0, "sent": 0, "skipped": 0, "errors": 0, "closed": 0, "ft_wins": 0, "ft_pnl": 0.0,
           "compared": 0, "same_outcome": 0, "diffs": []}
    for s in ov.get("strategies") or []:
        ops = [o for o in s.get("ops") or [] if ws <= o["created_ms"] < we]
        closed = [o for o in s.get("ops") or [] if o["status"] == "sent" and o.get("ft") and not o["ft"]["is_open"]
                  and o["ft"]["close_ms"] is not None and ws <= o["ft"]["close_ms"] < we]
        compared = [o for o in closed if o.get("cmp") and o["cmp"].get("pnl_diff_pp") is not None]
        wins = sum(1 for o in closed if (o["ft"]["pnl_abs"] or 0) > 0)
        row = {
            "strategy_id": s["strategy_id"], "name": s.get("name"), "emitted": len(ops),
            "sent": sum(1 for o in ops if o["status"] == "sent"),
            "skipped": sum(1 for o in ops if o["status"].startswith("skipped")),
            "skipped_by_reason": {k: sum(1 for o in ops if o["status"] == k) for k in
                                  ("skipped_pair_busy", "skipped_late", "skipped_unsupported")},
            "errors": sum(1 for o in ops if o["status"].startswith("error")),
            "closed": len(closed), "ft": _rate(wins, len(closed)), "ft_pnl": sum(o["ft"]["pnl_abs"] or 0.0 for o in closed),
            "compared": len(compared), "hyp": _rate(sum(1 for o in compared if o["hyp"]["win"]), len(compared)),
            "same_outcome": sum(1 for o in compared if o["cmp"].get("same_outcome")),
            "mean_pnl_diff_pp": _mean([o["cmp"]["pnl_diff_pp"] for o in compared]),
            "mean_entry_effect_bps": _mean([o["cmp"].get("entry_effect_bps") for o in compared]),
            "mean_exit_effect_bps": _mean([o["cmp"].get("exit_effect_bps") for o in compared]),
            "safety_stops": sum(1 for o in closed if o.get("cmp") and o["cmp"].get("safety_stop")),
            "exits_week": _count([o["ft"] for o in closed], "exit_reason"),  # hyp_due = tiempo; stop_loss = seguridad
        }
        if row["emitted"] or row["closed"]:
            per.append(row)
        for k in ("emitted", "sent", "skipped", "errors", "closed", "compared", "same_outcome"):
            tot[k] += row[k]
        tot["ft_wins"] += wins
        tot["ft_pnl"] += row["ft_pnl"]
        tot["diffs"] += [o["cmp"]["pnl_diff_pp"] for o in compared]
    diffs = tot.pop("diffs")
    exits: dict[str, int] = {}
    for r in per:
        for k, v in r["exits_week"].items():
            exits[k] = exits.get(k, 0) + v
    return {"strategies": per, "exits_week": exits, "totals": {**tot, "ft": _rate(tot["ft_wins"], tot["closed"]),
                                          "mean_pnl_diff_pp": _mean(diffs)},
            "cum": {"sent": (ov.get("totals") or {}).get("sent"), "closed": (ov.get("totals") or {}).get("closed"),
                    "ft_pnl": (ov.get("totals") or {}).get("ft_pnl_abs")},
            "notes": [n for n in ov.get("notes") or [] if "API" not in n]}


# ---- (d) hipotesis del grafo -----------------------------------------------------------------------------


def hypotheses_week(eval_db: str | Path, ws: int, we: int) -> dict[str, Any]:
    from .ft_bridge import strategy_name

    conn = _ro(eval_db)
    try:
        names = {r["strategy_id"]: strategy_name(r["description"]) or r["strategy_id"][:8]
                 for r in conn.execute("SELECT strategy_id, description FROM strategy_hypotheses")}
        ctx = _has_table(conn, "hypothesis_context")
        q = ("SELECT p.hypothesis_id, p.strategy_id, p.symbol, p.created_ts, p.direction, "
             + ("c.timeframe, c.daily_trend, c.trend_alignment, c.session, " if ctx else
                "NULL AS timeframe, NULL AS daily_trend, NULL AS trend_alignment, NULL AS session, ")
             + "r.status, r.resolved_at, r.win, r.pnl_pct, r.benchmark_pnl_pct, r.alpha_pct FROM pending_hypotheses p "
             + ("LEFT JOIN hypothesis_context c ON c.hypothesis_id = p.hypothesis_id " if ctx else "")
             + "LEFT JOIN hypothesis_resolutions r ON r.hypothesis_id = p.hypothesis_id WHERE p.created_ts < ?")
        rows = [dict(r) for r in conn.execute(q, (we / 1000,))]
        exc = {}
        if _has_table(conn, "hypothesis_excursions"):  # MAE/MFE descriptivo (evaluation/excursion.py)
            exc = {r[0]: (r[1], r[2]) for r in conn.execute("SELECT hypothesis_id, mae_atr, mfe_atr FROM hypothesis_excursions")}
        reports = {}
        if _has_table(conn, "strategy_reports"):
            for r in conn.execute("SELECT strategy_id, generated_at, publishable, report FROM strategy_reports WHERE id IN "
                                  "(SELECT MAX(id) FROM strategy_reports GROUP BY strategy_id)"):
                gen = _parse_iso_ms(r["generated_at"])
                if gen is not None and gen >= we + 2 * HOUR_MS:
                    continue  # reporte posterior al cierre: no es la foto de esta semana
                try:
                    blockers = (json.loads(r["report"]) or {}).get("publish_blockers") or []
                except ValueError:
                    blockers = []
                reports[r["strategy_id"]] = {"generated_at": r["generated_at"], "publishable": bool(r["publishable"]),
                                             "blockers": blockers[:5]}
    finally:
        conn.close()
    for r in rows:
        r["resolved_ms"] = _parse_iso_ms(r["resolved_at"])
        if r["status"] and (r["resolved_ms"] is None or r["resolved_ms"] >= we):
            r["status"] = None  # resuelta despues del cierre: al cierre de la semana estaba pendiente
    emitted = [r for r in rows if ws <= r["created_ts"] * 1000 < we]
    in_week = [r for r in rows if r["status"] and ws <= r["resolved_ms"] < we]
    resolved = [r for r in in_week if r["status"] == "resolved" and r["win"] is not None]
    cum = [r for r in rows if r["status"] == "resolved" and r["win"] is not None]

    def block(rs: list[dict[str, Any]]) -> dict[str, Any]:
        ok = [r for r in rs if r["status"] == "resolved" and r["win"] is not None]
        return {**_rate(sum(1 for r in ok if r["win"]), len(ok)),
                "expired_no_data": sum(1 for r in rs if r["status"] == "expired_no_data"),
                "mean_pnl_pct": _mean([r["pnl_pct"] for r in ok]),
                "mean_benchmark_pnl_pct": _mean([r["benchmark_pnl_pct"] for r in ok]),
                "mean_alpha_pct": _mean([r["alpha_pct"] for r in ok])}

    sids = sorted({r["strategy_id"] for r in emitted + in_week}, key=lambda s: names.get(s, s))
    by_strategy = [{"strategy_id": s, "name": names.get(s, s[:8]), "emitted": sum(1 for r in emitted if r["strategy_id"] == s),
                    **block([r for r in in_week if r["strategy_id"] == s]),
                    "cum": _rate(sum(1 for r in cum if r["strategy_id"] == s and r["win"]),
                                 sum(1 for r in cum if r["strategy_id"] == s)),
                    "report": reports.get(s)} for s in sids]
    seg = trend_segments([(r["daily_trend"], r["trend_alignment"], "resolved" if r["status"] == "resolved" else r["status"],
                           r["win"], r["pnl_pct"], r["benchmark_pnl_pct"], r["alpha_pct"], r["session"]) for r in in_week])
    for part in seg.values():
        for s in (part if isinstance(part, list) else [part]):
            s["small_sample"] = s["n"] < SMALL_N
    mae = [exc[r["hypothesis_id"]][0] for r in resolved if r["hypothesis_id"] in exc]
    mfe = [exc[r["hypothesis_id"]][1] for r in resolved if r["hypothesis_id"] in exc]
    return {
        # todas salen por tiempo (pre-registradas asi; sin stop ni objetivo)
        "exits_week": {"tiempo": len(resolved)} if resolved else {},
        "excursion_week": {"n": len([x for x in mae if x is not None]), "mean_mae_atr": _mean(mae), "mean_mfe_atr": _mean(mfe),
                           "nota": "MAE/MFE descriptivo, no pre-registrado"},
        "emitted": len(emitted),
        "emitted_by_timeframe": _count(emitted, "timeframe"),
        "pending_at_end": sum(1 for r in emitted if not r["status"]),
        "resolved_week": block(in_week),
        "cum": {**_rate(sum(1 for r in cum if r["win"]), len(cum)), "mean_alpha_pct": _mean([r["alpha_pct"] for r in cum])},
        "by_strategy": by_strategy,
        "by_daily_trend": [s for s in seg["by_daily_trend"] if s["total"]],
        "by_alignment": [s for s in seg["by_alignment"] if s["total"]],
        "by_session": [s for s in seg["by_session"] if s["total"]],
        "reports_note": "Estado shadow/publicable = ultimo reporte de evaluation/report.py (strategy_reports) anterior al cierre.",
    }


def _count(rows: list[dict[str, Any]], key: str) -> dict[str, int]:
    out: dict[str, int] = {}
    for r in rows:
        k = r.get(key) or "sin_dato"
        out[k] = out.get(k, 0) + 1
    return out


# ---- (e) salud -------------------------------------------------------------------------------------------


def _internal(symbol: str) -> str:
    s = symbol.replace("-", "").replace("/", "").upper()
    for q in ("USDT", "USDC"):
        if s.endswith(q) and len(s) > len(q):
            return f"{s[:-len(q)]}-{q}"
    return s


def _coverage(minutes: list[int], ws: int, we: int, first_ms: int | None = None) -> dict[str, Any]:
    """Minutos presentes vs esperados en [ws, we). Si la serie empezo a grabarse dentro de la semana (`first_ms`), lo
    anterior no cuenta como hueco: se espera desde ahi (y se informa `since_ms`)."""
    start = ws if first_ms is None or first_ms <= ws else min(first_ms, we)
    minutes = [m for m in minutes if m >= start]
    expected = (we - start) // MIN_MS
    longest, prev = 0, start - MIN_MS
    for m in minutes + [we]:
        longest = max(longest, (m - prev) // MIN_MS - 1)
        prev = m
    return {"present": len(minutes), "expected": expected, "missing": expected - len(minutes),
            "missing_frac": (expected - len(minutes)) / expected if expected else None, "longest_gap_min": longest,
            "since_ms": start if start > ws else None}


def health_week(eval_db: str | Path, history_db: str | Path | None, ws: int, we: int,
                symbols: tuple[str, ...] = DEFAULT_SYMBOLS) -> dict[str, Any]:
    out: dict[str, Any] = {}
    conn = _ro(eval_db)
    try:
        out["cycles"] = {r[0]: r[1] for r in conn.execute(
            "SELECT status, COUNT(*) FROM cycle_log WHERE ts_ms >= ? AND ts_ms < ? GROUP BY status", (ws, we))} \
            if _has_table(conn, "cycle_log") else None
        errs: dict[str, int] = {}
        if out["cycles"]:
            for (detail,) in conn.execute("SELECT detail FROM cycle_log WHERE status='error' AND ts_ms >= ? AND ts_ms < ?",
                                          (ws, we)):
                k = str(detail or "")[:120]
                errs[k] = errs.get(k, 0) + 1
        out["cycle_errors"] = [{"detail": k, "count": v} for k, v in sorted(errs.items(), key=lambda x: -x[1])[:8]]
        llm = None
        if _has_table(conn, "session_journal"):
            day0 = datetime.fromtimestamp(ws / 1000, tz=timezone.utc).date().isoformat()
            day1 = datetime.fromtimestamp(we / 1000, tz=timezone.utc).date().isoformat()
            llm = {r[0]: r[1] for r in conn.execute(
                "SELECT status, COUNT(*) FROM session_journal WHERE kind='llm' AND day >= ? AND day < ? GROUP BY status",
                (day0, day1))}
        out["sessions_llm"] = llm
    finally:
        conn.close()
    cyc = out["cycles"] or {}
    out["llm_calls"] = {
        "counted": False,
        "cycles_reaching_llm": cyc.get("ran", 0) + cyc.get("error", 0),
        "note": "Las llamadas al LLM no se pueden contar: cycle_log guarda el estado de cada ciclo pero no cuantas "
                "llamadas hizo ni a que proveedor. Se informa como aproximacion los ciclos que llegaron al grafo con LLM "
                "(corrio + error); para contarlas hace falta registrar las llamadas (falta).",
    }
    if history_db is None:
        out["bars_1m"] = out["klines_1m"] = {"error": "base historica no configurada"}
        return out
    try:
        h = _ro(history_db)
    except FileNotFoundError as exc:
        out["bars_1m"] = out["klines_1m"] = {"error": str(exc)}
        return out
    try:
        bars = []
        for s in symbols:
            sym = _internal(s)
            per: dict[str, list[int]] = {}
            for ex, m in h.execute("SELECT exchange, minute_ms FROM bars_1m WHERE symbol=? AND minute_ms >= ? AND "
                                   "minute_ms < ? ORDER BY exchange, minute_ms", (sym, ws, we)):
                per.setdefault(ex, []).append(m)
            if not per:
                bars.append({"symbol": sym, "exchange": None, **_coverage([], ws, we)})
            for ex, mins in sorted(per.items()):
                first = h.execute("SELECT MIN(minute_ms) FROM bars_1m WHERE exchange=? AND symbol=?", (ex, sym)).fetchone()[0]
                bars.append({"symbol": sym, "exchange": ex, **_coverage(mins, ws, we, first)})
        kl = []
        for s in symbols:
            sym = _internal(s)
            mins = [r[0] for r in h.execute("SELECT open_time_ms FROM klines WHERE symbol=? AND interval='1m' AND "
                                            "open_time_ms >= ? AND open_time_ms < ? ORDER BY open_time_ms", (sym, ws, we))]
            first = h.execute("SELECT MIN(open_time_ms) FROM klines WHERE symbol=? AND interval='1m'", (sym,)).fetchone()[0]
            kl.append({"symbol": sym, **_coverage(mins, ws, we, first)})
        archived: dict[str, int] = {}
        if _has_table(h, "archive_catalog"):  # lo archivado en Drive no esta en la base local: no es un hueco
            for tname, rows_ in h.execute("SELECT table_name, SUM(rows) FROM archive_catalog WHERE kind='table' AND "
                                          "table_name IN ('bars_1m', 'klines_1m') AND start_ms < ? AND end_ms > ? "
                                          "GROUP BY table_name", (we, ws)):
                archived[tname] = int(rows_ or 0)
    finally:
        h.close()
    out["bars_1m"] = {"rows": bars, "missing": sum(b["missing"] for b in bars),
                      "expected": sum(b["expected"] for b in bars), "archived_rows": archived.get("bars_1m", 0),
                      "note": "minutos sin fila en bars_1m (la grabacion no escribio: ingesta caida o reinicio); una fila "
                              "sin trades igual cuenta como presente; si la serie empezo a grabarse en la semana, se "
                              "espera desde ese minuto"}
    out["klines_1m"] = {"rows": kl, "missing": sum(k["missing"] for k in kl), "expected": sum(k["expected"] for k in kl),
                        "archived_rows": archived.get("klines_1m", 0),
                        "note": "velas 1m de Binance (las usa el bot de paper); las faltantes del exchange son reales"}
    if archived:
        out["archive_note"] = ("Parte de la semana ya se archivo en Drive (almacenamiento por niveles): esos minutos no "
                               "estan en la base local y aparecen como faltantes sin serlo.")
    return out


# ---- armado, texto y metricas ----------------------------------------------------------------------------


def _fmt_money(v: float | None, signed: bool = True) -> str:
    if v is None:
        return "s/d"
    if abs(v) < 0.005:
        v = 0.0
    return ("+" if v > 0 and signed else "−" if v < 0 else "") + f"{abs(v):,.2f}".replace(",", "X").replace(".", ",").replace("X", ".") + " US$"


def _fmt_pct(v: float | None, d: int = 1) -> str:
    return "s/d" if v is None else f"{v * 100:.{d}f}".replace(".", ",") + " %"


def _rate_text(b: dict[str, Any]) -> str:
    if not b.get("n"):
        return "sin casos"
    ci = b.get("ci95") or [0, 0]
    txt = f"{b['wins']} de {b['n']} ({_fmt_pct(b['rate'], 0)}, IC 95 % {_fmt_pct(ci[0], 0)}–{_fmt_pct(ci[1], 0)})"
    return txt + (" — muestra chica" if b.get("small_sample") else "")


def metrics(s: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Metricas planas para comparar semana contra semana (flechas). better: 'up' | 'down' | None."""
    out: dict[str, dict[str, Any]] = {}

    def put(key: str, label: str, value: Any, better: str | None, kind: str = "num") -> None:
        if value is not None:
            out[key] = {"label": label, "value": value, "better": better, "kind": kind}

    pb = s.get("paperbot") or {}
    for st in pb.get("strategies") or []:
        put(f"paperbot.{st['strategy_id']}.pnl", f"bot paper {st['strategy_id']}: PnL semanal", st["pnl_week"], "up", "money")
        put(f"paperbot.{st['strategy_id']}.vs_basket", f"bot paper {st['strategy_id']}: contra la canasta",
            st["vs_basket_week"], "up", "money")
    if pb.get("available"):
        put("paperbot.errors", "bot paper: corridas con error", (pb.get("runs") or {}).get("error"), "down", "int")
    ft = s.get("freqtrade") or {}
    if ft.get("available"):
        put("freqtrade.pnl", "Freqtrade Donchian: PnL cerrado", ft["week"]["pnl"], "up", "money")
        put("freqtrade.closed", "Freqtrade Donchian: cerradas", ft["week"]["n"], None, "int")
    hb = s.get("hyp_bridge") or {}
    if hb.get("available"):
        put("hyp_bridge.pnl", "Freqtrade hipótesis: PnL cerrado", hb["totals"]["ft_pnl"], "up", "money")
        put("hyp_bridge.sent", "Freqtrade hipótesis: enviadas", hb["totals"]["sent"], None, "int")
    hy = s.get("hypotheses") or {}
    if hy.get("available"):
        put("hypotheses.emitted", "hipótesis emitidas", hy["emitted"], None, "int")
        put("hypotheses.resolved", "hipótesis resueltas", hy["resolved_week"]["n"], None, "int")
        put("hypotheses.rate", "tasa de acierto de la semana", hy["resolved_week"]["rate"], "up", "frac")
        put("hypotheses.alpha", "alfa medio contra mantener", hy["resolved_week"]["mean_alpha_pct"], "up", "pct")
    he = s.get("health") or {}
    if he.get("available"):
        put("health.cycle_errors", "ciclos del grafo con error", (he.get("cycles") or {}).get("error", 0), "down", "int")
        if isinstance(he.get("bars_1m"), dict) and "missing" in he["bars_1m"]:
            put("health.bars_missing", "minutos faltantes en bars_1m", he["bars_1m"]["missing"], "down", "int")
    return out


_EXIT_LABEL = {"senal": "señal", "exit_signal": "señal", "stop_loss": "stop", "hyp_due": "tiempo", "roi": "objetivo (ROI)",
               "sin_dato": "sin registro"}


def _exits_text(c: dict[str, int] | None) -> str:
    return ", ".join(f"{_EXIT_LABEL.get(k, k)} {v}" for k, v in sorted((c or {}).items(), key=lambda kv: -kv[1]))


_ROLE_TITLE = {"principal": "Hipótesis principal (decide; criterio pre-registrado a 3 y 6 meses)",
               "seguimiento": "Seguimiento (exploratorio, no decide; corrección BH al evaluar)",
               "paper_previo": "Paper previo"}


def _fmt_num(v: float | None, d: int = 2) -> str:
    return "s/d" if v is None else f"{v:.{d}f}".replace(".", ",")


def _paperbot_line(st: dict[str, Any]) -> str:
    new = " (entró en la semana: su semana es parcial)" if st["started_in_week"] else ""
    line = (f"- {st['strategy_id']}{new}: en la semana {_fmt_money(st['pnl_week'])} contra "
            f"{_fmt_money(st['basket_pnl_week'])} de su canasta; acumulado {_fmt_money(st['pnl_cum'])} "
            f"(canasta {_fmt_money(st['basket_cum'])}); costos {_fmt_money(st['costs_week'], False)}, funding "
            f"{_fmt_money(st['funding_week'])}; trades cerrados: {_rate_text(st['trades_week'])}"
            + (f"; salidas: {_exits_text(st.get('exits_week'))}" if st.get("exits_week") else ""))
    sh = st.get("sharpe_cum") or {}
    if st.get("rol") == "seguimiento":
        line += f"; Sharpe acumulado {_fmt_num(sh.get('valor'))} en {sh.get('dias', 0)} días ({sh.get('marca')})"
    c = st.get("criterio")
    if c:
        cur = c.get("en_curso") or {}
        line += (f"; {c['dias']} días desde el inicio, criterio: {c['estado']}"
                 + ("" if c.get("veredicto_3m") else f" (Sharpe en curso {_fmt_num(cur.get('sharpe'))} vs referencia "
                    f"{_fmt_num(cur.get('sharpe_referencia'))}, informativo hasta los 3 meses)"))
    return line + "."


def summary_text(s: dict[str, Any]) -> str:
    """Resumen por codigo (sin LLM), en lenguaje historico."""
    L = [f"Semana {s['week']} ({s['week_start_at'][:10]} a {iso(s['week_end_ms'] - 1)[:10]}, UTC). {ONE_WEEK_NOTE}"]
    pb = s.get("paperbot") or {}
    if pb.get("available"):
        L.append("Bot propio de paper (dinero ficticio, 10.000 US$ por estrategia, sin reinversión):")
        for rol in ("principal", "seguimiento", "paper_previo"):
            sts = [st for st in pb["strategies"] if st.get("rol", "paper_previo") == rol]
            if not sts:
                continue
            L.append(f" {_ROLE_TITLE[rol]}:")
            for st in sts:
                L.append(_paperbot_line(st))
            if rol == "principal":
                L.append("  " + LECTURA_CONJUNTA)
        if pb.get("sin_datos_ultima_corrida"):
            L.append("  Sin datos en la última corrida ok de la semana (no operaron esa hora): "
                     + ", ".join(pb["sin_datos_ultima_corrida"]) + ".")
        r = pb["runs"]
        L.append(f"  Corridas: {r['ok']} ok, {r['error']} con error, {r['missing_hours']} horas sin corrida ok"
                 + (f"; {len(pb['gaps'])} hueco(s) registrados." if pb["gaps"] else "."))
    else:
        L.append(f"Bot propio de paper: sin datos ({pb.get('error')}).")
    ft = s.get("freqtrade") or {}
    if ft.get("available"):
        w = ft["week"]
        head = (f"{w['n']} operaciones cerradas en la semana, {_fmt_money(w['pnl'])} (ganadoras: {_rate_text(w)})"
                if w["n"] else "ninguna operación cerrada en la semana")
        L.append(f"Freqtrade Donchian (dry-run): {head}; acumulado cerrado {_fmt_money(ft['cum']['pnl'])}; "
                 f"{ft['open_at_end']} abiertas al cierre (sin PnL no realizado)"
                 + (f"; motivos de cierre: {_exits_text(ft.get('exits_week'))}." if ft.get("exits_week") else "."))
        c = ft.get("compare")
        if c and not c.get("error"):
            L.append(f"  Última comparación paper vs backtest publicada {c.get('generated_at')}: estado {c.get('status')}.")
        elif not c:
            L.append("  No hay comparación paper vs backtest publicada.")
    else:
        L.append(f"Freqtrade Donchian: sin datos ({ft.get('error')}).")
    hb = s.get("hyp_bridge") or {}
    if hb.get("available"):
        t = hb["totals"]
        L.append(f"Freqtrade de hipótesis: {t['emitted']} hipótesis de 1h en la semana, {t['sent']} enviadas, {t['skipped']} "
                 f"omitidas, {t['errors']} con error; {t['closed']} cerradas con {_fmt_money(t['ft_pnl'])} "
                 f"(ganadoras: {_rate_text(t['ft'])}); diferencia media con la resolución "
                 + ("s/d" if t["mean_pnl_diff_pp"] is None else f"{t['mean_pnl_diff_pp']:+.2f} pp".replace(".", ","))
                 + (f"; motivos de cierre: {_exits_text(hb.get('exits_week'))}." if hb.get("exits_week") else "."))
    else:
        L.append(f"Freqtrade de hipótesis: sin datos ({hb.get('error')}).")
    hy = s.get("hypotheses") or {}
    if hy.get("available"):
        rw = hy["resolved_week"]
        L.append(f"Hipótesis del grafo: {hy['emitted']} emitidas; resueltas en la semana: "
                 + (f"acertaron {_rate_text(rw)}" if rw["n"] else "ninguna")
                 + (f"; {rw['expired_no_data']} sin dato de precio" if rw["expired_no_data"] else "")
                 + ("" if rw["mean_alpha_pct"] is None else f"; alfa medio contra mantener {rw['mean_alpha_pct']:+.2f} %".replace(".", ","))
                 + f". Acumulado: {_rate_text(hy['cum'])}.")
    else:
        L.append(f"Hipótesis del grafo: sin datos ({hy.get('error')}).")
    he = s.get("health") or {}
    if he.get("available"):
        cyc = he.get("cycles") or {}
        b = he.get("bars_1m") or {}
        L.append("Salud: ciclos del grafo " + (", ".join(f"{k} {v}" for k, v in sorted(cyc.items())) or "ninguno")
                 + (f"; bars_1m sin {b['missing']} de {b['expected']} minutos esperados" if "missing" in b else
                    f"; bars_1m: {b.get('error')}")
                 + ". Llamadas al LLM: no se registran (se cuentan solo ciclos).")
    return "\n".join(L)


def build(week: str, paths: dict[str, Any], *, now_ms: int | None = None,
          symbols: tuple[str, ...] = DEFAULT_SYMBOLS) -> dict[str, Any]:
    ws, we = week_bounds(week)
    now = int(now_ms if now_ms is not None else time.time() * 1000)
    if we > now:
        raise ValueError(f"la semana {week} todavia no termino (cierra {iso(we)})")
    s: dict[str, Any] = {"week": week, "week_start_ms": ws, "week_end_ms": we, "week_start_at": iso(ws),
                         "week_end_at": iso(we), "generated_ms": now, "model_id": MODEL_ID, "small_n": SMALL_N}
    s["paperbot"] = _source(paperbot_week, paths["paperbot_db"], ws, we)
    s["freqtrade"] = _source(freqtrade_week, paths["freqtrade_db"], ws, we, paths.get("paper_summary"))
    s["hyp_bridge"] = _source(hyp_bridge_week, paths["bridge_db"], paths["eval_db"], paths["ft_hyp_db"], ws, we)
    s["hypotheses"] = _source(hypotheses_week, paths["eval_db"], ws, we)
    s["health"] = _source(health_week, paths["eval_db"], paths.get("history_db"), ws, we, symbols)
    failed = [k for k in ("paperbot", "freqtrade", "hyp_bridge", "hypotheses", "health") if not s[k].get("available")]
    s["status"] = "partial" if failed else "ok"
    s["failed_sources"] = failed
    notes = [ONE_WEEK_NOTE]
    if failed:
        notes.append("Fuentes sin datos esta semana: " + ", ".join(failed) + " (el motivo esta en cada tarjeta).")
    s["notes"] = notes
    s["metrics"] = metrics(s)
    s["narrative"] = None  # sin LLM: el resumen es por codigo (`text`)
    return s


# ---- persistencia (append-only, idempotente por semana) --------------------------------------------------


class WeeklyStore:
    def __init__(self, path: str | Path) -> None:
        self.conn = sqlite3.connect(str(path), timeout=30)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    def exists(self, week: str) -> bool:
        return self.conn.execute("SELECT 1 FROM weekly_reviews WHERE week=?", (week,)).fetchone() is not None

    def add(self, s: dict[str, Any], text: str) -> bool:
        """INSERT OR IGNORE: si la semana ya estaba cerrada no se toca (devuelve False)."""
        with self.conn:
            cur = self.conn.execute(
                "INSERT OR IGNORE INTO weekly_reviews (week, week_start_ms, week_end_ms, generated_ms, model_id, status, "
                "summary, text) VALUES (?,?,?,?,?,?,?,?)",
                (s["week"], s["week_start_ms"], s["week_end_ms"], s["generated_ms"], s["model_id"], s["status"],
                 json.dumps(s, ensure_ascii=False, default=str, allow_nan=False), text))
        return cur.rowcount == 1


def run(week: str | None, paths: dict[str, Any], *, now_ms: int | None = None, dry_run: bool = False,
        symbols: tuple[str, ...] = DEFAULT_SYMBOLS) -> dict[str, Any]:
    now = int(now_ms if now_ms is not None else time.time() * 1000)
    week = week or last_complete_week(now)
    if not dry_run:
        st = WeeklyStore(paths["eval_db"])
        try:
            if st.exists(week):
                return {"week": week, "written": False, "reason": "la semana ya estaba cerrada: no se recalcula"}
        finally:
            st.close()
    s = _clean(build(week, paths, now_ms=now, symbols=symbols))
    text = summary_text(s)
    if dry_run:
        return {"week": week, "written": False, "reason": "dry-run", "summary": s, "text": text}
    st = WeeklyStore(paths["eval_db"])
    try:
        written = st.add(s, text)
    finally:
        st.close()
    return {"week": week, "written": written, "status": s["status"], "failed_sources": s["failed_sources"], "text": text,
            "reason": None if written else "otra corrida cerro la semana al mismo tiempo"}


def _clean(o: Any) -> Any:
    if isinstance(o, float):
        return o if math.isfinite(o) else None
    if isinstance(o, dict):
        return {str(k): _clean(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_clean(v) for v in o]
    return o


# ---- lectura para el panel (GET /api/weekly) -------------------------------------------------------------


def _row(r: sqlite3.Row) -> dict[str, Any]:
    return {"week": r["week"], "week_start_ms": r["week_start_ms"], "week_end_ms": r["week_end_ms"],
            "week_start_at": iso(r["week_start_ms"]), "week_end_at": iso(r["week_end_ms"]),
            "generated_ms": r["generated_ms"], "generated_at": iso(r["generated_ms"]), "model_id": r["model_id"],
            "status": r["status"]}


def compare(cur: dict[str, Any], prev: dict[str, Any] | None) -> dict[str, Any]:
    """Semana contra semana sobre las metricas planas: delta y direccion (up/down/flat) + si es mejor o peor."""
    out = {}
    pm = (prev or {}).get("metrics") or {}
    for k, m in (cur.get("metrics") or {}).items():
        p = (pm.get(k) or {}).get("value")
        v = m.get("value")
        if p is None or v is None:
            out[k] = {**m, "prev": p, "delta": None, "dir": None, "good": None}
            continue
        delta = v - p
        d = "flat" if abs(delta) < 1e-12 else "up" if delta > 0 else "down"
        good = None if m.get("better") is None or d == "flat" else d == m["better"]
        out[k] = {**m, "prev": p, "delta": delta, "dir": d, "good": good}
    return out


def _open_reviews(eval_db: str | Path) -> sqlite3.Connection | None:
    conn = _ro(eval_db)
    if not _has_table(conn, "weekly_reviews"):
        conn.close()
        return None
    return conn


def list_reviews(eval_db: str | Path, limit: int = 52) -> dict[str, Any]:
    """Historial (lo mas nuevo primero) con las metricas planas + la ultima semana completa con su comparacion."""
    conn = _open_reviews(eval_db)
    if conn is None:
        return {"weeks": [], "latest": None, "note": "todavia no se cerro ninguna semana (heatsignal-weekly.timer, lunes 00:30 UTC)"}
    try:
        rows = conn.execute("SELECT * FROM weekly_reviews ORDER BY week_start_ms DESC LIMIT ?", (limit + 1,)).fetchall()
    finally:
        conn.close()
    weeks = []
    sums = [json.loads(r["summary"]) for r in rows]
    for i, r in enumerate(rows[:limit]):
        prev = sums[i + 1] if i + 1 < len(sums) and rows[i + 1]["week_start_ms"] == r["week_start_ms"] - WEEK_MS else None
        weeks.append({**_row(r), "metrics": compare(sums[i], prev), "failed_sources": sums[i].get("failed_sources") or []})
    latest = None
    if rows:
        latest = _full(rows[0], sums[0], sums[1] if len(sums) > 1 and rows[1]["week_start_ms"] == rows[0]["week_start_ms"] - WEEK_MS else None)
    return {"weeks": weeks, "latest": latest, "note": None}


def get_review(eval_db: str | Path, week: str) -> dict[str, Any] | None:
    ws, _ = week_bounds(week)
    conn = _open_reviews(eval_db)
    if conn is None:
        return None
    try:
        r = conn.execute("SELECT * FROM weekly_reviews WHERE week=?", (week,)).fetchone()
        p = conn.execute("SELECT summary FROM weekly_reviews WHERE week_start_ms=?", (ws - WEEK_MS,)).fetchone()
    finally:
        conn.close()
    if r is None:
        return None
    return _full(r, json.loads(r["summary"]), json.loads(p["summary"]) if p else None)


def _full(r: sqlite3.Row, s: dict[str, Any], prev: dict[str, Any] | None) -> dict[str, Any]:
    return {**_row(r), "summary": s, "text": r["text"], "prev_week": None if prev is None else prev.get("week"),
            "compare": compare(s, prev), "notes": s.get("notes") or [ONE_WEEK_NOTE]}


# ---- CLI -------------------------------------------------------------------------------------------------


def default_paths(env: dict[str, str] | None = None) -> dict[str, Any]:
    env = dict(os.environ if env is None else env)
    eval_db = Path(env.get("HEATSIGNAL_EVAL_DB") or "data/heatsignal_eval.sqlite").expanduser()
    data = eval_db.parent
    return {
        "eval_db": eval_db,
        "history_db": Path(env.get("HEATSIGNAL_HISTORY_DB") or data / "heatsignal_history.sqlite").expanduser(),
        "paperbot_db": Path(env.get("HEATSIGNAL_PAPERBOT_DB") or data / "paperbot.sqlite").expanduser(),
        "freqtrade_db": Path(env.get("HEATSIGNAL_FREQTRADE_DB") or data / "freqtrade_dryrun.sqlite").expanduser(),
        "paper_summary": data / "paper_summary.json",
        "ft_hyp_db": Path(env.get("HEATSIGNAL_FT_HYP_DB") or data / "freqtrade_hyp.sqlite").expanduser(),
        "bridge_db": Path(env.get("HEATSIGNAL_FT_BRIDGE_DB") or data / "ft_bridge.sqlite").expanduser(),
    }


def _symbols(env: dict[str, str] | None = None) -> tuple[str, ...]:
    raw = (os.environ if env is None else env).get("HEATSIGNAL_SYMBOLS", "")
    out = tuple(s.strip().upper() for s in raw.split(",") if s.strip())
    return out or DEFAULT_SYMBOLS


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m heatsignal.paper.weekly", description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="cierra una semana (por defecto la ultima completa)")
    r.add_argument("--week", help="semana ISO, p. ej. 2026-W42")
    r.add_argument("--dry-run", action="store_true", help="calcula e imprime sin escribir")
    r.add_argument("--now", help="instante ISO (tests / re-cierre manual)")
    r.add_argument("--json", action="store_true", help="imprimir el resumen completo en JSON")
    sub.add_parser("list", help="semanas cerradas")
    sh = sub.add_parser("show", help="una semana cerrada")
    sh.add_argument("week")
    args = ap.parse_args(argv)
    paths = default_paths()
    if args.cmd == "run":
        now = _parse_iso_ms(args.now) if args.now else None
        t0 = time.time()
        try:
            out = run(args.week, paths, now_ms=now, dry_run=args.dry_run, symbols=_symbols())
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        try:
            import resource
            out["peak_rss_kb"] = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        except ImportError:  # pragma: no cover
            pass
        out["seconds"] = round(time.time() - t0, 2)
        if args.json:
            print(json.dumps(out, ensure_ascii=False, indent=1, default=str))
        else:
            print(f"semana {out['week']}: " + ("escrita" if out["written"] else f"no se escribio ({out['reason']})")
                  + (f" · estado {out.get('status')}" if out.get("status") else "")
                  + f" · {out['seconds']} s · pico {out.get('peak_rss_kb')} KB")
            if out.get("text"):
                print(out["text"])
        return 0
    if args.cmd == "list":
        try:
            ls = list_reviews(paths["eval_db"])
        except FileNotFoundError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        for w in ls["weeks"]:
            print(f"{w['week']}  {w['status']:8s} generada {w['generated_at']}")
        if ls.get("note"):
            print(ls["note"])
        return 0
    try:
        rv = get_review(paths["eval_db"], args.week)
    except (ValueError, FileNotFoundError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if rv is None:
        print(f"la semana {args.week} no esta cerrada", file=sys.stderr)
        return 1
    print(rv["text"])
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
