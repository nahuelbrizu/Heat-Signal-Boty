"""
Guardrail de lenguaje historico-no-predictivo (principio 4 de CLAUDE.md).

Chequeo en CODIGO, independiente del prompt: detecta frases predictivas o de
certeza en espanol e ingles. El texto se normaliza (minusculas, sin tildes)
antes de buscar, asi "subira" y "subirá" matchean igual.

Es deliberadamente conservador (prefiere falsos positivos): un falso positivo
cuesta un reintento; un falso negativo publica una prediccion.
"""

from __future__ import annotations

import re
import unicodedata

_MOVE_VERBS_ES = (
    "subir|bajar|caer|reaccionar|rebotar|romper|perforar|explotar|dispararse|desplomarse|"
    "repuntar|corregir|colapsar|tocar|alcanzar|llegar"
)
_FUTURE_ES = (
    "subira|subiran|bajara|bajaran|caera|caeran|rebotara|rebotaran|reaccionara|reaccionaran|"
    "rompera|romperan|perforara|explotara|se disparara|se desplomara|repuntara|corregira|"
    "colapsara|alcanzara|tocara|llegara"
)
_MOVE_VERBS_EN = (
    "rise|fall|drop|go up|go down|pump|dump|bounce|break|reach|hit|rally|crash|moon|surge|plunge|recover"
)

PREDICTIVE_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p)
    for p in (
        # --- espanol ---
        rf"\b(va|van|iria|irian) a ({_MOVE_VERBS_ES})\b",
        rf"\b({_FUTURE_ES})\b",
        # "no garantiza" / "no se garantiza" es un disclaimer valido, no una promesa.
        r"(?<!no )(?<!no se )\bgarantiz\w*",
        r"\bseguro que\b",
        r"\bes seguro\b",
        r"\bcon (total )?(certeza|seguridad)\b",
        r"\bsin (ninguna )?duda\b",
        r"\bno (puede|va a) fallar\b",
        r"\b(esta|estan) por (subir|bajar|caer|explotar)\b",
        # --- ingles ---
        rf"\bwill (likely |definitely |surely |probably )?({_MOVE_VERBS_EN})\b",
        rf"\b(is|are) (going|about) to ({_MOVE_VERBS_EN})\b",
        r"(?<!not )(?<!no )\bguarantee\w*",
        r"\b(sure|certain|bound) to\b",
        r"\bdefinitely\b",
        r"\bcan'?t (lose|fail)\b",
        r"\bno doubt\b",
    )
)


def _normalize(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text.lower())
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


def find_predictive_phrases(text: str) -> list[str]:
    """Devuelve las frases predictivas encontradas (normalizadas), sin duplicados, en orden."""
    normalized = _normalize(text)
    found: list[str] = []
    for pattern in PREDICTIVE_PATTERNS:
        for match in pattern.finditer(normalized):
            phrase = match.group(0)
            if phrase not in found:
                found.append(phrase)
    return found
