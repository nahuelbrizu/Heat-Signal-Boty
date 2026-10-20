"""
Corrida horaria del bot de paper trading (proceso aparte; aca SI se usa pandas/numpy).

Una corrida en el instante `now` (el timer la dispara a los :02 UTC):

 1. H = piso de la hora de `now`. Si ya hay una corrida 'ok' para H -> se registra 'skipped' y no se opera.
 2. Actualiza las velas 1m de los 7 simbolos en la base historica (`history/klines.py`, incremental) y el funding
    liquidado en la cache propia (`funding.py`).
 3. Chequea los datos (sin fallos silenciosos): ultima vela = H - 1m, historia completa desde H - WINDOW_DAYS,
    0 minutos faltantes en las ultimas FRESH_HOURS h, <= MAX_MISSING_FRAC faltantes en la ventana, funding de cada
    simbolo con <= FUNDING_MAX_AGE_H h. Si algo falla: corrida 'error' con el motivo y NO se opera.
 4. Arma `MarketData` como el laboratorio (velas 1m indexadas por APERTURA, solo velas CERRADAS en H: apertura < H;
    funding liquidado hasta H) y llama a las MISMAS reglas de `lab/strategies.py` (CATALOG) con los parametros de
    `params.json`.
 5. Ejecucion simulada (convenciones de `lab/sim.py`):
      - corrida consecutiva (la anterior ok fue H - 1h): cada cambio de objetivo con decision t en (H - 1h, H] se
        llena al OPEN de la primera vela 1m con apertura >= t (si varias decisiones caen en la misma vela, vale la
        ultima); si esa vela todavia no existe, al ultimo precio disponible (`price_source=ultimo_precio`). Si al
        final la posicion no coincide con el objetivo de la regla en H (la ventana movil puede cambiar el estado de
        reglas con memoria), se re-sincroniza en H (`resincronizacion`).
      - primera corrida o despues de un hueco (VM apagada, corridas con error): NO se simulan las horas perdidas; se
        registra el hueco y la posicion se lleva al objetivo de la regla en H (`inicio` / `hueco`).
      - el modo se decide POR ESTRATEGIA: una estrategia que se suma a `params.json` despues (p. ej.
        max-ruptura-10d-v1 el 2026-10-19) entra "desde hoy": su primera corrida es `inicio` (orden al objetivo de la
        regla en H, capital 10.000 y canasta propios desde H) aunque las demas sigan en `consecutiva`. No se reescribe
        ni se recalcula nada de las otras.
      - costos 5 + 2 bps por lado; funding cobrado/pagado en cada liquidacion con la posicion vigente justo antes.
      - cada orden que cierra o da vuelta una posicion queda con su motivo en `exit_reasons` (stop / objetivo /
        tiempo / senal / rebalanceo, de `lab/exits.py`; o el motivo de la orden si no la pidio la regla).
 6. Escribe todo en `paperbot.sqlite` en UNA transaccion (append-only).
"""

from __future__ import annotations

import fcntl
import json
import logging
import resource
import time
import traceback
from collections.abc import Callable
from contextlib import nullcontext
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from ..evaluation.price_source import HttpGet
from ..history import klines as hk
from ..history.store import HistoryStore
from ..lab.exits import capture_exits, reason_map
from . import params as P
from .daily import build_daily, daily_source, history_days, update_daily_cache
from .funding import update_funding
from .ledger import CAPITAL, EPS, FEE_BPS, SLIPPAGE_BPS, Book
from .store import HOUR_MS, PaperbotStore

log = logging.getLogger("heatsignal.paperbot")

MIN_MS = 60_000
DAY_MS = 86_400_000
SYMBOLS = ["BTCUSDT", "SOLUSDT", "XRPUSDT", "ETHUSDT", "XLMUSDT", "BNBUSDT", "TRXUSDT"]
WINDOW_DAYS = 64          # reversion-xs-1h necesita 1440 velas 1h (30 d beta + 30 d sigma) + margen
FUNDING_DAYS = 100        # funding-prepago: p90/p10 de las 270 liquidaciones anteriores (90 d)
FRESH_HOURS = 3           # ultimas horas que tienen que estar completas (velas 1h + salidas por TP/SL en 1m)
MAX_MISSING_FRAC = 0.005  # minutos faltantes tolerados en toda la ventana (huecos reales del exchange)
FUNDING_MAX_AGE_H = 9
TARGET_TOL = 1e-9


class DataGapError(Exception):
    """Datos incompletos: la corrida queda en 'error' y no se opera."""

    def __init__(self, reason: str, detail: dict[str, Any] | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.detail = detail or {}


@dataclass
class EngineConfig:
    paperbot_db: Path
    history_db: Path
    params_path: Path = P.PARAMS_PATH
    symbols: list[str] = field(default_factory=lambda: list(SYMBOLS))
    window_days: int = WINDOW_DAYS
    funding_days: int = FUNDING_DAYS
    download: bool = True
    # inyectables en tests: catalogo {id: (spec, regla)} y entradas de params.json {id: {...}}
    catalog: dict[str, Any] | None = None
    entries: dict[str, dict[str, Any]] | None = None


def _ms_index(index: pd.Index) -> np.ndarray:
    return pd.DatetimeIndex(index).as_unit("ns").asi8 // 1_000_000


def _ts(ms: int) -> pd.Timestamp:
    return pd.Timestamp(ms, unit="ms", tz="UTC")


def peak_rss_kb() -> int:
    return int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)


# ---- datos ---------------------------------------------------------------------------------------------


def check_klines(hist: HistoryStore, symbols: list[str], H: int, window_days: int) -> dict[str, Any]:
    """Chequeo de huecos ANTES de cargar nada. Levanta DataGapError con el motivo."""
    start = H - window_days * DAY_MS
    expected = (H - start) // MIN_MS
    fresh_start = H - FRESH_HOURS * HOUR_MS
    out: dict[str, Any] = {}
    problems = []
    for s in symbols:
        sym = hk.internal_symbol(s)
        n, lo, hi = hist.query(
            "SELECT COUNT(*), MIN(open_time_ms), MAX(open_time_ms) FROM klines WHERE exchange=? AND symbol=? "
            "AND interval='1m' AND open_time_ms >= ? AND open_time_ms < ?", (hk.EXCHANGE, sym, start, H))[0]
        recent = hist.query("SELECT COUNT(*) FROM klines WHERE exchange=? AND symbol=? AND interval='1m' "
                            "AND open_time_ms >= ? AND open_time_ms < ?", (hk.EXCHANGE, sym, fresh_start, H))[0][0]
        missing = expected - n
        out[s] = {"velas": n, "faltan": missing, "primera_ms": lo, "ultima_ms": hi, "ultimas_horas": recent}
        if hi is None or hi != H - MIN_MS:
            problems.append(f"{s}: ultima vela 1m {'ninguna' if hi is None else _ts(hi).isoformat()} "
                            f"(se esperaba {_ts(H - MIN_MS).isoformat()})")
        elif lo is None or lo > start + HOUR_MS:
            problems.append(f"{s}: la historia empieza en {_ts(lo).isoformat()}, se necesita desde "
                            f"{_ts(start).isoformat()} (falta descargar o se archivo)")
        elif recent != FRESH_HOURS * 60:
            problems.append(f"{s}: faltan {FRESH_HOURS * 60 - recent} velas 1m en las ultimas {FRESH_HOURS} h")
        elif missing > MAX_MISSING_FRAC * expected:
            problems.append(f"{s}: faltan {missing} velas 1m de {expected} en la ventana ({missing / expected:.2%})")
    if problems:
        raise DataGapError("hueco de datos: " + "; ".join(problems), {"klines": out})
    return out


def load_klines(hist: HistoryStore, symbol: str, start_ms: int, end_ms: int) -> pd.DataFrame:
    """Velas 1m [start, end) con indice = apertura UTC; solo las columnas que usan las reglas y el simulador."""
    rows = hist.query("SELECT open_time_ms, open, high, low, close FROM klines WHERE exchange=? AND symbol=? "
                      "AND interval='1m' AND open_time_ms >= ? AND open_time_ms < ? ORDER BY open_time_ms",
                      (hk.EXCHANGE, hk.internal_symbol(symbol), start_ms, end_ms))
    arr = np.array(rows, dtype=np.float64).reshape(-1, 5)
    del rows
    idx = pd.DatetimeIndex(pd.to_datetime(arr[:, 0].astype(np.int64), unit="ms", utc=True), name="time")
    return pd.DataFrame(arr[:, 1:], index=idx, columns=["open", "high", "low", "close"])


@dataclass
class Prices:
    """Velas 1m de un simbolo como arrays (fills y marcas)."""
    open_ms: np.ndarray
    open: np.ndarray
    close: np.ndarray

    @classmethod
    def of(cls, k: pd.DataFrame) -> Prices:
        return cls(_ms_index(k.index), k["open"].to_numpy(), k["close"].to_numpy())

    def fill(self, t_ms: int) -> tuple[float, int, str]:
        """Open de la primera vela con apertura >= t; si no existe todavia, el ultimo close disponible."""
        j = int(np.searchsorted(self.open_ms, t_ms, side="left"))
        if j < self.open_ms.size:
            return float(self.open[j]), int(self.open_ms[j]), "open_1m_siguiente"
        return float(self.close[-1]), int(self.open_ms[-1] + MIN_MS), "ultimo_precio"

    def close_before(self, t_ms: int) -> float | None:
        """Close de la ultima vela con apertura < t (precio conocido en t)."""
        j = int(np.searchsorted(self.open_ms, t_ms, side="left")) - 1
        return float(self.close[j]) if j >= 0 else None

    def exec_index(self, t_ms: int) -> int:
        return int(np.searchsorted(self.open_ms, t_ms, side="left"))


def target_at(sig: pd.Series | None, t_ms: int) -> float:
    """Objetivo vigente en t: ultimo valor con decision <= t (0 antes del primero)."""
    if sig is None or not len(sig):
        return 0.0
    ms = _ms_index(sig.index)
    j = int(np.searchsorted(ms, t_ms, side="right")) - 1
    return 0.0 if j < 0 else float(sig.iloc[j])


def _clean(sig: pd.Series | None) -> pd.Series:
    if sig is None:
        return pd.Series(dtype=float)
    s = sig.dropna().sort_index(kind="stable")
    return s[~s.index.duplicated(keep="last")]


# ---- corrida -------------------------------------------------------------------------------------------


def _catalog(cfg: EngineConfig, entries: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Estrategias que corre el bot = las de `params.json` (las reglas salen del catalogo congelado del lab)."""
    if cfg.catalog is not None:
        return cfg.catalog
    from ..lab.strategies import CATALOG

    unknown = sorted(k for k in entries if k not in CATALOG)
    if unknown:
        raise P.ParamsError(f"{unknown}: no estan en el catalogo del laboratorio (lab/strategies.py)")
    return {k: CATALOG[k] for k in entries}


def run_once(cfg: EngineConfig, *, now_ms: int | None = None, http_get: HttpGet | None = None,
             sleep: Callable[[float], None] = time.sleep) -> dict[str, Any]:
    """Una corrida. Devuelve la fila de `runs` escrita (status ok | error | skipped)."""
    now = int(now_ms if now_ms is not None else time.time() * 1000)
    H = now - now % HOUR_MS
    run_id = f"pb-{H}-{now}"
    t0 = time.time()
    store = PaperbotStore(cfg.paperbot_db)
    lock_path = Path(cfg.paperbot_db).with_suffix(".lock")
    lock = open(lock_path, "a+")  # noqa: SIM115 - se cierra en finally
    try:
        base = {"run_id": run_id, "hour_ms": H, "started_ms": now}
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return _finish(store, base, "skipped", "otra corrida del bot esta en curso", {}, t0)
        if store.ok_run(H) is not None:
            return _finish(store, base, "skipped", f"ya hay una corrida ok para {_ts(H).isoformat()}: no se repite", {}, t0)
        try:
            return _run(cfg, store, base, H, now, http_get, sleep, t0)
        except DataGapError as exc:
            log.error("corrida %s: %s", run_id, exc.reason)
            return _finish(store, base, "error", exc.reason, exc.detail, t0)
        except P.ParamsError as exc:
            log.error("corrida %s: parametros: %s", run_id, exc)
            return _finish(store, base, "error", f"parametros: {exc}", {}, t0)
        except Exception as exc:  # noqa: BLE001 - toda falla queda registrada, nunca silenciosa
            log.exception("corrida %s fallo", run_id)
            return _finish(store, base, "error", f"{type(exc).__name__}: {exc}",
                           {"traceback": traceback.format_exc()[-4000:]}, t0)
    finally:
        try:
            fcntl.flock(lock, fcntl.LOCK_UN)
        finally:
            lock.close()
            store.close()


def _finish(store: PaperbotStore, base: dict, status: str, reason: str, detail: dict, t0: float, **rows) -> dict:
    detail = {**detail, "duracion_s": round(time.time() - t0, 3)}
    run = {**base, "finished_ms": int(time.time() * 1000), "status": status, "reason": reason, "detail": detail,
           "peak_rss_kb": peak_rss_kb()}
    store.record_run(run, **rows)
    return run


def _run(cfg: EngineConfig, store: PaperbotStore, base: dict, H: int, now: int, http_get: HttpGet | None,
         sleep: Callable[[float], None], t0: float) -> dict[str, Any]:
    entries = cfg.entries if cfg.entries is not None else P.load(cfg.params_path)
    catalog = _catalog(cfg, entries)
    plans = {}
    for sid, (spec, rule) in catalog.items():
        if sid not in entries:
            raise P.ParamsError(f"{sid}: falta en params.json")
        plans[sid] = (spec, rule, P.effective(sid, entries[sid], spec), list(entries[sid]["universe"]), entries[sid])
    order = lambda s: cfg.symbols.index(s) if s in cfg.symbols else 99  # noqa: E731
    symbols = sorted({s for p in plans.values() for s in p[3]}, key=order)
    # estrategias diarias (lote 6): velas diarias aparte (daily.py), con su historia minima por spec
    daily_need = {sid: d for sid, p in plans.items() if (d := history_days(p[0], p[1])) is not None}

    detail: dict[str, Any] = {"ventana_dias": cfg.window_days, "simbolos": symbols}
    hist = HistoryStore(cfg.history_db)
    try:
        # 1. datos nuevos
        if cfg.download:
            added = {}
            for s in symbols:
                try:
                    added[s] = hk.download(hist, s, H - (cfg.window_days + 1) * DAY_MS, http_get=http_get,
                                           clock_ms=lambda: now, sleep=sleep)
                except Exception as exc:  # noqa: BLE001
                    raise DataGapError(f"no se pudieron bajar las velas de {s}: {exc}") from exc
            detail["velas_nuevas"] = added
            fadded = {}
            for s in symbols:
                try:
                    fadded[s] = update_funding(store, s, H - cfg.funding_days * DAY_MS, now, http_get=http_get, sleep=sleep)
                except Exception as exc:  # noqa: BLE001
                    raise DataGapError(f"no se pudo bajar el funding de {s}: {exc}") from exc
            detail["funding_nuevo"] = fadded

        # 2. chequeos
        detail["chequeo_velas"] = check_klines(hist, symbols, H, cfg.window_days)
        stale = {s: store.last_funding_ms(s) for s in symbols}
        bad = {s: v for s, v in stale.items() if v is None or v < H - FUNDING_MAX_AGE_H * HOUR_MS}
        if bad:
            raise DataGapError("funding atrasado: " + "; ".join(
                f"{s}: ultima liquidacion {'ninguna' if v is None else _ts(v).isoformat()}" for s, v in bad.items()),
                {"funding_ultimo_ms": stale})
        detail["funding_ultimo_ms"] = stale

        # 3. carga (velas hasta `now`: las >= H solo se usan para el precio de los fills)
        start = H - cfg.window_days * DAY_MS
        full = {s: load_klines(hist, s, start, now) for s in symbols}

        # 4. velas diarias: el maximo que necesitan las diarias activas (1m agregadas en SQL o cache REST 1d)
        daily_bars: dict[str, pd.DataFrame] = {}
        daily_info: dict[str, dict[str, Any]] = {}
        if daily_need:
            days = max(daily_need.values())
            for s in sorted({s for sid in daily_need for s in plans[sid][3]}, key=order):
                fetch = None
                if cfg.download:
                    def fetch(a: int, b: int, s: str = s) -> int:
                        return update_daily_cache(store, s, a, b, now, http_get=http_get, sleep=sleep)
                daily_bars[s], daily_info[s] = build_daily(hist, store, s, H, days, fetch=fetch)
            detail["velas_diarias"] = {"dias": days, "minimo_por_estrategia": daily_need, "por_simbolo": daily_info}
    finally:
        hist.close()
    prices = {s: Prices.of(k) for s, k in full.items()}
    funding = {}
    for s in symbols:
        rows = store.funding_rates(s, H - cfg.funding_days * DAY_MS, H)
        funding[s] = pd.Series([r[1] for r in rows], index=pd.to_datetime([r[0] for r in rows], unit="ms", utc=True),
                               dtype=float, name="rate")
    from ..lab.strategies import MarketData

    closed = {s: k.loc[k.index < _ts(H)] for s, k in full.items()}  # reloj: solo velas cerradas en H
    marks = {s: prices[s].close_before(H) for s in symbols}

    prev = store.last_ok_run(before_ms=H)
    consecutive = prev is not None and prev["hour_ms"] == H - HOUR_MS
    mode = "consecutiva" if consecutive else ("inicio" if prev is None else "hueco")
    rows: dict[str, list] = {k: [] for k in ("orders", "funding", "equity", "positions", "trades", "basket", "gaps", "exits")}
    if prev is not None and not consecutive:
        hours = (H - prev["hour_ms"]) // HOUR_MS - 1
        rows["gaps"].append({"run_id": base["run_id"], "from_ms": prev["hour_ms"] + HOUR_MS, "to_ms": H - HOUR_MS,
                             "hours": hours})
        detail["hueco_horas"] = hours

    per_strategy = {}
    sin_datos: dict[str, str] = {}
    for sid, (spec, rule, params, universe, entry) in plans.items():
        need = daily_need.get(sid)
        if need is not None:
            short = {s: daily_info[s]["dias_contiguos"] for s in universe if daily_info[s]["dias_contiguos"] < need}
            if short:
                # sin fallos silenciosos: no se opera ni se escribe equity; la proxima corrida con datos entra por hueco
                errs = sorted({daily_info[s]["rest_error"] for s in short if daily_info[s].get("rest_error")})
                sin_datos[sid] = (f"faltan velas diarias: se necesitan {need} dias contiguos y hay "
                                  + ", ".join(f"{s} {n}" for s, n in short.items())
                                  + (f" (REST: {'; '.join(errs)})" if errs else ""))
                per_strategy[sid] = {"estado": "sin_datos", "motivo": sin_datos[sid], "spec_hash": spec.spec_hash(),
                                     "rol": entry.get("rol"), "universe": universe, "desde": entry.get("desde"),
                                     "historia_dias": need, "origen": entry.get("origen")}
                continue
        # Cada estrategia lleva su propio inicio: una que se suma despues entra "desde hoy" (orden `inicio` al
        # objetivo de la regla en H, canasta y capital propios desde H) sin tocar la historia de las demas.
        last_eq = store.last_equity_hour(sid)
        s_consecutive = consecutive and last_eq == prev["hour_ms"]
        s_mode = "consecutiva" if s_consecutive else ("inicio" if last_eq is None else "hueco")
        if need is not None:
            data = {s: MarketData(daily_bars[s], funding[s]) for s in universe}
            source = daily_source({id(md): daily_bars[s] for s, md in data.items()})
        else:
            data = {s: MarketData(closed[s], funding[s]) for s in universe}
            source = nullcontext()
        votes = None
        with source:
            with capture_exits() as rec:  # misma regla y mismo resultado; registra el motivo de cada salida
                signals = rule(data, params)
            if need is not None and isinstance(params.get("components"), dict):
                votes = _votes(params["components"], data, H)
        sigs = {s: _clean(signals.get(s)) for s in universe}
        try:
            reasons = {(s, int(pd.Timestamp(t).value // 1_000_000)): m
                       for (s, t), m in reason_map(sid, data, signals, rec, params).items() if s is not None}
        except Exception:  # noqa: BLE001 - el motivo es informativo: si falla, la orden queda sin motivo (no se inventa)
            log.exception("%s: no se pudo reconstruir el motivo de las salidas", sid)
            reasons = {}
        targets = {s: target_at(sigs[s], H) for s in universe}
        stored_orders = store.orders(sid)
        book = Book.replay(universe, stored_orders, store.funding_events(sid))
        new_orders: list[dict[str, Any]] = []

        def place(s: str, t_ms: int, w: float, reason: str, book=book, new_orders=new_orders) -> None:
            price, fill_ms, src = prices[s].fill(t_ms)
            new_orders.append(book.order(s, t_ms, fill_ms, price, w, reason, src))

        n_events = 0
        if s_consecutive:
            for s in universe:
                sig = sigs[s]
                if not len(sig):
                    continue
                ms = _ms_index(sig.index)
                sel = np.flatnonzero((ms > H - HOUR_MS) & (ms <= H))
                ev = [(int(ms[i]), float(sig.iloc[i])) for i in sel]
                n_events += len(ev)
                for k, (t, w) in enumerate(ev):
                    if k + 1 < len(ev) and prices[s].exec_index(ev[k + 1][0]) == prices[s].exec_index(t):
                        continue  # varias decisiones antes de la misma vela de ejecucion: vale la ultima
                    if abs(w - book.pos[s].weight) > TARGET_TOL:
                        place(s, t, w, "senal")
        for s in universe:
            if abs(targets[s] - book.pos[s].weight) > TARGET_TOL:
                place(s, H, targets[s], "resincronizacion" if s_consecutive else s_mode)

        # funding de las liquidaciones con posicion abierta (incluye las que llegaron tarde a la cache)
        all_orders = [dict(o) for o in stored_orders] + new_orders
        done = {(f["symbol"], f["funding_ms"]) for f in store.funding_events(sid)}
        first_fill = min((o["fill_ms"] for o in all_orders), default=None)
        new_funding = []
        if first_fill is not None:
            for s in universe:
                for f_ms, rate, mark in store.funding_rates(s, first_fill + 1, H):
                    if (s, f_ms) in done:
                        continue
                    u = book.units_before(all_orders, s, f_ms)
                    if abs(u) < EPS:
                        continue
                    px = prices[s].close_before(f_ms) if f_ms > prices[s].open_ms[0] else None
                    px = px if px is not None else mark
                    if px is None:
                        raise DataGapError(f"{s}: sin precio para el funding de {_ts(f_ms).isoformat()}")
                    new_funding.append({"symbol": s, "funding_ms": f_ms, "rate": rate, "units": u, "price": px,
                                        "cash": -u * px * rate})

        final = Book.replay(universe, all_orders, [dict(f) for f in store.funding_events(sid)] + new_funding)
        tot = final.totals(marks)

        # canasta igual-ponderada (buy & hold desde la primera corrida de la estrategia)
        bk = [dict(b) for b in store.basket(sid)]
        if not bk:
            alloc = CAPITAL / len(universe)
            bk = [{"strategy_id": sid, "symbol": s, "start_ms": H, "price": marks[s], "units": alloc / marks[s],
                   "cost": alloc * (FEE_BPS + SLIPPAGE_BPS) / 1e4} for s in universe]
            rows["basket"] += bk
        basket_eq = CAPITAL
        for b in bk:
            s = b["symbol"]
            basket_eq += b["units"] * (marks[s] - b["price"]) - b["cost"]
            for f_ms, rate, mark in store.funding_rates(s, b["start_ms"] + 1, H):
                px = prices[s].close_before(f_ms) if f_ms > prices[s].open_ms[0] else None
                px = px if px is not None else mark
                if px is not None:
                    basket_eq -= b["units"] * px * rate

        rid = base["run_id"]
        rows["orders"] += [{"run_id": rid, "strategy_id": sid, **o} for o in new_orders]
        for o in new_orders:
            w0, w1 = o["weight_from"], o["weight_to"]
            if abs(w0) < TARGET_TOL or (abs(w1) > TARGET_TOL and np.sign(w1) == np.sign(w0)):
                continue  # no cierra nada (entrada o ajuste de tamano)
            m = reasons.get((o["symbol"], o["decision_ms"])) if o["reason"] == "senal" else o["reason"]
            if m is not None:
                rows["exits"].append({"run_id": rid, "strategy_id": sid, "symbol": o["symbol"], "decision_ms": o["decision_ms"],
                                      "fill_ms": o["fill_ms"], "weight_from": w0, "weight_to": w1, "motivo": m})
        rows["funding"] += [{"run_id": rid, "strategy_id": sid, **f} for f in new_funding]
        rows["equity"].append({"run_id": rid, "hour_ms": H, "strategy_id": sid, "equity": tot["equity"],
                               "basket_equity": basket_eq, "realized": tot["realized"], "unrealized": tot["unrealized"],
                               "funding": tot["funding"], "fees": tot["fees"], "slippage": tot["slippage"],
                               "exposure": tot["exposure"]})
        for s in universe:
            p = final.pos[s]
            rows["positions"].append({"run_id": rid, "hour_ms": H, "strategy_id": sid, "symbol": s, "weight": p.weight,
                                      "target": targets[s], "units": p.units, "entry_price": p.avg,
                                      "mark_price": marks[s], "unrealized": p.unrealized(marks[s]),
                                      "realized": p.realized, "funding": p.funding, "fees": p.fees,
                                      "slippage": p.slippage})
            rows["trades"] += [{"run_id": rid, "strategy_id": sid, **t} for t in p.closed]
        per_strategy[sid] = {"spec_hash": spec.spec_hash(), "params": {k: params[k] for k in spec.grid},
                             "origen": entry.get("origen"), "universe": universe, "targets": targets,
                             "eventos": n_events, "ordenes": len(new_orders), "funding": len(new_funding),
                             "equity": tot["equity"], "modo": s_mode, "desde": entry.get("desde"),
                             "estado": "ok", "rol": entry.get("rol"), "historia_dias": need, "votos": votes,
                             "inicio_ms": H if last_eq is None else store.first_equity_hour(sid)}

    detail.update({"modo": mode, "estrategias": per_strategy, "marcas": marks, "sin_datos": sin_datos,
                   "anterior_ok_ms": None if prev is None else prev["hour_ms"]})
    note = {"consecutiva": "ok", "inicio": "primera corrida: posiciones llevadas al objetivo de la regla",
            "hueco": f"hueco de {detail.get('hueco_horas')} h sin simular; posiciones llevadas al objetivo"}[mode]
    if sin_datos:
        note += f"; SIN DATOS en esta corrida (no operaron): {', '.join(sorted(sin_datos))}"
    return _finish(store, base, "ok", note, detail, t0, orders=rows["orders"], funding=rows["funding"],
                   equity=rows["equity"], positions=rows["positions"], trades=rows["trades"], basket=rows["basket"],
                   gaps=rows["gaps"], exits=rows["exits"])


def _votes(components: dict[str, float], data: dict[str, Any], H: int) -> dict[str, dict[str, Any]]:
    """Balance de fuerzas de una familia en H, por simbolo: cuantas componentes estan long y cuantas short (cada una
    con su regla congelada). Solo informativo para el panel: la posicion la decide la regla de la familia."""
    from ..lab.strategies import CATALOG

    comp = {c: CATALOG[c][1](data, dict(CATALOG[c][0].params)) for c in components}
    out = {}
    for s in data:
        t = {c: target_at(_clean(sig.get(s)), H) for c, sig in comp.items()}
        out[s] = {"long": sum(1 for v in t.values() if v > TARGET_TOL), "short": sum(1 for v in t.values() if v < -TARGET_TOL),
                  "componentes": len(t), "neta": sum(components[c] * v for c, v in t.items())}
    return out


def summary_line(run: dict[str, Any]) -> str:
    d = run.get("detail") or {}
    if isinstance(d, str):
        d = json.loads(d)
    eq = {k: round(v.get("equity", 0), 2) for k, v in (d.get("estrategias") or {}).items()}
    return (f"{run['status']} H={_ts(run['hour_ms']).isoformat()} motivo={run.get('reason')!r} "
            f"pico_rss={run.get('peak_rss_kb')} KB equity={eq}")
