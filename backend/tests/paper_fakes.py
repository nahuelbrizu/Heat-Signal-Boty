"""Base SQLite falsa con el esquema minimo de Freqtrade 2026.9 (trades, orders, KeyValueStore) para tests y demo.

Columnas tomadas de freqtrade/persistence/trade_model.py (Trade/Order) y key_value_store.py; fechas como las
guarda SQLAlchemy: UTC sin zona, "YYYY-MM-DD HH:MM:SS.ffffff".
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE trades (
    id INTEGER PRIMARY KEY, exchange VARCHAR(25) NOT NULL, pair VARCHAR(25) NOT NULL, base_currency VARCHAR(25),
    stake_currency VARCHAR(25), is_open BOOLEAN NOT NULL, fee_open FLOAT NOT NULL, fee_open_cost FLOAT,
    fee_open_currency VARCHAR(25), fee_close FLOAT NOT NULL, fee_close_cost FLOAT, fee_close_currency VARCHAR(25),
    open_rate FLOAT, open_rate_requested FLOAT, open_trade_value FLOAT, close_rate FLOAT, close_rate_requested FLOAT,
    realized_profit FLOAT, close_profit FLOAT, close_profit_abs FLOAT, stake_amount FLOAT NOT NULL,
    max_stake_amount FLOAT, amount FLOAT, amount_requested FLOAT, open_date DATETIME NOT NULL, close_date DATETIME,
    stop_loss FLOAT, exit_reason VARCHAR(255), strategy VARCHAR(100), enter_tag VARCHAR(255), timeframe INTEGER,
    trading_mode VARCHAR(7), leverage FLOAT, is_short BOOLEAN NOT NULL, liquidation_price FLOAT,
    interest_rate FLOAT NOT NULL, funding_fees FLOAT, funding_fee_running FLOAT, record_version INTEGER NOT NULL
);
CREATE TABLE orders (
    id INTEGER PRIMARY KEY, ft_trade_id INTEGER, ft_order_side VARCHAR(25) NOT NULL, ft_pair VARCHAR(25) NOT NULL,
    ft_is_open BOOLEAN NOT NULL, ft_amount FLOAT NOT NULL, ft_price FLOAT NOT NULL, ft_cancel_reason VARCHAR(255),
    order_id VARCHAR(255) NOT NULL, status VARCHAR(255), symbol VARCHAR(25), order_type VARCHAR(50), side VARCHAR(25),
    price FLOAT, average FLOAT, amount FLOAT, filled FLOAT, remaining FLOAT, cost FLOAT, stop_price FLOAT,
    order_date DATETIME, order_filled_date DATETIME, order_update_date DATETIME, funding_fee FLOAT, ft_fee_base FLOAT,
    ft_order_tag VARCHAR(255)
);
CREATE TABLE "KeyValueStore" (
    id INTEGER PRIMARY KEY, "key" VARCHAR(25) NOT NULL, value_type VARCHAR(20) NOT NULL, string_value VARCHAR(255),
    datetime_value DATETIME, float_value FLOAT, int_value INTEGER
);
"""


def ft_dt(ms: int | None) -> str | None:
    if ms is None:
        return None
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).replace(tzinfo=None).strftime("%Y-%m-%d %H:%M:%S.%f")


def make_db(path: Path, trades: list[dict[str, Any]], *, bot_start_ms: int | None = None,
            startup_ms: int | None = None, fee: float = 0.0005) -> Path:
    """trades: [{id, pair, fills: [(ms, du, price, tag)], funding: float, close_ms?, is_open?}]

    Arma trades + orders coherentes (amount, stake, fee costs, close_profit_abs como Freqtrade: precio - fees + funding).
    """
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA)
    oid = 0
    for t in trades:
        pos = cost_basis = realized = fee_open_cost = fee_close_cost = 0.0
        max_stake = 0.0
        for ms, du, price, tag in t["fills"]:
            oid += 1
            notional = abs(du) * price
            if du > 0:
                cost_basis += notional
                pos += du
                fee_open_cost += notional * fee
            else:
                avg = cost_basis / pos
                realized += -du * (price - avg)
                cost_basis -= -du * avg
                pos += du
                fee_close_cost += notional * fee
            max_stake = max(max_stake, cost_basis)
            conn.execute(
                "INSERT INTO orders (id, ft_trade_id, ft_order_side, ft_pair, ft_is_open, ft_amount, ft_price, order_id,"
                " status, symbol, order_type, side, price, average, amount, filled, remaining, cost, order_date,"
                " order_filled_date, funding_fee, ft_order_tag) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (oid, t["id"], "buy" if du > 0 else "sell", t["pair"], 0, abs(du), price, f"dry_run_{oid}", "closed",
                 t["pair"], "market", "buy" if du > 0 else "sell", price, price, abs(du), abs(du), 0.0, notional,
                 ft_dt(ms - 100), ft_dt(ms), 0.0, tag))
        is_open = t.get("is_open", abs(pos) > 1e-12)
        first = t["fills"][0]
        last = t["fills"][-1]
        funding = t.get("funding", 0.0)
        close_abs = None if is_open else realized - fee_open_cost - fee_close_cost + funding
        conn.execute(
            "INSERT INTO trades (id, exchange, pair, base_currency, stake_currency, is_open, fee_open, fee_open_cost,"
            " fee_close, fee_close_cost, open_rate, close_rate, realized_profit, close_profit, close_profit_abs,"
            " stake_amount, max_stake_amount, amount, open_date, close_date, exit_reason, strategy, enter_tag,"
            " trading_mode, leverage, is_short, interest_rate, funding_fees, funding_fee_running, record_version)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (t["id"], "binance", t["pair"], t["pair"].split("/")[0], "USDT", int(is_open), fee, fee_open_cost, fee,
             None if is_open else fee_close_cost, first[2], None if is_open else last[2],
             realized if is_open else 0.0, None if is_open else close_abs / max_stake if max_stake else None, close_abs,
             cost_basis if is_open else max_stake, max_stake, pos if is_open else 0.0, ft_dt(first[0]),
             None if is_open else ft_dt(t.get("close_ms", last[0])), None if is_open else t.get("exit_reason", "exit_signal"),
             "DonchianEnsembleV1", "donchian", "FUTURES", 1.0, 0, 0.0, funding, 0.0, 2))
    kv = 0
    for key, ms in (("bot_start_time", bot_start_ms), ("startup_time", startup_ms)):
        if ms is not None:
            kv += 1
            conn.execute('INSERT INTO "KeyValueStore" (id, "key", value_type, datetime_value) VALUES (?,?,?,?)',
                         (kv, key, "datetime", ft_dt(ms)))
    conn.commit()
    conn.close()
    return path
