"""
Motor de alertas de oportunidad (proceso aparte, pandas; timer cada 5 min).

Una corrida en `now`:
 1. T = piso del minuto de `now`. Ventana a revisar: (fin de la ultima corrida ok, T], como mucho MAX_LOOKBACK
    (despues de un hueco no se inventan alertas viejas: se declara el hueco).
 2. Baja por REST las velas 1m que falten de los 7 simbolos (`history/klines.py`, incremental: un pedido por simbolo
    en regimen) y chequea que la ultima vela cerrada sea T - 1m (si no: corrida 'error', sin alertas).
 3. Carga velas 1m CERRADAS (apertura < T, con volumen) de los ultimos WINDOW_DAYS y el funding liquidado de
    `paperbot.sqlite` (si existe) y corre las MISMAS reglas del laboratorio (CATALOG, que incluye las generadas por
    bloques) con los parametros del bot paper si la estrategia esta ahi, o los de la spec.
 4. Cada cambio de objetivo con decision en la ventana -> alerta: entrada (0 -> +-x), salida (+-x -> 0) o giro (cambia
    el signo). Los cambios de tamano sin cambio de signo (rebalanceos) no alertan.
 5. Guarda corrida + alertas en una transaccion (`AlertStore.record_run`). Las de estrategias con automatico prendido
    quedan marcadas 'auto' para el ejecutor de la demo (fase 4).

Cobertura (v1): estrategias intradia (1m, 5min, 15min, 1h, 4h, 1h|4h) que solo necesitan velas y funding. Las
diarias, de calendario o de liquidacion y las que piden metrics 5m se listan como salteadas con el motivo.
Las alertas NO abren hipotesis ni operan: son aviso + cola para la demo.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import logging
import os
import sqlite3
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from ..history import klines as hk
from ..history.store import HistoryStore
from .store import AlertStore

log = logging.getLogger("heatsignal.alerts")

MIN_MS = 60_000
DAY_MS = 86_400_000
SYMBOLS = ["BTCUSDT", "SOLUSDT", "XRPUSDT", "ETHUSDT", "XLMUSDT", "BNBUSDT", "TRXUSDT"]
TF_MIN = {"1m": 1, "5min": 5, "15min": 15, "15m": 15, "1h": 60, "4h": 240, "1h|4h": 240}
WINDOW_DAYS = {1: 10, 5: 10, 15: 15, 60: 40, 240: 60}  # historia por temporalidad (indicadores + cadena de entradas)
MAX_LOOKBACK_MS = 60 * MIN_MS
DATA_NEEDS = {"klines", "funding"}


@dataclass
class AlertConfig:
    alerts_db: Path
    history_db: Path
    paperbot_db: Path | None = None
    lab_summary: Path | None = None
    params_path: Path | None = None
    symbols: list[str] = field(default_factory=lambda: list(SYMBOLS))
    download: bool = True
    catalog: dict[str, Any] | None = None  # inyectable en tests


def default_config() -> AlertConfig:
    data = Path(os.environ.get("HEATSIGNAL_DATA_DIR", "~/heatsignal/data")).expanduser()
    from ..paperbot.params import PARAMS_PATH

    return AlertConfig(
        alerts_db=Path(os.environ.get("HEATSIGNAL_ALERTS_DB", data / "alerts.sqlite")).expanduser(),
        history_db=Path(os.environ.get("HEATSIGNAL_HISTORY_DB", data / "heatsignal_history.sqlite")).expanduser(),
        paperbot_db=Path(os.environ.get("HEATSIGNAL_PAPERBOT_DB", data / "paperbot.sqlite")).expanduser(),
        lab_summary=data / "lab_summary.json", params_path=PARAMS_PATH)


# ---- elegibilidad y contexto --------------------------------------------------------------------------------


def eligible(catalog: dict[str, Any]) -> tuple[list[tuple[str, Any, Any, int]], dict[str, str]]:
    ok, skipped = [], {}
    for sid, (spec, rule) in catalog.items():
        if not spec.backtestable:
            skipped[sid] = "no se puede ejecutar (le faltan datos que no guardamos)"
        elif set(spec.needs) - DATA_NEEDS:
            skipped[sid] = f"necesita {sorted(set(spec.needs) - DATA_NEEDS)} (metrics 5m: todavia no en vivo)"
        elif spec.timeframe not in TF_MIN:
            skipped[sid] = f"temporalidad {spec.timeframe}: diarias / calendario / liquidacion en la proxima version"
        else:
            ok.append((sid, spec, rule, TF_MIN[spec.timeframe]))
    return ok, skipped


def _read_json(path: Path | None) -> Any:
    if path is None or not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def lab_index(path: Path | None) -> dict[str, dict[str, Any]]:
    d = _read_json(path)
    rows = (d or {}).get("strategies") if isinstance(d, dict) else None
    return {r.get("strategy_id"): r for r in rows or [] if isinstance(r, dict)}


def paper_index(path: Path | None) -> dict[str, dict[str, Any]]:
    d = _read_json(path)
    return (d or {}).get("strategies") or {} if isinstance(d, dict) else {}


def lab_status(r: dict[str, Any] | None) -> str:
    if r is None:
        return "sin probar"
    return "pasa" if (r.get("verdict") or {}).get("pasa") else "no pasa"


def universe_for(spec: Any, paper: dict[str, Any] | None, lab: dict[str, Any] | None, avail: list[str]) -> list[str]:
    syms = (paper or {}).get("universe") or (spec.params or {}).get("symbols") or (lab or {}).get("symbols") or SYMBOLS
    return [s for s in avail if s in set(syms)]


# ---- datos ----------------------------------------------------------------------------------------------------


def load_klines(hist: HistoryStore, symbol: str, start_ms: int, end_ms: int) -> pd.DataFrame:
    """Velas 1m [start, end) con las columnas del laboratorio (`lab/store.load_klines`): OHLC, volumen, volumen en USDT,
    trades y compra taker en USDT (las reglas de flujo las usan)."""
    rows = hist.query("SELECT open_time_ms, open, high, low, close, volume, quote_volume, n_trades, taker_buy_quote "
                      "FROM klines WHERE exchange=? AND symbol=? AND interval='1m' AND open_time_ms >= ? AND "
                      "open_time_ms < ? ORDER BY open_time_ms", (hk.EXCHANGE, hk.internal_symbol(symbol), start_ms, end_ms))
    arr = np.array([[np.nan if v is None else v for v in r] for r in rows], dtype=np.float64).reshape(-1, 9)
    idx = pd.DatetimeIndex(pd.to_datetime(arr[:, 0].astype(np.int64), unit="ms", utc=True), name="time")
    return pd.DataFrame(arr[:, 1:], index=idx, columns=["open", "high", "low", "close", "volume", "quote_volume",
                                                         "count", "taker_buy_quote_volume"])


def load_funding(paperbot_db: Path | None, symbol: str, start_ms: int, end_ms: int) -> pd.Series | None:
    if paperbot_db is None or not paperbot_db.is_file():
        return None
    try:
        conn = sqlite3.connect(f"file:{paperbot_db}?mode=ro", uri=True, timeout=5)
        try:
            rows = conn.execute("SELECT funding_ms, rate FROM funding_rates WHERE symbol=? AND funding_ms >= ? AND "
                                "funding_ms <= ? ORDER BY funding_ms", (symbol, start_ms, end_ms)).fetchall()
        finally:
            conn.close()
    except sqlite3.Error:
        return None
    return pd.Series([r[1] for r in rows], index=pd.to_datetime([r[0] for r in rows], unit="ms", utc=True),
                     dtype=float, name="rate")


# ---- deteccion ------------------------------------------------------------------------------------------------


def _clean(sig: pd.Series | None) -> pd.Series:
    if sig is None or not len(sig):
        return pd.Series(dtype=float)
    s = sig.dropna().sort_index(kind="stable")
    return s[~s.index.duplicated(keep="last")]


def changes_in_window(sig: pd.Series | None, from_ms: int, to_ms: int) -> list[tuple[int, float, float]]:
    """[(decision_ms, objetivo_anterior, objetivo_nuevo)] con decision en (from, to] y cambio de signo / de 0."""
    s = _clean(sig)
    if s.empty:
        return []
    ms = _ms(s.index)
    vals = s.to_numpy(dtype=float)
    out = []
    prev = 0.0
    for t, v in zip(ms, vals):
        if t > to_ms:
            break
        if t > from_ms and np.sign(v) != np.sign(prev):
            out.append((int(t), float(prev), float(v)))
        prev = v
    return out


def _kind(prev: float, new: float) -> str:
    if prev == 0:
        return "entrada"
    if new == 0:
        return "salida"
    return "giro"


def alert_id(strategy_id: str, symbol: str, decision_ms: int, kind: str) -> str:
    return hashlib.sha1(f"{strategy_id}|{symbol}|{decision_ms}|{kind}".encode()).hexdigest()[:16]


def _ms(index: pd.Index) -> np.ndarray:
    """Epoch ms de un indice de tiempo sin importar su resolucion (pandas 3 usa ms o us, no siempre ns)."""
    return pd.DatetimeIndex(index).as_unit("ns").asi8 // 1_000_000


def _price_at(k: pd.DataFrame, t_ms: int) -> float | None:
    ms = _ms(k.index)
    j = int(np.searchsorted(ms, t_ms, side="left")) - 1  # ultima vela con apertura < t: precio conocido en t
    return float(k["close"].iloc[j]) if j >= 0 else None


# ---- corrida ---------------------------------------------------------------------------------------------------


def run_once(cfg: AlertConfig, *, now_ms: int | None = None, http_get: Any = None) -> dict[str, Any]:
    now = now_ms or int(time.time() * 1000)
    T = now - now % MIN_MS
    run_id = f"alerts-{T}"
    t0 = time.time()
    lock = open(cfg.alerts_db.with_suffix(".lock"), "w")  # noqa: SIM115 - se libera al final
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return {"run_id": run_id, "status": "skipped", "reason": "otra corrida en curso"}
    store = AlertStore(cfg.alerts_db)
    try:
        last = store.last_ok_window_end()
        start_win = T - 5 * MIN_MS if last is None else last
        detail: dict[str, Any] = {}
        if T - start_win > MAX_LOOKBACK_MS:
            detail["hueco"] = (f"la ultima corrida ok termino hace {(T - start_win) // MIN_MS} min: solo se revisan los "
                               f"ultimos {MAX_LOOKBACK_MS // MIN_MS} min (no se inventan alertas viejas)")
            start_win = T - MAX_LOOKBACK_MS
        if start_win >= T:
            store.record_run(run_id, "skipped", window=(start_win, T), detail={"motivo": "ventana vacia"})
            return {"run_id": run_id, "status": "skipped", "reason": "ventana vacia"}
        try:
            out = _evaluate(cfg, store, T, start_win, detail, http_get)
        except Exception as exc:  # noqa: BLE001 - sin fallos silenciosos: la corrida queda 'error' con el motivo
            log.exception("alertas: la corrida fallo")
            detail["error"] = f"{type(exc).__name__}: {exc}"
            detail["traceback"] = traceback.format_exc()[-2000:]
            store.record_run(run_id, "error", window=(start_win, T), duration_s=time.time() - t0, detail=detail)
            return {"run_id": run_id, "status": "error", "error": detail["error"]}
        added = store.record_run(run_id, "ok", window=(start_win, T), n_eval=out["n_eval"], alerts=out["alerts"],
                                 duration_s=time.time() - t0, detail=detail)
        return {"run_id": run_id, "status": "ok", "window": [start_win, T], "n_eval": out["n_eval"],
                "alerts": added, "duration_s": round(time.time() - t0, 1), "errores": detail.get("errores", {})}
    finally:
        store.close()
        lock.close()


def _evaluate(cfg: AlertConfig, store: AlertStore, T: int, start_win: int, detail: dict[str, Any],
              http_get: Any) -> dict[str, Any]:
    from ..lab.exits import ficha_json
    from ..lab.strategies import CATALOG, MarketData

    catalog = cfg.catalog if cfg.catalog is not None else CATALOG
    todo, skipped = eligible(catalog)
    detail["salteadas"] = len(skipped)
    detail["salteadas_por_motivo"] = _count_reasons(skipped)
    max_days = max((WINDOW_DAYS[min(WINDOW_DAYS, key=lambda m: abs(m - tf))] for *_, tf in todo), default=10)
    hist = HistoryStore(cfg.history_db)
    try:
        if cfg.download:
            added = {}
            for s in cfg.symbols:
                try:
                    added[s] = hk.download(hist, s, T - (max_days + 1) * DAY_MS, http_get=http_get)
                except Exception as exc:  # noqa: BLE001
                    raise RuntimeError(f"no se pudieron bajar las velas de {s}: {exc}") from exc
            detail["velas_nuevas"] = added
        full = {s: load_klines(hist, s, T - max_days * DAY_MS, T) for s in cfg.symbols}
    finally:
        hist.close()
    stale = {s: (None if k.empty else int(k.index[-1].value // 1_000_000)) for s, k in full.items()}
    late = {s: v for s, v in stale.items() if v is None or v < T - 2 * MIN_MS}
    if late:
        raise RuntimeError("velas atrasadas: " + "; ".join(
            f"{s}: ultima {'ninguna' if v is None else pd.Timestamp(v, unit='ms', tz='UTC').isoformat()}" for s, v in late.items()))
    funding = {s: load_funding(cfg.paperbot_db, s, T - max_days * DAY_MS, T) for s in cfg.symbols}
    labs, papers = lab_index(cfg.lab_summary), paper_index(cfg.params_path)
    alerts: list[dict[str, Any]] = []
    errors: dict[str, str] = {}
    n_eval = 0
    for sid, spec, rule, tf in todo:
        days = WINDOW_DAYS[min(WINDOW_DAYS, key=lambda m: abs(m - tf))]
        paper, lab = papers.get(sid), labs.get(sid)
        universe = universe_for(spec, paper, lab, cfg.symbols)
        if not universe:
            continue
        cut = pd.Timestamp(T - days * DAY_MS, unit="ms", tz="UTC")
        data = {s: MarketData(full[s].loc[full[s].index >= cut], funding[s]) for s in universe}
        params = {**(spec.params or {}), **((paper or {}).get("params") or {})}
        try:
            sigs = rule(data, params)
        except Exception as exc:  # noqa: BLE001 - una estrategia rota no frena a las demas (queda en el detalle)
            errors[sid] = f"{type(exc).__name__}: {exc}"[:300]
            continue
        n_eval += 1
        ficha = ficha_json(sid)
        for sym in universe:
            for t, prev, new in changes_in_window(sigs.get(sym), start_win, T):
                kind = _kind(prev, new)
                alerts.append({
                    "alert_id": alert_id(sid, sym, t, kind), "run_id": f"alerts-{T}", "created_ms": T, "decision_ms": t,
                    "strategy_id": sid, "symbol": sym, "kind": kind,
                    "direction": "long" if new > 0 else "short" if new < 0 else "flat", "target": new, "prev_target": prev,
                    "price": _price_at(full[sym], t), "timeframe": spec.timeframe,
                    "source": "generada" if sid.startswith("gen-") else "catalogo", "lab_status": lab_status(lab),
                    "lab_sharpe": (lab or {}).get("sharpe"), "paper_role": (paper or {}).get("rol"),
                    "exit_plan": (ficha or {}).get("resumen")})
    if errors:
        detail["errores"] = errors
    return {"n_eval": n_eval, "alerts": alerts}


def _count_reasons(skipped: dict[str, str]) -> dict[str, int]:
    out: dict[str, int] = {}
    for r in skipped.values():
        key = r.split(":")[0]
        out[key] = out.get(key, 0) + 1
    return out
