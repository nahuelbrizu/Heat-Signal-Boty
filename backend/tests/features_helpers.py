"""Constructores compactos de eventos para los tests de features/."""

from __future__ import annotations

from heatsignal.schema.events import Coverage, Exchange, LiquidationEvent, Side, TickerEvent, TradeEvent

SYM = "SOL-USDT"


def liq(price: float, notional: float, side: Side = Side.SELL, source: Exchange = Exchange.BYBIT,
        ts: int = 1_000, coverage: Coverage | None = None, symbol: str = SYM) -> LiquidationEvent:
    if coverage is None:
        coverage = Coverage.FULL if source == Exchange.BYBIT else Coverage.PARTIAL
    return LiquidationEvent(source=source, symbol=symbol, side=side, price=price, qty=notional / price,
                            notional_usd=notional, ts_event_ms=ts, ts_ingest_ms=ts, coverage=coverage)


def trade(price: float, qty: float, side: Side = Side.BUY, source: Exchange = Exchange.BINANCE,
          ts: int = 1_000, block: bool = False, symbol: str = SYM) -> TradeEvent:
    return TradeEvent(source=source, symbol=symbol, side=side, price=price, qty=qty, ts_event_ms=ts,
                      ts_ingest_ms=ts, is_block_trade=block)


def ticker(source: Exchange, ts: int, mark: float | None = None, last: float | None = None,
           funding: float | None = None, oi: float | None = None, oi_usd: float | None = None,
           symbol: str = SYM) -> TickerEvent:
    return TickerEvent(source=source, symbol=symbol, mark_price=mark, last_price=last, funding_rate=funding,
                       open_interest=oi, open_interest_usd=oi_usd, ts_event_ms=ts, ts_ingest_ms=ts)
