"""
Puente hipotesis de 1h -> Freqtrade DRY-RUN de hipotesis (`heatsignal-freqtrade-hyp`, API 127.0.0.1:8082).

Que hace cada corrida (`heatsignal-ft-bridge.timer`, cada 5 min):
  1. Lee de `heatsignal_eval.sqlite` (SOLO LECTURA) las hipotesis NUEVAS de las estrategias de 1h
     (`exit_conditions.timeframe = "1h"` y `hypothesis_context.timeframe = '1h'`), sin resolucion y con `due_at`
     futuro. "Nueva" = emitida desde el primer arranque del puente (menos un margen de una corrida): al desplegarlo
     no se mandan hipotesis viejas.
  2. Por cada una hace `POST /api/v1/forceenter` con la direccion de la hipotesis, `stakeamount` fijo, leverage 1 y
     `entry_tag = hyp:<id8>:<due_epoch>` (la estrategia `HypothesisBridgeV1` cierra por tiempo al llegar a due_at).
  3. Registra CADA intento en `ft_bridge_orders` (base propia `data/ft_bridge.sqlite`, append-only con triggers):
       sent                 Freqtrade abrio el trade (trade_id, precio de entrada)
       skipped_pair_busy    ya hay un trade abierto en ese par (Freqtrade no admite dos por par sin ajuste)
       skipped_late         vencio o esta por vencer sin haberse podido mandar (ver reglas abajo)
       skipped_unsupported  el simbolo no esta en la whitelist de la instancia
       error                la API no respondio o rechazo el pedido: se reintenta en la proxima corrida
     Un indice UNIQUE parcial (hypothesis_id WHERE status <> 'error') garantiza a nivel de motor que una hipotesis
     tiene como mucho UN estado final: nunca se manda dos veces.

Reglas de reintento (sin fallos silenciosos: todo queda como fila):
  - Primer intento: solo si faltan >= MIN_FIRST_LEFT_S (5 min) para due_at.
  - Tras un `error`: se reintenta solo si due_at sigue a mas de RETRY_MIN_LEFT_S (1 h); si no, `skipped_late`.
  - Hipotesis nueva que vencio sin ningun intento (el puente estuvo caido toda su ventana): `skipped_late`.
  - Antes de mandar se lee `/api/v1/status`: si el trade del par ya tiene el tag de ESTA hipotesis (un envio
    anterior que dio timeout pero entro), se registra `sent` con ese trade_id en vez de mandarlo de nuevo.

Credenciales: `api_server.username/password` de `freqtrade/config.hyp.secrets.json`; nunca se imprimen ni se
guardan (los errores se recortan y no incluyen headers).

Solo libreria estandar (corre como oneshot liviano en la VM).

    python -m heatsignal.paper.ft_bridge run       # una corrida
    python -m heatsignal.paper.ft_bridge status    # ultimas corridas y envios

Variables: HEATSIGNAL_EVAL_DB, HEATSIGNAL_FT_BRIDGE_DB (default data/ft_bridge.sqlite), HEATSIGNAL_FT_HYP_API
(default http://127.0.0.1:8082), HEATSIGNAL_FT_HYP_SECRETS (default ~/heatsignal/freqtrade/config.hyp.secrets.json),
HEATSIGNAL_FT_HYP_STAKE (default 1000).
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .freqtrade_db import read_credentials

DEFAULT_BRIDGE_DB = "data/ft_bridge.sqlite"
DEFAULT_API = "http://127.0.0.1:8082"
DEFAULT_SECRETS = "~/heatsignal/freqtrade/config.hyp.secrets.json"
DEFAULT_STAKE = 1000.0
MIN_FIRST_LEFT_S = 300      # primer envio: al menos 5 min hasta el vencimiento
RETRY_MIN_LEFT_S = 3600     # reintento tras error: solo si due_at sigue a mas de 1 h
START_GRACE_S = 600         # "nuevas" = emitidas desde (primer arranque del puente - 10 min)
LOOKBACK_S = 2 * 86_400     # para marcar `skipped_late` las que vencieron sin intento
MAX_DETAIL = 300
STATUSES = ("sent", "skipped_pair_busy", "skipped_late", "skipped_unsupported", "error")
TAG_RE = re.compile(r"^hyp:([0-9a-f]{8}):([0-9]{9,11})$")

SCHEMA = """
CREATE TABLE IF NOT EXISTS ft_bridge_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS trg_ft_bridge_meta_no_update BEFORE UPDATE ON ft_bridge_meta
BEGIN SELECT RAISE(ABORT, 'ft_bridge_meta es append-only: UPDATE no permitido'); END;
CREATE TRIGGER IF NOT EXISTS trg_ft_bridge_meta_no_delete BEFORE DELETE ON ft_bridge_meta
BEGIN SELECT RAISE(ABORT, 'ft_bridge_meta es append-only: DELETE no permitido'); END;

CREATE TABLE IF NOT EXISTS ft_bridge_runs (
    run_id      TEXT PRIMARY KEY,
    ts_ms       INTEGER NOT NULL,
    status      TEXT NOT NULL CHECK (status IN ('idle', 'ok', 'api_down', 'partial')),
    candidates  INTEGER NOT NULL,
    sent        INTEGER NOT NULL,
    skipped     INTEGER NOT NULL,
    errors      INTEGER NOT NULL,
    detail      TEXT
);
CREATE TRIGGER IF NOT EXISTS trg_ft_bridge_runs_no_update BEFORE UPDATE ON ft_bridge_runs
BEGIN SELECT RAISE(ABORT, 'ft_bridge_runs es append-only: UPDATE no permitido'); END;
CREATE TRIGGER IF NOT EXISTS trg_ft_bridge_runs_no_delete BEFORE DELETE ON ft_bridge_runs
BEGIN SELECT RAISE(ABORT, 'ft_bridge_runs es append-only: DELETE no permitido'); END;

CREATE TABLE IF NOT EXISTS ft_bridge_orders (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id         TEXT NOT NULL,
    ts_ms          INTEGER NOT NULL,
    hypothesis_id  TEXT NOT NULL,
    strategy_id    TEXT NOT NULL,
    strategy_name  TEXT,
    symbol         TEXT NOT NULL,
    pair           TEXT,
    direction      TEXT NOT NULL CHECK (direction IN ('LONG', 'SHORT')),
    created_ts     REAL NOT NULL,
    due_ts         INTEGER NOT NULL,
    entry_tag      TEXT NOT NULL,
    stake_usdt     REAL,
    status         TEXT NOT NULL CHECK (status IN ('sent', 'skipped_pair_busy', 'skipped_late',
                                                   'skipped_unsupported', 'error')),
    trade_id       INTEGER,
    open_rate      REAL,
    detail         TEXT,
    CHECK (status <> 'sent' OR trade_id IS NOT NULL)
);
CREATE INDEX IF NOT EXISTS idx_ft_bridge_orders_hyp ON ft_bridge_orders (hypothesis_id, id);
-- Una hipotesis tiene como mucho UN estado final (todo menos 'error'): nunca se manda dos veces.
CREATE UNIQUE INDEX IF NOT EXISTS uq_ft_bridge_orders_final ON ft_bridge_orders (hypothesis_id)
    WHERE status <> 'error';
CREATE TRIGGER IF NOT EXISTS trg_ft_bridge_orders_no_update BEFORE UPDATE ON ft_bridge_orders
BEGIN SELECT RAISE(ABORT, 'ft_bridge_orders es append-only: UPDATE no permitido'); END;
CREATE TRIGGER IF NOT EXISTS trg_ft_bridge_orders_no_delete BEFORE DELETE ON ft_bridge_orders
BEGIN SELECT RAISE(ABORT, 'ft_bridge_orders es append-only: DELETE no permitido'); END;
"""


# ---- tag y par ----------------------------------------------------------------------------------------


def encode_tag(hypothesis_id: str, due_ts: float) -> str:
    """entry_tag de Freqtrade: 'hyp:<8 hex del hypothesis_id>:<due_at en segundos UTC>'."""
    id8 = str(hypothesis_id).replace("-", "").lower()[:8]
    if not re.fullmatch(r"[0-9a-f]{8}", id8):
        raise ValueError(f"hypothesis_id invalido para el tag: {hypothesis_id!r}")
    return f"hyp:{id8}:{int(due_ts)}"


def decode_tag(tag: str | None) -> tuple[str, int] | None:
    """Inverso de encode_tag (mismo formato que decode_tag de HypothesisBridgeV1). None si no es del puente."""
    if not tag:
        return None
    m = TAG_RE.match(str(tag).strip())
    return (m.group(1), int(m.group(2))) if m else None


def to_pair(symbol: str) -> str | None:
    """'BTC-USDT' / 'BTCUSDT' -> 'BTC/USDT:USDT' (perpetuo USDT-M de Freqtrade). None si no es un par en USDT."""
    s = str(symbol or "").upper().replace("/", "-").replace("_", "-")
    if "-" in s:
        base, _, quote = s.partition("-")
    elif s.endswith("USDT"):
        base, quote = s[:-4], "USDT"
    else:
        return None
    if quote != "USDT" or not re.fullmatch(r"[A-Z0-9]{1,20}", base or ""):
        return None
    return f"{base}/USDT:USDT"


def strategy_name(description: str | None) -> str | None:
    """Las descripciones registradas empiezan con '<nombre-vN>: ...'."""
    if not description:
        return None
    head = description.split(":", 1)[0].strip()
    return head if re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,99}", head) else None


# ---- base del puente ------------------------------------------------------------------------------------


class BridgeStore:
    """ft_bridge.sqlite: envios (append-only), corridas y el instante del primer arranque."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.path), timeout=10.0)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    def first_run_ms(self, now_ms: int) -> int:
        self.conn.execute("INSERT OR IGNORE INTO ft_bridge_meta (key, value) VALUES ('first_run_ms', ?)", (str(now_ms),))
        self.conn.commit()
        return int(self.conn.execute("SELECT value FROM ft_bridge_meta WHERE key = 'first_run_ms'").fetchone()[0])

    def attempts(self) -> dict[str, list[sqlite3.Row]]:
        out: dict[str, list[sqlite3.Row]] = {}
        for r in self.conn.execute("SELECT * FROM ft_bridge_orders ORDER BY id"):
            out.setdefault(r["hypothesis_id"], []).append(r)
        return out

    def add(self, row: dict[str, Any]) -> bool:
        """Inserta un intento. False si ya habia un estado final para esa hipotesis (no se duplica)."""
        cols = ("run_id", "ts_ms", "hypothesis_id", "strategy_id", "strategy_name", "symbol", "pair", "direction",
                "created_ts", "due_ts", "entry_tag", "stake_usdt", "status", "trade_id", "open_rate", "detail")
        try:
            self.conn.execute(f"INSERT INTO ft_bridge_orders ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                              tuple(row.get(c) for c in cols))
            self.conn.commit()
            return True
        except sqlite3.IntegrityError:
            self.conn.rollback()
            return False

    def add_run(self, run: dict[str, Any]) -> None:
        self.conn.execute(
            "INSERT INTO ft_bridge_runs (run_id, ts_ms, status, candidates, sent, skipped, errors, detail) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (run["run_id"], run["ts_ms"], run["status"], run["candidates"], run["sent"], run["skipped"], run["errors"],
             run.get("detail")))
        self.conn.commit()


# ---- hipotesis de 1h (solo lectura) ---------------------------------------------------------------------


def connect_ro(path: str | Path) -> sqlite3.Connection:
    p = Path(path).expanduser()
    if not p.is_file():
        raise FileNotFoundError(f"no existe la base de evaluacion: {p}")
    conn = sqlite3.connect(f"file:{urllib.parse.quote(str(p.resolve()))}?mode=ro", uri=True, timeout=10.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = 1")
    return conn


def hourly_hypotheses(eval_db: str | Path, *, since_ts: float, min_due_ts: float) -> list[dict[str, Any]]:
    """Hipotesis de las estrategias de 1h emitidas desde `since_ts` con due_ts > min_due_ts (con o sin resolucion)."""
    conn = connect_ro(eval_db)
    try:
        rows = conn.execute(
            "SELECT p.hypothesis_id, p.strategy_id, p.symbol, p.direction, p.created_ts, p.due_ts, "
            "p.entry_reference_price, s.description, "
            "EXISTS (SELECT 1 FROM hypothesis_resolutions r WHERE r.hypothesis_id = p.hypothesis_id) AS resolved "
            "FROM pending_hypotheses p "
            "JOIN hypothesis_context c ON c.hypothesis_id = p.hypothesis_id "
            "JOIN strategy_hypotheses s ON s.strategy_id = p.strategy_id "
            "WHERE c.timeframe = '1h' AND json_extract(s.exit_conditions, '$.timeframe') = '1h' "
            "AND p.created_ts >= ? AND p.due_ts > ? "
            "ORDER BY p.created_ts ASC, p.hypothesis_id ASC",
            (since_ts, min_due_ts),
        ).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]


# ---- API de Freqtrade -------------------------------------------------------------------------------------

# (method, url, headers, body|None, timeout) -> (status, bytes). Lanza OSError/URLError si no hay conexion.
HttpCall = Callable[[str, str, dict[str, str], bytes | None, float], tuple[int, bytes]]


def _urllib_call(method: str, url: str, headers: dict[str, str], body: bytes | None, timeout: float) -> tuple[int, bytes]:
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - URL de config (127.0.0.1)
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read() if hasattr(exc, "read") else b""


class ApiDown(Exception):
    """La API no respondio (conexion rechazada, timeout) o rechazo las credenciales."""


class FtClient:
    def __init__(self, base_url: str, user: str, password: str, *, call: HttpCall | None = None,
                 timeout: float = 15.0) -> None:
        self.base = base_url.rstrip("/") + "/api/v1/"
        token = base64.b64encode(f"{user}:{password}".encode()).decode()
        self._headers = {"Authorization": f"Basic {token}", "Accept": "application/json",
                         "Content-Type": "application/json"}
        self._call = call or _urllib_call
        self.timeout = timeout

    def _req(self, method: str, name: str, payload: dict[str, Any] | None = None) -> tuple[int, Any]:
        body = None if payload is None else json.dumps(payload).encode()
        try:
            status, raw = self._call(method, self.base + name, self._headers, body, self.timeout)
        except (OSError, urllib.error.URLError, TimeoutError) as exc:
            raise ApiDown(f"{name}: sin respuesta ({getattr(exc, 'reason', exc)})") from None
        if status == 401:
            raise ApiDown(f"{name}: credenciales rechazadas (401)")
        try:
            data = json.loads(raw) if raw else None
        except ValueError:
            data = {"raw": raw[:200].decode("utf-8", "replace")}
        return status, data

    def get_ok(self, name: str) -> Any:
        status, data = self._req("GET", name)
        if status != 200:
            raise ApiDown(f"{name}: HTTP {status} {_msg(data)}")
        return data

    def whitelist(self) -> list[str]:
        data = self.get_ok("whitelist")
        return list((data or {}).get("whitelist") or [])

    def open_trades(self) -> list[dict[str, Any]]:
        data = self.get_ok("status")
        return [t for t in (data or []) if isinstance(t, dict)]

    def forceenter(self, pair: str, side: str, stake: float, tag: str) -> tuple[int, Any]:
        return self._req("POST", "forceenter", {"pair": pair, "side": side, "stakeamount": stake, "leverage": 1,
                                                "entry_tag": tag, "ordertype": "market"})


def _msg(data: Any) -> str:
    if isinstance(data, dict):
        for k in ("error", "detail", "status", "raw"):
            if data.get(k):
                return str(data[k])[:MAX_DETAIL]
    return str(data)[:MAX_DETAIL] if data is not None else ""


# ---- corrida ----------------------------------------------------------------------------------------------


@dataclass
class BridgeConfig:
    eval_db: Path
    bridge_db: Path
    api_url: str = DEFAULT_API
    secrets_path: Path | None = None
    stake: float = DEFAULT_STAKE
    call: HttpCall | None = None  # inyectable en tests (sin red)

    @classmethod
    def from_env(cls) -> BridgeConfig:
        env = os.environ
        return cls(
            eval_db=Path(env.get("HEATSIGNAL_EVAL_DB", "data/heatsignal_eval.sqlite")).expanduser(),
            bridge_db=Path(env.get("HEATSIGNAL_FT_BRIDGE_DB", DEFAULT_BRIDGE_DB)).expanduser(),
            api_url=env.get("HEATSIGNAL_FT_HYP_API", DEFAULT_API).strip() or DEFAULT_API,
            secrets_path=Path(env.get("HEATSIGNAL_FT_HYP_SECRETS", DEFAULT_SECRETS)).expanduser(),
            stake=float(env.get("HEATSIGNAL_FT_HYP_STAKE", DEFAULT_STAKE)),
        )


def _plan(h: dict[str, Any], tries: list[sqlite3.Row], now_s: float) -> tuple[str, str | None]:
    """('skip_final', None) | ('late', motivo) | ('send', None) para una hipotesis."""
    if any(r["status"] != "error" for r in tries):
        return "skip_final", None
    left = h["due_ts"] - now_s
    errors = [r for r in tries if r["status"] == "error"]
    if left <= 0:
        why = (f"vencio sin poder mandarse ({len(errors)} errores; ultimo: {errors[-1]['detail']})" if errors
               else "vencio sin ningun intento (el puente no corrio durante toda su ventana)")
        return "late", why
    if errors:
        if left <= RETRY_MIN_LEFT_S:
            return "late", (f"no se reintenta: quedan {int(left // 60)} min (< {RETRY_MIN_LEFT_S // 60}) y "
                            f"hubo {len(errors)} errores (ultimo: {errors[-1]['detail']})")
        return "send", None
    if h["resolved"]:
        return "late", "ya tiene resolucion antes del primer envio"
    if left < MIN_FIRST_LEFT_S:
        return "late", f"quedan {int(left)} s para el vencimiento (< {MIN_FIRST_LEFT_S} s): no tiene sentido abrir"
    return "send", None


def run_once(cfg: BridgeConfig, *, now_ms: int | None = None) -> dict[str, Any]:
    now_ms = now_ms or int(time.time() * 1000)
    now_s = now_ms / 1000
    run_id = f"ftb-{now_ms}-{uuid.uuid4().hex[:6]}"
    store = BridgeStore(cfg.bridge_db)
    counts = {"sent": 0, "skipped": 0, "errors": 0}
    detail: list[str] = []
    try:
        first = store.first_run_ms(now_ms)
        since_ts = first / 1000 - START_GRACE_S
        hyps = hourly_hypotheses(cfg.eval_db, since_ts=since_ts, min_due_ts=now_s - LOOKBACK_S)
        tries = store.attempts()
        plans = [(h, *_plan(h, tries.get(h["hypothesis_id"], []), now_s)) for h in hyps]
        plans = [p for p in plans if p[1] != "skip_final"]

        def rec(h: dict[str, Any], status: str, why: str | None = None, *, trade_id: Any = None,
                open_rate: Any = None) -> None:
            row = {"run_id": run_id, "ts_ms": now_ms, "hypothesis_id": h["hypothesis_id"], "strategy_id": h["strategy_id"],
                   "strategy_name": strategy_name(h.get("description")), "symbol": h["symbol"], "pair": to_pair(h["symbol"]),
                   "direction": h["direction"], "created_ts": h["created_ts"], "due_ts": int(h["due_ts"]),
                   "entry_tag": encode_tag(h["hypothesis_id"], h["due_ts"]), "stake_usdt": cfg.stake, "status": status,
                   "trade_id": trade_id, "open_rate": open_rate, "detail": (why or "")[:MAX_DETAIL] or None}
            if store.add(row):
                counts["sent" if status == "sent" else "errors" if status == "error" else "skipped"] += 1

        for h, action, why in plans:
            if action == "late":
                rec(h, "skipped_late", why)
        to_send = [h for h, action, _ in plans if action == "send"]

        status = "idle"
        if to_send:
            status = "ok"
            client: FtClient | None = None
            api_error: str | None = None
            try:
                if cfg.secrets_path is None:
                    raise ApiDown("sin archivo de credenciales configurado (HEATSIGNAL_FT_HYP_SECRETS)")
                try:
                    user, password = read_credentials(cfg.secrets_path)
                except FileNotFoundError:
                    raise ApiDown(f"no existe {Path(cfg.secrets_path).name} (deploy/gen-freqtrade-hyp-secrets.sh)") from None
                except (OSError, ValueError, KeyError, TypeError):
                    raise ApiDown(f"{Path(cfg.secrets_path).name} ilegible (falta api_server.username/password)") from None
                client = FtClient(cfg.api_url, user, password, call=cfg.call)
                whitelist = set(client.whitelist())
                by_pair = {t.get("pair"): t for t in client.open_trades()}
            except ApiDown as exc:
                api_error = str(exc)
            for h in to_send:
                pair = to_pair(h["symbol"])
                tag = encode_tag(h["hypothesis_id"], h["due_ts"])
                if api_error is not None:
                    rec(h, "error", f"API de Freqtrade: {api_error}")
                    continue
                if pair is None or pair not in whitelist:
                    rec(h, "skipped_unsupported", f"{h['symbol']} no esta en la whitelist de la instancia de hipotesis")
                    continue
                busy = by_pair.get(pair)
                if busy is not None:
                    dec = decode_tag(busy.get("enter_tag"))
                    if dec is not None and dec[0] == tag.split(":")[1]:
                        rec(h, "sent", "reconciliada: el trade ya estaba abierto con el tag de esta hipotesis "
                                       "(un envio anterior sin respuesta entro)",
                            trade_id=busy.get("trade_id"), open_rate=busy.get("open_rate"))
                    else:
                        rec(h, "skipped_pair_busy", f"par ocupado por el trade {busy.get('trade_id')} "
                                                    f"({busy.get('enter_tag')})")
                    continue
                side = "long" if h["direction"] == "LONG" else "short"
                try:
                    code, data = client.forceenter(pair, side, cfg.stake, tag)  # type: ignore[union-attr]
                except ApiDown as exc:
                    api_error = str(exc)
                    rec(h, "error", f"API de Freqtrade: {api_error}")
                    continue
                if code == 200 and isinstance(data, dict) and data.get("trade_id") is not None:
                    rec(h, "sent", None, trade_id=int(data["trade_id"]), open_rate=data.get("open_rate"))
                    by_pair[pair] = {"trade_id": data["trade_id"], "enter_tag": tag, "pair": pair}
                elif code == 502 and "already open" in _msg(data):
                    rec(h, "skipped_pair_busy", f"Freqtrade: {_msg(data)}")
                else:
                    rec(h, "error", f"forceenter HTTP {code}: {_msg(data)}")
            if api_error is not None:
                status = "api_down"
                detail.append(api_error)
            elif counts["errors"]:
                status = "partial"
        run = {"run_id": run_id, "ts_ms": now_ms, "status": status, "candidates": len(plans), **counts,
               "detail": "; ".join(detail)[:MAX_DETAIL] or None}
        store.add_run(run)
        return run
    finally:
        store.close()


def summary_line(run: dict[str, Any]) -> str:
    return (f"ft_bridge {run['run_id']}: {run['status']} candidatas={run['candidates']} enviadas={run['sent']} "
            f"omitidas={run['skipped']} errores={run['errors']}" + (f" ({run['detail']})" if run.get("detail") else ""))


# ---- CLI ---------------------------------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m heatsignal.paper.ft_bridge",
                                 description="Puente hipotesis de 1h -> Freqtrade dry-run de hipotesis (simulado).")
    ap.add_argument("cmd", choices=("run", "status"))
    args = ap.parse_args(argv)
    cfg = BridgeConfig.from_env()
    if args.cmd == "run":
        try:
            run = run_once(cfg)
        except FileNotFoundError as exc:
            print(f"ft_bridge: {exc}", file=sys.stderr)
            return 2
        print(summary_line(run))
        return 0 if run["status"] in ("idle", "ok") else 1
    store = BridgeStore(cfg.bridge_db)
    try:
        for r in store.conn.execute("SELECT * FROM ft_bridge_runs ORDER BY ts_ms DESC LIMIT 10"):
            print(summary_line(dict(r)))
        for r in store.conn.execute("SELECT id, ts_ms, strategy_name, symbol, direction, status, trade_id, detail "
                                    "FROM ft_bridge_orders ORDER BY id DESC LIMIT 20"):
            print(dict(r))
    finally:
        store.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
