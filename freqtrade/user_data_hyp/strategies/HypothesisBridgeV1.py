"""
HypothesisBridgeV1: ejecuta en Freqtrade DRY-RUN (dinero simulado, sin API keys) las hipotesis de 1h del grafo.

NO tiene senales propias. Cada operacion la abre el puente de HeatSignal (`heatsignal.paper.ft_bridge`) con
`POST /api/v1/forceenter` y un `entry_tag` que codifica la hipotesis y su vencimiento:

    hyp:<id8>:<due_epoch>      id8 = primeros 8 hex del hypothesis_id; due_epoch = due_at en segundos UTC

La operacion se cierra por TIEMPO: `custom_exit` devuelve "hyp_due" en la primera iteracion del bot con
`current_time >= due_at` (el cierre de vela de 1h con el que el Resolver resuelve la hipotesis). Con
`process_throttle_secs = 60` la salida sale como mucho ~1 min despues del cierre de la vela, a precio de mercado
simulado (order book), no al mark de cierre de la vela: esa diferencia es justamente lo que se quiere medir.

Stoploss de SEGURIDAD: -20 % (con leverage 1 = movimiento de precio del 20 % en contra). No es parte de la
hipotesis (el Resolver no tiene stop): solo evita que un error del puente o un movimiento extremo deje una
operacion simulada perdiendo sin limite. Si llega a saltar, la operacion figura con exit_reason "stop_loss" y el
panel lo marca como diferencia de diseno. ROI desactivado (minimal_roi = 10000 %).

Defensas:
  - `confirm_trade_entry` rechaza cualquier entrada sin tag valido o con el vencimiento ya pasado (solo entra lo
    que manda el puente).
  - Un trade abierto con tag ilegible se cierra en la siguiente iteracion ("hyp_tag_invalido"): no hay forma de
    saber cuando venceria.
  - Sin ajuste de posicion: Freqtrade no permite dos trades en el mismo par (el puente lo registra como "par ocupado").

Todo lo de esta estrategia es solo libreria estandar + la interfaz de Freqtrade (corre en el venv de Freqtrade,
que no tiene el paquete heatsignal). El formato del tag lo comparte con `heatsignal/paper/ft_bridge.py`; los tests
verifican que los dos coinciden.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone

from freqtrade.persistence import Trade
from freqtrade.strategy import IStrategy

TAG_RE = re.compile(r"^hyp:([0-9a-f]{8}):([0-9]{9,11})$")
EXIT_DUE = "hyp_due"
EXIT_BAD_TAG = "hyp_tag_invalido"
SAFETY_STOPLOSS = -0.20


def decode_tag(tag: str | None) -> tuple[str, int] | None:
    """'hyp:1a2b3c4d:1792000000' -> ('1a2b3c4d', 1792000000); None si no es un tag del puente."""
    if not tag:
        return None
    m = TAG_RE.match(str(tag).strip())
    return (m.group(1), int(m.group(2))) if m else None


def _epoch(ts: datetime) -> float:
    if ts.tzinfo is None:  # Freqtrade pasa UTC; si viniera sin zona se asume UTC
        ts = ts.replace(tzinfo=timezone.utc)
    return ts.timestamp()


def exit_reason_for(tag: str | None, current_time: datetime) -> str | None:
    """Motivo de salida para un trade abierto con ese tag en `current_time`, o None si todavia no vence."""
    dec = decode_tag(tag)
    if dec is None:
        return EXIT_BAD_TAG
    return EXIT_DUE if _epoch(current_time) >= dec[1] else None


def entry_allowed(tag: str | None, current_time: datetime) -> bool:
    """Solo entra lo que manda el puente y que todavia no vencio."""
    dec = decode_tag(tag)
    return dec is not None and _epoch(current_time) < dec[1]


class HypothesisBridgeV1(IStrategy):
    INTERFACE_VERSION = 3

    timeframe = "1h"
    can_short = True
    minimal_roi = {"0": 100.0}  # ROI desactivado: la salida es por tiempo (custom_exit)
    stoploss = SAFETY_STOPLOSS
    trailing_stop = False
    use_exit_signal = True  # obligatorio: Freqtrade solo llama a custom_exit con use_exit_signal=True
    exit_profit_only = False
    ignore_roi_if_entry_signal = False
    position_adjustment_enable = False
    process_only_new_candles = True
    startup_candle_count = 0

    def populate_indicators(self, dataframe, metadata: dict):
        return dataframe

    def populate_entry_trend(self, dataframe, metadata: dict):
        dataframe["enter_long"] = 0
        dataframe["enter_short"] = 0
        return dataframe

    def populate_exit_trend(self, dataframe, metadata: dict):
        dataframe["exit_long"] = 0
        dataframe["exit_short"] = 0
        return dataframe

    def custom_exit(self, pair: str, trade: Trade, current_time: datetime, current_rate: float,
                    current_profit: float, **kwargs) -> str | None:
        return exit_reason_for(trade.enter_tag, current_time)

    def confirm_trade_entry(self, pair: str, order_type: str, amount: float, rate: float, time_in_force: str,
                            current_time: datetime, entry_tag: str | None, side: str, **kwargs) -> bool:
        return entry_allowed(entry_tag, current_time)

    def leverage(self, pair: str, current_time: datetime, current_rate: float, proposed_leverage: float,
                 max_leverage: float, entry_tag: str | None, side: str, **kwargs) -> float:
        return 1.0
