"""
donchian-ensemble-v1 en Freqtrade (paper trading / dry-run).

Misma regla aprobada y congelada en el laboratorio de HeatSignal (backend/src/heatsignal/lab/strategies.py,
spec_hash 86061813c7d1691c; decisions.md "Lote 1"):
  - velas de 4h; para N en (20, 40, 80, 160): el estado N se enciende cuando el close supera el maximo de las
    N velas ANTERIORES y se apaga cuando el close cae bajo el minimo de las N/2 velas anteriores;
  - objetivo bruto = promedio de los 4 estados (0, .25, .5, .75 o 1);
  - tamano por volatilidad: objetivo * target_vol / (desvio de retornos log de 180 velas * sqrt(velas por anio)),
    con tope 1x. target_vol = 0.15 (lo que eligio el walk-forward en la ultima ventana antes del holdout);
  - solo long; capital repartido en partes iguales entre los 7 pares (como el simulador del laboratorio).

Diferencias con el laboratorio (documentadas, para medir la paridad):
  - Freqtrade ejecuta al open de la vela siguiente (igual que el lab), pero los ajustes de tamano se hacen solo si
    el cambio supera REBALANCE_MIN (5% del capital del par), para no mandar ordenes minusculas.
  - El dry-run no modela slippage (el lab cargaba 2 bps por lado).
"""

from __future__ import annotations

from datetime import datetime

import numpy as np
import pandas as pd
from freqtrade.persistence import Trade
from freqtrade.strategy import IStrategy

CHANNELS = (20, 40, 80, 160)
VOL_WINDOW = 180
TARGET_VOL = 0.15
MAX_LEVERAGE = 1.0
BARS_PER_YEAR = 6 * 365
N_PAIRS = 7
REBALANCE_MIN = 0.05


def donchian_target(df: pd.DataFrame) -> pd.Series:
    """Objetivo de posicion (0..1) por vela, decidido al CIERRE de esa vela (Freqtrade ejecuta en la siguiente)."""
    close, high, low = df["close"].to_numpy(), df["high"], df["low"]
    states = []
    for n in CHANNELS:
        upper = high.shift(1).rolling(n, min_periods=n).max().to_numpy()
        lower = low.shift(1).rolling(max(2, n // 2), min_periods=max(2, n // 2)).min().to_numpy()
        st = np.zeros(len(df))
        on = False
        for i in range(len(df)):
            if not on and np.isfinite(upper[i]) and close[i] > upper[i]:
                on = True
            elif on and np.isfinite(lower[i]) and close[i] < lower[i]:
                on = False
            st[i] = 1.0 if on else 0.0
        states.append(st)
    raw = pd.Series(np.mean(states, axis=0), index=df.index)
    vol = np.log(df["close"] / df["close"].shift(1)).rolling(VOL_WINDOW, min_periods=VOL_WINDOW).std() * np.sqrt(BARS_PER_YEAR)
    scale = (TARGET_VOL / vol).clip(upper=MAX_LEVERAGE)
    return (raw * scale).fillna(0.0)


class DonchianEnsembleV1(IStrategy):
    INTERFACE_VERSION = 3
    timeframe = "4h"
    can_short = False
    stoploss = -0.99            # sin stop: la salida es por canal (como en el laboratorio)
    minimal_roi = {"0": 100}    # sin toma de ganancia por ROI
    use_exit_signal = True
    exit_profit_only = False
    position_adjustment_enable = True
    max_entry_position_adjustment = -1
    process_only_new_candles = True
    startup_candle_count = 400  # 160 velas de canal + 180 de volatilidad + margen

    def populate_indicators(self, dataframe: pd.DataFrame, metadata: dict) -> pd.DataFrame:
        dataframe["target"] = donchian_target(dataframe)
        return dataframe

    def populate_entry_trend(self, dataframe: pd.DataFrame, metadata: dict) -> pd.DataFrame:
        dataframe.loc[dataframe["target"] > 0, ["enter_long", "enter_tag"]] = (1, "donchian")
        return dataframe

    def populate_exit_trend(self, dataframe: pd.DataFrame, metadata: dict) -> pd.DataFrame:
        dataframe.loc[dataframe["target"] <= 0, ["exit_long", "exit_tag"]] = (1, "canal")
        return dataframe

    def _target_now(self, pair: str) -> float:
        df, _ = self.dp.get_analyzed_dataframe(pair, self.timeframe)
        return float(df["target"].iloc[-1]) if len(df) else 0.0

    def _pair_capital(self) -> float:
        return self.wallets.get_total_stake_amount() / N_PAIRS

    def custom_stake_amount(self, pair: str, current_time: datetime, current_rate: float, proposed_stake: float,
                            min_stake: float | None, max_stake: float, leverage: float, entry_tag: str | None,
                            side: str, **kwargs) -> float:
        stake = self._pair_capital() * self._target_now(pair)
        return float(min(max(stake, min_stake or 0.0), max_stake))

    def adjust_trade_position(self, trade: Trade, current_time: datetime, current_rate: float, current_profit: float,
                              min_stake: float | None, max_stake: float, current_entry_rate: float,
                              current_exit_rate: float, current_entry_profit: float, current_exit_profit: float,
                              **kwargs):
        base = self._pair_capital()
        target = self._target_now(trade.pair)
        if target <= 0:
            return None  # la salida total la hace populate_exit_trend
        delta = base * target - trade.stake_amount
        if abs(delta) < REBALANCE_MIN * base:
            return None
        if delta > 0:
            return min(delta, max_stake), "ajuste+"
        if min_stake and -delta < min_stake:
            return None
        return delta, "ajuste-"

    def leverage(self, pair: str, current_time: datetime, current_rate: float, proposed_leverage: float,
                 max_leverage: float, entry_tag: str | None, side: str, **kwargs) -> float:
        return 1.0
