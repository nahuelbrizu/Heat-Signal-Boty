# Compatibilidad (HeatSignal): FReinforcedStrategy.py original declara IntParameter sin `space` y Freqtrade
# 2026.9 aborta ("Cannot determine parameter space for adx_period"). Se re-declaran los 3 parametros con los
# MISMOS rangos y defaults y space="buy" (como FAdxSmaStrategy del mismo repo). En backtesting solo se usa el
# default, asi que la regla no cambia. Todo lo demas se hereda del archivo original, cargado sin tocarlo.
import sys
from pathlib import Path

from freqtrade.strategy import IntParameter

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _load import load_original  # noqa: E402

_orig = load_original("FReinforcedStrategy.py", "_benchmark_orig_FReinforcedStrategy")


class FReinforcedStrategyCompat(_orig.FReinforcedStrategy):
    adx_period = IntParameter(4, 24, default=14, space="buy")
    ema_short_period = IntParameter(4, 24, default=8, space="buy")
    ema_long_period = IntParameter(12, 175, default=21, space="buy")
