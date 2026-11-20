"""Velas de 5m/15m en vivo (live-5m15m-v1, 2026-11-19): agregacion de bars_1m, contexto, monitoreo de hipotesis
abiertas y la linea para el grafo. Sin red ni LLM."""

from __future__ import annotations

import sqlite3

from heatsignal.features.live import aggregate, build_live_context, live_brief_line, monitor_hypothesis
from heatsignal.history.live import LiveCandleProvider

MIN = 60_000
T0 = 1_792_195_200_000  # 2026-10-18 00:00 UTC


def _rows(n: int, start: int = T0, p: float = 100.0, step: float = 0.01, usd: float = 1000.0) -> list[tuple]:
    out = []
    for i in range(n):
        o, c = p, p + step
        out.append((start + i * MIN, o, max(o, c) + 0.02, min(o, c) - 0.02, c, usd))
        p = c
    return out


def test_aggregate_only_closed_and_filled() -> None:
    rows = _rows(32)  # 00:00 .. 00:31
    del rows[7:10]  # 3 minutos sin precio en 00:05-00:10 -> 2 de 5 (< 4): se descarta
    gaps: list[str] = []
    bars = aggregate(rows, 5, T0 + 32 * MIN, gaps)
    assert [b.open_ms for b in bars] == [T0, T0 + 10 * MIN, T0 + 15 * MIN, T0 + 20 * MIN, T0 + 25 * MIN]  # 00:30 abierta
    assert bars[0].open == rows[0][1] and bars[0].close == rows[4][4]
    assert gaps and "5m" in gaps[0]


def test_context_ok_with_enough_history_and_brief_line() -> None:
    now = T0 + 8 * 60 * MIN + 30_000
    ctx = build_live_context("BTC-USDT", _rows(8 * 60), now)
    assert ctx.status == "ok" and ctx.tfs["15m"].bars == 32 and ctx.tfs["5m"].bars == 96
    t = ctx.tfs["15m"]
    assert t.last_closed_open_ms == T0 + 7 * 60 * MIN + 45 * MIN
    assert t.dist_ema20_atr is not None and t.dist_ema20_atr > 0 and t.ret_3bars_pct > 0  # serie que sube
    assert ctx.vwap_day is not None and ctx.dist_vwap_atr15 > 0
    line = live_brief_line(ctx)
    assert "15m: cierre vs EMA20 +" in line and "no abre hipotesis" in line


def test_context_flags_stale_and_missing() -> None:
    now = T0 + 8 * 60 * MIN + 10 * MIN  # 10 min sin minutos nuevos
    ctx = build_live_context("BTC-USDT", _rows(8 * 60), now)
    assert ctx.status == "partial" and any("hace" in g for g in ctx.data_gaps)
    empty = build_live_context("BTC-USDT", [], now)
    assert empty.status == "missing" and "sin_datos" in live_brief_line(empty)


def test_monitor_hypothesis_pnl_excursions_and_alerts() -> None:
    now = T0 + 8 * 60 * MIN + 30_000
    rows = _rows(8 * 60)
    live = build_live_context("BTC-USDT", rows, now)
    created = T0 + 6 * 60 * MIN
    entry = rows[6 * 60][1]
    base = {"hypothesis_id": "h", "symbol": "BTC-USDT", "entry_price": entry, "created_ms": created,
            "due_ms": now + 30 * MIN}
    long = monitor_hypothesis({**base, "direction": "LONG"}, rows, live, now)
    assert long["pnl_pct"] > 0 and long["mfe_pct"] >= long["pnl_pct"] and long["minutes_to_due"] == 30
    assert long["minutes_seguida"] == 2 * 60
    short = monitor_hypothesis({**base, "direction": "SHORT"}, rows, live, now)
    assert short["pnl_pct"] < 0 and short["mae_pct"] < 0
    assert "el ultimo cierre de 15m quedo del lado contrario de la EMA20 de 15m" in short["alerts"]
    assert "las ultimas 3 velas de 5m y de 15m van en contra" in short["alerts"]
    assert not any("contrario" in a for a in long["alerts"])


def test_provider_reads_bars_1m(tmp_path) -> None:
    db = tmp_path / "h.sqlite"
    c = sqlite3.connect(db)
    c.execute("CREATE TABLE bars_1m (exchange TEXT, symbol TEXT, minute_ms INTEGER, open REAL, high REAL, low REAL, "
              "close REAL, buy_usd REAL, sell_usd REAL, block_usd REAL)")
    now = T0 + 8 * 60 * MIN + 30_000
    c.executemany("INSERT INTO bars_1m VALUES ('binance','BTC-USDT',?,?,?,?,?,?,0,0)", [r[:6] for r in _rows(8 * 60)])
    c.commit()
    c.close()
    p = LiveCandleProvider(db)
    ctx = p.context("BTC-USDT", now)
    assert ctx.status == "ok" and ctx.last_price is not None
    live, hyps = p.monitor("BTC-USDT", [{"hypothesis_id": "h", "symbol": "BTC-USDT", "direction": "LONG",
                                         "entry_price": 100.0, "created_ms": T0 + 60 * MIN, "due_ms": now}], now)
    assert live.status == "ok" and hyps[0]["status"] == "ok"
    assert LiveCandleProvider(tmp_path / "nope.sqlite").context("BTC-USDT", now).status == "missing"


def test_provider_fills_holes_with_rest_klines(tmp_path) -> None:
    db = tmp_path / "h.sqlite"
    c = sqlite3.connect(db)
    c.execute("CREATE TABLE bars_1m (exchange TEXT, symbol TEXT, minute_ms INTEGER, open REAL, high REAL, low REAL, "
              "close REAL, buy_usd REAL, sell_usd REAL, block_usd REAL)")
    c.execute("CREATE TABLE klines (exchange TEXT, symbol TEXT, interval TEXT, open_time_ms INTEGER, open REAL, "
              "high REAL, low REAL, close REAL, volume REAL, quote_volume REAL, n_trades INTEGER, taker_buy_quote REAL)")
    now = T0 + 8 * 60 * MIN + 30_000
    rows = _rows(8 * 60)
    hole = range(2 * 60, 6 * 60)  # 4 h sin grabador (PC suspendida)
    c.executemany("INSERT INTO bars_1m VALUES ('binance','BTC-USDT',?,?,?,?,?,?,0,0)",
                  [r[:6] for i, r in enumerate(rows) if i not in hole])
    c.executemany("INSERT INTO klines VALUES ('binance','BTC-USDT','1m',?,?,?,?,?,0,?,0,0)", [r[:6] for r in rows])
    c.commit()
    c.close()
    ctx = LiveCandleProvider(db).context("BTC-USDT", now)
    assert ctx.status == "ok" and ctx.tfs["15m"].bars == 32
    assert any("240 minuto(s) rellenados" in g for g in ctx.data_gaps)
