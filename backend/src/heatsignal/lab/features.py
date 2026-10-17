"""
Features del laboratorio: funciones PURAS sobre barras ya fechadas por disponibilidad (`lab/clock.py`).

Reglas (research/ml4t-metodologia.md):
  - Todo rolling mira solo hacia atras (ventanas trailing); nada de min/max/media de toda la serie.
  - Los percentiles para umbrales se calculan con ventana trailing (`rolling_percentile`), o en la ventana
    de entrenamiento del walk-forward y se congelan.
  - Flujo (OFI, liquidaciones) como senal: siempre con >= 1 barra de lag (lo aplica la estrategia).
  - OI en CONTRATOS, no en USD (en USD se mueve solo con el precio).
Cada funcion se testea con `assert_no_lookahead`.
"""

from __future__ import annotations

import numpy as np
import pandas as pd


def log_returns(close: pd.Series, periods: int = 1) -> pd.Series:
    return np.log(close / close.shift(periods))


def realized_vol(close: pd.Series, window: int, min_periods: int | None = None) -> pd.Series:
    """Desvio de los retornos log de 1 barra en una ventana trailing (sin anualizar)."""
    return log_returns(close).rolling(window, min_periods=min_periods or window).std()


def zscore(x: pd.Series, window: int, min_periods: int | None = None) -> pd.Series:
    roll = x.rolling(window, min_periods=min_periods or window)
    return (x - roll.mean()) / roll.std()


def rolling_percentile(x: pd.Series, window: int, q: float, min_periods: int | None = None) -> pd.Series:
    """Percentil q (0..1) de las ultimas `window` barras, SIN incluir la actual (umbral sin look-ahead)."""
    return x.shift(1).rolling(window, min_periods=min_periods or window).quantile(q)


def taker_imbalance(bars: pd.DataFrame) -> pd.Series:
    """(compra - venta) / total del volumen taker en USD; con klines: buy = taker_buy_quote_volume."""
    buy = bars["taker_buy_quote_volume"]
    total = bars["quote_volume"]
    sell = total - buy
    return ((buy - sell) / total.where(total > 0)).clip(-1, 1)


def donchian(bars: pd.DataFrame, n: int) -> pd.DataFrame:
    """Canal de las N barras ANTERIORES (no incluye la actual: romperlo significa superar el maximo previo)."""
    return pd.DataFrame({
        "upper": bars["high"].shift(1).rolling(n, min_periods=n).max(),
        "lower": bars["low"].shift(1).rolling(n, min_periods=n).min(),
    }, index=bars.index)


def atr(bars: pd.DataFrame, n: int = 14) -> pd.Series:
    prev = bars["close"].shift(1)
    tr = pd.concat([bars["high"] - bars["low"], (bars["high"] - prev).abs(), (bars["low"] - prev).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / n, adjust=False, min_periods=n).mean()


def oi_change_contracts(oi_contracts: pd.Series, periods: int) -> pd.Series:
    """Cambio relativo del OI en CONTRATOS en `periods` observaciones."""
    return oi_contracts / oi_contracts.shift(periods) - 1


def funding_zscore(rates: pd.Series, window: int = 42) -> pd.Series:
    """z-score del funding liquidado contra sus ultimas `window` liquidaciones (42 = 14 dias de 8 h)."""
    return zscore(rates, window)
