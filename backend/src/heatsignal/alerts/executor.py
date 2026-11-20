"""
Ejecutor de alertas en la DEMO DE BYBIT (fase 4 del plan "estrategias generadas + alertas", 2026-11-21).

Corre despues de cada pasada del motor de alertas (`ExecStartPost` de heatsignal-alerts.service) con el Python de
Freqtrade (trae ccxt; el backend no lo necesita). Toma:
  1. las alertas de ENTRADA / GIRO mandadas a la demo (boton "Mandar a la demo" o automatico de la estrategia);
  2. TODA alerta de SALIDA / GIRO de una estrategia que tiene una posicion abierta por este ejecutor en ese simbolo
     (una posicion abierta siempre se cierra cuando la regla sale, aunque nadie mande la salida).

Posiciones VIRTUALES por (estrategia, simbolo): cada estrategia tiene la suya; en el exchange (modo one-way) queda la
suma neta por simbolo. Cada orden manda solo el delta de esa estrategia y su precio de fill se le atribuye a ella. En
cada corrida se concilia la suma virtual contra la posicion real del exchange (diferencias -> aviso en el panel).

Tamano: `notional_usdt` x |objetivo| (default 1.000 USDT por unidad), apalancamiento 1x. Limites: `max_open`
posiciones virtuales abiertas y `max_gross_usdt` de exposicion bruta; una entrada que los pasa se rechaza con motivo.
Una entrada mas vieja que `max_entry_age_s` (el precio ya se movio) se rechaza; una salida se ejecuta siempre.
Sin stop en el exchange: las salidas (stop / objetivo / tiempo) las decide la regla y llegan como alertas (demora
de hasta ~5 min: la pasada del motor).

CANDADO: el ejecutor solo opera si la clave es de la DEMO: tiene que funcionar contra api-demo.bybit.com y ser
RECHAZADA por la cuenta real (api.bybit.com). Si la cuenta real la acepta, se niega a operar (clave equivocada).
Todo queda en `data/alerts.sqlite`: tabla `exec_events` (append-only) + acciones 'ejecutada' / 'rechazada'.
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .store import AlertStore

log = logging.getLogger("heatsignal.alerts.executor")

EXEC_SCHEMA = """
CREATE TABLE IF NOT EXISTS exec_events (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_ms        INTEGER NOT NULL,
    run_id       TEXT    NOT NULL,
    alert_id     TEXT,
    strategy_id  TEXT,
    symbol       TEXT,
    kind         TEXT    NOT NULL,      -- fill | rechazo | candado | conciliacion | error
    qty_delta    REAL,                  -- + compra / - venta (en la moneda base)
    price        REAL,
    notional     REAL,
    fee          REAL,
    order_id     TEXT,
    detail       TEXT
);
CREATE TRIGGER IF NOT EXISTS exec_events_no_update BEFORE UPDATE ON exec_events
BEGIN SELECT RAISE(ABORT, 'exec_events es append-only'); END;
CREATE TRIGGER IF NOT EXISTS exec_events_no_delete BEFORE DELETE ON exec_events
BEGIN SELECT RAISE(ABORT, 'exec_events es append-only'); END;
"""


@dataclass
class ExecConfig:
    alerts_db: Path
    keys_path: Path
    notional_usdt: float = 1_000.0
    max_open: int = 10
    max_gross_usdt: float = 10_000.0
    max_entry_age_s: int = 15 * 60


def default_config() -> ExecConfig:
    data = Path(os.environ.get("HEATSIGNAL_DATA_DIR", "~/heatsignal/data")).expanduser()
    keys = os.environ.get("HEATSIGNAL_DEMO_ALERTS_KEYS", "~/heatsignal/secrets/bybit_demo_alertas.json")
    return ExecConfig(alerts_db=Path(os.environ.get("HEATSIGNAL_ALERTS_DB", data / "alerts.sqlite")).expanduser(),
                      keys_path=Path(keys).expanduser())


def ccxt_symbol(symbol: str) -> str:
    """'BTCUSDT' -> 'BTC/USDT:USDT' (perpetuo lineal de Bybit)."""
    base = symbol[:-4] if symbol.endswith("USDT") else symbol
    return f"{base}/USDT:USDT"


class DemoLockError(RuntimeError):
    """La clave no es de la demo (o no se pudo comprobar): no se opera."""


def read_keys(path: Path) -> tuple[str, str]:
    d = json.loads(path.read_text(encoding="utf-8"))
    key, secret = str(d.get("key") or ""), str(d.get("secret") or "")
    if not key or not secret or "PEGAR" in key + secret:
        raise DemoLockError(f"faltan la clave y el secreto de la cuenta demo en {path}")
    return key, secret


def make_exchanges(key: str, secret: str) -> tuple[Any, Any]:
    """(demo, real) con la MISMA clave. La real solo se usa para comprobar que la rechaza (candado)."""
    import ccxt  # Python de Freqtrade

    opts = {"apiKey": key, "secret": secret, "enableRateLimit": True,
            "options": {"defaultType": "swap", "adjustForTimeDifference": True, "recvWindow": 10_000}}
    demo = ccxt.bybit(dict(opts))
    demo.enable_demo_trading(True)
    real = ccxt.bybit(dict(opts))
    return demo, real


def check_demo_only(demo: Any, real: Any) -> dict[str, Any]:
    """Candado: la clave tiene que funcionar en la demo y NO en la cuenta real."""
    if "demo" not in json.dumps(getattr(demo, "urls", {}), default=str).lower():
        raise DemoLockError("el cliente no apunta a la demo de Bybit")
    try:
        demo.load_time_difference()
        info = demo.privateGetV5UserQueryApi().get("result", {})
    except Exception as exc:  # noqa: BLE001
        raise DemoLockError(f"la clave no funciona en la demo: {type(exc).__name__}: {str(exc)[:200]}") from exc
    try:
        real.load_time_difference()
        real.privateGetV5UserQueryApi()
    except Exception as exc:  # noqa: BLE001 - lo esperado: la cuenta real la rechaza
        if "10003" in str(exc) or "invalid" in str(exc).lower() or type(exc).__name__ == "AuthenticationError":
            perms = info.get("permissions") or {}
            if "Withdraw" in (perms.get("Wallet") or []):
                raise DemoLockError("la clave tiene permiso de retiro: crearla sin retiros") from None
            return {"demo_uid": info.get("userID"), "permisos": perms}
        raise DemoLockError(f"no se pudo comprobar que la clave NO sea de la cuenta real: {type(exc).__name__}") from exc
    raise DemoLockError("la cuenta REAL acepta esta clave: no es una clave de la demo. No se opera.")


# ---- libro virtual ---------------------------------------------------------------------------------------------


class ExecBook:
    def __init__(self, store: AlertStore) -> None:
        self.store = store
        store.conn.executescript(EXEC_SCHEMA)

    def add(self, run_id: str, kind: str, *, alert_id: str | None = None, strategy_id: str | None = None,
            symbol: str | None = None, qty_delta: float | None = None, price: float | None = None,
            notional: float | None = None, fee: float | None = None, order_id: str | None = None,
            detail: dict[str, Any] | None = None) -> None:
        self.store.conn.execute(
            "INSERT INTO exec_events (ts_ms, run_id, alert_id, strategy_id, symbol, kind, qty_delta, price, notional, fee, "
            "order_id, detail) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (int(time.time() * 1000), run_id, alert_id, strategy_id, symbol, kind, qty_delta, price, notional, fee,
             order_id, None if detail is None else json.dumps(detail, ensure_ascii=False, default=str)))

    def positions(self) -> dict[tuple[str, str], dict[str, float]]:
        """(estrategia, simbolo) -> {qty, avg, realized, fees} con costo promedio (solo fills)."""
        out: dict[tuple[str, str], dict[str, Any]] = {}
        for sid, sym, q, px, fee, aid in self.store.conn.execute(
                "SELECT strategy_id, symbol, qty_delta, price, COALESCE(fee, 0), alert_id FROM exec_events "
                "WHERE kind = 'fill' ORDER BY id"):
            p = out.setdefault((sid, sym), {"qty": 0.0, "avg": 0.0, "realized": 0.0, "fees": 0.0, "open_alert": None})
            p["fees"] += fee
            if p["qty"] == 0 or (p["qty"] > 0) != (p["qty"] + q > 0):
                p["open_alert"] = aid  # alerta que abrio (o dio vuelta) la posicion vigente
            if p["qty"] == 0 or (p["qty"] > 0) == (q > 0):  # abre o agranda
                new = p["qty"] + q
                p["avg"] = (p["avg"] * abs(p["qty"]) + px * abs(q)) / abs(new) if new else 0.0
                p["qty"] = new
            else:  # reduce, cierra o da vuelta
                closed = min(abs(q), abs(p["qty"]))
                p["realized"] += closed * (px - p["avg"]) * (1 if p["qty"] > 0 else -1)
                rest = p["qty"] + q
                if abs(rest) < 1e-12:
                    p["qty"], p["avg"] = 0.0, 0.0
                elif (rest > 0) == (p["qty"] > 0):
                    p["qty"] = rest
                else:
                    p["qty"], p["avg"] = rest, px
        return out

    def recent(self, limit: int = 50) -> list[dict[str, Any]]:
        cols = ("id", "ts_ms", "run_id", "alert_id", "strategy_id", "symbol", "kind", "qty_delta", "price", "notional",
                "fee", "order_id", "detail")
        rows = self.store.conn.execute(f"SELECT {', '.join(cols)} FROM exec_events ORDER BY id DESC LIMIT ?", (limit,))
        return [{**dict(zip(cols, r)), "detail": json.loads(r[-1]) if r[-1] else None} for r in rows]


# ---- corrida ---------------------------------------------------------------------------------------------------


def _work(store: AlertStore, book: ExecBook) -> list[tuple[dict[str, Any], str]]:
    """[(alerta, motivo)] en orden de decision: entradas/giros mandados + salidas/giros de posiciones abiertas."""
    pos = book.positions()
    done = {r[0] for r in store.conn.execute(
        "SELECT alert_id FROM alert_actions WHERE action IN ('ejecutada', 'rechazada', 'descartada')")}
    items: dict[str, tuple[dict[str, Any], str]] = {}
    for a in store.queued():
        items[a["alert_id"]] = (a, "mandada")
    opened: dict[tuple[str, str], int] = {}  # decision de la alerta que abrio cada posicion vigente
    for k, p in pos.items():
        if abs(p["qty"]) > 0:
            src = store.get(p["open_alert"]) if p.get("open_alert") else None
            opened[k] = src["decision_ms"] if src else 0
    if opened:
        for a in store.recent(limit=500, kind="salida") + store.recent(limit=500, kind="giro"):
            k = (a["strategy_id"], a["symbol"])
            # solo salidas decididas DESPUES de la entrada vigente (una salida vieja sin procesar no cierra la nueva)
            if k in opened and a["decision_ms"] > opened[k] and a["alert_id"] not in done and a["alert_id"] not in items:
                items[a["alert_id"]] = (a, "cierre de posicion abierta")
    return sorted(items.values(), key=lambda x: (x[0]["decision_ms"], x[0]["alert_id"]))


def run_once(cfg: ExecConfig, *, exchanges: tuple[Any, Any] | None = None, now_ms: int | None = None) -> dict[str, Any]:
    now = now_ms or int(time.time() * 1000)
    run_id = f"exec-{now}"
    store = AlertStore(cfg.alerts_db)
    book = ExecBook(store)
    try:
        todo = _work(store, book)
        if not todo:
            return {"run_id": run_id, "status": "ok", "ejecutadas": 0, "rechazadas": 0, "nota": "nada en cola"}
        try:
            if exchanges is None:
                key, secret = read_keys(cfg.keys_path)
                exchanges = make_exchanges(key, secret)
            demo, real = exchanges
            lock = check_demo_only(demo, real)
        except (DemoLockError, OSError, ValueError) as exc:
            book.add(run_id, "candado", detail={"error": str(exc), "en_cola": len(todo)})
            return {"run_id": run_id, "status": "bloqueado", "error": str(exc), "en_cola": len(todo)}
        demo.load_markets()
        done_n = rej_n = 0
        for alert, why in todo:
            res = _execute(cfg, store, book, demo, alert, why, run_id, now)
            done_n += res == "ejecutada"
            rej_n += res == "rechazada"
        rec = reconcile(book, demo)
        if rec["diferencias"] or rec.get("error"):
            book.add(run_id, "conciliacion", detail=rec)
        return {"run_id": run_id, "status": "ok", "ejecutadas": done_n, "rechazadas": rej_n, "candado": lock,
                "conciliacion": rec}
    finally:
        store.close()


def _reject(store: AlertStore, book: ExecBook, run_id: str, alert: dict[str, Any], motivo: str) -> str:
    book.add(run_id, "rechazo", alert_id=alert["alert_id"], strategy_id=alert["strategy_id"], symbol=alert["symbol"],
             detail={"motivo": motivo})
    store.act(alert["alert_id"], "rechazada", origin="ejecutor", detail={"motivo": motivo})
    return "rechazada"


def _execute(cfg: ExecConfig, store: AlertStore, book: ExecBook, demo: Any, alert: dict[str, Any], why: str,
             run_id: str, now: int) -> str:
    sid, sym = alert["strategy_id"], alert["symbol"]
    pos = book.positions()
    cur = pos.get((sid, sym), {}).get("qty", 0.0)
    target = float(alert["target"])
    is_entry = alert["kind"] == "entrada" or (alert["kind"] == "giro" and cur == 0)
    if target == 0 and cur == 0:
        return _reject(store, book, run_id, alert, "no hay posicion abierta de esta estrategia en este simbolo")
    if is_entry and cur != 0:
        return _reject(store, book, run_id, alert, "la estrategia ya tiene una posicion abierta en este simbolo")
    if is_entry and now - alert["decision_ms"] > cfg.max_entry_age_s * 1000:
        return _reject(store, book, run_id, alert,
                       f"entrada vieja (mas de {cfg.max_entry_age_s // 60} min): el precio ya se movio")
    market = ccxt_symbol(sym)
    try:
        last = float(demo.fetch_ticker(market)["last"])
    except Exception as exc:  # noqa: BLE001 - queda en cola: se reintenta en la proxima corrida
        book.add(run_id, "error", alert_id=alert["alert_id"], strategy_id=sid, symbol=sym,
                 detail={"error": f"precio: {type(exc).__name__}: {exc}"[:300]})
        return "error"
    want = 0.0 if target == 0 else (1 if target > 0 else -1) * cfg.notional_usdt * abs(target) / last
    delta = want - cur
    if target != 0:
        n_open = sum(1 for k, p in pos.items() if p["qty"] != 0 and k != (sid, sym))
        gross = sum(abs(p["qty"]) * p["avg"] for k, p in pos.items() if k != (sid, sym)) + abs(want) * last
        if n_open + 1 > cfg.max_open:
            return _reject(store, book, run_id, alert, f"limite de {cfg.max_open} posiciones abiertas")
        if gross > cfg.max_gross_usdt:
            return _reject(store, book, run_id, alert, f"limite de exposicion bruta {cfg.max_gross_usdt:.0f} USDT")
    amount = float(demo.amount_to_precision(market, abs(delta)))
    if amount <= 0:
        return _reject(store, book, run_id, alert, "cantidad menor al minimo del exchange")
    side = "buy" if delta > 0 else "sell"
    try:
        order = demo.create_order(market, "market", side, amount, None, {"positionIdx": 0})
        if not order.get("average"):
            order = demo.fetch_order(order["id"], market)
    except Exception as exc:  # noqa: BLE001
        return _reject(store, book, run_id, alert, f"el exchange rechazo la orden: {type(exc).__name__}: {str(exc)[:200]}")
    filled = float(order.get("filled") or amount)
    price = float(order.get("average") or order.get("price") or last)
    fee = float((order.get("fee") or {}).get("cost") or 0.0)
    q = filled if side == "buy" else -filled
    book.add(run_id, "fill", alert_id=alert["alert_id"], strategy_id=sid, symbol=sym, qty_delta=q, price=price,
             notional=filled * price, fee=fee, order_id=str(order.get("id")),
             detail={"motivo": why, "objetivo": target, "precio_alerta": alert.get("price"), "ultimo": last})
    store.act(alert["alert_id"], "ejecutada", origin="ejecutor",
              detail={"order_id": order.get("id"), "precio": price, "cantidad": q, "motivo": why})
    return "ejecutada"


def reconcile(book: ExecBook, demo: Any) -> dict[str, Any]:
    """Suma virtual por simbolo vs posicion neta del exchange (contratos lineales: cantidad = moneda base)."""
    virt: dict[str, float] = {}
    for (_, sym), p in book.positions().items():
        virt[sym] = virt.get(sym, 0.0) + p["qty"]
    real: dict[str, float] = {}
    try:
        for p in demo.fetch_positions():
            c = float(p.get("contracts") or 0.0)
            if c:
                sym = str(p.get("symbol", "")).split("/")[0] + "USDT"
                real[sym] = real.get(sym, 0.0) + (c if p.get("side") == "long" else -c)
    except Exception as exc:  # noqa: BLE001
        return {"error": f"{type(exc).__name__}: {exc}"[:300], "diferencias": {}}
    diffs = {s: {"virtual": virt.get(s, 0.0), "exchange": real.get(s, 0.0)}
             for s in set(virt) | set(real)
             if abs(virt.get(s, 0.0) - real.get(s, 0.0)) > 1e-6 * max(1.0, abs(real.get(s, 0.0)))}
    return {"virtual": virt, "exchange": real, "diferencias": diffs}
