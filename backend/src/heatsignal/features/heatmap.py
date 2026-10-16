"""
Modelo de heatmap propio de liquidaciones — `liqcluster-v1`.

Entrada: LiquidationEvent crudos normalizados (multi-exchange), mark price y ATR.
Salida: HeatmapSnapshot con los clusters de precio donde se concentraron
liquidaciones en la ventana. Puro y deterministico: mismos inputs -> mismo output.

Algoritmo (auditable, cualquier cambio de logica o de defaults = nuevo model id)
--------------------------------------------------------------------------------
1. Seleccion: eventos del simbolo con ts en (now - window, now]. Se descartan
   eventos con notional_usd <= 0 o price <= 0 (se informan en `dropped_events`).
2. Ancho de bin `w`, normalizado por volatilidad:
     - si hay ATR (> 0):         w = bin_atr_mult * ATR              (basis "atr")
     - si no, y hay mark (> 0):  w = fallback_bin_pct * mark         (basis "pct_mark")
     - si no hay ni mark:        w = fallback_bin_pct * VWAP de las liquidaciones
                                                                    (basis "pct_liq_vwap")
   Cualquier fallback se declara en `data_gaps` del resultado: el caller lo copia
   al FeatureSet. Un umbral plano NUNCA se usa en silencio.
3. Binning anclado a 0: bin(price) = floor(price / w). Cada bin acumula notional
   total, por lado, por exchange, notional PARTIAL y cantidad de eventos.
4. Clustering "pico + vecinos": se recorren los bins no vacios por notional
   descendente (desempate: bin de menor precio primero). Cada bin no asignado
   siembra un cluster que crece hacia ambos lados por bins CONTIGUOS no vacios
   y no asignados, hasta `max_radius_bins` bins del semillero. Los clusters no se
   solapan y su ancho maximo es (2 * max_radius_bins + 1) * w, lo que evita que
   una zona densa colapse en un unico cluster gigante.
5. Por cluster:
     - price_low  = min_bin * w ; price_high = (max_bin + 1) * w (bordes de bin)
     - price_mid  = (price_low + price_high) / 2
     - notional_usd, n_events = sumas
     - dominant_side = lado con share de notional >= dominance_threshold, si no None
       (lado de la ORDEN de liquidacion: SELL = longs liquidados)
     - sources = exchanges presentes (orden binance, bybit, okx)
     - partial_coverage_share = notional de eventos PARTIAL / notional total
     - distance_atr = (price_mid - mark) / ATR, None si falta cualquiera
6. Ranking: top `top_n` por notional descendente (desempate: price_mid ascendente).

Limitacion conocida: Binance forceOrder y OKX son PARTIAL (<=1/seg/simbolo) — un
cluster dominado por esas fuentes subrepresenta el notional real. Por eso cada
cluster expone `partial_coverage_share`; el analista y el evaluador deben usarlo.
"""

from __future__ import annotations

import math
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Literal

from ..schema.events import Coverage, Exchange, LiquidationEvent, Side
from ..schema.features import HeatmapSnapshot, LiquidationCluster
from ._window import select, sorted_exchanges

HEATMAP_MODEL_ID = "liqcluster-v1"

DEFAULT_BIN_ATR_MULT = 0.25      # w = 0.25 * ATR
DEFAULT_FALLBACK_BIN_PCT = 0.001  # w = 0.1% del precio de referencia si no hay ATR
DEFAULT_MAX_RADIUS_BINS = 2       # cluster <= 5 bins (= 1.25 ATR con defaults)
DEFAULT_DOMINANCE_THRESHOLD = 0.6
DEFAULT_TOP_N = 10

BinBasis = Literal["atr", "pct_mark", "pct_liq_vwap"]


@dataclass
class _BinAgg:
    notional: float = 0.0
    n: int = 0
    partial_notional: float = 0.0
    by_side: dict[Side, float] = field(default_factory=lambda: {Side.BUY: 0.0, Side.SELL: 0.0})
    sources: set[Exchange] = field(default_factory=set)


@dataclass(frozen=True)
class HeatmapResult:
    snapshot: HeatmapSnapshot
    bin_width: float | None
    bin_basis: BinBasis | None
    n_events_used: int
    dropped_events: int
    partial_sources: list[Exchange]   # exchanges con al menos un evento PARTIAL en la ventana
    data_gaps: list[str]


def resolve_bin_width(
    atr: float | None,
    mark_price: float | None,
    liq_vwap: float | None,
    bin_atr_mult: float = DEFAULT_BIN_ATR_MULT,
    fallback_bin_pct: float = DEFAULT_FALLBACK_BIN_PCT,
) -> tuple[float | None, BinBasis | None]:
    """Cadena de resolucion explicita del ancho de bin (ver docstring del modulo)."""
    if atr is not None and atr > 0:
        return bin_atr_mult * atr, "atr"
    if mark_price is not None and mark_price > 0:
        return fallback_bin_pct * mark_price, "pct_mark"
    if liq_vwap is not None and liq_vwap > 0:
        return fallback_bin_pct * liq_vwap, "pct_liq_vwap"
    return None, None


def _dominant_side(by_side: dict[Side, float], total: float, threshold: float) -> Side | None:
    if total <= 0:
        return None
    for side in (Side.BUY, Side.SELL):
        if by_side[side] / total >= threshold:
            return side
    return None


def build_heatmap(
    symbol: str,
    liquidations: Iterable[LiquidationEvent],
    now_ms: int,
    window_s: int,
    mark_price: float | None,
    atr: float | None,
    *,
    bin_atr_mult: float = DEFAULT_BIN_ATR_MULT,
    fallback_bin_pct: float = DEFAULT_FALLBACK_BIN_PCT,
    max_radius_bins: int = DEFAULT_MAX_RADIUS_BINS,
    dominance_threshold: float = DEFAULT_DOMINANCE_THRESHOLD,
    top_n: int = DEFAULT_TOP_N,
) -> HeatmapResult:
    if not 0.5 < dominance_threshold <= 1.0:
        raise ValueError("dominance_threshold debe estar en (0.5, 1]")
    if max_radius_bins < 0 or top_n <= 0:
        raise ValueError("max_radius_bins >= 0 y top_n > 0")

    selected = select(liquidations, symbol, now_ms, window_s)
    events = [e for e in selected if e.notional_usd > 0 and e.price > 0]
    dropped = len(selected) - len(events)
    gaps: list[str] = []
    if dropped:
        gaps.append(f"heatmap: {dropped} liquidaciones descartadas por notional/precio <= 0")

    partial_sources = sorted_exchanges(e.source for e in events if e.coverage == Coverage.PARTIAL)

    total_notional = sum(e.notional_usd for e in events)
    liq_vwap = (sum(e.price * e.notional_usd for e in events) / total_notional) if total_notional > 0 else None

    w, basis = resolve_bin_width(atr, mark_price, liq_vwap, bin_atr_mult, fallback_bin_pct)
    if events and basis == "pct_mark":
        gaps.append(f"heatmap: sin ATR, bin por fallback {fallback_bin_pct:.4%} del mark (no normalizado por volatilidad)")
    elif events and basis == "pct_liq_vwap":
        gaps.append(
            f"heatmap: sin ATR ni mark, bin por fallback {fallback_bin_pct:.4%} del VWAP de liquidaciones"
        )

    def _snapshot(clusters: list[LiquidationCluster]) -> HeatmapSnapshot:
        return HeatmapSnapshot(
            heatmap_model_id=HEATMAP_MODEL_ID,
            symbol=symbol,
            ts_ms=now_ms,
            window_s=window_s,
            mark_price=mark_price,
            atr=atr,
            clusters=clusters,
        )

    if not events or w is None:
        return HeatmapResult(_snapshot([]), w, basis, 0, dropped, partial_sources, gaps)

    # 3. Binning
    bins: dict[int, _BinAgg] = {}
    for e in events:
        idx = math.floor(e.price / w)
        agg = bins.setdefault(idx, _BinAgg())
        agg.notional += e.notional_usd
        agg.n += 1
        agg.by_side[e.side] += e.notional_usd
        agg.sources.add(e.source)
        if e.coverage == Coverage.PARTIAL:
            agg.partial_notional += e.notional_usd

    # 4. Clustering pico + vecinos
    assigned: set[int] = set()
    groups: list[list[int]] = []
    for seed in sorted(bins, key=lambda i: (-bins[i].notional, i)):
        if seed in assigned:
            continue
        members = [seed]
        for step in (-1, 1):
            j = seed + step
            while abs(j - seed) <= max_radius_bins and j in bins and j not in assigned:
                members.append(j)
                j += step
        assigned.update(members)
        groups.append(sorted(members))

    # 5. Metricas por cluster
    clusters: list[LiquidationCluster] = []
    for members in groups:
        aggs = [bins[i] for i in members]
        notional = sum(a.notional for a in aggs)
        by_side = {s: sum(a.by_side[s] for a in aggs) for s in (Side.BUY, Side.SELL)}
        partial = sum(a.partial_notional for a in aggs)
        low = members[0] * w
        high = (members[-1] + 1) * w
        mid = (low + high) / 2
        distance = (mid - mark_price) / atr if (atr and atr > 0 and mark_price is not None) else None
        clusters.append(
            LiquidationCluster(
                price_low=low,
                price_high=high,
                price_mid=mid,
                notional_usd=notional,
                n_events=sum(a.n for a in aggs),
                dominant_side=_dominant_side(by_side, notional, dominance_threshold),
                sources=sorted_exchanges(s for a in aggs for s in a.sources),
                partial_coverage_share=min(1.0, max(0.0, partial / notional)),
                distance_atr=distance,
            )
        )

    # 6. Ranking
    clusters.sort(key=lambda c: (-c.notional_usd, c.price_mid))
    return HeatmapResult(_snapshot(clusters[:top_n]), w, basis, len(events), dropped, partial_sources, gaps)
