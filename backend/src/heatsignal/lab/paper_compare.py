"""
Paper trading contra backtest: el dry-run de Freqtrade (VM) contra la regla congelada del laboratorio sobre
las MISMAS velas y la misma ventana (etapa 3 del plan, decisions.md "Paper trading").

    python -m heatsignal.lab.paper_compare                     # desde el inicio del paper (bot_start_time)
    python -m heatsignal.lab.paper_compare --since 2026-10-20  # ventana desde esa fecha (UTC)
    python -m heatsignal.lab.paper_compare --upload            # y sube compare_<fecha>.json + paper_summary.json
    python -m heatsignal.lab.paper_compare --no-update         # sin bajar velas (usa lo que ya hay en la PC)

Pasos:
  1. Lee trades y ordenes del dry-run por SSH, SOLO LECTURA: manda `paper/freqtrade_db.py` (stdlib) a
     `python3 - dump <base>` en la VM, que abre la base con `file:...?mode=ro` y vuelca JSON.
  2. Actualiza las velas 1m del periodo en la PC (lab.vision: meses faltantes + diarios del mes en curso hasta
     ayer) y el funding del mes en curso (REST). Re-simula donchian-ensemble-v1 con la regla del CATALOG y
     los params congelados con target_vol 0.15 (lo mismo que corre el dry-run) en lab/sim.py, 10.000 US$
     repartidos en los 7 simbolos. Las senales son causales: se calculan con `WARMUP_DAYS` de historia previa y
     se simula SOLO [inicio, ultima vela) con la posicion objetivo vigente al arrancar (como el holdout).
  3. Compara por par y total (ver `compare`). Descomposicion EXACTA de la diferencia paper - backtest:
       timing        estar en el mercado cuando el otro no (o con distinto signo): entradas/salidas en otro
                     momento, operaciones que solo hizo uno;
       tamano        distinto tamano cuando los dos estan posicionados: umbral de rebalanceo 5%, reinversion de
                     Freqtrade, tradable_balance_ratio 0.99, redondeo de cantidades;
       precio_fill   precio real del fill del dry-run contra el open de la vela 1m siguiente (convencion del lab);
       comisiones    comisiones del lab (5 bps) menos las cobradas por el dry-run;
       slippage      slippage que el lab carga (2 bps por lado) y el dry-run no;
       funding_posicion   funding con las tasas reales y las posiciones del paper menos el del lab;
       funding_contable   lo que Freqtrade asento de funding menos ese recalculo (incluye funding fuera de la
                     ventana de trades que la cruzan y diferencias de mark price).
     PnL de precio = mark-to-market al close de la ultima vela de la ventana (no realizado incluido).
  4. Escribe ~/heatsignal-lab/paper/compare_<fecha>.json y paper_summary.json (ultimo + historial; una fila por
     fecha) y con --upload los sube a la VM (~/heatsignal/data/, donde los lee GET /api/paper/compare).
  5. Timer semanal: deploy/pc/heatsignal-paper-compare.{service,timer} (domingos 01:00 hora local).
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import sys
from collections.abc import Iterable
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Protocol

import numpy as np
import pandas as pd

from ..paper import freqtrade_db as ftdb
from .publish import VM, VM_DIR, VM_HOST, _nan_to_null
from .vision import lab_dir

log = logging.getLogger("heatsignal.lab.paper_compare")

STRATEGY_ID = "donchian-ensemble-v1"
TARGET_VOL = 0.15                  # = DonchianEnsembleV1.TARGET_VOL (lo eligio el walk-forward antes del holdout)
CAPITAL = 10_000.0                 # = dry_run_wallet de freqtrade/config.dryrun.json
TRADABLE_BALANCE_RATIO = 0.99      # = tradable_balance_ratio del dry-run
REBALANCE_MIN = 0.05               # = DonchianEnsembleV1.REBALANCE_MIN
WARMUP_DAYS = 400                  # historia previa para las senales (canal 160 x 4h + volatilidad 180 x 4h + histeresis)
VM_DB = "/home/nahue/heatsignal/data/freqtrade_dryrun.sqlite"
MATCH_TOLERANCE = pd.Timedelta(days=1)  # entradas paper/lab a mas de esto no se consideran la misma operacion
HISTORY_MAX = 156                  # ~3 anios de corridas semanales
COMPONENTS = ("timing", "tamano", "precio_fill", "comisiones", "slippage", "funding_posicion", "funding_contable")

DESIGN_DIFFERENCES = [
    {"id": "rebalanceo_5", "componente": "tamano",
     "texto": "El dry-run solo ajusta el tamano si el cambio supera el 5% del capital del par (REBALANCE_MIN); "
              "el laboratorio re-ajusta en cada cambio de objetivo."},
    {"id": "sin_slippage", "componente": "slippage",
     "texto": "El dry-run no modela slippage; el laboratorio carga 2 bps por lado."},
    {"id": "reinversion", "componente": "tamano",
     "texto": "Freqtrade reinvierte: el capital por par es (wallet actual x 0,99) / 7; el laboratorio usa nocional fijo "
              "de 10.000 / 7 sin reinversion."},
    {"id": "tradable_ratio", "componente": "tamano",
     "texto": "tradable_balance_ratio 0,99: el dry-run opera el 99% del wallet (1% menos de tamano que el laboratorio)."},
    {"id": "fill_real", "componente": "precio_fill",
     "texto": "El dry-run llena al precio del libro (price_side other) cuando procesa la vela; el laboratorio al open "
              "de la vela 1m siguiente a la decision."},
    {"id": "funding_ft", "componente": "funding_contable",
     "texto": "Freqtrade asienta el funding por operacion con su propio mark price; el laboratorio usa la tasa "
              "liquidada x close de la vela 1m."},
]


# ---- acceso a la VM (inyectable: los tests no usan SSH) ---------------------------------------------------


class Remote(Protocol):
    def dump(self) -> dict[str, Any]: ...  # volcado de la base de Freqtrade (freqtrade_db.dump)

    def upload(self, files: list[Path]) -> None: ...  # -> ~/heatsignal/data/


class GcloudRemote:
    """VM por `gcloud compute ssh/scp`, igual que lab.publish y lab.worker. Solo lectura de la base."""

    def __init__(self, host: str = VM_HOST, flags: list[str] | None = None, db_path: str = VM_DB) -> None:
        self.host, self.flags, self.db_path = host, list(VM if flags is None else flags), db_path

    def dump(self) -> dict[str, Any]:
        source = Path(ftdb.__file__).read_text(encoding="utf-8")
        res = subprocess.run(["gcloud", "compute", "ssh", *self.flags, self.host, "--command",
                              f"python3 - dump {self.db_path}"], input=source, capture_output=True, text=True,
                             timeout=300, env=os.environ.copy())
        lines = [ln for ln in res.stdout.strip().splitlines() if ln.startswith("{")]
        if not lines:
            raise RuntimeError(f"ssh/dump fallo (rc={res.returncode}): {res.stderr.strip()[-500:]}")
        out = json.loads(lines[-1])
        if out.get("error"):
            raise RuntimeError(f"la VM respondio: {out['error']}")
        return out

    def upload(self, files: list[Path]) -> None:
        subprocess.run(["gcloud", "compute", "scp", *self.flags, *map(str, files), f"{self.host}:{VM_DIR}"],
                       check=True, timeout=300, env=os.environ.copy())


# ---- datos de mercado en la PC ----------------------------------------------------------------------------


def update_market_data(symbols: Iterable[str], since: pd.Timestamp, *, root: Path | None = None,
                       today: date | None = None, run_job=None, funding_fn=None) -> dict[str, Any]:
    """Baja con lab.vision las velas 1m que falten desde el mes de `since` (el mes en curso con diarios hasta
    ayer) y el funding del mes en curso por REST. Los errores se informan, no se ocultan."""
    from . import vision
    from .store import update_current_funding

    root = root or lab_dir()
    today = today or datetime.now(timezone.utc).date()
    symbols = [s.upper() for s in symbols]
    jobs = vision.plan(symbols, ["klines", "funding"], today, root, since=f"{since:%Y-%m}")
    run = run_job or vision.run_job
    errors: list[str] = []
    rows = 0
    for job in jobs:
        try:
            rows += run(job, root, today)[1]
        except Exception as exc:  # noqa: BLE001 - un mes que falla se informa y sigue
            errors.append(f"{job.dataset} {job.symbol} {job.month}: {exc}")
    fund = funding_fn or update_current_funding
    for sym in symbols:
        try:
            fund(sym, root)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"funding del mes en curso {sym}: {exc}")
    for e in errors:
        log.warning("actualizacion de datos: %s", e)
    return {"jobs": len(jobs), "rows": rows, "errors": errors}


def load_window(symbols: Iterable[str], start: pd.Timestamp, warmup_days: int, root: Path | None = None):
    """MarketData por simbolo (velas 1m OHLC desde start - warmup, funding desde start - 1 dia)."""
    from .store import load_funding, load_klines
    from .strategies import MarketData

    data = {}
    for sym in symbols:
        k = load_klines(sym, start - pd.Timedelta(days=warmup_days), None, root)[["open", "high", "low", "close"]]
        try:
            f = load_funding(sym, start - pd.Timedelta(days=1), None, root)
        except FileNotFoundError:
            f = pd.Series(dtype=float, index=pd.DatetimeIndex([], tz="UTC"))
        data[sym] = MarketData(k, f, None)
    return data


# ---- utilidades numericas ----------------------------------------------------------------------------------


def _ns(ms: int | float) -> int:
    return int(ms) * 1_000_000


def _ts(ms: int | float | None) -> pd.Timestamp | None:
    return None if ms is None else pd.Timestamp(int(ms), unit="ms", tz="UTC")


def bar_pnl(u: np.ndarray, o: np.ndarray, c: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """(pnl por gap, pnl intra-vela) por vela con la convencion de lab/sim.py (posicion de la vela anterior en el
    gap, de la vela actual dentro de la vela)."""
    u_prev = np.empty_like(u)
    if u.size:
        u_prev[0] = u[0]  # la posicion inicial ya estaba antes de la ventana; el gap de la vela 0 es 0 (c_prev = o0)
        u_prev[1:] = u[:-1]
    c_prev = np.empty_like(c)
    if c.size:
        c_prev[0] = o[0]
        c_prev[1:] = c[:-1]
    return u_prev * (o - c_prev), u * (c - o)


def avg_cost_realized(u0: float, basis0: float, flows: list[tuple[float, float]]) -> float:
    """PnL de precio realizado por costo promedio. flows = [(du, precio)] en orden; u0 a precio basis0."""
    pos, avg, realized = u0, basis0, 0.0
    for du, px in flows:
        if pos == 0 or np.sign(du) == np.sign(pos):
            new = pos + du
            avg = (avg * pos + px * du) / new if new else 0.0
            pos = new
            continue
        close = -du if abs(du) <= abs(pos) else pos  # parte que reduce (con signo de la posicion)
        realized += close * (px - avg)
        pos += du
        if abs(pos) <= 1e-12:
            pos, avg = 0.0, 0.0
        elif np.sign(pos) != np.sign(pos - du):  # se dio vuelta: el resto abre a este precio
            avg = px
    return realized


def _bps(a: float | None, b: float | None) -> float | None:
    if a is None or b is None or not b:
        return None
    return (a / b - 1.0) * 1e4


def _stats(xs: list[float]) -> dict[str, Any]:
    xs = [x for x in xs if x is not None and np.isfinite(x)]
    if not xs:
        return {"n": 0, "mean": None, "median": None, "max_abs": None}
    arr = np.asarray(xs, dtype=float)
    return {"n": int(arr.size), "mean": float(arr.mean()), "median": float(np.median(arr)),
            "max_abs": float(np.abs(arr).max())}


# ---- comparacion -----------------------------------------------------------------------------------------------


def lab_signals(data, end: pd.Timestamp, start: pd.Timestamp):
    """Senales de la regla congelada (params del CATALOG con target_vol 0.15), cosidas al inicio de la ventana."""
    from .strategies import CATALOG

    spec, rule = CATALOG[STRATEGY_ID]
    params = {**spec.params, "target_vol": TARGET_VOL}
    sig = rule(data, params)
    stitched, target_start = {}, {}
    for s, v in sig.items():
        v = v[v.index < end]
        prev = v[v.index < start]
        head_val = float(prev.iloc[-1]) if len(prev) else 0.0
        head = pd.Series([head_val], index=pd.DatetimeIndex([start]))
        rest = v[v.index >= start]
        both = pd.concat([head, rest]).sort_index()
        stitched[s] = both[~both.index.duplicated(keep="last")]
        target_start[s] = head_val
    return spec, params, stitched, target_start


def compare(d: dict[str, Any], *, start: pd.Timestamp | None = None, end: pd.Timestamp | None = None,
            data=None, root: Path | None = None, warmup_days: int = WARMUP_DAYS, capital: float = CAPITAL,
            symbols: list[str] | None = None, start_source: str | None = None) -> dict[str, Any]:
    """Compara el volcado del dry-run `d` (freqtrade_db.dump) contra la re-simulacion del laboratorio."""
    from .baselines import buy_and_hold, equal_weight_basket
    from .sim import Costs, simulate
    from .strategies import SEVEN

    symbols = list(symbols or SEVEN)
    kv = d.get("kv", {})
    trades = d.get("trades", [])
    if start is None:
        if kv.get("bot_start_time"):
            start, start_source = _ts(kv["bot_start_time"]), "bot_start_time (primer arranque del bot)"
        elif trades:
            start, start_source = _ts(min(t["open_ms"] for t in trades if t["open_ms"])), "primer trade"
        else:
            raise ValueError("la base del dry-run no tiene bot_start_time ni trades: no hay inicio del paper")
    start = pd.Timestamp(start)
    start = start.tz_localize("UTC") if start.tzinfo is None else start.tz_convert("UTC")
    data = data if data is not None else load_window(symbols, start, warmup_days, root)
    last = min(md.klines.index[-1] for md in data.values()) + pd.Timedelta(minutes=1)
    end = min(end, last) if end is not None else last
    generated_at = datetime.now(timezone.utc).isoformat()
    base = {
        "kind": "paper_compare", "version": 1, "generated_at": generated_at, "strategy_id": STRATEGY_ID,
        "window": {"start": start.isoformat(), "end": end.isoformat(), "start_ms": int(start.value // 1_000_000),
                   "end_ms": int(end.value // 1_000_000), "days": max(0.0, (end - start) / pd.Timedelta(days=1)),
                   "start_source": start_source or "--since"},
        "design_differences": DESIGN_DIFFERENCES,
        "paper_source": {"db": d.get("db_path"), "read_at": ftdb.iso(d.get("read_at_ms")), "schema": d.get("schema"),
                         "trades": len(trades), "orders": len(d.get("orders", []))},
    }
    if end <= start + pd.Timedelta(minutes=1):
        return {**base, "status": "sin_datos",
                "notes": [f"No hay velas 1m en la PC despues del inicio del paper ({start}); la ultima es {last}. "
                          "Binance publica el archivo diario al dia siguiente: correr de nuevo manana."]}

    spec, params, stitched, target_start = lab_signals({s: data[s] for s in symbols}, end, start)
    prices = {s: data[s].klines[(data[s].klines.index >= start) & (data[s].klines.index < end)] for s in symbols}
    funding = {s: data[s].funding[(data[s].funding.index >= start) & (data[s].funding.index < end)] for s in symbols}
    costs = Costs()
    res = simulate(prices, stitched, funding, costs, capital)
    alloc = capital / len(symbols)
    paper_pair_base = capital * TRADABLE_BALANCE_RATIO / len(symbols)

    start_ms, end_ms = int(start.value // 1_000_000), int(end.value // 1_000_000)
    all_fills = ftdb.fills(d)
    by_pair, curves_paper = [], []
    totals = {k: {"precio": 0.0, "comisiones": 0.0, "slippage": 0.0, "funding": 0.0, "total": 0.0, "realizado": 0.0,
                  "no_realizado": 0.0, "entradas": 0, "ajustes": 0, "salidas": 0, "ordenes": 0} for k in ("paper", "sim")}
    decomp_tot = {k: 0.0 for k in COMPONENTS}
    excluded_after = 0
    data_gaps = []
    for sym in symbols:
        px = prices[sym]
        idx = px.index
        n = len(idx)
        if n == 0:
            data_gaps.append(f"{sym}: sin velas en la ventana")
            continue
        o, c = px["open"].to_numpy(float), px["close"].to_numpy(float)
        idx_ns = idx.as_unit("ns").asi8

        # --- paper: unidades en la grilla (convencion del lab: el fill vale desde la vela 1m con apertura >= t)
        pf = [f for f in all_fills if f["symbol"] == sym and f["time_ms"] is not None]
        before = [f for f in pf if f["time_ms"] < start_ms]
        inside = [f for f in pf if start_ms <= f["time_ms"] < end_ms]
        excluded_after += sum(1 for f in pf if f["time_ms"] >= end_ms)
        u0 = float(sum(f["du"] for f in before))
        du_grid = np.zeros(n)
        fill_bar = []
        for f in inside:
            j = int(np.searchsorted(idx_ns, _ns(f["time_ms"]), side="left"))
            if j >= n:
                excluded_after += 1
                fill_bar.append(None)
                continue
            du_grid[j] += f["du"]
            fill_bar.append(j)
        u_p = u0 + np.cumsum(du_grid)
        u_p[np.abs(u_p) < 1e-12] = 0.0
        inside_ok = [(f, j) for f, j in zip(inside, fill_bar) if j is not None]

        # --- laboratorio: unidades desde los fills del simulador
        sf = res.fills[res.fills["symbol"] == sym]
        u_s = np.full(n, np.nan)
        if len(sf):
            pos = idx.get_indexer(pd.DatetimeIndex(sf["time"]))
            u_s[pos] = sf["units_to"].to_numpy(float)
        u_s = pd.Series(u_s).ffill().fillna(0.0).to_numpy()

        gp, ip = bar_pnl(u_p, o, c)
        gs, is_ = bar_pnl(u_s, o, c)
        up_prev = np.r_[u_p[:1], u_p[:-1]]
        us_prev = np.r_[u_s[:1], u_s[:-1]]
        both_gap = (up_prev != 0) & (us_prev != 0) & (np.sign(up_prev) == np.sign(us_prev))
        both_in = (u_p != 0) & (u_s != 0) & (np.sign(u_p) == np.sign(u_s))
        d_gap, d_in = gp - gs, ip - is_
        size = float(d_gap[both_gap].sum() + d_in[both_in].sum())
        timing = float(d_gap[~both_gap].sum() + d_in[~both_in].sum())
        p_ref = float(gp.sum() + ip.sum())
        fill_eff = float(sum(f["du"] * (o[j] - f["price"]) for f, j in inside_ok))
        p_paper = p_ref + fill_eff
        fees_paper = float(sum(f["fee"] for f, _ in inside_ok))

        # funding: el asentado por Freqtrade (trades que cruzan la ventana) y el recalculado con tasas reales
        fund_ft = 0.0
        for t in trades:
            if t["symbol"] != sym or t["open_ms"] is None:
                continue
            if t["open_ms"] < end_ms and (t["close_ms"] is None or t["close_ms"] >= start_ms):
                fund_ft += t["funding_fees"] or 0.0
        fs = funding[sym]
        f_ns = fs.index.as_unit("ns").asi8
        ok = (f_ns > idx_ns[0]) & (f_ns <= idx_ns[-1] + 60_000_000_000)
        m = np.searchsorted(idx_ns, f_ns[ok], side="left") - 1
        fund_cash = -u_p[m] * c[m] * fs.to_numpy(float)[ok]
        fund_recalc = float(fund_cash.sum())

        sb = res.breakdown_by_symbol.loc[sym]
        sim_tot = float(sb["total"])
        paper_tot = p_paper - fees_paper + fund_ft
        comp = {
            "timing": timing, "tamano": size, "precio_fill": fill_eff,
            "comisiones": float(sb["comisiones"]) - fees_paper, "slippage": float(sb["slippage"]),
            "funding_posicion": fund_recalc - float(sb["funding"]), "funding_contable": fund_ft - fund_recalc,
        }
        residual = (paper_tot - sim_tot) - sum(comp.values())
        for k in COMPONENTS:
            decomp_tot[k] += comp[k]

        # realizado / no realizado (precio, costo promedio; la posicion inicial se valua al open de la ventana)
        real_p = avg_cost_realized(u0, o[0], [(f["du"], f["price"]) for f, _ in inside_ok])
        sim_flows = [(float(r.units_to - r.units_from), float(r.price)) for r in sf.itertuples()]
        real_s = avg_cost_realized(0.0, o[0], sim_flows)
        kinds_p = [f["kind"] for f, _ in inside_ok]
        kinds_s = []
        for r in sf.itertuples():
            kinds_s.append("entrada" if r.units_from == 0 else "salida" if r.units_to == 0 else
                           "ajuste+" if abs(r.units_to) > abs(r.units_from) else "ajuste-")
        paper_blk = {
            "precio": p_paper, "comisiones": fees_paper, "slippage": 0.0, "funding": fund_ft, "total": paper_tot,
            "funding_recalculado": fund_recalc, "realizado": real_p, "no_realizado": p_paper - real_p,
            "entradas": kinds_p.count("entrada"), "ajustes": sum(k.startswith("ajuste") for k in kinds_p),
            "ajustes_mas": kinds_p.count("ajuste+"), "ajustes_menos": kinds_p.count("ajuste-"),
            "salidas": kinds_p.count("salida"), "ordenes": len(kinds_p),
            "unidades_inicio": u0, "unidades_fin": float(u_p[-1]), "nocional_fin": float(u_p[-1] * c[-1]),
        }
        sim_blk = {
            "precio": float(sb["precio"]), "comisiones": float(sb["comisiones"]), "slippage": float(sb["slippage"]),
            "funding": float(sb["funding"]), "total": sim_tot, "realizado": real_s,
            "no_realizado": float(sb["precio"]) - real_s,
            "entradas": kinds_s.count("entrada"), "ajustes": sum(k.startswith("ajuste") for k in kinds_s),
            "ajustes_mas": kinds_s.count("ajuste+"), "ajustes_menos": kinds_s.count("ajuste-"),
            "salidas": kinds_s.count("salida"), "ordenes": len(kinds_s),
            "unidades_fin": float(u_s[-1]), "nocional_fin": float(u_s[-1] * c[-1]),
        }
        for side, blk in (("paper", paper_blk), ("sim", sim_blk)):
            for k in ("precio", "comisiones", "slippage", "funding", "total", "realizado", "no_realizado",
                      "entradas", "ajustes", "salidas", "ordenes"):
                totals[side][k] += blk[k]

        # operaciones: entradas/salidas paper vs lab, emparejadas por entrada mas cercana
        rows = _match_trades(sym, inside_ok, res, sf, o, idx)
        first_entry = next((f for f, _ in inside_ok if f["kind"] == "entrada"), None)
        by_pair.append({
            "symbol": sym, "paper": paper_blk, "sim": sim_blk, "diff": paper_tot - sim_tot,
            "decomposition": {**comp, "total": paper_tot - sim_tot, "residual": residual},
            "fill_bps": _stats([_bps(f["price"], o[j]) for f, j in inside_ok]),
            "trades": rows,
            "target_start_sim": target_start.get(sym),
            "paper_frac_first_entry": None if first_entry is None else first_entry["notional"] / paper_pair_base,
            "funding_events": int(ok.sum()),
        })

        # curva del paper (mismo grid): precio a precio de referencia + efecto del fill - comisiones + funding recalc.
        bar_tot = gp + ip
        for f, j in inside_ok:
            bar_tot[j] += f["du"] * (o[j] - f["price"]) - f["fee"]
        np.add.at(bar_tot, m, fund_cash)
        curves_paper.append(pd.Series(bar_tot, index=idx))

    for side in ("paper", "sim"):
        totals[side]["retorno"] = totals[side]["total"] / capital
    diff = totals["paper"]["total"] - totals["sim"]["total"]
    decomposition = {**decomp_tot, "total": diff, "residual": diff - sum(decomp_tot.values())}

    # curvas: paper, lab, canasta y buy & hold medio (retorno acumulado sobre el capital)
    curves = None
    if curves_paper:
        basket = equal_weight_basket(prices, funding, costs, capital).pnl_1m["total"]
        bh = buy_and_hold(prices, funding, costs, capital)
        bh_mean = sum(r.pnl_1m["total"] for r in bh.values()) / len(bh)
        grid = res.pnl_1m.index
        paper_bar = sum(s.reindex(grid, fill_value=0.0) for s in curves_paper)
        frame = pd.DataFrame({"paper": paper_bar, "sim": res.pnl_1m["total"],
                              "canasta": basket.reindex(grid, fill_value=0.0),
                              "buy_and_hold_medio": bh_mean.reindex(grid, fill_value=0.0)})
        freq = "1h" if (end - start) <= pd.Timedelta(days=31) else "1D"
        agg = frame.resample(freq, label="right", closed="left").sum().cumsum() / capital
        curves = {"freq": freq, "columns": list(agg.columns),
                  "points": [[t.isoformat(), *[float(v) for v in row]] for t, row in zip(agg.index, agg.to_numpy())],
                  "basket_return": float(basket.sum() / capital), "buy_and_hold_mean_return": float(bh_mean.sum() / capital)}

    notes = []
    days = (end - start) / pd.Timedelta(days=1)
    n_trades = len({f["trade_id"] for f in all_fills if start_ms <= (f["time_ms"] or 0) < end_ms})
    if days < 30 or n_trades < 10:
        notes.append(f"Ventana corta ({days:.1f} dias, {n_trades} operaciones del paper): la comparacion sirve para "
                     "verificar que el dry-run ejecuta la misma regla, no para evaluar la estrategia.")
    if excluded_after:
        notes.append(f"{excluded_after} fills del dry-run quedaron despues de la ultima vela 1m de la PC ({end}) y no "
                     "entran en esta comparacion (Binance publica el diario al dia siguiente).")
    if abs(decomposition["residual"]) > 1e-6 * capital:
        notes.append(f"Residuo de la descomposicion {decomposition['residual']:.6f} US$ (deberia ser ~0).")
    return _nan_to_null({
        **base, "status": "ok", "spec_hash": spec.spec_hash(), "params": params,
        "capital": {"sim": capital, "sim_por_par": alloc, "paper_wallet": capital,
                    "paper_por_par_base": paper_pair_base, "tradable_balance_ratio": TRADABLE_BALANCE_RATIO,
                    "rebalance_min": REBALANCE_MIN},
        "costs_lab": {"fee_bps": costs.fee_bps, "slippage_bps": costs.default_slippage_bps},
        "totals": {**totals, "diff": diff, "diff_frac": diff / capital},
        "decomposition": decomposition,
        "by_pair": by_pair,
        "curves": curves,
        "data": {"last_kline": str(last - pd.Timedelta(minutes=1)), "gaps": data_gaps,
                 "fills_after_window": excluded_after, "warmup_days": warmup_days},
        "notes": notes,
    })


def _match_trades(sym: str, inside_ok, res, sf: pd.DataFrame, o: np.ndarray, idx: pd.DatetimeIndex) -> list[dict[str, Any]]:
    """Filas paper vs lab por operacion (entrada/salida con tiempo y precio). Empareja por entrada mas cercana."""
    paper_tr: dict[Any, dict[str, Any]] = {}
    for f, _ in inside_ok:
        t = paper_tr.setdefault(f["trade_id"], {"trade_id": f["trade_id"], "entry_ms": None, "entry_price": None,
                                                "exit_ms": None, "exit_price": None, "ajustes": 0})
        if f["kind"] == "entrada":
            t["entry_ms"], t["entry_price"] = f["time_ms"], f["price"]
        elif f["kind"] == "salida":
            t["exit_ms"], t["exit_price"] = f["time_ms"], f["price"]
        else:
            t["ajustes"] += 1
    raw = dict(zip(pd.DatetimeIndex(sf["time"]), sf["price"].astype(float))) if len(sf) else {}
    sim_tr = []
    st = res.trades[res.trades["symbol"] == sym]
    for r in st.itertuples():
        et = pd.Timestamp(r.entry_time)
        xt = None if r.is_open else pd.Timestamp(r.exit_time)
        n_adj = int(((pd.DatetimeIndex(sf["time"]) > et) & (pd.DatetimeIndex(sf["time"]) < (xt or idx[-1] + pd.Timedelta(minutes=1)))).sum()) if len(sf) else 0
        sim_tr.append({"entry": et, "entry_price": raw.get(et), "exit": xt, "exit_price": None if xt is None else raw.get(xt),
                       "ajustes": n_adj, "pnl_net": float(r.pnl_net)})
    rows, used = [], set()
    for t in sorted(paper_tr.values(), key=lambda x: x["entry_ms"] or 0):
        et = _ts(t["entry_ms"])
        best, best_dt = None, None
        if et is not None:
            for i, s in enumerate(sim_tr):
                dt_ = abs(s["entry"] - et)
                if i not in used and dt_ <= MATCH_TOLERANCE and (best_dt is None or dt_ < best_dt):
                    best, best_dt = i, dt_
        s = sim_tr[best] if best is not None else None
        if best is not None:
            used.add(best)
        rows.append(_trade_row(t, s))
    for i, s in enumerate(sim_tr):
        if i not in used:
            rows.append(_trade_row(None, s))
    rows.sort(key=lambda r: r["paper_entry"] or r["sim_entry"] or "")
    return rows


def _trade_row(t: dict[str, Any] | None, s: dict[str, Any] | None) -> dict[str, Any]:
    def iso_ts(x):
        return None if x is None else pd.Timestamp(x).isoformat()

    pe = _ts(t["entry_ms"]) if t else None
    px = _ts(t["exit_ms"]) if t else None
    return {
        "match": "ambos" if t and s else "solo_paper" if t else "solo_lab",
        "paper_trade_id": t["trade_id"] if t else None,
        "paper_entry": iso_ts(pe), "paper_entry_price": t["entry_price"] if t else None,
        "paper_exit": iso_ts(px), "paper_exit_price": t["exit_price"] if t else None,
        "paper_ajustes": t["ajustes"] if t else None,
        "sim_entry": iso_ts(s["entry"]) if s else None, "sim_entry_price": s["entry_price"] if s else None,
        "sim_exit": iso_ts(s["exit"]) if s else None, "sim_exit_price": s["exit_price"] if s else None,
        "sim_ajustes": s["ajustes"] if s else None,
        "entry_delta_min": None if not (t and s and pe is not None) else (pe - s["entry"]) / pd.Timedelta(minutes=1),
        "entry_delta_bps": None if not (t and s) else _bps(t["entry_price"], s["entry_price"]),
        "exit_delta_min": None if not (t and s and px is not None and s["exit"] is not None) else (px - s["exit"]) / pd.Timedelta(minutes=1),
        "exit_delta_bps": None if not (t and s) else _bps(t["exit_price"], s["exit_price"]),
    }


# ---- archivos ------------------------------------------------------------------------------------------------


def history_row(result: dict[str, Any], file_name: str) -> dict[str, Any]:
    tot = result.get("totals") or {}
    return {
        "date": result["generated_at"][:10], "generated_at": result["generated_at"], "file": file_name,
        "status": result.get("status"), "window": result.get("window"),
        "paper_total": (tot.get("paper") or {}).get("total"), "sim_total": (tot.get("sim") or {}).get("total"),
        "diff": tot.get("diff"), "decomposition": result.get("decomposition"),
        "paper_orders": (tot.get("paper") or {}).get("ordenes"), "sim_orders": (tot.get("sim") or {}).get("ordenes"),
    }


def write_outputs(result: dict[str, Any], out_dir: Path) -> list[Path]:
    """compare_<fecha>.json + paper_summary.json (ultimo resultado + historial, una fila por fecha)."""
    out_dir.mkdir(parents=True, exist_ok=True)
    day = result["generated_at"][:10]
    cmp_path = out_dir / f"compare_{day}.json"
    cmp_path.write_text(json.dumps(result, ensure_ascii=False, indent=1))
    summary_path = out_dir / "paper_summary.json"
    history: list[dict[str, Any]] = []
    if summary_path.is_file():
        try:
            history = json.loads(summary_path.read_text()).get("history") or []
        except (OSError, ValueError):
            log.warning("paper_summary.json ilegible: se rehace el historial")
    history = [h for h in history if h.get("date") != day] + [history_row(result, cmp_path.name)]
    history.sort(key=lambda h: h.get("generated_at") or "")
    summary = {"generated_at": result["generated_at"], "latest": result, "history": history[-HISTORY_MAX:]}
    summary_path.write_text(json.dumps(_nan_to_null(summary), ensure_ascii=False, separators=(",", ":")))
    return [cmp_path, summary_path]


def run(*, remote: Remote, since: pd.Timestamp | None = None, out_dir: Path | None = None, update: bool = True,
        upload: bool = False, warmup_days: int = WARMUP_DAYS, root: Path | None = None) -> dict[str, Any]:
    d = remote.dump()
    if since is None:
        kv = d.get("kv", {})
        first = kv.get("bot_start_time") or min((t["open_ms"] for t in d.get("trades", []) if t["open_ms"]), default=None)
        start = _ts(first) if first else None
    else:
        start = since
    if start is None:
        raise ValueError("no se pudo determinar el inicio del paper (base vacia)")
    from .strategies import SEVEN

    upd = update_market_data(SEVEN, start - pd.Timedelta(days=warmup_days), root=root) if update else None
    result = compare(d, start=since, root=root, warmup_days=warmup_days)
    if upd is not None:
        result["data_update"] = upd
        if upd["errors"]:
            result.setdefault("notes", []).append(f"{len(upd['errors'])} errores al actualizar velas/funding (ver data_update).")
    files = write_outputs(result, out_dir or (lab_dir().parent / "paper"))
    result["_files"] = [str(f) for f in files]
    if upload:
        remote.upload(files)
    return result


def _print_summary(r: dict[str, Any]) -> None:
    w = r.get("window", {})
    print(f"ventana {w.get('start')} -> {w.get('end')} ({(w.get('days') or 0):.2f} dias) · estado {r.get('status')}")
    if r.get("status") != "ok":
        for n in r.get("notes", []):
            print("  " + n)
        return
    t = r["totals"]
    print(f"paper  total {t['paper']['total']:+.2f} US$ (precio {t['paper']['precio']:+.2f}, comisiones -{t['paper']['comisiones']:.2f}, "
          f"funding {t['paper']['funding']:+.2f}) · ordenes {t['paper']['ordenes']}")
    print(f"lab    total {t['sim']['total']:+.2f} US$ (precio {t['sim']['precio']:+.2f}, comisiones -{t['sim']['comisiones']:.2f}, "
          f"slippage -{t['sim']['slippage']:.2f}, funding {t['sim']['funding']:+.2f}) · ordenes {t['sim']['ordenes']}")
    dcp = r["decomposition"]
    print(f"diferencia {t['diff']:+.2f} US$ = " + " ".join(f"{k} {dcp[k]:+.2f}" for k in COMPONENTS) + f" (residuo {dcp['residual']:+.2e})")
    for p in r["by_pair"]:
        print(f"  {p['symbol']:<8} paper {p['paper']['total']:+8.2f} lab {p['sim']['total']:+8.2f} dif {p['diff']:+7.2f} · "
              f"ordenes {p['paper']['ordenes']}/{p['sim']['ordenes']} · objetivo al inicio {p['target_start_sim'] or 0:.3f}"
              + (f" · fill {p['fill_bps']['mean']:+.1f} bps" if p['fill_bps']['n'] else ""))
    for n in r.get("notes", []):
        print("  nota: " + n)


def main(argv: Iterable[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Compara el dry-run de Freqtrade contra el backtest del laboratorio.")
    p.add_argument("--since", help="YYYY-MM-DD (UTC): inicio de la ventana; por defecto, el inicio del paper")
    p.add_argument("--upload", action="store_true", help="subir compare_<fecha>.json y paper_summary.json a la VM")
    p.add_argument("--no-update", action="store_true", help="no bajar velas ni funding (usar lo que hay en la PC)")
    p.add_argument("--warmup-days", type=int, default=WARMUP_DAYS)
    p.add_argument("--out", default=None, help="carpeta de salida (por defecto ~/heatsignal-lab/paper)")
    args = p.parse_args(list(argv) if argv is not None else None)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    since = pd.Timestamp(args.since, tz="UTC") if args.since else None
    r = run(remote=GcloudRemote(), since=since, out_dir=Path(args.out) if args.out else None,
            update=not args.no_update, upload=args.upload, warmup_days=args.warmup_days)
    _print_summary(r)
    print("archivos: " + ", ".join(r["_files"]) + (" (subidos a la VM)" if args.upload else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
