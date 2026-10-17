# Compatibilidad (HeatSignal) para Freqtrade 2026.9 (numpy 2 + pandas 3). El archivo original NO se modifica: se
# carga con dos parches en memoria que no cambian parametros, ROI, stoploss, leverage ni la regla:
#  1. `from numpy.lib import math` (numpy 2 lo quito; el original no usa `math`) -> `import math`.
#  2. `df["Var"].iat[i] = ...` es asignacion encadenada: con pandas 3 (Copy-on-Write) NO escribe nada, Var queda
#     en 0 y la estrategia nunca opera. Se reemplaza por `df.iat[i, <col Var>] = ...`, que es lo que hacia con
#     pandas < 3 (la semantica con la que se publico).
#  3. `df["shortstop"] = 999999999999999999` crea una columna int64 y pandas 3 rechaza asignarle floats (pandas < 3
#     la convertia a float64 en la primera asignacion). Se crea directamente como float: mismo valor y semantica.
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _load import load_original  # noqa: E402

_orig = load_original("FOttStrategy.py", "_benchmark_orig_FOttStrategy", patches=[
    ("from numpy.lib import math", "import math"),
    ('df["Var"].iat[i] = (', 'df.iat[i, df.columns.get_loc("Var")] = ('),
    ('df["shortstop"] = 999999999999999999', 'df["shortstop"] = float(999999999999999999)'),
])


class FOttStrategyCompat(_orig.FOttStrategy):
    pass
