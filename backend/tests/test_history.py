"""Tests de la etapa 1: grabador por minuto, base historica, velas, resampleo y exportacion (sin red)."""

from __future__ import annotations

import csv
import gzip
import json
from datetime import date

import pytest

from heatsignal.history import export as export_mod
from heatsignal.history.klines import download, internal_symbol, load, resample
from heatsignal.history.recorder import MinuteRecorder
from heatsignal.history.store import HistoryStore, MinuteBar
from heatsignal.news.window import NewsWindow
from heatsignal.schema.events import Exchange, Side
from heatsignal.schema.news import NewsItem

from features_helpers import SYM, liq, ticker, trade

M0 = 1_792_195_200_000  # 2026-10-17 00:00 UTC
MIN = 60_000


class Clock:
    def __init__(self, ms: int) -> None:
        self.ms = ms

    def __call__(self) -> int:
        return self.ms


def _bars(store: HistoryStore) -> list[tuple]:
    return store.query("SELECT exchange, minute_ms, open, high, low, close, buy_usd, sell_usd, n_trades, block_usd, "
                       "liq_long_usd, liq_short_usd, n_liqs, mark, funding_8h, oi_usd FROM bars_1m "
                       "ORDER BY exchange, minute_ms")


# ---- grabador ---------------------------------------------------------------------------------------


async def test_recorder_aggregates_a_minute_and_writes_after_grace(tmp_path) -> None:
    store = HistoryStore(tmp_path / "h.sqlite")
    clock = Clock(M0 + 5_000)
    rec = MinuteRecorder(store, grace_s=10, flush_every_s=0, clock_ms=clock)
    await rec.write(trade(100.0, 2.0, side=Side.BUY, ts=M0 + 1_000))
    await rec.write(trade(103.0, 1.0, side=Side.SELL, ts=M0 + 2_000))
    await rec.write(trade(99.0, 1.0, side=Side.BUY, ts=M0 + 3_000))
    await rec.write(trade(500.0, 10.0, ts=M0 + 3_500, block=True))
    await rec.write(liq(98.0, 5_000.0, side=Side.SELL, ts=M0 + 4_000))
    await rec.write(liq(104.0, 1_000.0, side=Side.BUY, ts=M0 + 4_500))
    await rec.write(ticker(Exchange.BINANCE, M0 + 4_800, mark=100.5, funding=0.0001, oi=10.0))
    assert _bars(store) == []  # el minuto todavia no cerro

    clock.ms = M0 + MIN + 9_000  # cerro, pero no paso el margen de 10 s
    rec.flush()
    assert _bars(store) == []
    clock.ms = M0 + MIN + 10_000
    rec.flush()
    (binance, bybit) = _bars(store)
    assert binance == ("binance", M0, 100.0, 103.0, 99.0, 99.0, 299.0, 103.0, 3, 5000.0, 0.0, 0.0, 0,
                       100.5, 0.0001, 1005.0)
    assert bybit[10:13] == (5000.0, 1000.0, 2)  # longs liquidados / shorts liquidados
    assert store.query("SELECT COUNT(*) FROM liquidations")[0][0] == 2
    assert rec.stats.bars_written == 2 and rec.stats.late_events == 0


async def test_late_events_merge_instead_of_overwriting(tmp_path) -> None:
    store = HistoryStore(tmp_path / "h.sqlite")
    clock = Clock(M0)
    rec = MinuteRecorder(store, grace_s=10, flush_every_s=0, clock_ms=clock)
    await rec.write(trade(100.0, 1.0, ts=M0 + 1_000))
    clock.ms = M0 + MIN + 10_000
    rec.flush()
    await rec.write(trade(110.0, 1.0, side=Side.SELL, ts=M0 + 59_000))  # llego tarde
    rec.flush(everything=True)
    (row,) = _bars(store)
    assert row[2:9] == (100.0, 110.0, 100.0, 110.0, 100.0, 110.0, 2)
    assert rec.stats.late_events == 1


async def test_recorder_keeps_rows_in_memory_if_disk_fails(tmp_path) -> None:
    store = HistoryStore(tmp_path / "h.sqlite")
    rec = MinuteRecorder(store, grace_s=0, flush_every_s=0, clock_ms=lambda: M0 + 2 * MIN)
    store.upsert_bars = lambda bars: (_ for _ in ()).throw(OSError("disco lleno"))  # type: ignore[method-assign]
    await rec.write(trade(100.0, 1.0, ts=M0))
    assert rec.stats.write_errors >= 1 and rec._bars  # no se perdio: se reintenta en el proximo flush


def test_news_window_reports_only_new_items(tmp_path) -> None:
    store = HistoryStore(tmp_path / "h.sqlite")
    rec = MinuteRecorder(store)
    window = NewsWindow([], clock_ms=lambda: M0, on_new=rec.record_news)
    item = NewsItem(item_id="n1", source="sec", primary=True, title="t", summary="", url="u",
                    published_ms=M0 - 1000, fetched_ms=M0, symbols=["XRP"], market_wide=False)
    assert window.ingest([item]) == 1 and window.ingest([item]) == 0
    assert store.query("SELECT item_id, is_primary, symbols FROM news") == [("n1", 1, "XRP")]


# ---- velas -----------------------------------------------------------------------------------------


def _kline(open_ms: int, o: float, h: float, low: float, c: float) -> list:
    return [open_ms, str(o), str(h), str(low), str(c), "10", open_ms + MIN - 1, "1000", 5, "6", "600", "0"]


def test_download_pages_skips_open_candle_and_resumes(tmp_path) -> None:
    store = HistoryStore(tmp_path / "h.sqlite")
    now = M0 + 3 * MIN + 30_000  # la vela de M0+3m esta abierta
    pages = {M0: [_kline(M0 + i * MIN, 1, 2, 0.5, 1.5) for i in range(4)]}
    calls = []

    def fake_get(url, timeout):
        calls.append(url)
        start = int(url.split("startTime=")[1].split("&")[0])
        return 200, json.dumps(pages.get(start, [])).encode()

    assert download(store, "XRPUSDT", M0, http_get=fake_get, clock_ms=lambda: now, sleep=lambda s: None) == 3
    assert "symbol=XRPUSDT" in calls[0] and "interval=1m" in calls[0]
    assert store.last_kline_ms("binance", "XRP-USDT", "1m") == M0 + 2 * MIN
    # reanuda desde la ultima guardada: no vuelve a pedir desde M0
    pages[M0 + 3 * MIN] = [_kline(M0 + 3 * MIN, 1, 2, 0.5, 1.5)]
    assert download(store, "XRPUSDT", M0, http_get=fake_get, clock_ms=lambda: now + MIN, sleep=lambda s: None) == 1
    assert int(calls[-1].split("startTime=")[1].split("&")[0]) == M0 + 3 * MIN


def test_download_raises_on_rate_limit(tmp_path) -> None:
    store = HistoryStore(tmp_path / "h.sqlite")
    with pytest.raises(Exception, match="429"):
        download(store, "XRPUSDT", M0, http_get=lambda u, t: (429, b""), clock_ms=lambda: M0 + 10 * MIN,
                 sleep=lambda s: None)


def test_resample_is_exact_and_drops_incomplete_blocks() -> None:
    one = [(M0 + i * MIN, 10 + i, 20 + i, 5 + i, 11 + i, 1.0, 10.0, 2, 4.0) for i in range(12)]
    five = resample(one, 5)
    assert len(five) == 2  # 12 velas -> 2 bloques completos de 5 (el de 2 se descarta)
    assert five[0] == (M0, 10, 24, 5, 15, 5.0, 50.0, 10, 20.0)
    assert resample(one[:4] + one[5:10], 5) == [resample(one, 5)[1]]  # hueco en el primero -> fuera
    assert internal_symbol("XRPUSDT") == "XRP-USDT"


def test_load_derives_higher_intervals_from_1m(tmp_path) -> None:
    store = HistoryStore(tmp_path / "h.sqlite")
    rows = [("binance", "XRP-USDT", "1m", M0 + i * MIN, 1.0, 1.0 + i, 0.5, 1.0, 1.0, 1.0, 1, 0.5) for i in range(60)]
    store.add_klines(rows)
    (hour,) = load(store, "XRPUSDT", "1h", M0, M0 + 60 * MIN)
    assert hour[0] == M0 and hour[2] == 60.0 and hour[5] == 60.0
    assert len(load(store, "XRPUSDT", "15m", M0, M0 + 60 * MIN)) == 4
    with pytest.raises(ValueError):
        load(store, "XRPUSDT", "2h", M0, M0 + 1)


# ---- exportacion ------------------------------------------------------------------------------------


def test_export_day_and_pending_days(tmp_path) -> None:
    store = HistoryStore(tmp_path / "h.sqlite")
    store.upsert_bars([MinuteBar("binance", SYM, M0, 1, 1, 1, 1, buy_usd=5.0),
                       MinuteBar("binance", SYM, M0 + 86_400_000, 2, 2, 2, 2)])
    out = tmp_path / "export"
    assert export_mod.pending_days(store, out, date(2026, 10, 19)) == [date(2026, 10, 17), date(2026, 10, 18)]
    counts = export_mod.export_day(store, out, date(2026, 10, 17))
    assert counts == {"bars_1m": 1, "liquidations": 0, "news": 0, "klines_1m": 0}
    with gzip.open(out / "2026" / "10" / "17" / "bars_1m.csv.gz", "rt") as fh:
        rows = list(csv.reader(fh))
    assert rows[0][:3] == ["exchange", "symbol", "minute_ms"] and rows[1][:3] == ["binance", SYM, str(M0)]
    assert export_mod.pending_days(store, out, date(2026, 10, 19)) == [date(2026, 10, 18)]
    assert export_mod.pending_days(store, out, date(2026, 10, 18)) == []  # hoy no cerro: no se exporta
    assert not list(out.rglob("*.tmp"))


def test_upload_without_remote_warns_and_exports_anyway(tmp_path, caplog, monkeypatch) -> None:
    monkeypatch.delenv("HEATSIGNAL_DRIVE_REMOTE", raising=False)
    store = HistoryStore(tmp_path / "h.sqlite")
    store.upsert_bars([MinuteBar("binance", SYM, M0)])
    store.close()
    rc = export_mod.main(["--db", str(tmp_path / "h.sqlite"), "--out", str(tmp_path / "export"),
                          "--day", "2026-10-17"])
    assert rc == 0 and "sin respaldo en Drive" in caplog.text
    assert (tmp_path / "export" / "2026" / "10" / "17" / "news.csv.gz").exists()
