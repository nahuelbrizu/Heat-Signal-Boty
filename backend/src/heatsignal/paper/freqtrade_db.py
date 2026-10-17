"""
Lectura de SOLO LECTURA del paper trading (Freqtrade en dry-run de donchian-ensemble-v1).

Solo libreria estandar y sin imports relativos, por dos motivos:
  - corre en la VM dentro del panel (sin pandas);
  - la PC (`lab/paper_compare.py`) manda ESTE archivo por SSH a `python3 -` para volcar la base sin depender
    del codigo desplegado en la VM:  python3 - dump /home/nahue/heatsignal/data/freqtrade_dryrun.sqlite

Esquema verificado contra Freqtrade 2026.9 (`freqtrade/persistence/trade_model.py`, 2026-10-18):
  - trades: id, pair ("TRX/USDT:USDT"), is_open, is_short, leverage, open_date, close_date (UTC naive,
    "YYYY-MM-DD HH:MM:SS.ffffff"), open_rate, close_rate, amount, stake_amount, max_stake_amount, fee_open,
    fee_close (tasas), fee_open_cost, fee_close_cost (US$), realized_profit, close_profit (fraccion),
    close_profit_abs, funding_fees (POSITIVO = la operacion COBRO funding; negativo = pago), funding_fee_running,
    exit_reason, enter_tag, strategy, trading_mode.
  - orders: id, ft_trade_id, ft_order_side ("buy"/"sell"/"stoploss"), ft_pair, ft_is_open, status, order_type,
    side, price, average, amount, filled, cost, order_date, order_filled_date, funding_fee (funding acumulado
    del trade hasta esa orden), ft_order_tag ("donchian", "ajuste+", "ajuste-", ...).
  - KeyValueStore: key -> bot_start_time (primer arranque del bot = inicio del paper), startup_time (ultimo
    arranque del proceso).
Si una columna no existe (otra version de Freqtrade) se devuelve None en vez de fallar.

La base se abre con `file:...?mode=ro` (URI de SQLite) y `PRAGMA query_only`: nunca se escribe.
La API REST (127.0.0.1:8081) se lee con las credenciales de `config.secrets.json` (`api_server.username/password`),
que nunca se devuelven ni se loguean.
"""

from __future__ import annotations

import base64
import json
import math
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

SCHEMA = "freqtrade-2026.9"
TRADE_COLS = (
    "id", "pair", "is_open", "is_short", "leverage", "open_date", "close_date", "open_rate", "close_rate", "amount",
    "stake_amount", "max_stake_amount", "fee_open", "fee_close", "fee_open_cost", "fee_close_cost", "realized_profit",
    "close_profit", "close_profit_abs", "funding_fees", "funding_fee_running", "exit_reason", "enter_tag", "strategy",
    "trading_mode",
)
ORDER_COLS = (
    "id", "ft_trade_id", "ft_order_side", "ft_pair", "ft_is_open", "status", "order_type", "side", "price", "average",
    "amount", "filled", "cost", "order_date", "order_filled_date", "funding_fee", "ft_order_tag",
)
DEFAULT_WALLET = 10_000.0
SMALL_N = 30  # menos operaciones cerradas que esto: la tasa de acierto no dice casi nada


# ---- utilidades ---------------------------------------------------------------------------------------


def ft_time_ms(value: Any) -> int | None:
    """Fecha de Freqtrade (UTC sin zona, "2026-10-17 16:45:37.487875") -> epoch ms. Acepta ISO con zona."""
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, datetime):
        dt = value
    else:
        s = str(value).strip().replace("T", " ")
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(s)
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def iso(ms: int | float | None) -> str | None:
    return None if ms is None else datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat()


def pair_symbol(pair: str | None) -> str | None:
    """'TRX/USDT:USDT' -> 'TRXUSDT' (formato del laboratorio)."""
    if not pair:
        return None
    return str(pair).split(":")[0].replace("/", "").upper()


def _f(v: Any) -> float | None:
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) else None


def wilson(k: int, n: int, z: float = 1.959964) -> list[float] | None:
    if n <= 0:
        return None
    p = k / n
    den = 1 + z * z / n
    c = (p + z * z / (2 * n)) / den
    h = z * ((p * (1 - p) / n + z * z / (4 * n * n)) ** 0.5) / den
    return [max(0.0, c - h), min(1.0, c + h)]


# ---- base (solo lectura) ------------------------------------------------------------------------------


def connect_ro(path: str | Path) -> sqlite3.Connection:
    """Conexion de solo lectura. FileNotFoundError si la base no existe (Freqtrade nunca corrio ahi)."""
    p = Path(path).expanduser()
    if not p.is_file():
        raise FileNotFoundError(f"no existe la base de Freqtrade: {p}")
    conn = sqlite3.connect(f"file:{urllib.parse.quote(str(p.resolve()))}?mode=ro", uri=True, timeout=5.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = 1")
    return conn


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}


def _select(conn: sqlite3.Connection, table: str, wanted: tuple[str, ...], order: str = "id") -> list[dict[str, Any]]:
    have = _columns(conn, table)
    if not have:
        return []
    cols = [c for c in wanted if c in have]
    rows = conn.execute(f"SELECT {', '.join(cols)} FROM {table} ORDER BY {order}").fetchall()
    return [{c: (r[c] if c in cols else None) for c in wanted} for r in rows]


def _trade(r: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": r["id"], "pair": r["pair"], "symbol": pair_symbol(r["pair"]),
        "is_open": bool(r["is_open"]), "is_short": bool(r["is_short"]), "leverage": _f(r["leverage"]),
        "open_ms": ft_time_ms(r["open_date"]), "close_ms": ft_time_ms(r["close_date"]),
        "open_rate": _f(r["open_rate"]), "close_rate": _f(r["close_rate"]), "amount": _f(r["amount"]),
        "stake_amount": _f(r["stake_amount"]), "max_stake_amount": _f(r["max_stake_amount"]),
        "fee_open": _f(r["fee_open"]), "fee_close": _f(r["fee_close"]),
        "fee_open_cost": _f(r["fee_open_cost"]), "fee_close_cost": _f(r["fee_close_cost"]),
        "realized_profit": _f(r["realized_profit"]), "close_profit": _f(r["close_profit"]),
        "close_profit_abs": _f(r["close_profit_abs"]), "funding_fees": _f(r["funding_fees"]),
        "funding_fee_running": _f(r["funding_fee_running"]), "exit_reason": r["exit_reason"],
        "enter_tag": r["enter_tag"], "strategy": r["strategy"], "trading_mode": r["trading_mode"],
    }


def _order(r: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": r["id"], "trade_id": r["ft_trade_id"], "pair": r["ft_pair"], "symbol": pair_symbol(r["ft_pair"]),
        "ft_side": r["ft_order_side"], "side": r["side"], "status": r["status"], "order_type": r["order_type"],
        "is_open": bool(r["ft_is_open"]), "price": _f(r["price"]), "average": _f(r["average"]),
        "amount": _f(r["amount"]), "filled": _f(r["filled"]), "cost": _f(r["cost"]),
        "order_ms": ft_time_ms(r["order_date"]), "filled_ms": ft_time_ms(r["order_filled_date"]),
        "funding_fee": _f(r["funding_fee"]), "tag": r["ft_order_tag"],
    }


def read_dump(conn: sqlite3.Connection) -> dict[str, Any]:
    """Todo lo que hace falta de la base (es chica: una fila por operacion y por orden)."""
    kv: dict[str, Any] = {}
    if _columns(conn, "KeyValueStore"):
        for r in conn.execute("SELECT * FROM KeyValueStore"):
            d = dict(r)
            vt = d.get("value_type")
            val = d.get(f"{vt}_value") if vt else None
            kv[d["key"]] = ft_time_ms(val) if vt == "datetime" else val
    return {
        "schema": SCHEMA,
        "read_at_ms": int(time.time() * 1000),
        "trades": [_trade(r) for r in _select(conn, "trades", TRADE_COLS)],
        "orders": [_order(r) for r in _select(conn, "orders", ORDER_COLS)],
        "kv": kv,
    }


def dump(path: str | Path) -> dict[str, Any]:
    conn = connect_ro(path)
    try:
        out = read_dump(conn)
    finally:
        conn.close()
    out["db_path"] = str(path)
    return out


# ---- fills ----------------------------------------------------------------------------------------------


def fills(d: dict[str, Any]) -> list[dict[str, Any]]:
    """Ordenes ejecutadas como fills con signo, en orden de tiempo.

    `du` = cambio de unidades de la posicion (long: compra +, venta -; short al reves), `price` = precio medio
    del fill, `fee` = nocional * tasa del trade (fee_open en entradas/aumentos, fee_close en reducciones/salida),
    `kind` = entrada | ajuste+ | ajuste- | salida (por la posicion del trade antes y despues).
    """
    trades = {t["id"]: t for t in d.get("trades", [])}
    out: list[dict[str, Any]] = []
    by_trade: dict[Any, list[dict[str, Any]]] = {}
    for o in d.get("orders", []):
        if o["ft_side"] not in ("buy", "sell") or not (o["filled"] or 0) > 0:
            continue
        by_trade.setdefault(o["trade_id"], []).append(o)
    for tid, orders in by_trade.items():
        t = trades.get(tid) or {}
        short = bool(t.get("is_short"))
        pos = 0.0
        for o in sorted(orders, key=lambda x: (x["filled_ms"] or x["order_ms"] or 0, x["id"])):
            qty = float(o["filled"])
            sign = 1.0 if o["ft_side"] == "buy" else -1.0
            du = sign * qty * (-1.0 if short else 1.0)
            grows = abs(pos + du) > abs(pos)
            px = o["average"] or o["price"]
            rate = (t.get("fee_open") if grows else t.get("fee_close")) or 0.0
            before, pos = pos, pos + du
            if abs(pos) <= 1e-12 * max(1.0, abs(before)):
                pos = 0.0
            kind = "entrada" if before == 0 else "salida" if pos == 0 else ("ajuste+" if grows else "ajuste-")
            out.append({
                "trade_id": tid, "order_id": o["id"], "symbol": o["symbol"] or t.get("symbol"), "pair": o["pair"],
                "time_ms": o["filled_ms"] or o["order_ms"], "du": du, "units_after": pos, "price": px,
                "notional": abs(qty * (px or 0.0)), "fee": abs(qty * (px or 0.0)) * rate, "kind": kind, "tag": o["tag"],
            })
    out.sort(key=lambda f: (f["time_ms"] or 0, f["order_id"]))
    return out


# ---- API REST de Freqtrade ------------------------------------------------------------------------------

HttpFetch = Callable[[str, dict[str, str], float], tuple[int, bytes]]


def _urllib_fetch(url: str, headers: dict[str, str], timeout: float) -> tuple[int, bytes]:
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - URL de config (127.0.0.1)
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read() if hasattr(exc, "read") else b""


def read_credentials(secrets_path: str | Path) -> tuple[str, str]:
    """(usuario, clave) de `api_server` en config.secrets.json. Nunca se devuelven al cliente."""
    p = Path(secrets_path).expanduser()
    cfg = json.loads(p.read_text(encoding="utf-8"))["api_server"]
    return str(cfg["username"]), str(cfg["password"])


SHOW_CONFIG_KEYS = ("version", "strategy_version", "dry_run", "state", "runmode", "strategy", "bot_name", "timeframe",
                    "stake_currency", "stake_amount", "max_open_trades", "trading_mode", "exchange", "dry_run_wallet")
TRADE_API_KEYS = ("trade_id", "pair", "is_open", "open_date", "open_timestamp", "open_rate", "amount", "stake_amount",
                  "current_rate", "profit_abs", "profit_ratio", "total_profit_abs", "total_profit_ratio",
                  "realized_profit", "funding_fees", "leverage", "nr_of_successful_entries", "nr_of_successful_exits")


def api_snapshot(base_url: str, secrets_path: str | Path | None, *, fetch: HttpFetch | None = None,
                 timeout: float = 3.0) -> dict[str, Any]:
    """Estado vivo desde la API REST: show_config, health, balance y operaciones abiertas con precio actual.

    Nunca lanza: devuelve `{"ok": False, "error": ...}` si no hay credenciales o el bot no responde.
    """
    out: dict[str, Any] = {"ok": False, "url": base_url, "error": None}
    if not secrets_path:
        out["error"] = "sin archivo de credenciales configurado"
        return out
    try:
        user, password = read_credentials(secrets_path)
    except FileNotFoundError:
        out["error"] = f"no existe {Path(secrets_path).name}"
        return out
    except (OSError, ValueError, KeyError, TypeError):
        out["error"] = f"{Path(secrets_path).name} ilegible (falta api_server.username/password)"
        return out
    token = base64.b64encode(f"{user}:{password}".encode()).decode()
    headers = {"Authorization": f"Basic {token}", "Accept": "application/json"}
    get = fetch or _urllib_fetch
    base = base_url.rstrip("/") + "/api/v1/"
    errors = []
    for name in ("show_config", "health", "balance", "status"):
        try:
            status, body = get(base + name, headers, timeout)
        except (OSError, urllib.error.URLError, TimeoutError) as exc:
            errors.append(f"{name}: {getattr(exc, 'reason', exc)}")
            if name == "show_config":
                break  # el bot no responde: no insistir con las demas
            continue
        if status == 401:
            errors.append(f"{name}: credenciales rechazadas (401)")
            break
        if status != 200:
            errors.append(f"{name}: HTTP {status}")
            continue
        try:
            data = json.loads(body)
        except ValueError:
            errors.append(f"{name}: respuesta no JSON")
            continue
        if name == "show_config" and isinstance(data, dict):
            out[name] = {k: data.get(k) for k in SHOW_CONFIG_KEYS if k in data}
        elif name == "status" and isinstance(data, list):
            out[name] = [{k: t.get(k) for k in TRADE_API_KEYS if k in t} for t in data if isinstance(t, dict)]
        elif name == "balance" and isinstance(data, dict):
            out[name] = {k: data.get(k) for k in ("total", "total_bot", "value", "stake", "starting_capital",
                                                   "starting_capital_ratio", "note") if k in data}
        else:
            out[name] = data
    out["ok"] = any(k in out for k in ("show_config", "health", "balance", "status"))
    out["error"] = "; ".join(errors) or None
    return out


# ---- vista del panel --------------------------------------------------------------------------------------


def paper_state(d: dict[str, Any], *, api: dict[str, Any] | None = None, wallet_start: float | None = None,
                now_ms: int | None = None) -> dict[str, Any]:
    """Estado del dry-run para el panel: wallet, abiertas (con PnL no realizado si la API respondio),
    cerradas con PnL, curva de equity por operacion cerrada y resumen por par."""
    now_ms = now_ms or int(time.time() * 1000)
    api = api or {}
    cfg = api.get("show_config") or {}
    bal = api.get("balance") or {}
    live = {t.get("trade_id"): t for t in (api.get("status") or [])}
    start_wallet = _f(bal.get("starting_capital")) or _f(cfg.get("dry_run_wallet")) or wallet_start or DEFAULT_WALLET
    fl = fills(d)
    fills_by_trade: dict[Any, list[dict[str, Any]]] = {}
    for f in fl:
        fills_by_trade.setdefault(f["trade_id"], []).append(f)

    def base_trade(t: dict[str, Any]) -> dict[str, Any]:
        fs = fills_by_trade.get(t["id"], [])
        fees = (t["fee_open_cost"] or 0.0) + (t["fee_close_cost"] or 0.0)
        return {
            "trade_id": t["id"], "pair": t["pair"], "symbol": t["symbol"], "direction": "short" if t["is_short"] else "long",
            "open_ms": t["open_ms"], "open_at": iso(t["open_ms"]), "open_rate": t["open_rate"], "amount": t["amount"],
            "stake_amount": t["stake_amount"], "max_stake_amount": t["max_stake_amount"], "leverage": t["leverage"],
            "fees": fees, "funding": t["funding_fees"] or 0.0, "enter_tag": t["enter_tag"],
            "orders": len(fs), "adjustments": sum(1 for f in fs if f["kind"].startswith("ajuste")),
            "fills": [{"time_ms": f["time_ms"], "t": iso(f["time_ms"]), "kind": f["kind"], "price": f["price"],
                       "units": abs(f["du"]), "notional": f["notional"], "fee": f["fee"], "tag": f["tag"]} for f in fs],
        }

    closed, open_ = [], []
    for t in d.get("trades", []):
        b = base_trade(t)
        if t["is_open"]:
            lv = live.get(t["id"]) or {}
            cur = _f(lv.get("current_rate"))
            unreal = _f(lv.get("profit_abs"))
            b.update({
                "current_rate": cur, "unrealized_pnl": unreal, "unrealized_ratio": _f(lv.get("profit_ratio")),
                "realized_partial": t["realized_profit"] or 0.0,
                "duration_s": (now_ms - t["open_ms"]) / 1000 if t["open_ms"] else None,
                "live": bool(lv),
            })
            open_.append(b)
        else:
            pnl = t["close_profit_abs"]
            b.update({
                "close_ms": t["close_ms"], "close_at": iso(t["close_ms"]), "close_rate": t["close_rate"],
                "pnl": pnl, "pnl_ratio": t["close_profit"], "exit_reason": t["exit_reason"],
                "duration_s": (t["close_ms"] - t["open_ms"]) / 1000 if t["close_ms"] and t["open_ms"] else None,
                "win": None if pnl is None else pnl > 0,
            })
            closed.append(b)
    closed.sort(key=lambda x: x["close_ms"] or 0)
    open_.sort(key=lambda x: x["open_ms"] or 0)

    equity = []
    cum = 0.0
    first_ms = d.get("kv", {}).get("bot_start_time") or (min((t["open_ms"] for t in d.get("trades", []) if t["open_ms"]), default=None))
    if first_ms:
        equity.append({"time_ms": first_ms, "t": iso(first_ms), "equity": start_wallet, "pnl": 0.0, "trade_id": None})
    for c in closed:
        cum += c["pnl"] or 0.0
        equity.append({"time_ms": c["close_ms"], "t": iso(c["close_ms"]), "equity": start_wallet + cum, "pnl": c["pnl"],
                       "trade_id": c["trade_id"], "symbol": c["symbol"]})

    realized_partial = sum(o["realized_partial"] for o in open_)
    unreal_known = [o["unrealized_pnl"] for o in open_ if o["unrealized_pnl"] is not None]
    unrealized = sum(unreal_known) if len(unreal_known) == len(open_) else None
    realized = cum + realized_partial
    wins = sum(1 for c in closed if c["win"])
    fees = sum(x["fees"] for x in closed + open_)
    funding = sum(x["funding"] for x in closed + open_)
    equity_now = None if unrealized is None else start_wallet + realized + unrealized
    if equity_now is not None:
        equity.append({"time_ms": now_ms, "t": iso(now_ms), "equity": equity_now, "pnl": None, "trade_id": None,
                       "mark_to_market": True})

    pairs: dict[str, dict[str, Any]] = {}
    for x in closed + open_:
        p = pairs.setdefault(x["symbol"], {"symbol": x["symbol"], "pair": x["pair"], "closed": 0, "wins": 0, "realized": 0.0,
                                           "fees": 0.0, "funding": 0.0, "open": False, "stake_open": None,
                                           "unrealized": None, "orders": 0, "adjustments": 0, "last_ms": None})
        p["fees"] += x["fees"]
        p["funding"] += x["funding"]
        p["orders"] += x["orders"]
        p["adjustments"] += x["adjustments"]
        p["last_ms"] = max(p["last_ms"] or 0, x.get("close_ms") or x["open_ms"] or 0) or None
        if "close_ms" in x:
            p["closed"] += 1
            p["wins"] += int(bool(x["win"]))
            p["realized"] += x["pnl"] or 0.0
        else:
            p["open"] = True
            p["stake_open"] = x["stake_amount"]
            p["realized"] += x["realized_partial"]
            p["unrealized"] = x["unrealized_pnl"]
    for p in pairs.values():
        p["hit_rate"] = p["wins"] / p["closed"] if p["closed"] else None
        p["ci95"] = wilson(p["wins"], p["closed"])

    kv = d.get("kv", {})
    health = api.get("health") or {}
    startup_ms = _f(health.get("bot_startup_ts"))
    startup_ms = int(startup_ms * 1000) if startup_ms and startup_ms < 1e11 else (int(startup_ms) if startup_ms else kv.get("startup_time"))
    bot_start = _f(health.get("bot_start_ts"))
    bot_start = int(bot_start * 1000) if bot_start and bot_start < 1e11 else (int(bot_start) if bot_start else kv.get("bot_start_time"))
    last_proc = _f(health.get("last_process_ts"))
    last_proc = int(last_proc * 1000) if last_proc and last_proc < 1e11 else (int(last_proc) if last_proc else None)
    notes = []
    if len(closed) < SMALL_N:
        notes.append(f"Muestra chica: {len(closed)} operaciones cerradas. Con menos de {SMALL_N} la tasa de acierto y el "
                     "PnL por operacion no permiten sacar conclusiones (mirar el IC).")
    if not api.get("ok"):
        notes.append("La API de Freqtrade no respondio: los datos salen de la base y falta el PnL no realizado "
                     "(precio actual) de las operaciones abiertas." + (f" ({api.get('error')})" if api.get("error") else ""))
    return {
        "source": {"db": d.get("db_path"), "read_at_ms": d.get("read_at_ms"), "read_at": iso(d.get("read_at_ms")),
                   "schema": d.get("schema"), "api": {"ok": bool(api.get("ok")), "url": api.get("url"), "error": api.get("error")}},
        "bot": {
            "dry_run": cfg.get("dry_run"), "state": cfg.get("state"), "strategy": cfg.get("strategy") or next(
                (t["strategy"] for t in d.get("trades", []) if t.get("strategy")), None),
            "bot_name": cfg.get("bot_name"), "version": cfg.get("version"), "timeframe": cfg.get("timeframe"),
            "trading_mode": cfg.get("trading_mode"), "max_open_trades": cfg.get("max_open_trades"),
            "bot_start_ms": bot_start, "bot_start_at": iso(bot_start), "startup_ms": startup_ms, "startup_at": iso(startup_ms),
            "uptime_s": (now_ms - startup_ms) / 1000 if startup_ms else None,
            "paper_days": (now_ms - bot_start) / 86_400_000 if bot_start else None,
            "last_process_ms": last_proc, "last_process_at": iso(last_proc),
        },
        "wallet": {
            "start": start_wallet, "total_api": _f(bal.get("total")), "realized": realized, "unrealized": unrealized,
            "equity": equity_now, "return": None if equity_now is None else equity_now / start_wallet - 1,
            "realized_return": realized / start_wallet, "fees": fees, "funding": funding,
            "stake_open": sum(o["stake_amount"] or 0.0 for o in open_),
        },
        "totals": {"open": len(open_), "closed": len(closed), "wins": wins,
                   "hit_rate": wins / len(closed) if closed else None, "ci95": wilson(wins, len(closed)),
                   "orders": len(fl), "adjustments": sum(1 for f in fl if f["kind"].startswith("ajuste"))},
        "open_trades": open_,
        "closed_trades": closed,
        "equity": equity,
        "by_pair": sorted(pairs.values(), key=lambda p: p["symbol"] or ""),
        "notes": notes,
    }


def brief(d: dict[str, Any]) -> dict[str, Any]:
    """Resumen liviano para /api/summary (sin API REST): conteos y PnL realizado."""
    trades = d.get("trades", [])
    closed = [t for t in trades if not t["is_open"]]
    return {
        "open": sum(1 for t in trades if t["is_open"]),
        "closed": len(closed),
        "realized": sum(t["close_profit_abs"] or 0.0 for t in closed) + sum(t["realized_profit"] or 0.0 for t in trades if t["is_open"]),
        "bot_start_ms": d.get("kv", {}).get("bot_start_time"),
        "last_trade_ms": max((t["close_ms"] or t["open_ms"] or 0 for t in trades), default=None) or None,
        "read_at_ms": d.get("read_at_ms"),
    }


# ---- CLI (tambien es lo que corre la PC por SSH con `python3 - dump RUTA`) ---------------------------------


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 2 or args[0] != "dump":
        print("uso: python -m heatsignal.paper.freqtrade_db dump RUTA_SQLITE", file=sys.stderr)
        return 2
    try:
        out = dump(args[1])
    except FileNotFoundError as exc:
        print(json.dumps({"error": str(exc), "missing": True}))
        return 3
    print(json.dumps(out, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    sys.exit(main())
