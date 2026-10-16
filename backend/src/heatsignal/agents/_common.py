"""
Utilidades compartidas por los nodos del grafo.

- Render de un `FeatureSet` a texto para los prompts (siempre incluye `data_gaps`
  de forma explicita: "ninguno declarado" tambien es informacion).
- Reconciliacion de la salida batcheada del LLM contra los simbolos de entrada:
  simbolos inventados se descartan (logueado) y simbolos omitidos se completan
  con una entrada explicita "sin analisis" — nunca se pierde un simbolo en silencio.
- Union de `data_gaps` declarados por la capa de features con los que el LLM
  reporto: los huecos se propagan por CODIGO, no dependen de que el LLM los repita.
"""

from __future__ import annotations

import logging
from typing import Callable, Iterable, Protocol, Sequence, TypeVar

from ..schema.features import FeatureSet

logger = logging.getLogger(__name__)

HISTORICAL_LANGUAGE_RULE = (
    "Usa SIEMPRE lenguaje historico y descriptivo ('en ventanas similares el precio "
    "reacciono en X% de los casos', 'el desequilibrio observado fue...'), NUNCA predictivo "
    "('va a subir', 'subira', 'seguro que'). No inventes simbolos que no esten en el input "
    "y no omitas ninguno de los que si estan. Si un simbolo declara huecos de datos, "
    "mencionalos explicitamente en `data_gaps` y baja la confianza en consecuencia: "
    "no los ignores."
)


def format_data_gaps(fs: FeatureSet) -> str:
    if not fs.data_gaps:
        return "huecos de datos: ninguno declarado"
    return "huecos de datos DECLARADOS: " + "; ".join(fs.data_gaps)


def _fmt(value: float | None, fmt: str = "{:,.2f}") -> str:
    return "n/d" if value is None else fmt.format(value)


def render_heatmap(fs: FeatureSet) -> str:
    hm = fs.heatmap
    lines = [
        f"{fs.symbol} [heatmap {hm.heatmap_model_id}, ventana {hm.window_s}s, "
        f"mark {_fmt(hm.mark_price)}, ATR {_fmt(hm.atr)}]"
    ]
    if not hm.clusters:
        lines.append("  sin clusters de liquidacion en la ventana.")
    for c in hm.clusters:
        side = c.dominant_side.value if c.dominant_side else "mixto"
        lines.append(
            f"  - cluster {c.price_low:,.2f}-{c.price_high:,.2f} (mid {c.price_mid:,.2f}): "
            f"notional ${c.notional_usd:,.0f}, {c.n_events} eventos, lado orden dominante {side}, "
            f"distancia {_fmt(c.distance_atr, '{:+.2f}')} ATR, "
            f"cobertura PARTIAL {c.partial_coverage_share:.0%}, "
            f"fuentes {','.join(s.value for s in c.sources) or 'n/d'}"
        )
    lines.append(f"  {format_data_gaps(fs)}")
    return "\n".join(lines)


def render_order_flow(fs: FeatureSet) -> str:
    of = fs.order_flow
    return (
        f"{fs.symbol} [ventana {of.window_s}s]: compra taker ${of.buy_volume_usd:,.0f}, "
        f"venta taker ${of.sell_volume_usd:,.0f}, CVD ${of.cvd_usd:+,.0f}, "
        f"imbalance {of.imbalance:+.3f}, {of.n_trades} trades, "
        f"block trades (excluidos) ${of.block_trade_volume_usd:,.0f}, "
        f"fuentes {','.join(s.value for s in of.sources) or 'n/d'}\n  {format_data_gaps(fs)}"
    )


def render_context(fs: FeatureSet) -> str:
    ctx = fs.context
    funding = ", ".join(f"{ex.value}={rate:+.5f}" for ex, rate in ctx.funding_rate_by_exchange.items())
    return (
        f"{fs.symbol}: regimen (calculado por codigo) {ctx.regime}, mark {_fmt(ctx.mark_price)}, "
        f"funding [{funding or 'n/d'}], OI ${_fmt(ctx.open_interest_usd, '{:,.0f}')}, "
        f"cambio OI {_fmt(ctx.oi_change_pct, '{:+.2f}')}%, ATR {_fmt(ctx.atr)} "
        f"({_fmt(ctx.atr_pct, '{:.2f}')}%)\n  {format_data_gaps(fs)}"
    )


def merge_gaps(declared: Iterable[str], reported: Iterable[str]) -> list[str]:
    """Union ordenada y sin duplicados: primero lo declarado por features, despues lo del LLM."""
    out: list[str] = []
    for gap in [*declared, *reported]:
        if gap and gap not in out:
            out.append(gap)
    return out


class _HasSymbol(Protocol):
    symbol: str


T = TypeVar("T", bound=_HasSymbol)


def reconcile_by_symbol(
    items: Sequence[T],
    features_by_symbol: dict[str, FeatureSet],
    *,
    node_name: str,
    fill: Callable[[T, FeatureSet], T],
    make_missing: Callable[[FeatureSet], T],
) -> list[T]:
    """Alinea la salida del LLM con los simbolos de entrada (orden de entrada).

    - Simbolo desconocido -> descartado y logueado (el LLM lo invento).
    - Simbolo duplicado -> se queda el primero, el resto se loguea.
    - Simbolo omitido -> `make_missing(fs)`: entrada explicita, confianza 0.
    - `fill(item, fs)` completa por codigo los campos derivados de features.
    """
    by_symbol: dict[str, T] = {}
    for item in items:
        if item.symbol not in features_by_symbol:
            logger.warning("%s: el LLM devolvio un simbolo no solicitado %r; descartado", node_name, item.symbol)
            continue
        if item.symbol in by_symbol:
            logger.warning("%s: simbolo duplicado %r en la salida del LLM; se conserva el primero", node_name, item.symbol)
            continue
        by_symbol[item.symbol] = item
    out: list[T] = []
    for symbol, fs in features_by_symbol.items():
        item = by_symbol.get(symbol)
        if item is None:
            logger.warning("%s: el LLM omitio el simbolo %r; se marca explicitamente", node_name, symbol)
            out.append(make_missing(fs))
        else:
            out.append(fill(item, fs))
    return out


MISSING_ANALYSIS_TEXT = "SIN ANALISIS: el modelo omitio este simbolo en su salida."
