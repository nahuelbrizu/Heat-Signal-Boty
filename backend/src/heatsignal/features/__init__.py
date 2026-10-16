"""
Capa de features: codigo puro y deterministico (sin I/O, sin LLM) que transforma
eventos normalizados (`schema/events.py`) en un `FeatureSet` (`schema/features.py`).
"""

from .builder import build_feature_set
from .context import REGIME_MODEL_ID, build_market_context, classify_regime
from .heatmap import HEATMAP_MODEL_ID, build_heatmap
from .order_flow import build_order_flow
from .volatility import Bar, bars_from_marks, bars_from_trades, wilder_atr

__all__ = [
    "HEATMAP_MODEL_ID",
    "REGIME_MODEL_ID",
    "Bar",
    "bars_from_marks",
    "bars_from_trades",
    "build_feature_set",
    "build_heatmap",
    "build_market_context",
    "build_order_flow",
    "classify_regime",
    "wilder_atr",
]
