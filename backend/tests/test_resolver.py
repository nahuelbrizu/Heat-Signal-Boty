"""Tests del Resolver y de las PriceSource: PnL a mano, costos, staleness, sin red."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

from heatsignal.evaluation.evaluator import Evaluator
from heatsignal.evaluation.pending_store import PendingHypothesisStore, due_at
from heatsignal.evaluation.price_source import (
    BinanceKlinePriceSource,
    PriceSourceError,
    StaticPriceSource,
    to_binance_symbol,
)
from heatsignal.evaluation.resolver import (
    DEFAULT_COST_BPS,
    Resolver,
    buy_and_hold_pct,
    gross_pnl_pct,
    net_pnl_pct,
    record_to_signal,
)
from heatsignal.evaluation.strategy_registry import StrategyHypothesis, StrategyRegistry
from heatsignal.schema.hypotheses import HypothesisStatus, PendingHypothesis
from heatsignal.schema.signals import Direction

T0 = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)
H = 3600
EXIT = T0 + timedelta(seconds=H)


# --------------------------------------------------------------------------- formulas


@pytest.mark.parametrize(
    "direction, entry, exit_, cost_bps, gross, net, bench, alpha",
    [
        # LONG 100 -> 102, 10 bps: bruto +2, neto +1.9, B&H +2, alfa -0.1
        (Direction.LONG, "100", "102", 10, 2.0, 1.9, 2.0, -0.1),
        # SHORT 100 -> 102: bruto -2, neto -2.1, B&H +2, alfa -4.1
        (Direction.SHORT, "100", "102", 10, -2.0, -2.1, 2.0, -4.1),
        # SHORT 100 -> 97: bruto +3, neto +2.9, B&H -3, alfa +5.9
        (Direction.SHORT, "100", "97", 10, 3.0, 2.9, -3.0, 5.9),
        # LONG 200 -> 190, sin costos: -5
        (Direction.LONG, "200", "190", 0, -5.0, -5.0, -5.0, 0.0),
        # SHORT lineal (no inversa): 50 -> 25 => +50%, no +100%
        (Direction.SHORT, "50", "25", 0, 50.0, 50.0, -50.0, 100.0),
    ],
)
def test_pnl_formulas_hand_computed(direction, entry, exit_, cost_bps, gross, net, bench, alpha) -> None:
    e, x = Decimal(entry), Decimal(exit_)
    assert gross_pnl_pct(direction, e, x) == pytest.approx(gross)
    assert net_pnl_pct(direction, e, x, cost_bps) == pytest.approx(net)
    assert buy_and_hold_pct(e, x) == pytest.approx(bench)
    assert net_pnl_pct(direction, e, x, cost_bps) - buy_and_hold_pct(e, x) == pytest.approx(alpha)


def test_default_cost_is_round_trip_taker() -> None:
    assert DEFAULT_COST_BPS == 10.0


def test_pnl_rejects_non_positive_prices() -> None:
    with pytest.raises(ValueError):
        gross_pnl_pct(Direction.LONG, Decimal("0"), Decimal("1"))


# --------------------------------------------------------------------------- fixtures


def _setup(tmp_path: Path):
    registry = StrategyRegistry(tmp_path / "eval.db")
    strategy = StrategyHypothesis(description="s", entry_conditions={}, exit_conditions={}, author="t")
    registry.register(strategy)
    return PendingHypothesisStore(tmp_path / "eval.db", registry), strategy


def _hyp(strategy_id, direction=Direction.LONG, entry="100", symbol="BTC-USDT", created_at=T0, conf=0.6):
    return PendingHypothesis(
        strategy_id=strategy_id,
        symbol=symbol,
        created_at=created_at,
        direction=direction,
        confidence_score=conf,
        entry_reference_price=Decimal(entry),
        horizon_s=H,
        rationale="x",
    )


def _source(price: str, at: datetime = EXIT, symbol: str = "BTC-USDT", **kw) -> StaticPriceSource:
    return StaticPriceSource({symbol: [(at, Decimal(price))]}, **kw)


# --------------------------------------------------------------------------- resolver


def test_resolve_long_and_short_with_costs(tmp_path: Path) -> None:
    store, s = _setup(tmp_path)
    long_h = _hyp(s.strategy_id, Direction.LONG)
    short_h = _hyp(s.strategy_id, Direction.SHORT)
    store.add(long_h)
    store.add(short_h)

    summary = Resolver(store, _source("102"), cost_bps=10).resolve_due(EXIT)
    assert (summary.due, summary.resolved, summary.expired_no_data, summary.errors) == (2, 2, 0, [])

    by_id = {r.hypothesis.hypothesis_id: r for r in store.list_by_strategy(s.strategy_id)}
    lr, sr = by_id[long_h.hypothesis_id], by_id[short_h.hypothesis_id]
    assert lr.resolution.exit_price == Decimal("102")
    assert lr.resolution.pnl_pct == pytest.approx(1.9) and lr.resolution.win is True
    assert lr.metrics.gross_pnl_pct == pytest.approx(2.0)
    assert lr.metrics.alpha_pct == pytest.approx(-0.1)
    assert lr.metrics.cost_bps == 10
    assert lr.metrics.exit_price_ts == EXIT
    assert sr.resolution.pnl_pct == pytest.approx(-2.1) and sr.resolution.win is False
    assert sr.metrics.benchmark_pnl_pct == pytest.approx(2.0)
    assert sr.metrics.alpha_pct == pytest.approx(-4.1)


def test_costs_turn_small_gross_win_into_loss(tmp_path: Path) -> None:
    store, s = _setup(tmp_path)
    store.add(_hyp(s.strategy_id))
    Resolver(store, _source("100.05"), cost_bps=10).resolve_due(EXIT)  # bruto +0.05, neto -0.05
    [r] = store.list_by_strategy(s.strategy_id)
    assert r.metrics.gross_pnl_pct == pytest.approx(0.05)
    assert r.resolution.pnl_pct == pytest.approx(-0.05)
    assert r.resolution.win is False


def test_net_zero_is_not_a_win(tmp_path: Path) -> None:
    store, s = _setup(tmp_path)
    store.add(_hyp(s.strategy_id))
    Resolver(store, _source("100.1"), cost_bps=10).resolve_due(EXIT)  # bruto +0.1 - 0.1 = 0
    [r] = store.list_by_strategy(s.strategy_id)
    assert r.resolution.pnl_pct == 0.0
    assert r.resolution.win is False


def test_zero_cost_gives_gross(tmp_path: Path) -> None:
    store, s = _setup(tmp_path)
    store.add(_hyp(s.strategy_id))
    Resolver(store, _source("100.05"), cost_bps=0).resolve_due(EXIT)
    [r] = store.list_by_strategy(s.strategy_id)
    assert r.resolution.pnl_pct == pytest.approx(0.05) and r.resolution.win is True


def test_negative_cost_rejected(tmp_path: Path) -> None:
    store, _ = _setup(tmp_path)
    with pytest.raises(ValueError):
        Resolver(store, _source("1"), cost_bps=-1)


def test_stale_price_expires_after_grace(tmp_path: Path) -> None:
    store, s = _setup(tmp_path)
    store.add(_hyp(s.strategy_id))
    # Ultima observacion 3 min antes de la salida, tolerancia 2 min -> stale -> None.
    source = _source("105", at=EXIT - timedelta(minutes=3), max_staleness=timedelta(minutes=2))
    resolver = Resolver(store, source, no_data_grace=timedelta(minutes=10))

    within = resolver.resolve_due(EXIT + timedelta(minutes=5))
    assert (within.deferred_no_data, within.expired_no_data) == (1, 0)
    assert store.pending_count() == 1

    after = resolver.resolve_due(EXIT + timedelta(minutes=10))
    assert (after.deferred_no_data, after.expired_no_data) == (0, 1)
    [r] = store.list_by_strategy(s.strategy_id)
    assert r.status == HypothesisStatus.EXPIRED_NO_DATA
    assert r.resolution.exit_price is None and r.resolution.pnl_pct is None and r.resolution.win is None
    assert r.metrics is None


def test_price_within_staleness_is_used(tmp_path: Path) -> None:
    store, s = _setup(tmp_path)
    store.add(_hyp(s.strategy_id))
    source = _source("101", at=EXIT - timedelta(minutes=2), max_staleness=timedelta(minutes=2))
    assert Resolver(store, source).resolve_due(EXIT).resolved == 1


def test_unknown_symbol_expires_not_dropped(tmp_path: Path) -> None:
    store, s = _setup(tmp_path)
    store.add(_hyp(s.strategy_id, symbol="NOPE-USDT"))
    summary = Resolver(store, _source("1"), no_data_grace=timedelta(0)).resolve_due(EXIT)
    assert summary.expired_no_data == 1
    assert len(store.list_by_strategy(s.strategy_id)) == 1  # registrada, no descartada


class _BrokenSource:
    def price_at(self, symbol, ts):
        raise PriceSourceError("timeout")


def test_transient_source_error_keeps_pending(tmp_path: Path) -> None:
    store, s = _setup(tmp_path)
    h = _hyp(s.strategy_id)
    store.add(h)
    summary = Resolver(store, _BrokenSource(), no_data_grace=timedelta(0)).resolve_due(EXIT + timedelta(days=1))
    assert summary.resolved == summary.expired_no_data == 0
    assert [e.hypothesis_id for e in summary.errors] == [h.hypothesis_id]
    assert store.pending_count() == 1


class _BadPriceSource:
    def price_at(self, symbol, ts):
        return Decimal("-5")


def test_non_positive_price_treated_as_no_data(tmp_path: Path) -> None:
    store, s = _setup(tmp_path)
    store.add(_hyp(s.strategy_id))
    summary = Resolver(store, _BadPriceSource(), no_data_grace=timedelta(0)).resolve_due(EXIT)
    assert summary.expired_no_data == 1


def test_resolve_is_idempotent(tmp_path: Path) -> None:
    store, s = _setup(tmp_path)
    store.add(_hyp(s.strategy_id))
    resolver = Resolver(store, _source("101"))
    assert resolver.resolve_due(EXIT).resolved == 1
    second = resolver.resolve_due(EXIT + timedelta(hours=1))
    assert (second.due, second.resolved) == (0, 0)


def test_not_due_is_untouched(tmp_path: Path) -> None:
    store, s = _setup(tmp_path)
    h = _hyp(s.strategy_id)
    store.add(h)
    resolver = Resolver(store, _source("101"))
    assert resolver.resolve_due(EXIT - timedelta(seconds=1)).due == 0
    with pytest.raises(ValueError):
        resolver.resolve_one(h, EXIT - timedelta(seconds=1))


def test_exit_price_queried_at_horizon_not_now(tmp_path: Path) -> None:
    store, s = _setup(tmp_path)
    store.add(_hyp(s.strategy_id))
    calls: list[datetime] = []

    class Spy:
        def price_at(self, symbol, ts):
            calls.append(ts)
            return Decimal("101")

    Resolver(store, Spy()).resolve_due(EXIT + timedelta(hours=5))
    assert calls == [EXIT]


# --------------------------------------------------------------------------- Signal


def test_record_to_signal_and_evaluator_accepts(tmp_path: Path) -> None:
    store, s = _setup(tmp_path)
    h = _hyp(s.strategy_id, Direction.SHORT, entry="200")
    store.add(h)
    Resolver(store, _source("190"), cost_bps=10).resolve_due(EXIT)  # bruto +5, neto +4.9
    [record] = store.list_by_strategy(s.strategy_id)
    sig = record_to_signal(record)
    assert sig.signal_id == h.hypothesis_id
    assert sig.strategy_id == s.strategy_id
    assert sig.direction == Direction.SHORT
    assert (sig.entry_price, sig.exit_price) == (Decimal("200"), Decimal("190"))
    assert (sig.entry_time, sig.exit_time) == (T0, EXIT)
    assert sig.pnl_pct == pytest.approx(4.9)
    assert sig.pnl == Decimal("9.8")  # 200 * 4.9 / 100
    result = Evaluator().evaluate(s.strategy_id, [sig], [200.0])
    assert result.n_signals == 1 and result.win_rate == 1.0 and result.passed is False


def test_expired_record_is_not_a_signal(tmp_path: Path) -> None:
    store, s = _setup(tmp_path)
    store.add(_hyp(s.strategy_id))
    Resolver(store, _source("1", symbol="ETH-USDT"), no_data_grace=timedelta(0)).resolve_due(EXIT)
    [record] = store.list_by_strategy(s.strategy_id)
    with pytest.raises(ValueError):
        record_to_signal(record)


# --------------------------------------------------------------------------- StaticPriceSource


def test_static_source_no_look_ahead_and_staleness_boundary() -> None:
    src = StaticPriceSource(
        {"BTC-USDT": [(T0, Decimal("1")), (T0 + timedelta(seconds=90), Decimal("2"))]},
        max_staleness=timedelta(seconds=60),
    )
    assert src.price_at("BTC-USDT", T0 - timedelta(seconds=1)) is None  # antes de todo
    assert src.price_at("BTC-USDT", T0 + timedelta(seconds=60)) == Decimal("1")  # borde incluido
    assert src.price_at("BTC-USDT", T0 + timedelta(seconds=61)) is None  # stale, y no mira el futuro
    assert src.price_at("BTC-USDT", T0 + timedelta(seconds=90)) == Decimal("2")
    assert src.price_at("ETH-USDT", T0) is None


# --------------------------------------------------------------------------- Binance


def _kline(open_ms: int, open_price: str = "81036.20") -> list:
    return [open_ms, open_price, "81134.70", "81027.80", "81134.70", "108.555", open_ms + 59_999,
            "8800533.5", 3830, "42.3", "3437299.3", "0"]


class FakeHttp:
    def __init__(self, status: int, body) -> None:
        self.status = status
        self.body = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.urls: list[str] = []

    def __call__(self, url: str, timeout_s: float):
        self.urls.append(url)
        return self.status, self.body


TS = datetime(2026, 10, 16, 18, 42, 30, 123000, tzinfo=timezone.utc)
TS_MS = int(TS.timestamp() * 1000)
CANDLE_MS = TS_MS - 30_123  # vela de 1m que contiene TS


def test_to_binance_symbol() -> None:
    assert to_binance_symbol("BTC-USDT") == "BTCUSDT"
    assert to_binance_symbol("eth-usdt") == "ETHUSDT"


def test_binance_request_params() -> None:
    http = FakeHttp(200, [_kline(CANDLE_MS)])
    BinanceKlinePriceSource(http_get=http).price_at("BTC-USDT", TS)
    url = urlparse(http.urls[0])
    assert (url.scheme, url.netloc, url.path) == ("https", "fapi.binance.com", "/fapi/v1/markPriceKlines")
    assert parse_qs(url.query) == {
        "symbol": ["BTCUSDT"],
        "interval": ["1m"],
        "endTime": [str(TS_MS)],
        "limit": ["1"],
    }


def test_binance_mark_price_path() -> None:
    http = FakeHttp(200, [_kline(CANDLE_MS)])
    BinanceKlinePriceSource(http_get=http, price_kind="mark").price_at("BTC-USDT", TS)
    assert urlparse(http.urls[0]).path == "/fapi/v1/markPriceKlines"
    with pytest.raises(ValueError):
        BinanceKlinePriceSource(price_kind="index")


def test_binance_uses_open_of_containing_candle() -> None:
    src = BinanceKlinePriceSource(http_get=FakeHttp(200, [_kline(CANDLE_MS, "81036.20")]))
    assert src.price_at("BTC-USDT", TS) == Decimal("81036.20")


def test_binance_empty_means_no_data() -> None:
    assert BinanceKlinePriceSource(http_get=FakeHttp(200, [])).price_at("BTC-USDT", TS) is None


def test_binance_stale_candle_means_no_data() -> None:
    old = CANDLE_MS - 10 * 60_000  # hueco de mantenimiento: la ultima vela es de 10 min antes
    src = BinanceKlinePriceSource(http_get=FakeHttp(200, [_kline(old)]), max_staleness=timedelta(minutes=2))
    assert src.price_at("BTC-USDT", TS) is None


def test_binance_candle_after_ts_is_rejected() -> None:
    src = BinanceKlinePriceSource(http_get=FakeHttp(200, [_kline(TS_MS + 1)]))
    assert src.price_at("BTC-USDT", TS) is None


def test_binance_invalid_symbol_means_no_data() -> None:
    http = FakeHttp(400, {"code": -1121, "msg": "Invalid symbol."})
    assert BinanceKlinePriceSource(http_get=http).price_at("NOPE-USDT", TS) is None


@pytest.mark.parametrize(
    "status, body",
    [
        (500, b"oops"),
        (429, {"code": -1003, "msg": "Too many requests"}),
        (418, b""),
        (400, {"code": -1100, "msg": "Illegal characters"}),
        (200, {"not": "a list"}),
        (200, b"<html>"),
        (200, [["bad"]]),
    ],
)
def test_binance_transport_or_format_errors_raise(status, body) -> None:
    with pytest.raises(PriceSourceError):
        BinanceKlinePriceSource(http_get=FakeHttp(status, body)).price_at("BTC-USDT", TS)


def test_binance_zero_price_means_no_data() -> None:
    src = BinanceKlinePriceSource(http_get=FakeHttp(200, [_kline(CANDLE_MS, "0")]))
    assert src.price_at("BTC-USDT", TS) is None


def test_resolver_with_binance_source_end_to_end(tmp_path: Path) -> None:
    store, s = _setup(tmp_path)
    created = TS - timedelta(seconds=H)
    store.add(_hyp(s.strategy_id, entry="80000", created_at=created))
    http = FakeHttp(200, [_kline(CANDLE_MS, "80800")])
    Resolver(store, BinanceKlinePriceSource(http_get=http), cost_bps=10).resolve_due(TS)
    [r] = store.list_by_strategy(s.strategy_id)
    assert r.resolution.exit_price == Decimal("80800")
    assert r.resolution.pnl_pct == pytest.approx(0.9)  # +1% bruto - 0.1
    assert due_at(r.hypothesis) == TS
