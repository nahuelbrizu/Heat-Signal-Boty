"""
Order flow del taker en una ventana -> OrderFlowWindow.

- Volumen USD de cada trade = price * qty (qty en unidades del activo base).
- buy/sell = volumen por lado del TAKER (agresor). cvd_usd = buy - sell.
- imbalance = (buy - sell) / (buy + sell), recortado a [-1, 1]; 0.0 si no hubo
  volumen (ventana vacia NO es "equilibrio": el caller declara el hueco).
- Block trades (Bybit `BT`) se negocian fuera del libro: se EXCLUYEN de buy, sell,
  cvd, imbalance y n_trades, y se reportan aparte en block_trade_volume_usd.
- n_trades cuenta solo trades no-block. sources = exchanges con algun trade
  (incluidos block) en la ventana.
- Ventana: (now - window, now], sin eventos futuros.
"""

from __future__ import annotations

from collections.abc import Iterable

from ..schema.events import Side, TradeEvent
from ..schema.features import OrderFlowWindow
from ._window import select, sorted_exchanges


def build_order_flow(symbol: str, trades: Iterable[TradeEvent], now_ms: int, window_s: int) -> OrderFlowWindow:
    sel = select(trades, symbol, now_ms, window_s)
    buy = sell = block = 0.0
    n = 0
    for t in sel:
        usd = t.price * t.qty
        if t.is_block_trade:
            block += usd
            continue
        n += 1
        if t.side == Side.BUY:
            buy += usd
        else:
            sell += usd
    total = buy + sell
    imbalance = (buy - sell) / total if total > 0 else 0.0
    return OrderFlowWindow(
        symbol=symbol,
        ts_ms=now_ms,
        window_s=window_s,
        buy_volume_usd=buy,
        sell_volume_usd=sell,
        cvd_usd=buy - sell,
        imbalance=min(1.0, max(-1.0, imbalance)),
        n_trades=n,
        block_trade_volume_usd=block,
        sources=sorted_exchanges(t.source for t in sel),
    )
