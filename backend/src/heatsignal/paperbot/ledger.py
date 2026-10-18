"""
Contabilidad del paper trading, con las convenciones de `lab/sim.py` (sin pandas).

- Tamano: nocional fijo sin reinversion. Capital por estrategia 10.000 USDT repartido en partes iguales entre los
  simbolos de su universo; un objetivo w se ejecuta como `unidades = w * capital_por_simbolo / precio_fill`, y esas
  unidades quedan fijas hasta el proximo cambio (el nocional deriva con el precio).
- Costos por lado: comision 5 bps + slippage 2 bps sobre |delta unidades| * precio (= EXPECTED_COST_BPS del lab).
  El PnL de precio se calcula con precios crudos y el slippage va aparte, asi
  equity = capital + realizado + no_realizado + funding - comisiones - slippage  (suma exacta).
- Funding: `cash = -unidades_vigentes_justo_antes * precio * tasa` (tasa positiva: el long paga).
- Trade = tramo contiguo con el mismo signo; los ajustes de tamano son parte del trade; en una reversion la parte
  de costos que cierra va al trade viejo y la que abre al nuevo.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

CAPITAL = 10_000.0
FEE_BPS = 5.0
SLIPPAGE_BPS = 2.0
EPS = 1e-12


def sign(x: float) -> int:
    return 1 if x > EPS else -1 if x < -EPS else 0


def costs(units_from: float, units_to: float, price: float) -> tuple[float, float, float]:
    """(nocional operado, comision, slippage)."""
    notional = abs(units_to - units_from) * price
    return notional, notional * FEE_BPS / 1e4, notional * SLIPPAGE_BPS / 1e4


@dataclass
class Trade:
    direction: int
    entry_ms: int
    entry_price: float
    max_units: float = 0.0
    pnl_price: float = 0.0
    funding: float = 0.0
    fees: float = 0.0
    slippage: float = 0.0

    def as_row(self, symbol: str, exit_ms: int, exit_price: float) -> dict[str, Any]:
        return {"symbol": symbol, "direction": "long" if self.direction > 0 else "short", "entry_ms": self.entry_ms,
                "exit_ms": exit_ms, "entry_price": self.entry_price, "exit_price": exit_price,
                "max_units": self.max_units, "pnl_price": self.pnl_price, "funding": self.funding, "fees": self.fees,
                "slippage": self.slippage, "pnl_net": self.pnl_price + self.funding - self.fees - self.slippage}


@dataclass
class Position:
    symbol: str
    units: float = 0.0
    weight: float = 0.0
    avg: float | None = None       # precio medio de entrada de las unidades abiertas
    realized: float = 0.0          # PnL de precio realizado
    funding: float = 0.0
    fees: float = 0.0
    slippage: float = 0.0
    trade: Trade | None = None
    closed: list[dict[str, Any]] = field(default_factory=list)

    def fill(self, t_ms: int, price: float, units_to: float, weight_to: float, fee: float, slip: float) -> None:
        u0, u1 = self.units, units_to
        self.fees += fee
        self.slippage += slip
        s0, s1 = sign(u0), sign(u1)
        if s0 != 0 and s0 == s1:  # mismo signo: ajuste de tamano dentro del trade
            if abs(u1) < abs(u0):
                r = (u0 - u1) * (price - self.avg)
                self.realized += r
                self.trade.pnl_price += r
            else:
                self.avg = (abs(u0) * self.avg + abs(u1 - u0) * price) / abs(u1)
            self.trade.fees += fee
            self.trade.slippage += slip
            self.trade.max_units = max(self.trade.max_units, abs(u1))
        else:
            tot = abs(u1 - u0)
            close_frac = abs(u0) / tot if (s0 != 0 and tot > 0) else 0.0
            if s0 != 0:  # cierra todo el tramo actual
                r = u0 * (price - self.avg)
                self.realized += r
                self.trade.pnl_price += r
                self.trade.fees += fee * close_frac
                self.trade.slippage += slip * close_frac
                self.closed.append(self.trade.as_row(self.symbol, t_ms, price))
                self.trade = None
                self.avg = None
            if s1 != 0:
                self.trade = Trade(s1, t_ms, price, abs(u1), 0.0, 0.0, fee * (1 - close_frac), slip * (1 - close_frac))
                self.avg = price
        self.units = 0.0 if s1 == 0 else u1
        self.weight = 0.0 if s1 == 0 else weight_to

    def add_funding(self, cash: float) -> None:
        self.funding += cash
        if self.trade is not None:
            self.trade.funding += cash

    def unrealized(self, mark: float) -> float:
        return 0.0 if self.avg is None else self.units * (mark - self.avg)


class Book:
    """Libro de una estrategia: posiciones por simbolo reconstruidas desde el log (ordenes + funding)."""

    def __init__(self, symbols: list[str], capital: float = CAPITAL) -> None:
        self.capital = capital
        self.alloc = capital / len(symbols)
        self.pos = {s: Position(s) for s in symbols}

    @classmethod
    def replay(cls, symbols: list[str], orders: list[Any], funding: list[Any], capital: float = CAPITAL) -> Book:
        """Ordenes y funding guardados, en orden temporal. Una liquidacion en el mismo instante que un fill se aplica
        ANTES (la orden ejecutada al open de la vela que abre justo en la liquidacion llega tarde, como en lab/sim.py)."""
        book = cls(symbols, capital)
        events = [(int(f["funding_ms"]), 0, i, f) for i, f in enumerate(funding)] + \
                 [(int(o["fill_ms"]), 1, i, o) for i, o in enumerate(orders)]
        for _, kind, _, ev in sorted(events, key=lambda e: e[:3]):
            p = book.pos.setdefault(ev["symbol"], Position(ev["symbol"]))
            if kind == 0:
                p.add_funding(float(ev["cash"]))
            else:
                p.fill(int(ev["fill_ms"]), float(ev["price"]), float(ev["units_to"]), float(ev["weight_to"]),
                       float(ev["fee"]), float(ev["slippage"]))
        return book

    def order(self, symbol: str, decision_ms: int, fill_ms: int, price: float, weight_to: float, reason: str,
              price_source: str) -> dict[str, Any]:
        """Ejecuta un cambio de objetivo y devuelve la fila de `orders` (sin run_id/strategy_id)."""
        p = self.pos[symbol]
        units_to = 0.0 if abs(weight_to) < EPS else weight_to * self.alloc / price
        notional, fee, slip = costs(p.units, units_to, price)
        row = {"symbol": symbol, "decision_ms": decision_ms, "fill_ms": fill_ms, "price": price,
               "price_source": price_source, "weight_from": p.weight, "weight_to": weight_to, "units_from": p.units,
               "units_to": units_to, "notional": notional, "fee": fee, "slippage": slip, "reason": reason}
        p.fill(fill_ms, price, units_to, weight_to, fee, slip)
        return row

    def units_before(self, orders: list[dict[str, Any]], symbol: str, t_ms: int) -> float:
        """Unidades vigentes justo antes de t (ultima orden con fill_ms < t)."""
        u = 0.0
        for o in sorted((o for o in orders if o["symbol"] == symbol), key=lambda o: o["fill_ms"]):
            if o["fill_ms"] < t_ms:
                u = float(o["units_to"])
        return u

    def totals(self, marks: dict[str, float]) -> dict[str, float]:
        realized = sum(p.realized for p in self.pos.values())
        unreal = sum(p.unrealized(marks[s]) for s, p in self.pos.items() if s in marks)
        funding = sum(p.funding for p in self.pos.values())
        fees = sum(p.fees for p in self.pos.values())
        slip = sum(p.slippage for p in self.pos.values())
        gross = sum(abs(p.units) * marks[s] for s, p in self.pos.items() if s in marks)
        return {"equity": self.capital + realized + unreal + funding - fees - slip, "realized": realized,
                "unrealized": unreal, "funding": funding, "fees": fees, "slippage": slip,
                "exposure": gross / self.capital}
