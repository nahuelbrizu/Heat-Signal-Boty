"""
Reloj de disponibilidad: cuando se CONOCE cada dato. Es la defensa principal contra el look-ahead.

Regla del laboratorio (research/ml4t-metodologia.md): toda serie que entra a una estrategia se indexa por el
instante en que el dato estuvo disponible, nunca por el que pone el proveedor.

  - Vela 1m de Binance: el proveedor la fecha en su APERTURA; se conoce al cierre -> disponible en open + 1m.
  - Velas agregadas (5m, 15m, 30m, 1h, 4h): se arman desde 1m y se fechan al CIERRE
    (label="right", closed="left"). Solo cuentan bloques completos.
  - metrics (OI, long/short, ratio taker), cada 5m: el ratio taker es del periodo que EMPIEZA en ts; por
    prudencia todo metrics se toma como disponible en ts + 5m.
  - funding: la tasa liquidada se conoce en la liquidacion (no se usa el funding "predicho" en vivo).
  - premium index 1m: como las velas, disponible en open + 1m.

`assert_no_lookahead` comprueba cualquier funcion de features: el valor en t no puede cambiar si se borra
todo lo posterior a t.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence

import numpy as np
import pandas as pd

ONE_MIN = pd.Timedelta(minutes=1)
METRICS_DELAY = pd.Timedelta(minutes=5)

OHLCV_AGG = {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum",
             "quote_volume": "sum", "count": "sum", "taker_buy_volume": "sum", "taker_buy_quote_volume": "sum"}


class LookAheadError(AssertionError):
    pass


def available_1m(klines: pd.DataFrame) -> pd.DataFrame:
    """Velas 1m re-fechadas a su cierre (cuando se conocen)."""
    out = klines.copy()
    out.index = klines.index + ONE_MIN
    return out


def resample_bars(klines: pd.DataFrame, rule: str) -> pd.DataFrame:
    """Agrega velas 1m (indice = apertura) a `rule` ('5min', '15min', '1h', '4h', '1D').

    Indice del resultado = CIERRE del bloque (cuando se conoce). Bloques incompletos (faltan velas 1m)
    se descartan: una vela de 1h hecha con 50 minutos no es la vela real.
    """
    cols = {c: f for c, f in OHLCV_AGG.items() if c in klines.columns}
    grouped = klines.resample(rule, label="right", closed="left")
    bars = grouped.agg(cols)
    n = grouped["close"].count()
    expected = int(pd.Timedelta(rule) / ONE_MIN)
    return bars[n == expected].dropna(subset=["close"])


def available_metrics(metrics: pd.DataFrame) -> pd.DataFrame:
    out = metrics.copy()
    out.index = metrics.index + METRICS_DELAY
    return out


def asof(left_index: pd.DatetimeIndex, right: pd.DataFrame | pd.Series, *,
         tolerance: pd.Timedelta | None = None) -> pd.DataFrame | pd.Series:
    """Para cada instante de `left_index`, el ultimo valor de `right` disponible en o antes de ese instante.

    `right` tiene que estar indexado por disponibilidad (usar las funciones de arriba). Con `tolerance`,
    un dato mas viejo que eso queda NaN (hueco declarado, no arrastre infinito).
    """
    frame = right.to_frame() if isinstance(right, pd.Series) else right
    left = pd.DataFrame(index=left_index)
    merged = pd.merge_asof(left, frame.sort_index(), left_index=True, right_index=True, direction="backward",
                           tolerance=tolerance)
    merged.index = left_index
    return merged[right.name] if isinstance(right, pd.Series) else merged


def assert_no_lookahead(
    fn: Callable[[pd.DataFrame], pd.DataFrame | pd.Series],
    data: pd.DataFrame,
    *,
    cut_points: Sequence[int] | None = None,
    n_cuts: int = 8,
    rtol: float = 1e-9,
) -> None:
    """Falla si `fn(data)` en algun instante t depende de datos posteriores a t.

    Calcula `fn` con todos los datos y con los datos truncados en varios puntos; los valores en t <= corte
    tienen que coincidir. Es el mismo principio que `lookahead-analysis` de Freqtrade.
    """
    full = fn(data)
    full = full.to_frame() if isinstance(full, pd.Series) else full
    n = len(data)
    cuts = list(cut_points) if cut_points is not None else [int(n * k / (n_cuts + 1)) for k in range(1, n_cuts + 1)]
    for c in cuts:
        if c <= 1 or c >= n:
            continue
        cut_time = data.index[c - 1]
        part = fn(data.iloc[:c])
        part = part.to_frame() if isinstance(part, pd.Series) else part
        common = part.index[part.index <= cut_time].intersection(full.index)
        if common.empty:
            continue
        a = full.loc[common, part.columns].to_numpy(dtype=float)
        b = part.loc[common].to_numpy(dtype=float)
        same = np.isclose(a, b, rtol=rtol, atol=0.0, equal_nan=True)
        if not same.all():
            row, col = np.argwhere(~same)[0]
            raise LookAheadError(
                f"look-ahead: la columna {part.columns[col]!r} en {common[row]} vale {a[row, col]!r} con todos los "
                f"datos y {b[row, col]!r} con datos hasta {cut_time}")
