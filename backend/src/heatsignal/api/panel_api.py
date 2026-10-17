"""
API de solo lectura del panel interno, pensada para una single page que la consume por partes.

Contrato completo con ejemplos de respuesta: `api/API.md`. Resumen de rutas en el docstring de `api/panel.py`.

Convenciones:
  - Todo es GET y de solo lectura, salvo dos acciones POST ("Analizar ahora" y la cola del laboratorio), que
    exigen el header `X-HeatSignal-Action: 1` (403 sin el: una pagina de otro origen no puede mandarlo sin un
    preflight CORS que el panel no acepta), `Content-Type: application/json` (415) y cuerpo <= 4 KB (413).
  - Listas paginadas: `{"total", "limit", "offset", "items": [...]}`; `total` = cuantos cumplen el filtro.
  - Tiempos: cada instante va dos veces, `*_ms` (epoch ms UTC) y su ISO 8601 UTC (`*_at` o `t`).
  - Parametros de tiempo (`since`, `until`): epoch ms (solo digitos) o ISO 8601 (`2026-10-17T00:00:00Z`;
    sin zona se toma UTC). Intervalos semiabiertos [since, until).
  - Errores: 422 si un parametro no tiene el tipo/rango esperado (lo valida FastAPI), 400 si el valor es
    valido como tipo pero no tiene sentido (fecha ilegible, rango demasiado grande), 404 si no existe,
    503 si la parte del sistema que lo provee esta apagada. Cuerpo: `{"detail": "mensaje en espanol"}`.

Hilos: los endpoints corren en el event loop (las conexiones SQLite de evaluacion y la ventana en memoria no
son thread-safe). Lo que puede escanear mucho (base historica y checkpoints del grafo) corre en
`asyncio.to_thread` con una conexion de solo lectura nueva, abierta y cerrada en ese hilo.

Importante: este modulo corre en la VM (sin pandas). No importar nada de `heatsignal.lab`.
"""

from __future__ import annotations

import asyncio
import json
import math
import os
import re
import sys
import time
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal
from uuid import UUID

from fastapi import FastAPI, HTTPException, Path as PathParam, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from ..schema.hypotheses import HypothesisStatus

if TYPE_CHECKING:  # pragma: no cover
    from .panel import PanelContext

# ---- limites (acotan memoria y tiempo de cada consulta) ----------------------------------------------
MAX_PAGE = 500  # filas por pagina en hipotesis / ciclos
MAX_NEWS_PAGE = 200
MAX_LIQ_PAGE = 1000
MAX_OFFSET = 100_000
MAX_BARS = 5000  # barras por exchange en /api/history/bars
DEFAULT_BARS = 500
TF_MS = {"1m": 60_000, "5m": 300_000, "15m": 900_000, "1h": 3_600_000}
EXCHANGE_PRIORITY = ("binance", "bybit", "okx", "hyperliquid")  # referencia de precio en `combine=true`
LAB_ID = re.compile(r"^[a-z0-9][a-z0-9._-]{0,99}$")  # = lab.publish.SAFE_ID (sin importar heatsignal.lab)
RUN_ID = re.compile(r"^[A-Za-z0-9._:-]{1,120}$")
SUMMARY_TTL_S = 10.0
CHECKPOINT_TIMEOUT_S = 15.0
CALIBRATION_BINS = 10

# ---- acciones (POST) ------------------------------------------------------------------------------
ACTION_HEADER = "x-heatsignal-action"  # exigido en todo POST: una pagina de otro origen no puede mandarlo
MAX_BODY_BYTES = 4096                  # sin preflight CORS aceptado (el panel no responde CORS)
ANALYZE_COOLDOWN_S = 300.0             # como maximo 1 analisis manual cada 5 min por simbolo
ANALYZE_BUSY_RETRY_S = 60              # sugerencia de reintento si ya hay uno en curso
MAX_ANALYZE_JOBS = 100                 # trabajos recordados en memoria (los viejos se leen del cycle_log)
MAX_PENDING_LAB_REQUESTS = 10
LAB_REQUEST_ID = re.compile(r"^[0-9a-f]{32}$")


class AnalyzeBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    symbol: str = Field(min_length=1, max_length=30)


class LabRequestBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    strategy_ids: list[str] = Field(min_length=1, max_length=20)
    symbols: list[str] = Field(min_length=1, max_length=10)
    note: str = Field("", max_length=500)


async def action_body(request: Request, model: type[BaseModel]) -> Any:
    """Valida un POST de accion: header propio (403), JSON (415), tamano (413), forma (400/422)."""
    if request.headers.get(ACTION_HEADER) != "1":
        raise HTTPException(status_code=403, detail="falta el header X-HeatSignal-Action: 1")
    if not request.headers.get("content-type", "").split(";")[0].strip().lower() == "application/json":
        raise HTTPException(status_code=415, detail="el cuerpo tiene que ser application/json")
    declared = request.headers.get("content-length")
    if declared is not None and declared.isdigit() and int(declared) > MAX_BODY_BYTES:
        raise HTTPException(status_code=413, detail=f"cuerpo demasiado grande (maximo {MAX_BODY_BYTES} bytes)")
    body = b""
    async for chunk in request.stream():
        body += chunk
        if len(body) > MAX_BODY_BYTES:
            raise HTTPException(status_code=413, detail=f"cuerpo demasiado grande (maximo {MAX_BODY_BYTES} bytes)")
    try:
        data = json.loads(body or b"null")
    except ValueError:
        raise HTTPException(status_code=400, detail="JSON invalido") from None
    try:
        return model.model_validate(data)
    except ValidationError as exc:
        raise RequestValidationError(exc.errors(include_url=False, include_context=False)) from None


def binance_symbol(raw: str) -> str:
    """'xrp-usdt' -> 'XRPUSDT' (formato del laboratorio y de HEATSIGNAL_SYMBOLS)."""
    return internal_symbol(raw).replace("-", "")


def lab_request_json(r: dict[str, Any], events: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    out = {**r, "created_at": iso(r["created_ms"]), "status_at": iso(r["status_ms"]),
           "result_url": f"/api/lab/requests/{r['request_id']}/result" if r["status"] == "done" else None}
    if events is not None:
        out["events"] = [{**e, "ts_at": iso(e["ts_ms"])} for e in events]
    return out


# ---- utilidades -----------------------------------------------------------------------------------

def iso(ms: int | float | None) -> str | None:
    if ms is None:
        return None
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat()


def to_ms(dt: datetime | None) -> int | None:
    return None if dt is None else int(dt.timestamp() * 1000)


def now_ms() -> int:
    return int(time.time() * 1000)


def parse_time_ms(value: str | None, name: str) -> int | None:
    """`since`/`until`: epoch ms (solo digitos) o ISO 8601 (sin zona = UTC). 400 si no se entiende."""
    if value is None or value == "":
        return None
    v = value.strip()
    if re.fullmatch(r"-?\d{1,16}", v):
        return int(v)
    try:
        dt = datetime.fromisoformat(v.replace("Z", "+00:00").replace("z", "+00:00"))
    except ValueError:
        raise HTTPException(status_code=400, detail=f"{name} invalido: {value!r} (usar epoch ms o ISO 8601)") from None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def time_range(since: str | None, until: str | None) -> tuple[int | None, int | None]:
    a, b = parse_time_ms(since, "since"), parse_time_ms(until, "until")
    if a is not None and b is not None and a >= b:
        raise HTTPException(status_code=400, detail="since debe ser anterior a until")
    return a, b


def internal_symbol(raw: str) -> str:
    """'SOLUSDT' / 'sol-usdt' / 'SOL/USDT' -> 'SOL-USDT' (formato interno de eventos e hipotesis)."""
    s = raw.strip().upper().replace("/", "-").replace("_", "-")
    if not re.fullmatch(r"[A-Z0-9]{1,20}(-[A-Z0-9]{2,10})?", s):
        raise HTTPException(status_code=400, detail=f"simbolo invalido: {raw!r}")
    if "-" in s:
        return s
    for quote in ("USDT", "USDC", "USD"):
        if s.endswith(quote) and len(s) > len(quote):
            return f"{s[:-len(quote)]}-{quote}"
    return s


def base_asset(raw: str) -> str:
    """'XRP' / 'XRPUSDT' / 'XRP-USDT' -> 'XRP' (las noticias se etiquetan por activo base)."""
    return internal_symbol(raw).split("-", 1)[0]


def page(total: int, limit: int, offset: int, items: list[Any], **extra: Any) -> dict[str, Any]:
    return {"total": total, "limit": limit, "offset": offset, "items": items, **extra}


def wilson(k: int, n: int, z: float = 1.959964) -> list[float] | None:
    from .panel import wilson as _w

    return _w(k, n, z)


_CACHE: dict[tuple[str, int], tuple[float, Any]] = {}


def cached(name: str, owner: Any, ttl_s: float, fn: Callable[[], Any]) -> tuple[float, Any]:
    """Cache corto en memoria por (nombre, objeto). Devuelve (momento del calculo, valor)."""
    key = (name, id(owner))
    hit = _CACHE.get(key)
    if hit is None or time.time() - hit[0] > ttl_s:
        hit = (time.time(), fn())
        _CACHE[key] = hit
    return hit


def ro_query(db_path: Path, fn: Callable[[Any], Any]) -> Any:
    """Corre `fn(conn)` con una conexion de solo lectura a la base historica (llamar via to_thread)."""
    from ..history.store import open_readonly

    conn = open_readonly(db_path)
    try:
        return fn(conn)
    finally:
        conn.close()


# ---- vistas ---------------------------------------------------------------------------------------

def hypothesis_json(rec: Any, now: datetime | None = None) -> dict[str, Any]:
    """Una hipotesis + su resolucion, plana (comoda para tablas)."""
    h, res, met = rec.hypothesis, rec.resolution, rec.metrics
    now = now or datetime.now(timezone.utc)
    due = h.created_at + timedelta(seconds=h.horizon_s)
    real_move = None
    if res is not None and res.exit_price is not None:
        real_move = float((res.exit_price / h.entry_reference_price - 1) * 100)
    return {
        "hypothesis_id": str(h.hypothesis_id),
        "strategy_id": str(h.strategy_id),
        "run_id": h.run_id,
        "symbol": h.symbol,
        "direction": h.direction.value,
        "confidence": h.confidence_score,
        "entry_price": float(h.entry_reference_price),
        "entry": str(h.entry_reference_price),
        "horizon_s": h.horizon_s,
        "atr_normalized_trigger": h.atr_normalized_trigger,
        "heatmap_model_id": h.heatmap_model_id,
        "rationale": h.rationale,
        "created_at": h.created_at.isoformat(),
        "created_ms": to_ms(h.created_at),
        "due_at": due.isoformat(),
        "due_ms": to_ms(due),
        "status": rec.status.value,
        "overdue": res is None and due <= now,  # vencida, esperando al job de resolucion
        "resolved_at": None if res is None else res.resolved_at.isoformat(),
        "resolved_ms": None if res is None else to_ms(res.resolved_at),
        "exit_price": None if res is None or res.exit_price is None else float(res.exit_price),
        "exit": None if res is None or res.exit_price is None else str(res.exit_price),
        "exit_price_at": None if met is None or met.exit_price_ts is None else met.exit_price_ts.isoformat(),
        "win": None if res is None else res.win,
        "pnl_pct": None if res is None else res.pnl_pct,  # neto de costos
        "gross_pnl_pct": None if met is None else met.gross_pnl_pct,
        "cost_bps": None if met is None else met.cost_bps,
        "real_move_pct": real_move,  # cuanto se movio el precio (sin importar la direccion)
        "benchmark_pnl_pct": None if met is None else met.benchmark_pnl_pct,  # buy & hold en la misma ventana
        "alpha_pct": None if met is None else met.alpha_pct,
    }


def track_record(st: dict[str, Any] | None) -> dict[str, Any]:
    st = st or {}
    n, wins = st.get("resolved", 0), st.get("wins", 0)
    rate = wins / n if n else None
    return {
        "resolved": n, "wins": wins, "rate": rate, "ci95": wilson(wins, n),
        "mean_pnl_pct": st.get("mean_pnl_pct"), "mean_gross_pnl_pct": st.get("mean_gross_pnl_pct"),
        "mean_benchmark_pnl_pct": st.get("mean_benchmark_pnl_pct"), "mean_alpha_pct": st.get("mean_alpha_pct"),
        "brier": st.get("brier") if n else None,
        # Brier de decir siempre la tasa observada (referencia sin habilidad): el modelo deberia quedar abajo
        "brier_reference": rate * (1 - rate) if rate is not None else None,
        "mean_confidence": st.get("mean_confidence") if n else None,
    }


def hypothesis_counts(st: dict[str, Any] | None) -> dict[str, int]:
    st = st or {}
    return {k: int(st.get(k, 0)) for k in ("total", "pending", "due", "resolved", "expired_no_data", "wins")}


def strategy_json(s: Any, st: dict[str, Any] | None) -> dict[str, Any]:
    st = st or {}
    return {
        "strategy_id": str(s.strategy_id),
        "description": s.description,
        "status": s.status.value,
        "author": s.author,
        "registered_at": s.registered_at.isoformat(),
        "registered_ms": to_ms(s.registered_at),
        "symbol": s.entry_conditions.get("symbol"),
        "horizon_s": s.exit_conditions.get("horizon_s"),
        "hypotheses": hypothesis_counts(st),
        "first_hypothesis_ms": None if st.get("first_created_ts") is None else int(st["first_created_ts"] * 1000),
        "last_hypothesis_ms": None if st.get("last_created_ts") is None else int(st["last_created_ts"] * 1000),
        "track_record": track_record(st),
    }


def calibration(pairs: list[tuple[float, bool]], n_bins: int = CALIBRATION_BINS) -> list[dict[str, Any]]:
    """Curva de calibracion: por bucket de confidence, tasa real de acierto (IC Wilson). Python puro."""
    bins: list[list[tuple[float, bool]]] = [[] for _ in range(n_bins)]
    for p, w in pairs:
        bins[min(max(math.floor(round(p * n_bins, 9)), 0), n_bins - 1)].append((p, w))
    out = []
    for k, members in enumerate(bins):
        n = len(members)
        hits = sum(1 for _, w in members if w)
        out.append({
            "lower": k / n_bins, "upper": (k + 1) / n_bins, "n": n,
            "mean_confidence": sum(p for p, _ in members) / n if n else None,
            "hit_rate": hits / n if n else None, "ci95": wilson(hits, n),
        })
    return out


def cycle_json(row: dict[str, Any]) -> dict[str, Any]:
    return {**row, "ts_at": iso(row.get("ts_ms"))}


def news_json(it: Any, stored: bool) -> dict[str, Any]:
    return {
        "item_id": it["item_id"], "source": it["source"], "primary": bool(it["primary"]), "title": it["title"],
        "summary": it["summary"], "url": it["url"],
        "published_ms": it["published_ms"], "published_at": iso(it["published_ms"]),
        "fetched_ms": it["fetched_ms"], "fetched_at": iso(it["fetched_ms"]),
        "symbols": list(it["symbols"]), "market_wide": bool(it["market_wide"]), "stored": stored,
    }


def _news_from_model(it: Any) -> dict[str, Any]:
    return {"item_id": it.item_id, "source": it.source, "primary": it.primary, "title": it.title,
            "summary": it.summary, "url": it.url, "published_ms": it.published_ms, "fetched_ms": it.fetched_ms,
            "symbols": it.symbols, "market_wide": it.market_wide}


def _news_from_row(r: tuple) -> dict[str, Any]:
    return {"item_id": r[0], "source": r[1], "primary": r[2], "title": r[3], "summary": r[4], "url": r[5],
            "published_ms": r[6], "fetched_ms": r[7], "symbols": [x for x in (r[8] or "").split(",") if x],
            "market_wide": r[9]}


def _escape_like(s: str) -> str:
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


# ---- consultas pesadas (corren en otro hilo) ------------------------------------------------------

def query_bars(conn: Any, symbol: str, exchange: str | None, since: int, until: int, tf_ms: int) -> list[tuple]:
    """Barras agregadas a `tf_ms` en SQL (indice symbol+minute_ms). open/close/mark/funding/oi = valor del
    primer/ultimo minuto con dato dentro de la barra; high/low = extremos; volumenes y liquidaciones = suma."""
    ex_clause = "AND exchange = :ex" if exchange else ""
    sql = f"""
    WITH g AS (
        SELECT exchange, (minute_ms / :tf) * :tf AS t, COUNT(*) AS minutes,
               MAX(high) AS high, MIN(low) AS low,
               SUM(buy_usd) AS buy, SUM(sell_usd) AS sell, SUM(n_trades) AS nt, SUM(block_usd) AS blk,
               SUM(liq_long_usd) AS ll, SUM(liq_short_usd) AS ls, SUM(n_liqs) AS nl,
               MIN(CASE WHEN open IS NOT NULL THEN minute_ms END) AS m_open,
               MAX(CASE WHEN close IS NOT NULL THEN minute_ms END) AS m_close,
               MAX(CASE WHEN mark IS NOT NULL THEN minute_ms END) AS m_mark,
               MAX(CASE WHEN funding_8h IS NOT NULL THEN minute_ms END) AS m_fund,
               MAX(CASE WHEN oi_usd IS NOT NULL THEN minute_ms END) AS m_oi
        FROM bars_1m
        WHERE symbol = :sym AND minute_ms >= :since AND minute_ms < :until {ex_clause}
        GROUP BY exchange, t
    )
    SELECT g.exchange, g.t, g.minutes, o.open, g.high, g.low, c.close, g.buy, g.sell, g.nt, g.blk,
           g.ll, g.ls, g.nl, m.mark, f.funding_8h, i.oi_usd
    FROM g
    LEFT JOIN bars_1m o ON o.exchange = g.exchange AND o.symbol = :sym AND o.minute_ms = g.m_open
    LEFT JOIN bars_1m c ON c.exchange = g.exchange AND c.symbol = :sym AND c.minute_ms = g.m_close
    LEFT JOIN bars_1m m ON m.exchange = g.exchange AND m.symbol = :sym AND m.minute_ms = g.m_mark
    LEFT JOIN bars_1m f ON f.exchange = g.exchange AND f.symbol = :sym AND f.minute_ms = g.m_fund
    LEFT JOIN bars_1m i ON i.exchange = g.exchange AND i.symbol = :sym AND i.minute_ms = g.m_oi
    ORDER BY g.t, g.exchange
    """
    params = {"tf": tf_ms, "sym": symbol, "since": since, "until": until, "ex": exchange}
    return conn.execute(sql, params).fetchall()


def bar_json(r: tuple) -> dict[str, Any]:
    (ex, t, minutes, o, h, lo, c, buy, sell, nt, blk, ll, ls, nl, mark, fund, oi) = r
    return {
        "exchange": ex, "t_ms": t, "t": iso(t), "minutes": minutes,
        "open": o, "high": h, "low": lo, "close": c,
        "buy_usd": buy, "sell_usd": sell, "delta_usd": (buy or 0) - (sell or 0), "n_trades": nt, "block_usd": blk,
        "liq_long_usd": ll, "liq_short_usd": ls, "n_liqs": nl, "mark": mark, "funding_8h": fund, "oi_usd": oi,
    }


def combine_bars(bars: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Suma los exchanges por barra. Precio (OHLC y mark) del exchange de referencia presente (prioridad
    binance > bybit > okx > hyperliquid): no se promedian velas de mercados distintos. Funding = media de los
    exchanges con dato; OI = suma de los que lo informan (Binance no lo trae por WS)."""
    by_t: dict[int, list[dict[str, Any]]] = {}
    for b in bars:
        by_t.setdefault(b["t_ms"], []).append(b)
    out = []
    for t in sorted(by_t):
        group = by_t[t]
        ref = min(group, key=lambda b: EXCHANGE_PRIORITY.index(b["exchange"])
                  if b["exchange"] in EXCHANGE_PRIORITY else len(EXCHANGE_PRIORITY))
        fundings = [b["funding_8h"] for b in group if b["funding_8h"] is not None]
        ois = [b["oi_usd"] for b in group if b["oi_usd"] is not None]
        row = {
            "exchange": "all", "t_ms": t, "t": iso(t), "ref_exchange": ref["exchange"],
            "exchanges": sorted(b["exchange"] for b in group),
            "open": ref["open"], "high": ref["high"], "low": ref["low"], "close": ref["close"], "mark": ref["mark"],
            "funding_8h": sum(fundings) / len(fundings) if fundings else None,
            "oi_usd": sum(ois) if ois else None, "oi_exchanges": len(ois),
        }
        for k in ("buy_usd", "sell_usd", "delta_usd", "n_trades", "block_usd", "liq_long_usd", "liq_short_usd",
                  "n_liqs", "minutes"):
            row[k] = sum((b[k] or 0) for b in group)
        out.append(row)
    return out


# ---- checkpoints del grafo (respaldo de /api/cycles/{run_id}) --------------------------------------

def checkpoint_report_sync(checkpoint_db: Path, run_id: str) -> dict[str, Any] | None:
    """Ultimo estado del grafo para `run_id` leido de los checkpoints (solo lectura, en otro hilo).

    thread_id = "<simbolos>:<run_id>" (agents/checkpoint.py): se busca por sufijo. Devuelve run_view del
    estado final, las decisiones y las hipotesis emitidas, o None si no hay un estado con reporte.
    """
    import sqlite3

    from ..agents.checkpoint import make_serializer
    from ..history.store import open_readonly
    from ..pipeline.analysis import run_view

    if not checkpoint_db.exists():
        return None
    conn = open_readonly(checkpoint_db)
    try:
        try:
            row = conn.execute(
                "SELECT type, checkpoint FROM checkpoints WHERE checkpoint_ns = '' AND thread_id LIKE ? ESCAPE '\\' "
                "ORDER BY checkpoint_id DESC LIMIT 1", ("%:" + _escape_like(run_id),)).fetchone()
        except sqlite3.OperationalError:  # base sin tabla de checkpoints (grafo nunca corrio)
            return None
    finally:
        conn.close()
    if row is None:
        return None
    values = make_serializer().loads_typed((row[0], row[1])).get("channel_values", {})
    if "report" not in values:
        return None
    decisions = [{"symbol": d.symbol, "outcome": d.outcome, "reason": d.reason, "strategy_id": d.strategy_id}
                 for d in values.get("synthesis_decisions") or []]
    return {"report": run_view(values), "decisions": decisions,
            "n_hypotheses": len(values.get("pending_hypotheses") or [])}


# ---- system --------------------------------------------------------------------------------------

def _code_version() -> dict[str, Any]:
    """Commit si se puede saber: .git del repo (desarrollo) o un archivo REVISION en la raiz (deploy con
    `git archive`: escribir `git rev-parse HEAD > REVISION`). `source_mtime`: git archive conserva la fecha
    del commit como mtime de los archivos, asi que sirve de aproximacion de la version desplegada."""
    here = Path(__file__).resolve()
    out: dict[str, Any] = {"commit": None, "source": None}
    for parent in here.parents:
        rev = parent / "REVISION"
        if rev.is_file():
            out.update(commit=rev.read_text().strip()[:40] or None, source="REVISION")
            break
        git = parent / ".git"
        if git.is_dir():
            try:
                head = (git / "HEAD").read_text().strip()
                if head.startswith("ref:"):
                    ref = head.split(" ", 1)[1]
                    out["branch"] = ref.rsplit("/", 1)[-1]
                    ref_file = git / ref
                    if ref_file.is_file():
                        out["commit"] = ref_file.read_text().strip()
                    else:
                        packed = git / "packed-refs"
                        for line in packed.read_text().splitlines() if packed.is_file() else []:
                            if line.endswith(" " + ref):
                                out["commit"] = line.split(" ", 1)[0]
                else:
                    out["commit"] = head
                out["source"] = "git"
            except OSError:
                pass
            break
    try:
        mtime = max(p.stat().st_mtime for p in here.parent.parent.rglob("*.py"))
        out["source_mtime_ms"] = int(mtime * 1000)
        out["source_mtime_at"] = iso(mtime * 1000)
    except (OSError, ValueError):
        out["source_mtime_ms"] = None
    return out


def _file_info(path: Path | None) -> dict[str, Any] | None:
    if path is None:
        return None
    files = [p for p in path.parent.glob(path.name + "*") if p.is_file()] if path.parent.exists() else []
    if not files:
        return {"path": str(path), "exists": False, "size_bytes": 0}
    main = path.stat().st_mtime if path.exists() else None
    return {"path": str(path), "exists": path.exists(), "size_bytes": sum(p.stat().st_size for p in files),
            "modified_ms": None if main is None else int(main * 1000), "modified_at": iso(None if main is None else main * 1000)}


def _rss_bytes() -> int | None:
    try:
        for line in Path("/proc/self/status").read_text().splitlines():
            if line.startswith("VmRSS:"):
                return int(line.split()[1]) * 1024
    except (OSError, ValueError, IndexError):
        return None
    return None


# ---- rutas ---------------------------------------------------------------------------------------

def register_api(app: FastAPI, ctx: PanelContext, build_id: str) -> None:  # noqa: C901 - una ruta por bloque
    from .panel import _cycle_view, _history_summary, _history_view, _lab_view, _per_symbol_view, json_safe

    version = _code_version()

    def _require_eval() -> None:
        if ctx.registry is None or ctx.store is None:
            raise HTTPException(status_code=503, detail="evaluacion no disponible (ciclo de analisis apagado)")

    def _require_history() -> Any:
        if ctx.history is None:
            raise HTTPException(status_code=503, detail="grabacion historica apagada (HEATSIGNAL_RECORD=0)")
        return Path(ctx.history.store.path)

    def _strategy_stats() -> tuple[float, dict[str, dict[str, Any]]]:
        return cached("strategy_stats", ctx.store, SUMMARY_TTL_S, ctx.store.stats_by_strategy)

    def _configured_symbols() -> list[str]:
        return [internal_symbol(s) for s in ctx.config.get("symbols", [])]

    # ---------------------------------------------------------------- summary

    @app.get("/api/summary")
    async def summary() -> dict[str, Any]:
        ctx.health.sample()
        now = now_ms()
        workers = [{k: w[k] for k in ("worker", "connected", "last_message_age_s", "reconnects", "uptime_ratio")}
                   for w in ctx.health.snapshot()]
        memory = None
        if ctx.memory is not None:
            symbols = {}
            for sym, row in ctx.memory.summary().items():
                marks = row["mark_by_source"]
                last_trade = row["last_trade"]
                symbols[sym] = {
                    "counts": row["counts"], "mark_by_source": marks,
                    "mark": sum(marks.values()) / len(marks) if marks else None,
                    "last_trade_age_s": None if last_trade is None else round((now - last_trade["ts_ms"]) / 1000, 1),
                }
            memory = {"started_at_ms": ctx.memory.started_at_ms, "started_at": iso(ctx.memory.started_at_ms),
                      "accumulated_s": round(ctx.memory.accumulated_s(), 1), "stats": ctx.memory.stats.as_dict(),
                      "symbols": symbols}

        last = None if ctx.analysis is None else ctx.analysis.last_result
        last_ran = None if ctx.analysis is None else ctx.analysis.last_ran

        def _brief(r: Any, with_decisions: bool) -> dict[str, Any] | None:
            if r is None:
                return None
            from ..pipeline.cycle_log import ts_from_run_id

            ts = ts_from_run_id(r.run_id) or None
            out = {"run_id": r.run_id, "ts_ms": ts, "ts_at": iso(ts), "status": r.status,
                   "n_hypotheses": r.n_hypotheses, "detail": r.detail,
                   "age_s": None if ts is None else round((now - ts) / 1000, 1)}
            if with_decisions:
                out["decisions"] = r.decisions
            return out

        interval_s = ctx.config.get("analysis_interval_s")
        last_brief = _brief(last, False)
        next_ms = None
        if last_brief and last_brief["ts_ms"] and interval_s:
            next_ms = int(last_brief["ts_ms"] + interval_s * 1000)

        counts: dict[str, Any] = {"strategies": None, "hypotheses": None, "cycles": None, "news_window": None}
        if ctx.registry is not None and ctx.store is not None:
            strategies = ctx.registry.get_all()
            by_status: dict[str, int] = {}
            for s in strategies:
                by_status[s.status.value] = by_status.get(s.status.value, 0) + 1
            counts["strategies"] = {"total": len(strategies), "by_status": by_status}
            _, stats = _strategy_stats()
            agg = {k: sum(st[k] for st in stats.values()) for k in
                   ("total", "pending", "due", "resolved", "expired_no_data", "wins")}
            counts["hypotheses"] = agg
        if ctx.cycle_log is not None:
            c = ctx.cycle_log.counts()
            counts["cycles"] = {"total": sum(c.values()), "by_status": c}
        if ctx.news is not None:
            counts["news_window"] = ctx.news.summary()["items"]

        hist = await _history_summary(ctx) if ctx.history is not None else None
        recording: dict[str, Any] = {"enabled": ctx.history is not None, "status": "off"}
        if ctx.history is not None:
            st = ctx.history.stats.as_dict()
            last_flush = st.get("last_flush_ms")
            age = None if last_flush is None else round((now - last_flush) / 1000, 1)
            if last_flush is None:
                status = "starting" if time.time() - ctx.started_at < 120 else "stale"
            else:
                status = "ok" if age is not None and age < 120 else "stale"
            recording = {"enabled": True, "status": status, "last_flush_age_s": age, "stats": st,
                         "rows": None if hist is None else {k: hist[1][k]["rows"] for k in ("bars_1m", "liquidations", "news")},
                         "size_bytes": None if hist is None else hist[1]["size_bytes"],
                         "summary_age_s": None if hist is None else round(time.time() - hist[0], 1)}

        news_fresh = None
        if ctx.news is not None:
            news_fresh = {name: {"last_ok_age_s": None if f["last_ok_ms"] is None else round((now - f["last_ok_ms"]) / 1000, 1),
                                 "error": f["error"]}
                          for name, f in ctx.news.summary()["feeds"].items()}
        lab = _lab_view(ctx)
        lab_brief = None
        if isinstance(lab, dict) and "strategies" in lab:
            lab_brief = {"generated_at": lab.get("generated_at"), "strategies": len(lab["strategies"]),
                         "passing": sum(1 for s in lab["strategies"] if (s.get("verdict") or {}).get("pasa"))}
        elif lab is not None:
            lab_brief = lab  # {"error": ...}

        return {
            "build": build_id,
            "now_ms": now,
            "now_at": iso(now),
            "uptime_s": round(time.time() - ctx.started_at, 1),
            "workers": workers,
            "memory": memory,
            "analysis": {
                "enabled": ctx.analysis is not None,
                "graph_enabled": ctx.analysis is not None and ctx.analysis.graph_enabled,
                "llm_providers": [] if ctx.analysis is None else list(getattr(ctx.analysis, "llm_providers", [])),
                "interval_s": interval_s,
                "last_cycle": last_brief,
                "last_ran": _brief(last_ran, True),
                "next_cycle_eta_ms": next_ms,
            },
            "counts": counts,
            "freshness": {
                "workers_last_message_age_s": {w["worker"]: w["last_message_age_s"] for w in workers},
                "last_cycle_age_s": None if last_brief is None else last_brief["age_s"],
                "news_feeds": news_fresh,
                "recorder_last_flush_age_s": recording.get("last_flush_age_s"),
            },
            "recording": recording,
            "lab": lab_brief,
        }

    # ---------------------------------------------------------------- hipotesis

    @app.get("/api/hypotheses")
    async def hypotheses(
        strategy_id: UUID | None = None,
        symbol: str | None = Query(None, max_length=30),
        status: Literal["pending", "resolved", "expired_no_data"] | None = None,
        win: bool | None = None,
        since: str | None = Query(None, description="created_at >= since (epoch ms o ISO)"),
        until: str | None = Query(None, description="created_at < until (epoch ms o ISO)"),
        order: Literal["desc", "asc"] = "desc",
        limit: int = Query(50, ge=1, le=MAX_PAGE),
        offset: int = Query(0, ge=0, le=MAX_OFFSET),
    ) -> dict[str, Any]:
        _require_eval()
        a, b = time_range(since, until)
        sym = internal_symbol(symbol) if symbol else None
        total, recs = ctx.store.query(
            strategy_id=strategy_id, symbol=sym, status=None if status is None else HypothesisStatus(status),
            win=win, since_ts=None if a is None else a / 1000, until_ts=None if b is None else b / 1000,
            limit=limit, offset=offset, newest_first=order == "desc")
        now = datetime.now(timezone.utc)
        return page(total, limit, offset, [hypothesis_json(r, now) for r in recs],
                    filters={"strategy_id": None if strategy_id is None else str(strategy_id), "symbol": sym,
                             "status": status, "win": win, "since_ms": a, "until_ms": b, "order": order})

    @app.get("/api/hypotheses/{hypothesis_id}")
    async def hypothesis_detail(hypothesis_id: UUID) -> dict[str, Any]:
        _require_eval()
        rec = ctx.store.get_record(hypothesis_id)
        if rec is None:
            raise HTTPException(status_code=404, detail=f"hipotesis {hypothesis_id} no existe")
        out = hypothesis_json(rec)
        s = ctx.registry.get(rec.hypothesis.strategy_id)
        out["strategy"] = None if s is None else {
            "strategy_id": str(s.strategy_id), "description": s.description, "status": s.status.value,
            "entry_conditions": s.entry_conditions, "exit_conditions": s.exit_conditions}
        cycle = None
        run_id = rec.hypothesis.run_id
        if run_id and ctx.cycle_log is not None:
            row = ctx.cycle_log.get(run_id)
            if row is not None:
                decision = next((d for d in row["decisions"] if d.get("symbol") == rec.hypothesis.symbol), None)
                cycle = {"run_id": row["run_id"], "ts_ms": row["ts_ms"], "ts_at": iso(row["ts_ms"]),
                         "status": row["status"], "detail": row["detail"], "decision": decision,
                         "has_report": row["report"] is not None,
                         "analysis": ((row["report"] or {}).get("symbols") or {}).get(rec.hypothesis.symbol)}
        out["cycle"] = cycle
        out["cycle_url"] = None if not run_id else f"/api/cycles/{run_id}"
        return out

    # ---------------------------------------------------------------- estrategias

    @app.get("/api/strategies")
    async def strategies() -> dict[str, Any]:
        _require_eval()
        ts, stats = _strategy_stats()
        items = [strategy_json(s, stats.get(str(s.strategy_id).lower())) for s in ctx.registry.get_all()]
        return {"total": len(items), "items": items, "stats_age_s": round(time.time() - ts, 1)}

    @app.get("/api/strategies/{strategy_id}")
    async def strategy_detail(
        strategy_id: UUID,
        limit: int = Query(50, ge=1, le=MAX_PAGE, description="hipotesis a incluir (mas nuevas primero)"),
    ) -> dict[str, Any]:
        _require_eval()
        s = ctx.registry.get(strategy_id)
        if s is None:
            raise HTTPException(status_code=404, detail=f"estrategia {strategy_id} no esta registrada")
        stats = ctx.store.stats_by_strategy().get(str(strategy_id).lower())
        out = strategy_json(s, stats)
        out["entry_conditions"] = s.entry_conditions
        out["exit_conditions"] = s.exit_conditions
        out["status_history"] = [{"status": st.value, "changed_at": t.isoformat(), "changed_ms": to_ms(t)}
                                 for st, t in ctx.registry.status_history(strategy_id)]
        out["calibration"] = calibration(ctx.store.calibration_pairs(strategy_id))
        total, recs = ctx.store.query(strategy_id=strategy_id, limit=limit)
        now = datetime.now(timezone.utc)
        out["hypotheses_page"] = page(total, limit, 0, [hypothesis_json(r, now) for r in recs],
                                      next=f"/api/hypotheses?strategy_id={strategy_id}&offset={limit}"
                                      if total > limit else None)
        return out

    # ---------------------------------------------------------------- ciclos

    @app.get("/api/cycles")
    async def cycles(
        status: Literal["warmup", "no_strategies", "no_llm", "ran", "error"] | None = None,
        limit: int = Query(50, ge=1, le=MAX_PAGE),
        offset: int = Query(0, ge=0, le=MAX_OFFSET),
    ) -> dict[str, Any]:
        if ctx.cycle_log is None:
            raise HTTPException(status_code=503, detail="historial de ciclos no disponible (analisis apagado)")
        total, rows = ctx.cycle_log.page(status=status, limit=limit, offset=offset)
        return page(total, limit, offset, [cycle_json(r) for r in rows], counts=ctx.cycle_log.counts())

    _checkpoint_cache: dict[str, dict[str, Any] | None] = {}

    async def _from_checkpoint(run_id: str) -> dict[str, Any] | None:
        if ctx.checkpoint_db is None:
            return None
        if run_id in _checkpoint_cache:  # un checkpoint final no cambia: se cachea (acotado)
            return _checkpoint_cache[run_id]
        try:
            found = await asyncio.wait_for(
                asyncio.to_thread(checkpoint_report_sync, Path(ctx.checkpoint_db), run_id), CHECKPOINT_TIMEOUT_S)
        except Exception as exc:  # noqa: BLE001 - el respaldo nunca tumba el endpoint: se informa
            return {"error": f"{type(exc).__name__}: {exc}"[:300]}
        if len(_checkpoint_cache) > 64:
            _checkpoint_cache.clear()
        _checkpoint_cache[run_id] = found
        return found

    @app.get("/api/cycles/{run_id}")
    async def cycle_detail(run_id: str = PathParam(..., max_length=120)) -> dict[str, Any]:
        if not RUN_ID.match(run_id):
            raise HTTPException(status_code=400, detail=f"run_id invalido: {run_id!r}")
        row = None if ctx.cycle_log is None else ctx.cycle_log.get(run_id)
        source = None
        notes: list[str] = []
        if row is not None and row["report"] is not None:
            source = "cycle_log"
        elif row is None or row["status"] == "ran":
            ck = await _from_checkpoint(run_id)
            if ck is not None and "error" in ck:
                notes.append(f"no se pudo leer el checkpoint: {ck['error']}")
            elif ck is not None:
                source = "checkpoint"
                if row is None:
                    from ..pipeline.cycle_log import ts_from_run_id

                    row = {"run_id": run_id, "ts_ms": ts_from_run_id(run_id), "status": "ran",
                           "n_hypotheses": ck["n_hypotheses"], "detail": "(solo en checkpoints)",
                           "decisions": ck["decisions"]}
                row = {**row, "report": ck["report"]}
        if row is None:
            raise HTTPException(status_code=404, detail=f"ciclo {run_id} no existe")
        if source is None and row["status"] != "ran":
            notes.append(f"estado {row['status']}: el grafo no corrio, no hay razonamiento")
        elif source is None:
            notes.append("ciclo sin reporte guardado ni checkpoint disponible")
        hyps: list[dict[str, Any]] = []
        if ctx.store is not None:
            _, recs = ctx.store.query(run_id=run_id, limit=MAX_PAGE, newest_first=False)
            hyps = [hypothesis_json(r) for r in recs]
        return {**cycle_json(row), "report_source": source, "hypotheses": hyps, "notes": notes}

    @app.get("/api/analysis/latest")
    async def analysis_latest() -> dict[str, Any]:
        per = _per_symbol_view(ctx)
        last_ran = None if ctx.analysis is None else _cycle_view(ctx.analysis, ran=True)
        return {"symbols": {s: {**v, "ts_at": iso(v.get("ts_ms"))} for s, v in per.items()},
                "last_ran": None if last_ran is None else {k: last_ran[k] for k in
                                                           ("run_id", "status", "n_hypotheses", "detail", "decisions")}}

    # ---------------------------------------------------------------- noticias

    @app.get("/api/news")
    async def news(
        symbol: str | None = Query(None, max_length=30, description="activo base: XRP, XRPUSDT o XRP-USDT"),
        source: str | None = Query(None, max_length=60),
        primary: bool | None = None,
        q: str | None = Query(None, min_length=2, max_length=100, description="texto en titulo o resumen"),
        since: str | None = Query(None, description="published >= since"),
        until: str | None = Query(None, description="published < until"),
        limit: int = Query(50, ge=1, le=MAX_NEWS_PAGE),
        offset: int = Query(0, ge=0, le=10_000),
    ) -> dict[str, Any]:
        if ctx.history is None and ctx.news is None:
            raise HTTPException(status_code=503, detail="noticias apagadas y sin base historica")
        a, b = time_range(since, until)
        asset = base_asset(symbol) if symbol else None

        def match(it: dict[str, Any]) -> bool:
            if asset is not None and asset not in it["symbols"]:
                return False
            if source is not None and it["source"] != source:
                return False
            if primary is not None and bool(it["primary"]) != primary:
                return False
            if q is not None and q.lower() not in f"{it['title']} {it['summary']}".lower():
                return False
            if a is not None and it["published_ms"] < a:
                return False
            return b is None or it["published_ms"] < b

        window = [d for d in (_news_from_model(it) for it in (ctx.news.items() if ctx.news else [])) if match(d)]

        if ctx.history is None:
            window.sort(key=lambda d: (-d["published_ms"], d["item_id"]))
            return page(len(window), limit, offset, [news_json(d, False) for d in window[offset:offset + limit]],
                        sources={"db": 0, "window_only": len(window)})

        where, params = [], []
        if asset is not None:
            where.append("(',' || symbols || ',') LIKE ?")
            params.append(f"%,{asset},%")
        if source is not None:
            where.append("source = ?")
            params.append(source)
        if primary is not None:
            where.append("is_primary = ?")
            params.append(int(primary))
        if q is not None:
            where.append("(title LIKE ? ESCAPE '\\' OR summary LIKE ? ESCAPE '\\')")
            params += [f"%{_escape_like(q)}%"] * 2
        if a is not None:
            where.append("published_ms >= ?")
            params.append(a)
        if b is not None:
            where.append("published_ms < ?")
            params.append(b)
        clause = f"WHERE {' AND '.join(where)}" if where else ""
        cols = "item_id, source, is_primary, title, summary, url, published_ms, fetched_ms, symbols, market_wide"
        ids = [d["item_id"] for d in window]

        def work(conn: Any) -> tuple[int, list[tuple], set[str], int]:
            total = int(conn.execute(f"SELECT COUNT(*) FROM news {clause}", params).fetchone()[0])
            stored: set[str] = set()
            for i in range(0, len(ids), 500):
                chunk = ids[i:i + 500]
                stored |= {r[0] for r in conn.execute(
                    f"SELECT item_id FROM news WHERE item_id IN ({','.join('?' * len(chunk))})", chunk)}
            extras = len(ids) - len(stored)
            # con items solo-en-memoria hay que mezclar: se trae desde 0 hasta offset+limit y se corta en Python
            lim, off = (offset + limit, 0) if extras else (limit, offset)
            rows = conn.execute(f"SELECT {cols} FROM news {clause} ORDER BY published_ms DESC, item_id LIMIT ? OFFSET ?",
                                [*params, lim, off]).fetchall()
            return total, rows, stored, extras

        total, rows, stored, extras = await asyncio.to_thread(ro_query, _require_history(), work)
        items = [news_json(_news_from_row(r), True) for r in rows]
        if extras:
            items += [news_json(d, False) for d in window if d["item_id"] not in stored]
            items.sort(key=lambda d: (-d["published_ms"], d["item_id"]))
            items = items[offset:offset + limit]
        return page(total + extras, limit, offset, items, sources={"db": total, "window_only": extras})

    # ---------------------------------------------------------------- historia

    @app.get("/api/history/summary")
    async def history_summary() -> dict[str, Any]:
        _require_history()
        view = await _history_view(ctx)
        upload = ctx.history.store.path.parent / "last_upload.json"
        try:
            last_upload = json.loads(upload.read_text()) if upload.is_file() else None
        except (OSError, ValueError):
            last_upload = {"error": "last_upload.json ilegible"}
        if isinstance(last_upload, dict) and "ts_ms" in last_upload:
            last_upload["ts_at"] = iso(last_upload["ts_ms"])
        return {**view, "last_upload": last_upload}

    @app.get("/api/history/bars")
    async def history_bars(
        symbol: str = Query(..., max_length=30),
        exchange: Literal["binance", "bybit", "okx", "hyperliquid"] | None = None,
        tf: Literal["1m", "5m", "15m", "1h"] = "1m",
        since: str | None = Query(None, description=f"default: until - {DEFAULT_BARS} barras"),
        until: str | None = Query(None, description="default: ahora"),
        combine: bool = Query(False, description="sumar exchanges en una sola serie (exchange='all')"),
    ) -> dict[str, Any]:
        path = _require_history()
        sym = internal_symbol(symbol)
        tf_ms = TF_MS[tf]
        a, b = time_range(since, until)
        b = now_ms() if b is None else b
        a = b - DEFAULT_BARS * tf_ms if a is None else a
        if a >= b:
            raise HTTPException(status_code=400, detail="since debe ser anterior a until")
        a -= a % tf_ms  # barras completas: arranca en el borde de la temporalidad
        n_bars = math.ceil((b - a) / tf_ms)
        if n_bars > MAX_BARS:
            raise HTTPException(status_code=400, detail=(
                f"rango demasiado grande: {n_bars} barras de {tf} (maximo {MAX_BARS}); "
                "acortar since/until o usar una temporalidad mayor"))
        rows = await asyncio.to_thread(ro_query, path, lambda c: query_bars(c, sym, exchange, a, b, tf_ms))
        bars = [bar_json(r) for r in rows]
        exchanges = sorted({x["exchange"] for x in bars})
        if combine:
            bars = combine_bars(bars)
        return {"symbol": sym, "tf": tf, "tf_ms": tf_ms, "since_ms": a, "since_at": iso(a), "until_ms": b,
                "until_at": iso(b), "exchange": exchange, "combined": combine, "exchanges": exchanges,
                "count": len(bars), "bars": bars}

    @app.get("/api/history/liquidations")
    async def history_liquidations(
        symbol: str | None = Query(None, max_length=30),
        exchange: Literal["binance", "bybit", "okx", "hyperliquid"] | None = None,
        side: Literal["long", "short"] | None = Query(None, description="posicion liquidada"),
        since: str | None = None,
        until: str | None = None,
        min_usd: float = Query(0.0, ge=0),
        limit: int = Query(200, ge=1, le=MAX_LIQ_PAGE),
        offset: int = Query(0, ge=0, le=MAX_OFFSET),
    ) -> dict[str, Any]:
        path = _require_history()
        a, b = time_range(since, until)
        where, params = [], []
        if symbol:
            where.append("symbol = ?")
            params.append(internal_symbol(symbol))
        if exchange:
            where.append("exchange = ?")
            params.append(exchange)
        if side:  # la orden de liquidacion de un long es SELL
            where.append("side = ?")
            params.append("sell" if side == "long" else "buy")
        if min_usd > 0:
            where.append("notional_usd >= ?")
            params.append(min_usd)
        if a is not None:
            where.append("ts_ms >= ?")
            params.append(a)
        if b is not None:
            where.append("ts_ms < ?")
            params.append(b)
        clause = f"WHERE {' AND '.join(where)}" if where else ""

        def work(conn: Any) -> tuple[tuple, list[tuple]]:
            agg = conn.execute(
                "SELECT COUNT(*), SUM(CASE WHEN side = 'sell' THEN notional_usd ELSE 0 END), "
                f"SUM(CASE WHEN side = 'buy' THEN notional_usd ELSE 0 END) FROM liquidations {clause}", params).fetchone()
            rows = conn.execute(
                "SELECT exchange, symbol, ts_ms, side, price, qty, notional_usd, coverage FROM liquidations "
                f"{clause} ORDER BY ts_ms DESC LIMIT ? OFFSET ?", [*params, limit, offset]).fetchall()
            return agg, rows

        agg, rows = await asyncio.to_thread(ro_query, path, work)
        items = [{"exchange": e, "symbol": s, "ts_ms": t, "ts_at": iso(t), "side": sd,
                  "position": "long" if sd == "sell" else "short", "price": p, "qty": qty, "notional_usd": n,
                  "coverage": cov} for e, s, t, sd, p, qty, n, cov in rows]
        return page(int(agg[0]), limit, offset, items,
                    totals={"n": int(agg[0]), "long_usd": agg[1] or 0.0, "short_usd": agg[2] or 0.0})

    # ---------------------------------------------------------------- acciones: analizar ahora

    analyze_jobs: dict[str, dict[str, Any]] = {}
    analyze_last: dict[str, float] = {}  # simbolo -> momento del ultimo pedido aceptado
    analyze_tasks: set[asyncio.Task] = set()

    def _analyze_view(job: dict[str, Any]) -> dict[str, Any]:
        return {k: job.get(k) for k in ("run_id", "symbol", "status", "detail", "n_hypotheses", "queued_at",
                                         "queued_ms", "started_ms", "finished_ms")} | {
            "cycle_url": f"/api/cycles/{job['run_id']}" if job["status"] not in ("queued", "running") else None}

    async def _run_analyze(job: dict[str, Any]) -> None:
        job["status"], job["started_ms"] = "running", now_ms()
        try:
            res = await ctx.analysis.run_symbols([job["symbol"]], reason="manual", run_id=job["run_id"])
            job.update(status=res.status, detail=res.detail, n_hypotheses=res.n_hypotheses)
        except Exception as exc:  # noqa: BLE001 - el estado del trabajo lo informa
            job.update(status="error", detail=f"{type(exc).__name__}: {exc}"[:500])
        job["finished_ms"] = now_ms()

    @app.post("/api/analyze", status_code=202)
    async def analyze(request: Request) -> Any:
        body = await action_body(request, AnalyzeBody)
        if ctx.analysis is None or not ctx.analysis.graph_enabled:
            raise HTTPException(status_code=503, detail="el grafo LLM esta deshabilitado (sin API key o analisis apagado)")
        sym = internal_symbol(body.symbol)
        if sym not in ctx.analysis.symbols:
            raise HTTPException(status_code=400, detail=f"simbolo no configurado: {body.symbol!r}")
        busy = [j for j in analyze_jobs.values() if j["status"] in ("queued", "running")]
        if busy:
            return JSONResponse(status_code=429, headers={"Retry-After": str(ANALYZE_BUSY_RETRY_S)}, content={
                "detail": f"ya hay un analisis manual en curso ({busy[0]['symbol']})",
                "retry_after_s": ANALYZE_BUSY_RETRY_S, "run_id": busy[0]["run_id"]})
        wait = ANALYZE_COOLDOWN_S - (time.time() - analyze_last.get(sym, -1e18))
        if wait > 0:
            return JSONResponse(status_code=429, headers={"Retry-After": str(math.ceil(wait))}, content={
                "detail": f"{sym} ya se analizo a mano hace menos de {ANALYZE_COOLDOWN_S / 60:.0f} min",
                "retry_after_s": math.ceil(wait)})
        queued = now_ms()
        run_id = f"manual-{queued}"
        job = {"run_id": run_id, "symbol": sym, "status": "queued", "detail": None, "n_hypotheses": None,
               "queued_ms": queued, "queued_at": iso(queued), "started_ms": None, "finished_ms": None}
        analyze_jobs[run_id] = job
        analyze_last[sym] = time.time()
        while len(analyze_jobs) > MAX_ANALYZE_JOBS:
            analyze_jobs.pop(next(iter(analyze_jobs)))
        task = asyncio.create_task(_run_analyze(job), name=f"analyze-{sym}")
        analyze_tasks.add(task)  # referencia fuerte: si no, la tarea puede recolectarse a mitad de camino
        task.add_done_callback(analyze_tasks.discard)
        return {"run_id": run_id, "symbol": sym, "queued_at": job["queued_at"], "queued_ms": queued, "status": "queued"}

    @app.get("/api/analyze/{run_id}")
    async def analyze_status(run_id: str = PathParam(..., max_length=120)) -> dict[str, Any]:
        if not RUN_ID.match(run_id):
            raise HTTPException(status_code=400, detail=f"run_id invalido: {run_id!r}")
        job = analyze_jobs.get(run_id)
        if job is not None:
            return _analyze_view(job)
        row = None if ctx.cycle_log is None else ctx.cycle_log.get(run_id)  # tras un reinicio: del historial
        if row is None:
            raise HTTPException(status_code=404, detail=f"analisis {run_id} no existe")
        return {"run_id": run_id, "symbol": (row["decisions"][0]["symbol"] if row["decisions"] else None),
                "status": row["status"], "detail": row["detail"], "n_hypotheses": row["n_hypotheses"],
                "queued_at": None, "queued_ms": None, "started_ms": None, "finished_ms": row["ts_ms"],
                "cycle_url": f"/api/cycles/{run_id}"}

    # ---------------------------------------------------------------- acciones: cola del laboratorio
    # (se registran ANTES que /api/lab/{strategy_id}: si no, "requests" se tomaria como un strategy_id)

    def _require_queue() -> Any:
        if ctx.lab_requests is None:
            raise HTTPException(status_code=503, detail="cola del laboratorio no disponible")
        return ctx.lab_requests

    @app.post("/api/lab/requests", status_code=201)
    async def lab_request_create(request: Request) -> dict[str, Any]:
        body = await action_body(request, LabRequestBody)
        queue = _require_queue()
        lab_view = _lab_view(ctx)
        if not isinstance(lab_view, dict) or not isinstance(lab_view.get("strategies"), list):
            raise HTTPException(status_code=503, detail="no hay resumen del laboratorio publicado: no se pueden validar las estrategias")
        valid = sorted({str(s["strategy_id"]) for s in lab_view["strategies"] if s.get("strategy_id")})
        ids = list(dict.fromkeys(body.strategy_ids))
        if "*" in ids:
            if len(ids) > 1:
                raise HTTPException(status_code=400, detail='"*" va solo (todas las estrategias publicadas)')
            ids = valid
        unknown = [i for i in ids if i not in valid]
        if unknown:
            raise HTTPException(status_code=400, detail=f"estrategias desconocidas: {unknown} (validas: {valid})")
        configured = {binance_symbol(s) for s in ctx.config.get("symbols", [])}
        syms = list(dict.fromkeys(binance_symbol(s) for s in body.symbols))
        bad = [s for s in syms if s not in configured]
        if bad:
            raise HTTPException(status_code=400, detail=f"simbolos no configurados: {bad}")
        if queue.count("pending") >= MAX_PENDING_LAB_REQUESTS:
            return JSONResponse(status_code=429, content={
                "detail": f"ya hay {MAX_PENDING_LAB_REQUESTS} solicitudes pendientes: esperar a que el worker las procese",
                "retry_after_s": 300})
        return lab_request_json(queue.create(ids, syms, body.note.strip()))

    @app.get("/api/lab/requests")
    async def lab_requests_list(
        status: Literal["pending", "running", "done", "error"] | None = None,
        limit: int = Query(50, ge=1, le=200),
        offset: int = Query(0, ge=0, le=MAX_OFFSET),
    ) -> dict[str, Any]:
        queue = _require_queue()
        total, items = queue.list(status=status, limit=limit, offset=offset)
        return page(total, limit, offset, [lab_request_json(r) for r in items],
                    pending=queue.count("pending"), max_pending=MAX_PENDING_LAB_REQUESTS)

    def _request_or_404(request_id: str) -> dict[str, Any]:
        if not LAB_REQUEST_ID.match(request_id):
            raise HTTPException(status_code=400, detail=f"request_id invalido: {request_id!r}")
        r = _require_queue().get(request_id)
        if r is None:
            raise HTTPException(status_code=404, detail=f"solicitud {request_id} no existe")
        return r

    @app.get("/api/lab/requests/{request_id}")
    async def lab_request_detail(request_id: str = PathParam(..., max_length=64)) -> dict[str, Any]:
        r = _request_or_404(request_id)
        return lab_request_json(r, ctx.lab_requests.events(request_id))

    @app.get("/api/lab/requests/{request_id}/result")
    async def lab_request_result(request_id: str = PathParam(..., max_length=64)) -> Any:
        r = _request_or_404(request_id)
        path = ctx.lab_requests.results_dir / f"{request_id}.json"
        if r["status"] != "done" or not path.is_file():
            raise HTTPException(status_code=404, detail=f"la solicitud esta {r['status']}: todavia no hay resultado")
        try:
            return json_safe(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            raise HTTPException(status_code=500, detail=f"{path.name} ilegible") from None

    # ---------------------------------------------------------------- laboratorio

    _lab_detail_cache: dict[str, tuple[float, Any]] = {}

    @app.get("/api/lab")
    async def lab() -> dict[str, Any]:
        view = _lab_view(ctx)
        if view is None:
            raise HTTPException(status_code=404, detail="no hay resumen del laboratorio publicado (lab.publish --upload)")
        return view

    @app.get("/api/lab/{strategy_id}")
    async def lab_detail(strategy_id: str = PathParam(..., max_length=100)) -> Any:
        if not LAB_ID.match(strategy_id):
            raise HTTPException(status_code=400, detail=f"strategy_id de laboratorio invalido: {strategy_id!r}")
        if ctx.lab_summary_path is None:
            raise HTTPException(status_code=404, detail="laboratorio no configurado")
        path = ctx.lab_summary_path.parent / f"lab_detail_{strategy_id}.json"
        if not path.is_file():
            raise HTTPException(status_code=404, detail=f"sin detalle publicado para {strategy_id}")
        mtime = path.stat().st_mtime
        hit = _lab_detail_cache.get(strategy_id)
        if hit is None or hit[0] != mtime:  # se relee solo si el archivo cambio
            try:
                hit = (mtime, json_safe(json.loads(path.read_text(encoding="utf-8"))))
            except (OSError, ValueError):
                raise HTTPException(status_code=500, detail=f"{path.name} ilegible") from None
            _lab_detail_cache[strategy_id] = hit
        return hit[1]

    # ---------------------------------------------------------------- mercado (ventana en memoria)

    @app.get("/api/market/{symbol}")
    async def market(
        symbol: str = PathParam(..., max_length=30),
        trades: int = Query(50, ge=0, le=500, description="ultimos trades a incluir"),
        liquidations: int = Query(50, ge=0, le=500, description="ultimas liquidaciones a incluir"),
    ) -> dict[str, Any]:
        if ctx.memory is None:
            raise HTTPException(status_code=503, detail="ventana en memoria no disponible (modo persistent)")
        sym = internal_symbol(symbol)
        if sym not in _configured_symbols() and sym not in ctx.memory.symbols():
            raise HTTPException(status_code=404, detail=f"simbolo no configurado: {symbol!r}")
        now = now_ms()

        # ticker por exchange: ultimo valor NO nulo de cada campo (los exchanges mandan campos por separado)
        fields = ("mark_price", "last_price", "funding_rate", "open_interest", "open_interest_usd")
        per_ex: dict[str, dict[str, Any]] = {}
        for ev in ctx.memory.tail("ticker", sym, 30_000):
            row = per_ex.setdefault(ev.source.value, {"ts_ms": ev.ts_ingest_ms})
            for f in fields:
                v = getattr(ev, f)
                if v is not None and f not in row:
                    row[f] = v
                    row[f + "_ts_ms"] = ev.ts_ingest_ms
        exchanges = {}
        for ex, row in sorted(per_ex.items()):
            oi_usd = row.get("open_interest_usd")
            if oi_usd is None and row.get("open_interest") is not None:
                ref = row.get("mark_price") or row.get("last_price")
                oi_usd = None if ref is None else row["open_interest"] * ref
            exchanges[ex] = {
                "mark": row.get("mark_price"), "last": row.get("last_price"), "funding_8h": row.get("funding_rate"),
                "open_interest": row.get("open_interest"), "oi_usd": oi_usd,
                "ts_ms": row["ts_ms"], "ts_at": iso(row["ts_ms"]), "age_s": round((now - row["ts_ms"]) / 1000, 1),
                "funding_age_s": None if "funding_rate_ts_ms" not in row else round((now - row["funding_rate_ts_ms"]) / 1000, 1),
            }
        marks = [e["mark"] for e in exchanges.values() if e["mark"] is not None]
        fundings = [e["funding_8h"] for e in exchanges.values() if e["funding_8h"] is not None]
        ois = [e["oi_usd"] for e in exchanges.values() if e["oi_usd"] is not None]

        # flujo taker de los ultimos 5 min por exchange (recorre desde el final hasta salir de la ventana)
        flow: dict[str, dict[str, float]] = {}
        cutoff = now - 5 * 60_000
        for ev in ctx.memory.tail("trade", sym, 200_000):
            if ev.ts_event_ms < cutoff:
                break
            f = flow.setdefault(ev.source.value, {"buy_usd": 0.0, "sell_usd": 0.0, "n": 0})
            usd = ev.price * ev.qty
            f["buy_usd" if ev.side.value == "buy" else "sell_usd"] += usd
            f["n"] += 1
        for f in flow.values():
            f["delta_usd"] = f["buy_usd"] - f["sell_usd"]

        # liquidaciones: totales 1 h y 24 h por exchange y lado (son pocas: se recorren todas)
        # orden por hora del evento (OKX manda liquidaciones tarde: la llegada no es el orden real)
        liqs = sorted(ctx.memory.tail("liquidation", sym, 200_000), key=lambda e: e.ts_event_ms, reverse=True)
        liq_tot: dict[str, dict[str, Any]] = {}
        for label, span in (("1h", 3_600_000), ("24h", 86_400_000)):
            t = {"long_usd": 0.0, "short_usd": 0.0, "n": 0, "by_exchange": {}}
            for ev in liqs:
                if ev.ts_event_ms < now - span:
                    continue
                k = "long_usd" if ev.side.value == "sell" else "short_usd"
                t[k] += ev.notional_usd
                t["n"] += 1
                e = t["by_exchange"].setdefault(ev.source.value, {"long_usd": 0.0, "short_usd": 0.0, "n": 0})
                e[k] += ev.notional_usd
                e["n"] += 1
            liq_tot[label] = t

        def trade_json(ev: Any) -> dict[str, Any]:
            return {"exchange": ev.source.value, "ts_ms": ev.ts_event_ms, "ts_at": iso(ev.ts_event_ms),
                    "side": ev.side.value, "price": ev.price, "qty": ev.qty, "usd": ev.price * ev.qty,
                    "block": ev.is_block_trade}

        def liq_json(ev: Any) -> dict[str, Any]:
            return {"exchange": ev.source.value, "ts_ms": ev.ts_event_ms, "ts_at": iso(ev.ts_event_ms),
                    "side": ev.side.value, "position": "long" if ev.side.value == "sell" else "short",
                    "price": ev.price, "qty": ev.qty, "notional_usd": ev.notional_usd, "coverage": ev.coverage.value}

        mem_counts = (ctx.memory.summary().get(sym) or {}).get("counts")
        per = _per_symbol_view(ctx).get(sym)
        open_h = None
        if ctx.store is not None:
            open_h, _ = ctx.store.query(symbol=sym, status=HypothesisStatus.PENDING, limit=1)
        return {
            "symbol": sym, "now_ms": now, "now_at": iso(now),
            "exchanges": exchanges,
            "mark_mean": sum(marks) / len(marks) if marks else None,
            "funding_8h_mean": sum(fundings) / len(fundings) if fundings else None,
            "oi_usd_total": sum(ois) if ois else None, "oi_exchanges": len(ois),
            "flow_5m": flow,
            "liquidations_window": liq_tot,
            "last_trades": [trade_json(ev) for ev in ctx.memory.tail("trade", sym, trades)],
            "last_liquidations": [liq_json(ev) for ev in liqs[:liquidations]],
            "window_counts": mem_counts,
            "open_hypotheses": open_h,
            "last_analysis": None if per is None else {**per, "ts_at": iso(per.get("ts_ms"))},
        }

    # ---------------------------------------------------------------- sistema

    @app.get("/api/system")
    async def system() -> dict[str, Any]:
        now = now_ms()
        dbs: dict[str, Any] = {name: _file_info(Path(p)) for name, p in ctx.db_paths.items()}
        if ctx.history is not None and "history" not in dbs:
            dbs["history"] = _file_info(Path(ctx.history.store.path))
        if ctx.checkpoint_db is not None and "checkpoints" not in dbs:
            dbs["checkpoints"] = _file_info(Path(ctx.checkpoint_db))
        if ctx.lab_summary_path is not None:
            dbs["lab_summary"] = _file_info(ctx.lab_summary_path)
            dbs["lab_details"] = len(list(ctx.lab_summary_path.parent.glob("lab_detail_*.json")))
        data_dir = next((Path(p).parent for p in ctx.db_paths.values()), None)
        if data_dir is None and ctx.history is not None:
            data_dir = Path(ctx.history.store.path).parent
        disk = None
        if data_dir is not None and data_dir.exists():
            import shutil

            du = shutil.disk_usage(data_dir)
            disk = {"path": str(data_dir), "total_bytes": du.total, "free_bytes": du.free}

        export = None
        if ctx.history is not None:
            hist = await _history_summary(ctx)
            upload = Path(ctx.history.store.path).parent / "last_upload.json"
            try:
                last_upload = json.loads(upload.read_text()) if upload.is_file() else None
            except (OSError, ValueError):
                last_upload = {"error": "last_upload.json ilegible"}
            if isinstance(last_upload, dict) and "ts_ms" in last_upload:
                last_upload["ts_at"] = iso(last_upload["ts_ms"])
            export = {"last_export_day": hist[1].get("last_export_day"), "export_days": hist[1].get("export_days"),
                      "last_upload": last_upload}

        errors: dict[str, Any] = {
            "cycles": [], "news_feeds": {}, "workers_down": [w["worker"] for w in ctx.health.snapshot() if not w["connected"]],
            "recorder_write_errors": None if ctx.history is None else ctx.history.stats.write_errors,
            "memory_evicted_by_cap": None if ctx.memory is None else ctx.memory.stats.evicted_by_cap,
        }
        if ctx.cycle_log is not None:
            _, rows = ctx.cycle_log.page(status="error", limit=10)
            errors["cycles"] = [{"run_id": r["run_id"], "ts_ms": r["ts_ms"], "ts_at": iso(r["ts_ms"]),
                                 "detail": r["detail"]} for r in rows]
        if ctx.news is not None:
            errors["news_feeds"] = {n: f["error"] for n, f in ctx.news.summary()["feeds"].items() if f["error"]}
        return {
            "build": build_id,
            "version": version,
            "python": sys.version.split()[0],
            "pid": os.getpid(),
            "rss_bytes": _rss_bytes(),
            "started_ms": int(ctx.started_at * 1000), "started_at": iso(ctx.started_at * 1000),
            "uptime_s": round(time.time() - ctx.started_at, 1),
            "now_ms": now, "now_at": iso(now),
            "config": ctx.config,
            "settings": ctx.settings,
            "databases": dbs,
            "disk": disk,
            "export": export,
            "errors": errors,
        }

