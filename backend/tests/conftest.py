"""Configuracion comun de los tests.

Las estrategias generadas por bloques (lab/builder.py) se cargan al CATALOG desde data/lab_generated.jsonl. Los
tests NO tienen que depender del registro real de la PC (crece cuando el agente propone): se apunta a un archivo
que no existe. Los tests del constructor pasan su propio path.
"""

import os

os.environ["HEATSIGNAL_GENERATED_PATH"] = os.path.join(os.path.dirname(__file__), "_sin_registro_generadas.jsonl")
