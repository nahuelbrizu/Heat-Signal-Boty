"""Bases y API falsas para el puente de hipotesis -> Freqtrade (sin red)."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from heatsignal.evaluation.pending_store import PendingHypothesisStore
from heatsignal.evaluation.strategy_registry import StrategyRegistry
from paper_fakes import SCHEMA as FT_SCHEMA, ft_dt

S_1H = "515b5470-d6e5-4bd9-adc7-818d43fc2dec"
S_1H_B = "da81e854-6cba-48b4-b6a3-8d8395f459d9"
S_OLD = "c9e50488-e55d-4076-8c32-7c25981791b0"
H = 3600
PASSWORD = "clave-super-secreta-del-bot"


def make_eval_db(path: Path) -> Path:
    reg = StrategyRegistry(path)
    store = PendingHypothesisStore(path, reg)
    store.close()
    reg.close()
    conn = sqlite3.connect(path)
    for sid, desc, tf in ((S_1H, "1h-apertura-continuacion-a-favor-v1: al cerrar la vela de apertura", "1h"),
                          (S_1H_B, "1h-retroceso-ema-a-favor-v1: a favor de la tendencia", "1h"),
                          (S_OLD, "xrp-acumulacion-v1: LONG de largo plazo", None)):
        conn.execute("INSERT INTO strategy_hypotheses VALUES (?,?,?,?,?,?)",
                     (sid, desc, json.dumps({"timeframe": tf}), json.dumps({"timeframe": tf} if tf else {"horizon_s": 1}),
                      "2026-10-18T00:00:00+00:00", "nahue"))
    conn.commit()
    conn.close()
    return path


def add_hyp(path: Path, hid: str, *, strategy: str = S_1H, symbol: str = "BTC-USDT", direction: str = "LONG",
            created_ts: float, due_ts: float, timeframe: str | None = "1h", entry: str = "100000") -> str:
    conn = sqlite3.connect(path)
    created = datetime.fromtimestamp(created_ts, tz=timezone.utc).isoformat()
    conn.execute(
        "INSERT INTO pending_hypotheses (hypothesis_id, strategy_id, symbol, created_at, created_ts, due_ts, direction, "
        "confidence_score, entry_reference_price, horizon_s, atr_normalized_trigger, rationale, heatmap_model_id, run_id) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (hid, strategy, symbol, created, created_ts, due_ts, direction, 0.6, entry, int(due_ts - created_ts), None,
         "test", None, None))
    if timeframe:
        conn.execute("INSERT INTO hypothesis_context (hypothesis_id, timeframe, horizon_bars) VALUES (?,?,?)",
                     (hid, timeframe, 4))
    conn.commit()
    conn.close()
    return hid


def resolve_hyp(path: Path, hid: str, *, exit_price: float, pnl_pct: float, gross: float, win: bool, at_ts: float) -> None:
    conn = sqlite3.connect(path)
    conn.execute("INSERT INTO hypothesis_resolutions (hypothesis_id, status, resolved_at, exit_price, pnl_pct, win, "
                 "gross_pnl_pct, cost_bps, benchmark_pnl_pct, alpha_pct, exit_price_ts) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                 (hid, "resolved", datetime.fromtimestamp(at_ts, tz=timezone.utc).isoformat(), str(exit_price), pnl_pct,
                  int(win), gross, 10.0, 0.0, pnl_pct, None))
    conn.commit()
    conn.close()


def write_secrets(path: Path) -> Path:
    path.write_text(json.dumps({"api_server": {"username": "hs-hyp-x", "password": PASSWORD, "listen_port": 8082}}))
    return path


class FakeFt:
    """API REST de Freqtrade 2026.9 en memoria: whitelist, status y forceenter (mismas respuestas que el codigo real)."""

    def __init__(self, whitelist: list[str] | None = None) -> None:
        self.whitelist = whitelist or ["BTC/USDT:USDT", "SOL/USDT:USDT", "XRP/USDT:USDT", "ETH/USDT:USDT",
                                       "XLM/USDT:USDT", "BNB/USDT:USDT", "TRX/USDT:USDT"]
        self.open: dict[str, dict[str, Any]] = {}
        self.calls: list[tuple[str, str, Any]] = []
        self.down = False
        self.timeout_on_enter = False  # el trade entra pero la respuesta no llega
        self.next_id = 1
        self.auth_seen: list[str] = []

    def call(self, method: str, url: str, headers: dict[str, str], body: bytes | None, timeout: float) -> tuple[int, bytes]:
        name = url.rsplit("/", 1)[-1]
        payload = json.loads(body) if body else None
        self.calls.append((method, name, payload))
        self.auth_seen.append(headers.get("Authorization", ""))
        if self.down:
            raise OSError("Connection refused")
        if name == "whitelist":
            return 200, json.dumps({"whitelist": self.whitelist, "length": len(self.whitelist), "method": ["StaticPairList"]}).encode()
        if name == "status":
            return 200, json.dumps(list(self.open.values())).encode()
        if name == "forceenter":
            pair = payload["pair"]
            if pair in self.open:
                return 502, json.dumps({"error": f"Error querying /api/v1/forceenter: position for {pair} already open - "
                                                 f"id: {self.open[pair]['trade_id']}"}).encode()
            t = {"trade_id": self.next_id, "pair": pair, "is_short": payload["side"] == "short",
                 "enter_tag": payload["entry_tag"], "open_rate": 100_000.0, "stake_amount": payload["stakeamount"]}
            self.next_id += 1
            self.open[pair] = t
            if self.timeout_on_enter:
                raise TimeoutError("timed out")
            return 200, json.dumps(t).encode()
        return 404, b'{"detail": "Not Found"}'

    def enters(self) -> list[Any]:
        return [p for m, n, p in self.calls if n == "forceenter"]


def make_ft_db(path: Path, trades: list[dict[str, Any]]) -> Path:
    """trades: [{id, pair, is_short, tag, open_ms, open_rate, close_ms?, close_rate?, profit_abs?, profit?, exit_reason?}]"""
    conn = sqlite3.connect(path)
    conn.executescript(FT_SCHEMA)
    for t in trades:
        is_open = t.get("close_ms") is None
        conn.execute(
            "INSERT INTO trades (id, exchange, pair, is_open, fee_open, fee_open_cost, fee_close, fee_close_cost, open_rate,"
            " close_rate, realized_profit, close_profit, close_profit_abs, stake_amount, max_stake_amount, amount,"
            " open_date, close_date, exit_reason, strategy, enter_tag, trading_mode, leverage, is_short, interest_rate,"
            " funding_fees, funding_fee_running, record_version) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (t["id"], "binance", t["pair"], int(is_open), 0.0005, 0.5, 0.0005, None if is_open else 0.5, t["open_rate"],
             t.get("close_rate"), 0.0, t.get("profit"), t.get("profit_abs"), 1000.0, 1000.0, 0.01, ft_dt(t["open_ms"]),
             ft_dt(t.get("close_ms")), t.get("exit_reason"), "HypothesisBridgeV1", t["tag"], "FUTURES", 1.0,
             int(t.get("is_short", False)), 0.0, t.get("funding", 0.0), 0.0, 2))
    conn.commit()
    conn.close()
    return path
