"""
Clasificacion de los avisos de `data_gaps` (texto) en tres clases.

- estructural: limitacion conocida y permanente de la fuente, no una falla. P.ej. Binance `forceOrder` manda
  como mucho 1 liquidacion por segundo y simbolo (PARTIAL), o 0 liquidaciones de un exchange que esta
  conectado y sano en la ventana (es normal en 30 min tranquilos).
- respaldo: el dato faltaba en la fuente propia y se reconstruyo completo por REST.
- real: hueco de verdad (exchange caido o sin mensajes, velas faltantes sin respaldo, `sin_datos`, fallos
  de REST). Todo aviso que no matchea un patron conocido cae aca: lo desconocido nunca se esconde.

Los avisos siguen siendo texto (contrato de `FeatureSet.data_gaps`); esto solo los rotula. Al LLM le llegan
los reales como "huecos" y los demas en UNA linea corta de notas; el panel y los logs muestran todo con su
clase.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable

from ..schema.features import GapClass

# (patron, clase, nota corta para el LLM a partir del match)
_RULES: list[tuple[re.Pattern[str], GapClass, Callable[[re.Match[str]], str]]] = [
    (re.compile(r"^liquidaciones ([a-z/]+) PARTIAL"), "estructural",
     lambda m: f"liqs {m.group(1)} muestreadas (<=1/s)"),
    (re.compile(r"^liquidaciones: sin eventos de (\w+) en la ventana \(conectado y sano\)"), "estructural",
     lambda m: f"0 liqs {m.group(1)}"),
    (re.compile(r"^liquidaciones: 0 eventos en la ventana \(exchanges conectados y sanos\)"), "estructural",
     lambda m: "0 liqs en la ventana"),
    (re.compile(r"^noticias: .*no publican RSS"), "estructural", lambda m: "sin RSS del emisor"),
    (re.compile(r"^htf: (\d+) de las ultimas \d+ velas de 1h vinieron del REST"), "respaldo",
     lambda m: f"{m.group(1)} velas 1h por REST"),
    (re.compile(r"^sesion: (\d+) minuto\(s\) vinieron del REST"), "respaldo",
     lambda m: f"{m.group(1)} min de sesion por REST"),
    (re.compile(r"^htf: (base historica no disponible|no se pudo abrir la base historica|fallo la lectura de la "
                r"base historica).*se usa REST"), "respaldo", lambda m: "htf por REST"),
    (re.compile(r"^sesion: base historica no disponible \(precio solo por REST"), "respaldo",
     lambda m: "sesion por REST"),
]

GAP_CLASSES: tuple[GapClass, ...] = ("real", "respaldo", "estructural")


def classify_gap(text: str) -> tuple[GapClass, str]:
    """(clase, nota corta). Para un hueco real la nota es el texto completo."""
    for pattern, cls, short in _RULES:
        m = pattern.search(text)
        if m:
            return cls, short(m)
    return "real", text


def gap_classes(gaps: Iterable[str]) -> list[dict[str, str]]:
    """Para el panel/API: [{"text", "cls"}] en el mismo orden."""
    return [{"text": g, "cls": classify_gap(g)[0]} for g in gaps]


def split_gaps(gaps: Iterable[str]) -> tuple[list[str], list[str]]:
    """(huecos reales completos, notas cortas de lo estructural/respaldo sin duplicados)."""
    real: list[str] = []
    notes: list[str] = []
    for g in gaps:
        cls, short = classify_gap(g)
        if cls == "real":
            if g not in real:
                real.append(g)
        elif short not in notes:
            notes.append(short)
    return real, notes


def real_gaps(gaps: Iterable[str]) -> list[str]:
    return split_gaps(gaps)[0]


def render_gaps(gaps: Iterable[str]) -> str:
    """Una linea para los prompts: huecos reales + notas de cobertura (si hay)."""
    real, notes = split_gaps(gaps)
    out = ("huecos de datos: " + "; ".join(real)) if real else "huecos de datos: ninguno declarado"
    if notes:
        out += " | notas de cobertura (conocidas, no son fallas): " + "; ".join(notes)
    return out
