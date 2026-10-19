"""Contexto multi-temporalidad (features/htf.py + history/hourly.py): regla de tendencia, sin look-ahead, huecos. Sin red."""

from __future__ import annotations

import json
import sqlite3

import pytest

from heatsignal.features.htf import (
    DAY_MS,
    HOUR_MS,
    build_htf_context,
    classify_trend,
    trend_alignment,
)
from heatsignal.history.hourly import HourlyCandleProvider
from heatsignal.history.store import HistoryStore, MinuteBar
from heatsignal.schema.features import Candle1h

DAY0 = 1_792_195_200_000  # 2026-10-18 00:00 UTC
SYM = "XRP-USDT"


def _series(n_hours: int, end_open_ms: int, price0: float, step: float, *, wiggle: float = 0.3) -> list[Candle1h]:
    """Serie sintetica de velas de 1h que termina con la vela que abre en `end_open_ms`."""
    out = []
    start = end_open_ms - (n_hours - 1) * HOUR_MS
    p = price0
    for i in range(n_hours):
        o = p
        c = p + step
        hi, lo = max(o, c) + wiggle, min(o, c) - wiggle
        out.append(Candle1h(open_ms=start + i * HOUR_MS, open=o, high=hi, low=lo, close=c, source="klines",
                            vwap_pv=(hi + lo + c) / 3 * 60, vwap_vol=60.0))  # klines 1m: traen volumen (VWAP)
        p = c
    return out


# ---- la regla ---------------------------------------------------------------------------------------

@pytest.mark.parametrize("chg, close, ema, slope, want", [
    (1.5, 110.0, 100.0, 0.1, "alcista"),
    (2.0, 110.0, 100.0, -0.1, "lateral"),     # pendiente de 1h en contra
    (2.0, 95.0, 100.0, 0.5, "lateral"),       # bajo la EMA diaria
    (1.49, 110.0, 100.0, 0.5, "lateral"),     # no llega a 1.5 ATR
    (-1.5, 90.0, 100.0, -0.2, "bajista"),
    (-3.0, 101.0, 100.0, -0.2, "lateral"),
    (None, 90.0, 100.0, -0.2, "sin_datos"),
    (-3.0, 90.0, None, -0.2, "sin_datos"),
])
def test_daily_trend_rule(chg, close, ema, slope, want) -> None:
    trend, reason = classify_trend(chg, close, ema, slope)
    assert trend == want
    assert reason


def test_alignment() -> None:
    assert trend_alignment("LONG", "alcista") == "a_favor"
    assert trend_alignment("SHORT", "alcista") == "en_contra"
    assert trend_alignment("SHORT", "bajista") == "a_favor"
    assert trend_alignment("LONG", "lateral") == "lateral"
    assert trend_alignment("LONG", "sin_datos") == "sin_datos"
    assert trend_alignment("LONG", None) == "sin_datos"


def test_uptrend_context_is_alcista_and_complete() -> None:
    now = DAY0 + 10 * HOUR_MS + 20 * 60_000  # 10:20 UTC: la ultima vela cerrada es la de 09:00
    cs = _series(24 * 20 + 10, DAY0 + 9 * HOUR_MS, 100.0, 0.05, wiggle=0.02)
    h = build_htf_context(SYM, cs, now)
    assert h.daily_trend == "alcista", h.trend_reason
    assert h.status == "ok" and not h.data_gaps
    assert len(h.candles_1h) == 48 and h.candles_1h[-1].open_ms == DAY0 + 9 * HOUR_MS
    assert h.day_hours_closed == 10 and h.day_open == pytest.approx(cs[-10].open)
    assert h.change_from_open_pct > 0 and h.daily_closes >= 10 and h.ema10_1d is not None
    assert h.pos_in_day_range == pytest.approx(1.0, abs=0.05)
    assert h.setups.extremo_rango_diario == "maximo"


def test_downtrend_is_bajista() -> None:
    now = DAY0 + 12 * HOUR_MS
    h = build_htf_context(SYM, _series(24 * 15 + 12, DAY0 + 11 * HOUR_MS, 200.0, -0.05, wiggle=0.02), now)
    assert h.daily_trend == "bajista"
    assert trend_alignment("SHORT", h.daily_trend) == "a_favor"


def test_only_closed_candles_no_lookahead() -> None:
    now = DAY0 + 10 * HOUR_MS + 30 * 60_000
    cs = _series(24 * 12 + 11, DAY0 + 10 * HOUR_MS, 100.0, 0.05)  # incluye la vela de 10:00, ABIERTA
    future = Candle1h(open_ms=DAY0 + 11 * HOUR_MS, open=1.0, high=999.0, low=0.5, close=999.0, source="rest")
    h = build_htf_context(SYM, [*cs, future], now)
    assert h.last_closed_open_ms == DAY0 + 9 * HOUR_MS
    assert all(c.open_ms + HOUR_MS <= now for c in h.candles_1h)
    assert h.day_high < 999.0
    assert any("abiertas o futuras descartadas" in g for g in h.data_gaps)
    assert h.status == "partial" or h.status == "ok"


def test_without_candles_is_declared_missing() -> None:
    h = build_htf_context(SYM, [], DAY0 + 5 * HOUR_MS)
    assert h.status == "missing" and h.daily_trend == "sin_datos"
    assert any("sin velas" in g for g in h.data_gaps)


def test_holes_and_partial_candles_are_declared() -> None:
    cs = _series(24 * 12 + 6, DAY0 + 5 * HOUR_MS, 100.0, 0.02)
    del cs[-3]
    cs[-1] = cs[-1].model_copy(update={"source": "bars_1m", "minutes": 52})
    h = build_htf_context(SYM, cs, DAY0 + 6 * HOUR_MS)
    assert h.status == "partial"
    assert any("1 hora(s) sin vela" in g for g in h.data_gaps)
    assert any("52/60" in g for g in h.data_gaps)


def test_close_day_override_describes_the_finished_day() -> None:
    cs = _series(24 * 12, DAY0 + 23 * HOUR_MS, 100.0, 0.05)
    h = build_htf_context(SYM, cs, DAY0 + DAY_MS, day_start_ms=DAY0)
    assert h.day_hours_closed == 24 and h.day_start_ms == DAY0


def test_pullback_setup_flag() -> None:
    now = DAY0 + 15 * HOUR_MS
    cs = _series(24 * 15 + 15, DAY0 + 14 * HOUR_MS, 100.0, 0.08, wiggle=0.05)
    assert build_htf_context(SYM, cs, now).setups.pullback_ema20_1h_a_favor is False  # extendida, sin retroceso
    prev = build_htf_context(SYM, cs[:-1], now - HOUR_MS)  # EMA20 hasta la vela anterior
    ema, atr = prev.ema20_1h, prev.atr14_1h
    # ultima vela: retrocede hasta la EMA20 y cierra apenas arriba
    cs[-1] = cs[-1].model_copy(update={"low": ema - 0.1 * atr, "close": ema + 0.1 * atr, "high": cs[-1].open})
    h = build_htf_context(SYM, cs, now)
    assert h.daily_trend == "alcista", h.trend_reason
    assert h.setups.pullback_ema20_1h_a_favor is True
    assert h.setups.continuacion_a_favor is True


# ---- proveedor: base -> REST -> hueco ------------------------------------------------------------

def _store_with(tmp_path, *, kline_hours: list[int], bar_hours: list[int], bar_minutes: int = 60) -> str:
    path = tmp_path / "history.sqlite"
    st = HistoryStore(path)
    rows = []
    for h in kline_hours:
        for m in range(60):
            t = h + m * 60_000
            rows.append(("binance", SYM, "1m", t, 1.0, 1.1, 0.9, 1.05, 10.0, 10.0, 5, 5.0))
    st.add_klines(rows)
    bars = []
    for h in bar_hours:
        for m in range(bar_minutes):
            bars.append(MinuteBar("binance", SYM, h + m * 60_000, open=2.0, high=2.2, low=1.9, close=2.1))
    st.upsert_bars(bars)
    st.close()
    return str(path)


class FakeHttp:
    def __init__(self, candles_by_open: dict[int, float] | None = None, status: int = 200) -> None:
        self.calls: list[str] = []
        self.candles = candles_by_open or {}
        self.status = status

    def __call__(self, url: str, timeout: float):
        self.calls.append(url)
        if self.status != 200:
            return self.status, b'{"code": -1003}'
        body = [[t, "3", "3.3", "2.9", str(c), "1", t + HOUR_MS - 1, "1", 1, "1", "1", "0"]
                for t, c in sorted(self.candles.items())]
        return 200, json.dumps(body).encode()


def test_provider_prefers_db_then_rest_and_caches(tmp_path) -> None:
    now = DAY0 + 6 * HOUR_MS + 5 * 60_000
    klines = [DAY0 + i * HOUR_MS for i in range(0, 3)]
    bars = [DAY0 + 3 * HOUR_MS]
    db = _store_with(tmp_path, kline_hours=klines, bar_hours=bars)
    http = FakeHttp({DAY0 + 4 * HOUR_MS: 3.1, DAY0 + 5 * HOUR_MS: 3.2})
    p = HourlyCandleProvider(db, lookback_h=6, http_get=http)
    cs, gaps = p.candles(SYM, now)
    src = {c.open_ms: c.source for c in cs}
    assert src[DAY0] == "klines" and src[DAY0 + 3 * HOUR_MS] == "bars_1m" and src[DAY0 + 5 * HOUR_MS] == "rest"
    assert len(http.calls) == 1 and "interval=1h" in http.calls[0]
    assert any("REST de Binance" in g for g in gaps)
    cs2, _ = p.candles(SYM, now + 60_000)  # misma hora: todo del cache, sin pedidos nuevos
    assert len(cs2) == len(cs) and len(http.calls) == 1


def test_provider_partial_bars_and_rest_failure_are_declared(tmp_path) -> None:
    now = DAY0 + 3 * HOUR_MS + 5 * 60_000
    db = _store_with(tmp_path, kline_hours=[DAY0], bar_hours=[DAY0 + HOUR_MS], bar_minutes=55)
    http = FakeHttp(status=429)
    p = HourlyCandleProvider(db, lookback_h=3, http_get=http)
    cs, gaps = p.candles(SYM, now)
    assert [c.minutes for c in cs] == [60, 55]
    assert any("fallo el REST" in g for g in gaps)
    assert any("faltan" in g for g in gaps)
    h = p.context(SYM, now)
    assert any("55/60" in g for g in h.data_gaps)
    # el hueco no se reintenta en cada ciclo (como mucho una vez por hora)
    p.candles(SYM, now + 60_000)
    assert len(http.calls) == 1


def test_provider_without_db_uses_rest_and_says_so(tmp_path) -> None:
    now = DAY0 + 2 * HOUR_MS + 5 * 60_000
    http = FakeHttp({DAY0: 3.0, DAY0 + HOUR_MS: 3.1})
    p = HourlyCandleProvider(tmp_path / "no-existe.sqlite", lookback_h=2, http_get=http)
    cs, gaps = p.candles(SYM, now)
    assert [c.source for c in cs] == ["rest", "rest"]
    assert any("base historica no disponible" in g for g in gaps)


def test_rest_candle_still_open_is_ignored(tmp_path) -> None:
    now = DAY0 + HOUR_MS + 10  # la vela de 01:00 recien abre
    http = FakeHttp({DAY0: 3.0, DAY0 + HOUR_MS: 9.9})
    p = HourlyCandleProvider(None, lookback_h=2, http_get=http)
    cs, _ = p.candles(SYM, now)
    assert [c.open_ms for c in cs] == [DAY0]


def test_db_reads_are_readonly(tmp_path) -> None:
    db = _store_with(tmp_path, kline_hours=[DAY0], bar_hours=[])
    conn = sqlite3.connect(db)
    before = conn.execute("SELECT COUNT(*) FROM klines").fetchone()[0]
    HourlyCandleProvider(db, lookback_h=2, rest=False).candles(SYM, DAY0 + 2 * HOUR_MS + 120_000)
    assert conn.execute("SELECT COUNT(*) FROM klines").fetchone()[0] == before


def test_provider_cost_does_not_grow_with_hour_of_day(tmp_path) -> None:
    """El panel pide /api/htf cada 60 s todo el dia: el cache por hora cerrada evita releer las 1m del dia.

    Una consulta a la base por hora nueva (no por pedido), y el resultado con cache es identico al de un
    proveedor recien creado (mismo VWAP, niveles y tendencia)."""
    hours = [DAY0 - 24 * HOUR_MS + i * HOUR_MS for i in range(48)]
    db = _store_with(tmp_path, kline_hours=hours[:24], bar_hours=hours[24:])
    p = HourlyCandleProvider(db, lookback_h=30, rest=False, db_grace_ms=0)
    queries_at: dict[int, int] = {}
    for minute in range(0, 23 * 60 + 59, 1):  # un pedido por minuto durante el dia UTC
        now = DAY0 + minute * 60_000 + 30_000
        p.context(SYM, now)
        queries_at[minute // 60] = p.stats["db_queries"]
    assert queries_at[23] <= 24 + 1  # una por hora cerrada (+ la carga inicial), no 1440
    per_hour = [queries_at[h] - queries_at[h - 1] for h in range(1, 24)]
    assert max(per_hour) <= 1
    now = DAY0 + 23 * HOUR_MS + 30 * 60_000
    cached = p.context(SYM, now).model_dump()
    fresh = HourlyCandleProvider(db, lookback_h=30, rest=False, db_grace_ms=0).context(SYM, now).model_dump()
    assert cached == fresh
