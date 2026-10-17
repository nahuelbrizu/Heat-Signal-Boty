"""Carga un archivo ORIGINAL de benchmark/ (sin modificar el archivo) como modulo, para heredar de su clase.

`patches` = lista de (texto_original, reemplazo) aplicados en memoria; cada texto tiene que aparecer EXACTAMENTE
una vez (si el original cambia, falla en vez de aplicar un parche a ciegas).
"""

import sys
import types
from pathlib import Path

ORIG_DIR = Path(__file__).resolve().parent.parent


def load_original(filename: str, module_name: str, patches=()):
    if module_name in sys.modules:
        return sys.modules[module_name]
    path = ORIG_DIR / filename
    src = path.read_text()
    for old, new in patches:
        n = src.count(old)
        if n != 1:
            raise RuntimeError(f"parche de compatibilidad no aplica a {filename}: {old!r} aparece {n} veces")
        src = src.replace(old, new)
    mod = types.ModuleType(module_name)
    mod.__file__ = str(path)
    sys.modules[module_name] = mod
    exec(compile(src, str(path), "exec"), mod.__dict__)
    return mod
