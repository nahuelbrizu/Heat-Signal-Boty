"""
Simulador vectorizado del laboratorio de backtest (etapa 2, ver plan-laboratorio.md).

Interfaz acordada
-----------------
- Precios: ``DataFrame`` de velas 1m por simbolo, indice ``DatetimeIndex`` UTC = HORA DE APERTURA de la
  vela, columnas ``open, high, low, close``. Grilla de 1 minuto, puede tener huecos.
- Funding: ``Series`` por simbolo, indice UTC = momento de liquidacion (fundingTime), valor = tasa del
  intervalo. Con tasa positiva el LONG paga y el SHORT cobra: ``pnl_funding = -nocional * tasa``.
- Senal: ``Series`` de posicion objetivo por simbolo, indice UTC = momento de DECISION (la informacion
  ya estaba disponible en ese instante), valor = fraccion del capital asignado al simbolo (-1..+1,
  puede ser fraccional). Se mantiene hasta el proximo valor (forward-fill). Antes del primer valor la
  posicion es 0.

Convenciones elegidas (documentadas porque cambian los numeros)
---------------------------------------------------------------
1. **Ejecucion**: una decision en ``t`` se ejecuta al OPEN de la primera vela 1m con apertura >= t. Si la
   decision cae justo en el cierre de la vela ``j`` (= apertura de ``j+1``) se ejecuta en ``j+1``; si cae
   en un hueco, en la siguiente vela disponible. Nunca al close de la vela de decision. Si varias
   decisiones caen antes de la misma vela de ejecucion, vale la ULTIMA. Decisiones posteriores a la
   ultima vela se descartan.
2. **Tamano (nocional fijo, sin reinversion)**: capital asignado al simbolo = ``capital / n_simbolos``
   (``allocation="equal"``, default) o ``capital`` completo si la senal ya trae pesos de cartera
   (``allocation="total"``: el valor de la senal es fraccion del capital TOTAL). Cuando el objetivo
   cambia, se compran/venden unidades = ``w * capital_asignado / open_ejecucion``; esas unidades se
   mantienen fijas hasta el proximo cambio (el nocional deriva con el precio, como un contrato real).
   Si la senal repite el mismo valor NO se opera (salvo ``rebalance=True``, que re-ajusta al nocional
   objetivo en cada fila de la senal; lo usa la canasta rebalanceada).
3. **Costos**: en cada cambio de posicion, ``|delta unidades| * open_ejecucion * (fee + slippage)``. Una
   reversion long->short paga sobre 2x el nocional. El slippage puede multiplicarse por barra con
   ``Costs.slippage_mult_fn(simbolo, ts_ejecucion)``.
4. **Mark-to-market** en la grilla 1m con el close. El PnL de precio de la vela ``j`` es
   ``u[j-1] * (open[j] - close[j-1]) + u[j] * (close[j] - open[j])`` (el primer termino cubre gaps y
   huecos con la posicion anterior). El PnL de precio se calcula con precios crudos; el slippage va
   aparte, asi la descomposicion precio + funding - comisiones - slippage suma exacto al total.
5. **Funding**: en cada liquidacion ``f`` se usa la posicion vigente JUSTO ANTES de ``f`` (la de la ultima
   vela con apertura < f; una orden ejecutada al open de la vela que abre exactamente en ``f`` llega
   tarde a esa liquidacion) y como precio el close de esa vela. Se cuentan liquidaciones en
   ``(primera_apertura, ultima_apertura + 1m]``. Si la liquidacion cae en un hueco se usa el ultimo
   close disponible.
6. **Retornos**: PnL / capital inicial (no compuesto). Los diarios agrupan por dia UTC de la hora de
   apertura de cada vela (la vela 23:59 pertenece al dia D). Dias sin velas dan 0.
7. **Trades**: un trade es un tramo contiguo con el mismo signo de posicion distinto de 0. Ajustes de
   tamano dentro del tramo son parte del mismo trade. En una reversion los costos se reparten: la parte
   que cierra va al trade viejo y la que abre al nuevo. Un trade abierto al final se valua al ultimo
   close sin costo de salida y se marca ``is_open=True``.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Literal

import numpy as np
import pandas as pd

ONE_MINUTE = pd.Timedelta(minutes=1)
PRICE_COLUMNS = ("open", "high", "low", "close")

TRADE_COLUMNS = [
    "symbol",
    "direction",
    "entry_time",
    "exit_time",
    "entry_price",
    "exit_price",
    "notional_entry",
    "pnl_price",
    "fees",
    "slippage",
    "funding",
    "pnl_net",
    "ret_net",
    "mfe",
    "mae",
    "duration",
    "bars",
    "is_open",
]

FILL_COLUMNS = ["symbol", "time", "units_from", "units_to", "price", "notional", "fee", "slippage"]


@dataclass(frozen=True)
class Costs:
    """Costos de ejecucion.

    - ``fee_bps``: comision por lado en bps (5 = taker Binance).
    - ``slippage_bps``: slippage por lado en bps por simbolo; simbolos ausentes usan ``default_slippage_bps``.
    - ``slippage_mult_fn``: opcional, ``(simbolo, ts_ejecucion) -> multiplicador`` (ej. 2-3 en barras con
      liquidaciones extremas). Solo se evalua en las velas donde hay un fill.
    """

    fee_bps: float = 5.0
    slippage_bps: Mapping[str, float] = field(default_factory=dict)
    default_slippage_bps: float = 2.0
    slippage_mult_fn: Callable[[str, pd.Timestamp], float] | None = None

    def slippage_for(self, symbol: str) -> float:
        return float(self.slippage_bps.get(symbol, self.default_slippage_bps))


@dataclass
class SimResult:
    """Resultado de ``simulate``.

    - ``pnl_1m``: DataFrame en la grilla union de velas (indice = apertura de la vela) con columnas
      ``price, funding, fees, slippage, total, turnover`` en moneda de cotizacion. ``fees`` y ``slippage``
      son costos (positivos); ``total = price + funding - fees - slippage``; ``turnover`` = nocional operado.
    - ``equity``: ``capital + cumsum(total)``, indexada por CIERRE de la vela (apertura + 1m), que es cuando
      ese valor es conocido.
    - ``daily_returns``: PnL neto diario / capital. ``daily_gross_returns``: (precio + funding) / capital.
      ``daily_turnover``: nocional operado diario / capital (sirve para la curva costo -> Sharpe).
    - ``trades`` / ``fills``: DataFrames (ver ``TRADE_COLUMNS`` / ``FILL_COLUMNS``).
    - ``breakdown``: totales ``precio, funding, comisiones, slippage, total`` (comisiones y slippage como
      costos positivos; total = precio + funding - comisiones - slippage).
    - ``breakdown_by_symbol``: DataFrame con la misma descomposicion por simbolo.
    """

    capital: float
    pnl_1m: pd.DataFrame
    equity: pd.Series
    daily_returns: pd.Series
    daily_gross_returns: pd.Series
    daily_turnover: pd.Series
    trades: pd.DataFrame
    fills: pd.DataFrame
    breakdown: dict[str, float]
    breakdown_by_symbol: pd.DataFrame

    @property
    def total_pnl(self) -> float:
        return self.breakdown["total"]


def _as_utc_index(index: pd.Index, what: str) -> pd.DatetimeIndex:
    if not isinstance(index, pd.DatetimeIndex):
        raise TypeError(f"{what}: el indice tiene que ser DatetimeIndex, es {type(index).__name__}")
    if index.tz is None:
        raise ValueError(f"{what}: el indice tiene que ser tz-aware UTC (es naive)")
    # todo en ns: asi8 se compara entre indices que pueden venir con resoluciones distintas
    return index.tz_convert("UTC").as_unit("ns")


def _ffill_nan(arr: np.ndarray, fill_start: float = 0.0) -> np.ndarray:
    """Forward-fill de NaN en un array 1d; los NaN iniciales toman ``fill_start``."""
    valid = ~np.isnan(arr)
    pos = np.where(valid, np.arange(arr.size), -1)
    np.maximum.accumulate(pos, out=pos)
    out = np.where(pos >= 0, arr[np.maximum(pos, 0)], fill_start)
    return out


@dataclass
class _SymbolSim:
    open_index: pd.DatetimeIndex
    price: np.ndarray
    funding: np.ndarray
    fees: np.ndarray
    slippage: np.ndarray
    turnover: np.ndarray
    trades: pd.DataFrame
    fills: pd.DataFrame


def _simulate_symbol(
    symbol: str,
    df: pd.DataFrame,
    signal: pd.Series,
    funding: pd.Series | None,
    costs: Costs,
    alloc: float,
    rebalance: bool,
) -> _SymbolSim:
    idx = _as_utc_index(df.index, f"precios[{symbol}]")
    if not idx.is_monotonic_increasing or idx.has_duplicates:
        raise ValueError(f"precios[{symbol}]: el indice tiene que ser creciente y sin duplicados")
    missing = [c for c in PRICE_COLUMNS if c not in df.columns]
    if missing:
        raise ValueError(f"precios[{symbol}]: faltan columnas {missing}")
    o = df["open"].to_numpy(dtype=np.float64)
    h = df["high"].to_numpy(dtype=np.float64)
    lo = df["low"].to_numpy(dtype=np.float64)
    c = df["close"].to_numpy(dtype=np.float64)
    n = o.size
    if n == 0:
        raise ValueError(f"precios[{symbol}]: sin velas")
    if np.isnan(o).any() or np.isnan(c).any():
        raise ValueError(f"precios[{symbol}]: hay open/close NaN (los huecos se representan sin fila)")

    # --- 1. mapear decisiones a velas de ejecucion (open de la primera vela con apertura >= t)
    sig = signal.dropna()
    sig_idx = _as_utc_index(sig.index, f"senal[{symbol}]")
    order = np.argsort(sig_idx.asi8, kind="stable")
    sig_ts = sig_idx.asi8[order]
    w = sig.to_numpy(dtype=np.float64)[order]
    j = np.searchsorted(idx.asi8, sig_ts, side="left")
    keep = j < n
    j, w = j[keep], w[keep]
    if j.size:
        last = np.r_[j[1:] != j[:-1], True]  # varias decisiones antes de la misma vela: vale la ultima
        j, w = j[last], w[last]

    # --- 2. unidades objetivo por fila de ejecucion
    if j.size:
        if rebalance:
            units_rows = w * alloc / o[j]
        else:
            prev_w = np.r_[0.0, w[:-1]]
            changed = w != prev_w
            units_rows = _ffill_nan(np.where(changed, w * alloc / o[j], np.nan), 0.0)
        units_rows = np.where(w == 0.0, 0.0, units_rows)  # flat exacto
    else:
        units_rows = np.empty(0)

    u = np.full(n, np.nan)
    u[j] = units_rows
    u = _ffill_nan(u, 0.0)
    u_prev = np.empty(n)
    u_prev[0] = 0.0
    u_prev[1:] = u[:-1]

    # --- 3. fills y costos
    du = u - u_prev
    fill_bars = np.flatnonzero(du != 0.0)
    fill_notional = np.abs(du[fill_bars]) * o[fill_bars]
    fee_rate = costs.fee_bps / 1e4
    slip_base = costs.slippage_for(symbol) / 1e4
    if costs.slippage_mult_fn is not None and fill_bars.size:
        mult = np.array(
            [float(costs.slippage_mult_fn(symbol, idx[b])) for b in fill_bars], dtype=np.float64
        )
    else:
        mult = np.ones(fill_bars.size)
    slip_rate = slip_base * mult
    fee_fill = fill_notional * fee_rate
    slip_fill = fill_notional * slip_rate

    fees = np.zeros(n)
    slippage = np.zeros(n)
    turnover = np.zeros(n)
    fees[fill_bars] = fee_fill
    slippage[fill_bars] = slip_fill
    turnover[fill_bars] = fill_notional

    # --- 4. PnL de precio (mark-to-market con el close)
    c_prev = np.empty(n)
    c_prev[0] = o[0]
    c_prev[1:] = c[:-1]
    gap_pnl = u_prev * (o - c_prev)
    intra_pnl = u * (c - o)
    price = gap_pnl + intra_pnl

    # --- 5. funding con la posicion vigente justo antes de cada liquidacion
    fund = np.zeros(n)
    fund_bar = np.empty(0, dtype=np.int64)
    fund_cash = np.empty(0)
    if funding is not None and len(funding):
        f = funding.dropna()
        f_idx = _as_utc_index(f.index, f"funding[{symbol}]")
        f_ts = f_idx.asi8
        rates = f.to_numpy(dtype=np.float64)
        in_range = (f_ts > idx.asi8[0]) & (f_ts <= idx.asi8[-1] + ONE_MINUTE.value)
        f_ts, rates = f_ts[in_range], rates[in_range]
        m = np.searchsorted(idx.asi8, f_ts, side="left") - 1
        cash = -u[m] * c[m] * rates
        np.add.at(fund, m, cash)
        fund_bar, fund_cash = m, cash

    # --- 6. trades (tramos con el mismo signo de posicion)
    trades = _build_trades(
        symbol, idx, o, h, lo, c, u, u_prev, fill_bars, fee_fill, slip_fill, slip_rate,
        gap_pnl, intra_pnl, fund_bar, fund_cash, alloc,
    )

    fills = pd.DataFrame(
        {
            "symbol": symbol,
            "time": idx[fill_bars],
            "units_from": u_prev[fill_bars],
            "units_to": u[fill_bars],
            "price": o[fill_bars],
            "notional": fill_notional,
            "fee": fee_fill,
            "slippage": slip_fill,
        },
        columns=FILL_COLUMNS,
    )
    return _SymbolSim(idx, price, fund, fees, slippage, turnover, trades, fills)


def _build_trades(
    symbol: str,
    idx: pd.DatetimeIndex,
    o: np.ndarray,
    h: np.ndarray,
    lo: np.ndarray,
    c: np.ndarray,
    u: np.ndarray,
    u_prev: np.ndarray,
    fill_bars: np.ndarray,
    fee_fill: np.ndarray,
    slip_fill: np.ndarray,
    slip_rate: np.ndarray,
    gap_pnl: np.ndarray,
    intra_pnl: np.ndarray,
    fund_bar: np.ndarray,
    fund_cash: np.ndarray,
    alloc: float,
) -> pd.DataFrame:
    n = u.size
    s = np.sign(u).astype(np.int8)
    s_prev = np.sign(u_prev).astype(np.int8)
    starts = np.flatnonzero((s != 0) & (s != s_prev))
    if starts.size == 0:
        return pd.DataFrame(columns=TRADE_COLUMNS)
    exits = np.flatnonzero((s_prev != 0) & (s != s_prev))
    if exits.size and exits[0] <= starts[0]:
        # no deberia pasar: la posicion arranca en 0
        raise AssertionError("inconsistencia interna en la segmentacion de trades")
    k = starts.size
    is_open = np.zeros(k, dtype=bool)
    if exits.size == k - 1:
        exits = np.r_[exits, n]
        is_open[-1] = True
    elif exits.size != k:
        raise AssertionError("inconsistencia interna en la segmentacion de trades")

    seg = np.cumsum((s != 0) & (s != s_prev)) - 1
    seg = np.where(s != 0, seg, -1)
    seg_prev = np.empty(n, dtype=seg.dtype)
    seg_prev[0] = -1
    seg_prev[1:] = seg[:-1]

    def _accumulate(seg_ids: np.ndarray, values: np.ndarray) -> np.ndarray:
        ok = seg_ids >= 0
        return np.bincount(seg_ids[ok], weights=values[ok], minlength=k)[:k]

    pnl_price = _accumulate(seg, intra_pnl) + _accumulate(seg_prev, gap_pnl)
    funding = _accumulate(seg[fund_bar], fund_cash) if fund_bar.size else np.zeros(k)

    # costos: en un fill que mantiene el signo todo va al tramo actual; si cambia el signo
    # la parte que cierra (|u_prev|) va al tramo viejo y la que abre (|u|) al nuevo.
    fb = fill_bars
    up, uc = u_prev[fb], u[fb]
    same = (np.sign(up) == np.sign(uc)) & (uc != 0)
    tot = np.abs(uc - up)
    with np.errstate(invalid="ignore", divide="ignore"):
        close_frac = np.where(same, 0.0, np.abs(up) / tot)
    open_frac = 1.0 - close_frac
    seg_new = seg[fb]
    seg_old = seg_prev[fb]
    fees = _accumulate(seg_new, fee_fill * open_frac) + _accumulate(seg_old, fee_fill * close_frac)
    slip = _accumulate(seg_new, slip_fill * open_frac) + _accumulate(seg_old, slip_fill * close_frac)

    # precios efectivos (con slippage) de entrada y salida
    slip_at_bar = np.zeros(n + 1)
    slip_at_bar[fb] = slip_rate
    direction = s[starts].astype(np.float64)
    entry_price = o[starts] * (1.0 + direction * slip_at_bar[starts])
    exit_idx = np.minimum(exits, n - 1)
    exit_price = np.where(
        is_open, c[-1], o[exit_idx] * (1.0 - direction * slip_at_bar[np.minimum(exits, n)])
    )

    # MFE / MAE sobre las velas [entrada, salida)
    h_ext = np.r_[h, np.nan]
    l_ext = np.r_[lo, np.nan]
    pairs = np.empty(2 * k, dtype=np.int64)
    pairs[0::2] = starts
    pairs[1::2] = exits
    max_h = np.maximum.reduceat(h_ext, pairs)[0::2]
    min_l = np.minimum.reduceat(l_ext, pairs)[0::2]
    long_ = direction > 0
    mfe = np.where(long_, max_h / entry_price - 1.0, 1.0 - min_l / entry_price)
    mae = np.where(long_, min_l / entry_price - 1.0, 1.0 - max_h / entry_price)

    entry_time = idx[starts]
    exit_time = pd.to_datetime(
        np.where(is_open, idx.asi8[-1] + ONE_MINUTE.value, idx.asi8[exit_idx]), unit="ns", utc=True
    )
    pnl_net = pnl_price + funding - fees - slip
    return pd.DataFrame(
        {
            "symbol": symbol,
            "direction": np.where(long_, "long", "short"),
            "entry_time": entry_time,
            "exit_time": exit_time,
            "entry_price": entry_price,
            "exit_price": exit_price,
            "notional_entry": np.abs(u[starts]) * o[starts],
            "pnl_price": pnl_price,
            "fees": fees,
            "slippage": slip,
            "funding": funding,
            "pnl_net": pnl_net,
            "ret_net": pnl_net / alloc,
            "mfe": mfe,
            "mae": mae,
            "duration": exit_time - entry_time,
            "bars": exits - starts,
            "is_open": is_open,
        },
        columns=TRADE_COLUMNS,
    )


def simulate(
    prices: Mapping[str, pd.DataFrame],
    signals: Mapping[str, pd.Series],
    funding: Mapping[str, pd.Series] | None,
    costs: Costs | None = None,
    capital: float = 1.0,
    *,
    allocation: Literal["equal", "total"] = "equal",
    rebalance: bool = False,
) -> SimResult:
    """Simula las senales contra velas 1m. Ver el docstring del modulo para las convenciones.

    Solo se simulan los simbolos presentes en ``signals`` (todos tienen que estar en ``prices``). Con
    ``allocation="equal"`` el capital se reparte en partes iguales entre esos simbolos.
    """
    if costs is None:
        costs = Costs()
    if capital <= 0:
        raise ValueError("capital tiene que ser > 0")
    symbols = list(signals.keys())
    if not symbols:
        raise ValueError("signals vacio")
    missing = [s for s in symbols if s not in prices]
    if missing:
        raise KeyError(f"senal sin precios para {missing}")
    if allocation == "equal":
        alloc = capital / len(symbols)
    elif allocation == "total":
        alloc = capital
    else:
        raise ValueError(f"allocation invalida: {allocation!r}")

    sims: dict[str, _SymbolSim] = {}
    for sym in symbols:
        f = funding.get(sym) if funding is not None else None
        sims[sym] = _simulate_symbol(sym, prices[sym], signals[sym], f, costs, alloc, rebalance)

    # grilla union y agregacion por bincount (sin concatenar 3.7M filas)
    union = sims[symbols[0]].open_index
    for sym in symbols[1:]:
        union = union.union(sims[sym].open_index)
    m = len(union)
    cols = ("price", "funding", "fees", "slippage", "turnover")
    agg = {col: np.zeros(m) for col in cols}
    by_symbol = {}
    for sym, sim in sims.items():
        pos = union.get_indexer(sim.open_index)
        for col in cols:
            arr = getattr(sim, col)
            agg[col] += np.bincount(pos, weights=arr, minlength=m)
        tot = {
            "precio": float(sim.price.sum()),
            "funding": float(sim.funding.sum()),
            "comisiones": float(sim.fees.sum()),
            "slippage": float(sim.slippage.sum()),
        }
        tot["total"] = tot["precio"] + tot["funding"] - tot["comisiones"] - tot["slippage"]
        by_symbol[sym] = tot

    total = agg["price"] + agg["funding"] - agg["fees"] - agg["slippage"]
    pnl_1m = pd.DataFrame(
        {
            "price": agg["price"],
            "funding": agg["funding"],
            "fees": agg["fees"],
            "slippage": agg["slippage"],
            "total": total,
            "turnover": agg["turnover"],
        },
        index=union,
    )
    equity = pd.Series(capital + np.cumsum(total), index=union + ONE_MINUTE, name="equity")

    daily = pnl_1m.resample("1D").sum()
    daily_returns = (daily["total"] / capital).rename("ret")
    daily_gross = ((daily["price"] + daily["funding"]) / capital).rename("ret_gross")
    daily_turnover = (daily["turnover"] / capital).rename("turnover")

    trades_list = [sims[s].trades for s in symbols if len(sims[s].trades)]
    trades = (
        pd.concat(trades_list, ignore_index=True)
        .sort_values(["entry_time", "symbol"], kind="stable")
        .reset_index(drop=True)
        if trades_list
        else pd.DataFrame(columns=TRADE_COLUMNS)
    )
    fills_list = [sims[s].fills for s in symbols if len(sims[s].fills)]
    fills = (
        pd.concat(fills_list, ignore_index=True)
        .sort_values(["time", "symbol"], kind="stable")
        .reset_index(drop=True)
        if fills_list
        else pd.DataFrame(columns=FILL_COLUMNS)
    )

    breakdown = {
        "precio": float(agg["price"].sum()),
        "funding": float(agg["funding"].sum()),
        "comisiones": float(agg["fees"].sum()),
        "slippage": float(agg["slippage"].sum()),
    }
    breakdown["total"] = (
        breakdown["precio"] + breakdown["funding"] - breakdown["comisiones"] - breakdown["slippage"]
    )
    return SimResult(
        capital=capital,
        pnl_1m=pnl_1m,
        equity=equity,
        daily_returns=daily_returns,
        daily_gross_returns=daily_gross,
        daily_turnover=daily_turnover,
        trades=trades,
        fills=fills,
        breakdown=breakdown,
        breakdown_by_symbol=pd.DataFrame.from_dict(by_symbol, orient="index"),
    )
