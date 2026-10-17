"""
Baselines obligatorios del laboratorio (plan-laboratorio.md, punto 7).

- ``buy_and_hold``: long 1x por simbolo desde la primera vela, con funding y costos (un perpetuo
  comprado y mantenido paga funding: es el baseline pasivo realista).
- ``equal_weight_basket``: canasta equal-weight de los simbolos, rebalanceada al nocional objetivo una
  vez por dia (00:00 UTC).
- ``random_entries``: senales al azar con la MISMA cantidad de trades, la MISMA distribucion de
  duraciones y la MISMA proporcion long/short que la senal dada (se conservan los trades y se
  permutan sus posiciones de inicio). ``random_entries_null`` simula cada permutacion y devuelve la
  distribucion nula del Sharpe; ``empirical_pvalue`` da el p-valor.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from dataclasses import dataclass

import numpy as np
import pandas as pd

from heatsignal.lab.sim import Costs, SimResult, simulate
from heatsignal.lab.stats import sharpe_ratio


def buy_and_hold(
    prices: Mapping[str, pd.DataFrame],
    funding: Mapping[str, pd.Series] | None = None,
    costs: Costs | None = None,
    capital: float = 1.0,
) -> dict[str, SimResult]:
    """Un ``SimResult`` por simbolo: long 1x del capital desde el open de la primera vela."""
    out = {}
    for sym, df in prices.items():
        sig = pd.Series([1.0], index=df.index[:1])
        f = {sym: funding[sym]} if funding is not None and sym in funding else None
        out[sym] = simulate({sym: df}, {sym: sig}, f, costs, capital)
    return out


def equal_weight_basket(
    prices: Mapping[str, pd.DataFrame],
    funding: Mapping[str, pd.Series] | None = None,
    costs: Costs | None = None,
    capital: float = 1.0,
    rebalance_freq: str = "1D",
) -> SimResult:
    """Canasta long equal-weight (capital / n por simbolo) rebalanceada cada ``rebalance_freq``.

    En cada frontera (00:00 UTC por defecto) se re-ajustan las unidades al nocional objetivo; el ajuste
    paga costos sobre el delta.
    """
    signals = {}
    for sym, df in prices.items():
        idx = df.index
        start = idx[0].floor(rebalance_freq)
        dates = pd.date_range(start, idx[-1], freq=rebalance_freq)
        dates = dates[dates >= start]
        if len(dates) == 0 or dates[0] > idx[0]:
            dates = dates.insert(0, idx[0])
        signals[sym] = pd.Series(1.0, index=dates)
    return simulate(prices, signals, funding, costs, capital, rebalance=True)


# ---------------------------------------------------------------------------- entradas al azar

@dataclass(frozen=True)
class SignalTrades:
    """Trades extraidos de una senal en una grilla regular."""

    grid: pd.DatetimeIndex
    starts: np.ndarray  # posicion en la grilla
    lengths: np.ndarray  # cantidad de pasos de grilla
    blocks: list[np.ndarray]  # valores de posicion dentro de cada trade
    directions: np.ndarray  # +1 / -1


def _infer_step(index: pd.DatetimeIndex) -> pd.Timedelta:
    diffs = np.diff(index.as_unit("ns").asi8)
    diffs = diffs[diffs > 0]
    if diffs.size == 0:
        raise ValueError("no se puede inferir la grilla: pasar freq")
    return pd.Timedelta(int(np.gcd.reduce(diffs)), unit="ns")


def extract_trades(
    signal: pd.Series,
    freq: str | pd.Timedelta | None = None,
    start: pd.Timestamp | None = None,
    end: pd.Timestamp | None = None,
) -> SignalTrades:
    """Pasa la senal (forward-fill) a una grilla regular y extrae los trades (tramos de mismo signo).

    ``freq`` por defecto = MCD de las diferencias del indice de la senal (ej. 4h si decide cada 4h).
    La grilla va de ``start`` (default primer indice) a ``end`` (default ultimo indice).
    """
    sig = signal.dropna().sort_index()
    if sig.index.tz is None:
        raise ValueError("la senal tiene que tener indice tz-aware UTC")
    step = pd.Timedelta(freq) if freq is not None else _infer_step(sig.index)
    start = sig.index[0] if start is None else pd.Timestamp(start)
    end = sig.index[-1] if end is None else pd.Timestamp(end)
    grid = pd.date_range(start, end, freq=step)
    inside = sig.index[(sig.index >= start) & (sig.index <= end)]
    if not inside.isin(grid).all():
        raise ValueError("el indice de la senal no cae sobre la grilla; pasar freq/start compatibles")
    vals = sig.reindex(grid, method="ffill").fillna(0.0).to_numpy(dtype=np.float64)
    s = np.sign(vals)
    s_prev = np.r_[0.0, s[:-1]]
    s_next = np.r_[s[1:], 0.0]
    starts = np.flatnonzero((s != 0) & (s != s_prev))
    ends = np.flatnonzero((s != 0) & (s != s_next)) + 1
    blocks = [vals[a:b].copy() for a, b in zip(starts, ends)]
    return SignalTrades(
        grid=grid,
        starts=starts,
        lengths=ends - starts,
        blocks=blocks,
        directions=s[starts].astype(np.int8),
    )


def _place(n: int, lengths: np.ndarray, rng: np.random.Generator) -> tuple[np.ndarray, np.ndarray]:
    """Orden aleatorio de los trades e inicios aleatorios sin solaparse (estrellas y barras).

    Deja al menos 1 paso libre entre trades si hay lugar (asi dos trades del mismo signo no se fusionan);
    si no hay lugar, los permite pegados.
    """
    k = lengths.size
    order = rng.permutation(k)
    total = int(lengths.sum())
    min_gap = 1 if n - total - (k - 1) >= 0 else 0
    free = n - total - min_gap * (k - 1)
    if free < 0:
        raise ValueError("los trades no entran en la grilla")
    # k+1 huecos >= 0 que suman ``free``: elegir k posiciones de free + k
    cuts = np.sort(rng.choice(free + k, size=k, replace=False))
    gaps = cuts - np.arange(k)  # huecos acumulados antes de cada trade (no decreciente)
    lens = lengths[order]
    cum_len = np.r_[0, np.cumsum(lens)[:-1]]
    starts = gaps + cum_len + min_gap * np.arange(k)
    return order, starts


def random_entries(
    signal: pd.Series,
    n_perm: int = 1000,
    seed: int | None = None,
    *,
    freq: str | pd.Timedelta | None = None,
    start: pd.Timestamp | None = None,
    end: pd.Timestamp | None = None,
) -> Iterator[pd.Series]:
    """Genera ``n_perm`` senales aleatorias (iterador, para no materializar 1000 series).

    Cada una tiene los MISMOS trades (mismas duraciones, mismos valores de posicion, misma direccion)
    que ``signal`` en la grilla de ``extract_trades``, en un orden aleatorio y con inicios aleatorios sin
    solaparse dentro de la misma ventana. La serie devuelta solo tiene los puntos de cambio (formato
    de senal esparso que entiende ``simulate``).
    """
    tr = extract_trades(signal, freq, start, end)
    n = len(tr.grid)
    rng = np.random.default_rng(seed)
    for _ in range(n_perm):
        vals = np.zeros(n)
        if tr.lengths.size:
            order, starts = _place(n, tr.lengths, rng)
            for i, st in zip(order, starts):
                vals[st : st + tr.lengths[i]] = tr.blocks[i]
        prev = np.r_[0.0, vals[:-1]]
        change = vals != prev
        change[0] = True
        yield pd.Series(vals[change], index=tr.grid[change])


def random_shift_joint(
    signals: dict[str, pd.Series],
    n_perm: int = 1000,
    seed: int | None = None,
    *,
    start: pd.Timestamp,
    end: pd.Timestamp,
) -> Iterator[dict[str, pd.Series]]:
    """Nulo CONJUNTO para estrategias de varias patas (pares, cobertura con BTC, long/short cross-sectional).

    `random_entries` baraja cada simbolo por separado y rompe la cobertura: el azar queda direccional. Aca
    todas las patas se corren juntas, en circulo, el mismo desplazamiento al azar sobre una grilla comun:
    se conservan las duraciones, el tamano de cada pata y la relacion entre patas en cada instante; solo se
    pierde la sincronia con los precios. Desplazamiento en [1, n-1] pasos (nunca la senal original).
    """
    sigs = {s: v.dropna().sort_index() for s, v in signals.items() if len(v)}
    if not sigs:
        return
    start, end = pd.Timestamp(start), pd.Timestamp(end)
    idx = pd.DatetimeIndex(sorted(set().union(*(v.index[(v.index >= start) & (v.index <= end)] for v in sigs.values()))))
    step = _infer_step(idx.append(pd.DatetimeIndex([start])).unique().sort_values())
    grid = pd.date_range(start, end, freq=step)
    vals = {s: v.reindex(grid.union(v.index)).ffill().reindex(grid).fillna(0.0).to_numpy(dtype=np.float64)
            for s, v in sigs.items()}
    n = len(grid)
    if n < 2:
        return
    rng = np.random.default_rng(seed)
    for _ in range(n_perm):
        k = int(rng.integers(1, n))
        out = {}
        for s, a in vals.items():
            r = np.roll(a, k)
            prev = np.r_[np.nan, r[:-1]]
            change = r != prev
            out[s] = pd.Series(r[change], index=grid[change])
        yield out


def random_entries_null(
    prices: pd.DataFrame,
    signal: pd.Series,
    funding: pd.Series | None = None,
    costs: Costs | None = None,
    n_perm: int = 1000,
    seed: int | None = None,
    symbol: str = "SYM",
    **grid_kwargs,
) -> np.ndarray:
    """Sharpe anualizado (retornos diarios netos) de cada permutacion de ``random_entries``."""
    f = {symbol: funding} if funding is not None else None
    out = np.empty(n_perm)
    for i, sig in enumerate(random_entries(signal, n_perm, seed, **grid_kwargs)):
        res = simulate({symbol: prices}, {symbol: sig}, f, costs)
        out[i] = sharpe_ratio(res.daily_returns)
    return out


def empirical_pvalue(observed: float, null: np.ndarray) -> float:
    """p = (1 + #(null >= observado)) / (1 + n). Los NaN de la nula cuentan como no-extremos."""
    null = np.asarray(null, dtype=np.float64)
    return float((1 + np.sum(null >= observed)) / (1 + null.size))
