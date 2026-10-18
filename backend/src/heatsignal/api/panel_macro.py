"""
Rutas de solo lectura del panel para el contexto macro y el diario de lecciones (2026-10-19).

    GET /api/macro                   series de FRED con su fecha de publicacion (point-in-time a ahora), calendario
                                     (proximas 48 h y proximos por tipo), Polymarket (ultima lectura, variacion 24 h y
                                     serie de 7 dias), estado de cada fuente y el bloque exacto que recibe el grafo
    GET /api/lessons                 diario de lecciones (strategy_id, symbol, daily_trend, outcome, source,
                                     window_verdict, limit, offset) + facetas para los filtros
    GET /api/lessons/{hypothesis_id} la leccion de una hipotesis (404 si todavia no tiene)

Las lecciones son para el usuario: el panel las muestra, el grafo nunca las lee (evaluation/lessons.py).
Todo corre en otro hilo con conexiones SQLite de solo lectura abiertas y cerradas ahi (las bases las escriben otros
procesos: heatsignal-macro.timer y heatsignal-eval.timer).
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import UUID

from fastapi import FastAPI, HTTPException, Query

if TYPE_CHECKING:  # pragma: no cover
    from .panel import PanelContext

POLY_SERIES_DAYS = 7
POLY_SERIES_MAX_POINTS = 168  # una por hora


def _downsample(points: list[tuple[int, float]], n: int) -> list[tuple[int, float]]:
    if len(points) <= n:
        return points
    step = len(points) / n
    out = [points[int(i * step)] for i in range(n)]
    if out[-1] != points[-1]:
        out[-1] = points[-1]
    return out


def macro_view(db_path: Path | None, now_ms: int) -> dict[str, Any]:
    from ..macro.calendar import load_calendar
    from ..macro.context import (EVENT_WINDOW_H, MACRO_CONTEXT_ID, build_block, estimate_tokens, fred_summaries,
                                 poly_rows, source_status)
    from ..macro.polymarket import labels, load_config
    from ..macro.store import MacroStore, ms_to_iso

    try:
        cal, cal_err = load_calendar(), None
    except Exception as exc:  # noqa: BLE001
        cal, cal_err = None, f"calendario ilegible: {exc}"
    try:
        lab = labels(load_config())
        poly_cfg_err = None
    except Exception as exc:  # noqa: BLE001
        lab, poly_cfg_err = None, f"lista de Polymarket ilegible: {exc}"
    store = None
    db_note = None
    if db_path is None:
        db_note = "base macro no configurada en este proceso"
    elif not Path(db_path).exists():
        db_note = f"todavia no existe {db_path} (heatsignal-macro.timer no corrio)"
    else:
        store = MacroStore(db_path, readonly=True)
    try:
        st = source_status(store, now_ms, cal, cal_err)
        if poly_cfg_err:
            st["polymarket"]["detail"] = poly_cfg_err
        fred = fred_summaries(store, now_ms)
        polys, poly_ms = poly_rows(store, now_ms, lab) if store is not None else ([], None)
        for r in polys:
            r["series"] = _downsample(store.poly_series(r["market_id"], now_ms - POLY_SERIES_DAYS * 86_400_000, now_ms),
                                      POLY_SERIES_MAX_POINTS)
        block = build_block(store, now_ms, calendar=cal, cal_error=cal_err, labels=lab)
        counts = store.fred_counts() if store is not None else {}
    finally:
        if store is not None:
            store.close()
    calendar = None
    if cal is not None:
        calendar = {
            "calendar_id": cal.calendar_id, "verified_at": cal.verified_at, "sources": cal.sources,
            "window_h": EVENT_WINDOW_H,
            "upcoming": [e.as_dict() for e in cal.upcoming(now_ms, EVENT_WINDOW_H)],
            "next": [e.as_dict() for e in cal.next_events(now_ms, 10)],
            "next_by_kind": {k: (e.as_dict() if e else None) for k, e in cal.next_by_kind(now_ms).items()},
            "warning": cal.coverage_warning(now_ms),
        }
    return {
        "now_ms": now_ms, "now_at": ms_to_iso(now_ms), "context_id": MACRO_CONTEXT_ID, "db_note": db_note,
        "sources": st, "fred": fred, "fred_counts": counts, "calendar": calendar, "calendar_error": cal_err,
        "polymarket": polys, "polymarket_read_ms": poly_ms, "polymarket_read_at": ms_to_iso(poly_ms),
        "block": block, "block_chars": len(block), "block_tokens_est": estimate_tokens(block),
    }


def register_macro_api(app: FastAPI, ctx: PanelContext) -> None:
    def _db(name: str) -> Path | None:
        p = ctx.db_paths.get(name)
        return None if p is None else Path(p)

    @app.get("/api/macro")
    async def macro() -> dict[str, Any]:
        from .panel import json_safe

        try:
            return json_safe(await asyncio.to_thread(macro_view, _db("macro"), int(time.time() * 1000)))
        except Exception as exc:  # noqa: BLE001 - error explicito, no silencio
            raise HTTPException(status_code=503, detail=f"no se pudo leer la base macro: {exc}") from None

    def _lessons_read(fn: Any) -> Any:
        from ..history.store import open_readonly

        db = _db("eval")
        if db is None or not db.exists():
            raise HTTPException(status_code=503, detail="base de evaluacion no disponible")
        conn = open_readonly(db)
        try:
            return fn(conn)
        finally:
            conn.close()

    @app.get("/api/lessons")
    async def lessons(
        strategy_id: UUID | None = None,
        symbol: str | None = Query(None, max_length=30),
        daily_trend: str | None = Query(None, pattern="^(alcista|bajista|lateral|sin_datos|none)$"),
        outcome: str | None = Query(None, pattern="^(acierto|fallo)$"),
        source: str | None = Query(None, pattern="^(llm|codigo)$"),
        window_verdict: str | None = Query(None, pattern="^(alcanza|dudoso|no_alcanza|sin_dato)$"),
        limit: int = Query(50, ge=1, le=200),
        offset: int = Query(0, ge=0, le=100_000),
    ) -> dict[str, Any]:
        from ..evaluation import lessons as ls
        from .panel_api import internal_symbol

        sym = internal_symbol(symbol) if symbol else None
        return await asyncio.to_thread(_lessons_read, lambda c: ls.query(
            c, limit=limit, offset=offset, strategy_id=None if strategy_id is None else str(strategy_id), symbol=sym,
            daily_trend=daily_trend, outcome=outcome, source=source, window_verdict=window_verdict))

    @app.get("/api/lessons/{hypothesis_id}")
    async def lesson(hypothesis_id: UUID) -> dict[str, Any]:
        from ..evaluation import lessons as ls

        out = await asyncio.to_thread(_lessons_read, lambda c: ls.get_one(c, str(hypothesis_id)))
        if out is None:
            raise HTTPException(status_code=404, detail=f"la hipotesis {hypothesis_id} todavia no tiene leccion")
        return out
