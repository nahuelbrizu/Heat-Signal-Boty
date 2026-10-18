"""
Corte de resultados por tendencia del dia (`daily_trend`), por "a favor / en contra" (`trend_alignment`) y por la
sesion de mercado de la que salio la hipotesis (`session`, ver `features/sessions.py`).

Cada segmento informa n (resueltas), aciertos, tasa con IC 95 % de Wilson, pendientes, `expired_no_data`, y el
PnL neto medio junto al benchmark pasivo (buy & hold en la misma ventana) y el alfa medio: una tasa sin n, IC y
referencia no se publica (CLAUDE.md, "Honestidad estadistica").

Las hipotesis previas al contexto 1h (sin `daily_trend`) van al segmento "sin_registro": se cuentan, no se
descartan. Puro (sin scipy ni pandas): lo usan el job de evaluacion y el panel de la VM.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

Z95 = 1.959964
SESSIONS = ("asia", "europa", "eeuu", "cierre")
TRENDS = ("alcista", "bajista", "lateral", "sin_datos", "sin_registro")
ALIGNMENTS = ("a_favor", "en_contra", "lateral", "sin_datos", "sin_registro")


def wilson(k: int, n: int, z: float = Z95) -> list[float] | None:
    """IC de Wilson para k/n; None si n == 0 (sin datos no hay intervalo)."""
    if n <= 0:
        return None
    p = k / n
    den = 1 + z * z / n
    c = (p + z * z / (2 * n)) / den
    h = z * ((p * (1 - p) / n + z * z / (4 * n * n)) ** 0.5) / den
    return [max(0.0, c - h), min(1.0, c + h)]


def _mean(xs: list[float]) -> float | None:
    return sum(xs) / len(xs) if xs else None


def _segment(key: dict[str, str], rows: list[tuple]) -> dict[str, Any]:
    resolved = [r for r in rows if r[2] == "resolved" and r[3] is not None]
    wins = sum(int(bool(r[3])) for r in resolved)
    n = len(resolved)
    return {
        **key,
        "total": len(rows),
        "pending": sum(1 for r in rows if r[2] == "pending"),
        "expired_no_data": sum(1 for r in rows if r[2] == "expired_no_data"),
        "n": n,
        "wins": wins,
        "rate": wins / n if n else None,
        "ci95": wilson(wins, n),
        "mean_pnl_pct": _mean([r[4] for r in resolved if r[4] is not None]),
        "mean_benchmark_pnl_pct": _mean([r[5] for r in resolved if r[5] is not None]),
        "mean_alpha_pct": _mean([r[6] for r in resolved if r[6] is not None]),
    }


def trend_segments(rows: Iterable[tuple]) -> dict[str, Any]:
    """rows: (daily_trend, trend_alignment, status, win, pnl_pct, benchmark_pnl_pct, alpha_pct[, session]).

    Devuelve {"overall", "by_daily_trend", "by_alignment", "by_trend_and_alignment"}; los segmentos vacios se omiten
    (salvo en `by_daily_trend` / `by_alignment`, donde se listan las categorias con n=0 para que se vea el hueco).
    """
    norm = [(r[0] or "sin_registro", r[1] or "sin_registro", *r[2:7], (r[7] if len(r) > 7 else None) or "sin_registro")
            for r in rows]
    names = list(SESSIONS) + sorted({r[7] for r in norm} - set(SESSIONS) - {"sin_registro"}) + ["sin_registro"]
    by_session = [_segment({"session": n}, [r for r in norm if r[7] == n]) for n in names]
    by_trend = [_segment({"daily_trend": t}, [r for r in norm if r[0] == t]) for t in TRENDS]
    by_align = [_segment({"trend_alignment": a}, [r for r in norm if r[1] == a]) for a in ALIGNMENTS]
    cross = []
    for t in TRENDS:
        for a in ALIGNMENTS:
            sub = [r for r in norm if r[0] == t and r[1] == a]
            if sub:
                cross.append(_segment({"daily_trend": t, "trend_alignment": a}, sub))
    return {
        "overall": _segment({}, norm),
        "by_daily_trend": [s for s in by_trend if s["total"] or s["daily_trend"] != "sin_registro"],
        "by_alignment": [s for s in by_align if s["total"] or s["trend_alignment"] != "sin_registro"],
        "by_trend_and_alignment": cross,
        "by_session": [s for s in by_session if s["total"] or s["session"] != "sin_registro"],
    }
