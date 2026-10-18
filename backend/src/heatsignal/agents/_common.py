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
import math
from datetime import datetime, timezone
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


def fmt_price(value: float | None, sig: int = 5) -> str:
    """Precio/ATR con al menos `sig` cifras significativas (y 2 decimales minimo).

    Con 2 decimales fijos un ATR de XRP (0.00054) se leia '0.00' y el LLM no veia la volatilidad.
    """
    if value is None:
        return "n/d"
    if value == 0 or not math.isfinite(value):
        return f"{value:,.2f}"
    decimals = min(10, max(2, sig - 1 - math.floor(math.log10(abs(value)))))
    return f"{value:,.{decimals}f}"


def render_heatmap(fs: FeatureSet) -> str:
    hm = fs.heatmap
    lines = [
        f"{fs.symbol} [heatmap {hm.heatmap_model_id}, ventana {hm.window_s}s, "
        f"mark {fmt_price(hm.mark_price)}, ATR {fmt_price(hm.atr)}]"
    ]
    if not hm.clusters:
        lines.append("  sin clusters de liquidacion en la ventana.")
    for c in hm.clusters:
        side = c.dominant_side.value if c.dominant_side else "mixto"
        lines.append(
            f"  - cluster {fmt_price(c.price_low)}-{fmt_price(c.price_high)} (mid {fmt_price(c.price_mid)}): "
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
    funding = ", ".join(f"{ex.value}={rate:+.7f}" for ex, rate in ctx.funding_rate_by_exchange.items())
    out = (
        f"{fs.symbol}: regimen (calculado por codigo) {ctx.regime}, mark {fmt_price(ctx.mark_price)}, "
        f"funding [{funding or 'n/d'}], OI ${_fmt(ctx.open_interest_usd, '{:,.0f}')}, "
        f"cambio OI {_fmt(ctx.oi_change_pct, '{:+.2f}')}%, ATR {fmt_price(ctx.atr)} "
        f"({_fmt(ctx.atr_pct, '{:.3f}')}%)\n  {format_data_gaps(fs)}"
    )
    if fs.htf is not None:
        out += "\n" + render_htf(fs, n_candles=6)
    return out


def _hh(ms: int | None) -> str:
    if ms is None:
        return "n/d"
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%d %H:%M")


_SETUP_TEXT = {
    "pullback_ema20_1h_a_favor": "retroceso a la EMA20 1h a favor de la tendencia",
    "continuacion_a_favor": "continuacion a favor (cierre del lado de la tendencia, no extendido)",
}


def render_htf(fs: FeatureSet, n_candles: int = 6) -> str:
    """Contexto 1h + tendencia del dia (calculado por CODIGO). Compacto: las 48 velas quedan en el estado/panel."""
    h = fs.htf
    if h is None:
        return f"  {fs.symbol} [contexto 1h]: no calculado (apagado)"
    head = (f"  {fs.symbol} [contexto 1h {h.htf_model_id}, SOLO velas de 1h CERRADAS hasta {_hh(h.last_closed_open_ms)} "
            f"UTC (+1h), estado {h.status}, fuentes {h.sources or 'ninguna'}]")
    if h.status == "missing":
        return head + f"\n    tendencia diaria: sin_datos ({h.trend_reason})"
    setups = [txt for k, txt in _SETUP_TEXT.items() if getattr(h.setups, k)]
    if h.setups.extremo_rango_diario:
        setups.append(f"cierre en el extremo {h.setups.extremo_rango_diario} del rango diario")
    lines = [
        head,
        f"    tendencia diaria (codigo, regla fija): {h.daily_trend} — {h.trend_reason}",
        f"    dia UTC ({h.day_hours_closed} velas cerradas): apertura {fmt_price(h.day_open)}, max {fmt_price(h.day_high)}, "
        f"min {fmt_price(h.day_low)}, ultimo cierre {fmt_price(h.last_close)} "
        f"({_fmt(h.change_from_open_pct, '{:+.2f}')}% / {_fmt(h.change_from_open_atr, '{:+.2f}')} ATR1h desde la apertura), "
        f"posicion en el rango {_fmt(h.pos_in_day_range, '{:.2f}')}, rango {_fmt(h.day_range_atr, '{:.1f}')} ATR1h",
        f"    dia anterior: " + ("n/d" if not h.prev_day else ", ".join(f"{k} {fmt_price(v)}" for k, v in h.prev_day.items())),
        f"    EMA20 1h {fmt_price(h.ema20_1h)} (cierre a {_fmt(h.dist_ema20_1h_atr, '{:+.2f}')} ATR, pendiente "
        f"{_fmt(h.ema20_1h_slope_atr, '{:+.2f}')} ATR en 4 velas), SMA50 1h {fmt_price(h.sma50_1h)}, "
        f"EMA10 diaria {fmt_price(h.ema10_1d)} (cierre {_fmt(h.close_vs_ema10_1d_pct, '{:+.2f}')}%), "
        f"SMA20 diaria {fmt_price(h.sma20_1d)}, ATR14 1h {fmt_price(h.atr14_1h)} ({_fmt(h.atr14_1h_pct, '{:.3f}')}%)",
        f"    setups (codigo): {'; '.join(setups) or 'ninguno'}",
    ]
    if n_candles and h.candles_1h:
        lines.append("    ultimas velas 1h (UTC, O/H/L/C): " + " | ".join(
            f"{_hh(c.open_ms)[-5:]} {fmt_price(c.open)}/{fmt_price(c.high)}/{fmt_price(c.low)}/{fmt_price(c.close)}"
            for c in h.candles_1h[-n_candles:]))
    return "\n".join(lines)


def htf_digest_line(fs: FeatureSet) -> str:
    """Una linea para el digest del debate y del Sintetizador."""
    h = fs.htf
    if h is None:
        return "- Contexto 1h / tendencia del dia: no calculado"
    if h.status == "missing":
        return f"- Contexto 1h / tendencia del dia (codigo): sin_datos ({h.trend_reason})"
    setups = [k for k in _SETUP_TEXT if getattr(h.setups, k)]
    if h.setups.extremo_rango_diario:
        setups.append(f"extremo_rango_diario={h.setups.extremo_rango_diario}")
    return (
        f"- Contexto 1h / tendencia del dia (codigo, {h.htf_model_id}): daily_trend={h.daily_trend}; cambio desde la "
        f"apertura UTC {_fmt(h.change_from_open_pct, '{:+.2f}')}% ({_fmt(h.change_from_open_atr, '{:+.2f}')} ATR1h); "
        f"posicion en el rango diario {_fmt(h.pos_in_day_range, '{:.2f}')}; cierre vs EMA20 1h "
        f"{_fmt(h.dist_ema20_1h_atr, '{:+.2f}')} ATR; setups: {', '.join(setups) or 'ninguno'}"
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
