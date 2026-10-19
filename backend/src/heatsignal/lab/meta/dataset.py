"""
Etapa 1 del meta-labeling (plan-entrenamiento.md): dataset de eventos y etiquetas de las primarias congeladas.

    python -m heatsignal.lab.meta.dataset build [--out ~/heatsignal-lab/meta]

Escribe `events_v1.parquet` y `events_v1_resumen.json`. NO es un intento: no escribe en `results/` ni en
`trials.sqlite`. La especificacion es `preregistro.json` (congelado); lo que el pre-registro no fija se decide
aca y queda documentado:

1. **Datos**: velas, funding y metrics del DESARROLLO de cada primaria, cortados con `run.development` en el
   inicio de SU holdout (el de la corrida registrada en `results/`, `HOLDOUT_START`). Se carga con `end` = el
   mayor de esos inicios, asi el holdout ni siquiera se lee. Eventos con `t_decision` >= `DEV_START`
   (2021-03-01, "2021-03" del pre-registro); los datos anteriores solo sirven de calentamiento.
2. **Eventos**: la regla congelada con `spec.params` sobre el desarrollo (como `run.simulate_combo`). Apertura =
   la posicion objetivo pasa de 0 a != 0 o cambia de signo. En tsmom ademas cada fila que cambia el tamano con
   el mismo signo es un `rebalanceo`. `t_fin_regla` = cuando la regla cierra ese tramo (vuelve a 0 o gira; para
   un rebalanceo, el fin del tramo que lo contiene); NaT si sigue abierto al final del desarrollo.
   Una renovacion con el mismo valor (max-ruptura que encadena dias, ema que re-entra en el instante de la
   salida) NO es evento: el simulador no opera; se cuenta en `descartes.renovaciones_sin_cambio`.
   `precio_entrada` = open de la primera vela 1m con apertura >= `t_decision` (como `lab/sim.py`).
3. **Barreras** (velas 1m desde la vela de entrada, la que abre en `t_decision`; la vela de la decision NO
   cuenta; empate en la misma vela -> stop):
   - ema-cruce: sus niveles (stop 1,5 ATR y objetivo 3 ATR desde el close de la senal, tomados de la propia
     regla con `exits.capture_exits`) y tope 720 h.
   - resto: sigma = desvio de los retornos log diarios (velas 1D completas, `clock.resample_bars`) de los
     ultimos 20 dias cerrados a mas tardar en `t_decision` (minimo 15 retornos validos: un dia con una vela 1m
     faltante se descarta entero); barreras en precio `entrada * exp(+-k*sigma)`, k = 2. Tope de tiempo:
     max-ruptura 1 dia (su `hold_days`); Donchian y tsmom `min(t_fin_regla, t_decision + 30 d)` (30 d si la regla
     no cerro dentro del desarrollo).
   - Salida: decidida al cierre de la vela que toca (o en el tope de tiempo) y ejecutada al open de la vela
     siguiente, igual que `exits_with_levels` + simulador (no se asume fill en el nivel exacto).
4. **PnL neto** por unidad de nocional de entrada: `lado * (salida/entrada - 1)` - 7 bps por lado (5 comision +
   2 slippage, `Costs()` por defecto, sobre el nocional de entrada y de salida) + funding con la convencion de
   `lab/sim.py` (liquidaciones en (entrada, salida], posicion vigente justo antes, precio = close de la ultima
   vela con apertura < liquidacion). `etiqueta` = 1 si `pnl_neto` > 0. MAE/MFE como en `sim._build_trades`.
   `ventana_completa` = False si el desarrollo termina antes de la salida (se valua al ultimo close).
5. **Pesos por unicidad** (Lopez de Prado, AFML cap. 4): vida del evento = [t_decision, t_salida]; concurrencia
   = eventos vivos de la misma primaria (todas las monedas) en cada instante; peso = promedio temporal de
   1/concurrencia sobre la vida del evento (equivale a la media por vela 1m).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import sys
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from ..clock import resample_bars
from ..exits import capture_exits
from ..run import development, load_market
from ..sim import Costs
from ..strategies import CATALOG, SEVEN, MarketData

log = logging.getLogger("heatsignal.lab.meta.dataset")

PREREG_PATH = Path(__file__).with_name("preregistro.json")
VERSION = "events_v1"
PRIMARIAS = ("donchian-ensemble-v1", "tsmom-diario-v1", "ema-cruce-v1", "max-ruptura-10d-v1")
# inicio del holdout de la corrida registrada de cada primaria (results/<id>/*.json, data.holdout_start)
HOLDOUT_START = {
    "donchian-ensemble-v1": pd.Timestamp("2026-07-16", tz="UTC"),
    "tsmom-diario-v1": pd.Timestamp("2026-07-17", tz="UTC"),
    "ema-cruce-v1": pd.Timestamp("2026-07-17", tz="UTC"),
    "max-ruptura-10d-v1": pd.Timestamp("2026-07-17", tz="UTC"),
}
DEV_START = pd.Timestamp("2021-03-01", tz="UTC")
MAX_HORIZON = pd.Timedelta(days=30)
SIGMA_DAYS = 20
SIGMA_MIN_OBS = 15
MIN = pd.Timedelta(minutes=1)
_COSTS = Costs()
COST_PER_SIDE = (_COSTS.fee_bps + _COSTS.default_slippage_bps) / 1e4  # 7 bps

COLUMNS = ["primaria", "symbol", "tipo", "t_decision", "lado", "tamaño", "tamaño_previo", "t_entrada",
           "precio_entrada", "t_fin_regla", "sigma_diaria", "nivel_sl", "nivel_tp", "t_limite", "barrera",
           "t_decision_salida", "t_salida", "precio_salida", "pnl_bruto", "costos", "funding", "pnl_neto",
           "etiqueta", "mae", "mfe", "ventana_completa", "peso_unicidad"]


def load_prereg(path: Path = PREREG_PATH) -> tuple[dict[str, Any], str]:
    raw = path.read_bytes()
    return json.loads(raw), hashlib.sha256(raw).hexdigest()


# ---- 1. eventos ------------------------------------------------------------------------------------------


def signal_events(sig: pd.Series, rebalances: bool = False) -> list[dict[str, Any]]:
    """Aperturas (0 -> != 0 o cambio de signo) y, con `rebalances`, cambios de tamano con el mismo signo.
    Cada evento lleva `t_fin_regla` = cuando la regla cierra el tramo (NaT si no lo cierra)."""
    s = sig.sort_index()
    s = s[~s.index.duplicated(keep="last")].fillna(0.0)
    events: list[dict[str, Any]] = []
    open_ids: list[int] = []  # eventos del tramo vigente
    pos = 0.0
    for t, w in s.items():
        w = float(w)
        if w == pos:
            continue
        flip = pos != 0 and (w == 0 or np.sign(w) != np.sign(pos))
        if flip:
            for i in open_ids:
                events[i]["t_fin_regla"] = t
            open_ids = []
        if w != 0 and (pos == 0 or np.sign(w) != np.sign(pos)):
            open_ids.append(len(events))
            events.append({"tipo": "apertura", "t_decision": t, "lado": int(np.sign(w)), "tamaño": w,
                           "tamaño_previo": pos, "t_fin_regla": pd.NaT})
        elif w != 0 and rebalances:
            open_ids.append(len(events))
            events.append({"tipo": "rebalanceo", "t_decision": t, "lado": int(np.sign(w)), "tamaño": w,
                           "tamaño_previo": pos, "t_fin_regla": pd.NaT})
        pos = w
    return events


# ---- 2. sigma y triple barrier ---------------------------------------------------------------------------


def daily_sigma(klines: pd.DataFrame, at: Iterable[pd.Timestamp], days: int = SIGMA_DAYS,
                min_obs: int = SIGMA_MIN_OBS) -> np.ndarray:
    """Desvio de los retornos log diarios de los `days` dias cerrados a mas tardar en cada instante de `at`
    (velas 1D completas indexadas por su cierre; un dia incompleto queda NaN y no se salta)."""
    at = pd.DatetimeIndex(list(at))
    d = resample_bars(klines[["open", "high", "low", "close"]], "1D")["close"]
    if d.empty or not len(at):
        return np.full(len(at), np.nan)
    d = d.reindex(pd.date_range(d.index[0], d.index[-1], freq="1D"))
    sig = np.log(d).diff().rolling(days, min_periods=min_obs).std()
    idx = np.searchsorted(sig.index.as_unit("ns").asi8, at.as_unit("ns").asi8, side="right") - 1
    vals = sig.to_numpy()
    return np.where(idx >= 0, vals[np.clip(idx, 0, None)], np.nan)


class _Arrays:
    """Velas 1m y funding de un simbolo en numpy (indices en ns)."""

    def __init__(self, md: MarketData):
        k = md.klines
        self.t = k.index.as_unit("ns").asi8
        self.o, self.h, self.l, self.c = (k[c].to_numpy(float) for c in ("open", "high", "low", "close"))
        f = md.funding if md.funding is not None else pd.Series(dtype=float, index=pd.DatetimeIndex([], tz="UTC"))
        f = f.dropna().sort_index()
        self.ft = pd.DatetimeIndex(f.index).as_unit("ns").asi8
        self.fr = f.to_numpy(float)


def _ns(t: pd.Timestamp) -> int:
    return int(pd.Timestamp(t).as_unit("ns").value)


def triple_barrier(a: _Arrays, t_decision: pd.Timestamp, lado: int, sl: float | None, tp: float | None,
                   t_limite: pd.Timestamp, cost: float = COST_PER_SIDE) -> dict[str, Any] | None:
    """Etiqueta de un evento. Barreras desde la vela que abre en (o despues de) `t_decision`; empate -> stop;
    salida al open de la vela siguiente a la que toca (o de la primera con apertura >= `t_limite`)."""
    n = len(a.t)
    i0 = int(np.searchsorted(a.t, _ns(t_decision), side="left"))
    if i0 >= n:
        return None
    entry = a.o[i0]
    iend = max(int(np.searchsorted(a.t, _ns(t_limite), side="left")), i0)
    hi, lo = a.h[i0:iend], a.l[i0:iend]
    big = iend - i0
    j_sl = j_tp = big
    if sl is not None and np.isfinite(sl):
        m = (lo <= sl) if lado > 0 else (hi >= sl)
        j_sl = int(m.argmax()) if m.any() else big
    if tp is not None and np.isfinite(tp):
        m = (hi >= tp) if lado > 0 else (lo <= tp)
        j_tp = int(m.argmax()) if m.any() else big
    if j_sl < big and j_sl <= j_tp:
        barrera, j = "sl", i0 + j_sl
    elif j_tp < big:
        barrera, j = "tp", i0 + j_tp
    else:
        barrera, j = "tiempo", None
    if j is not None:
        t_exit_dec = int(a.t[j]) + MIN.value
        last = j
    else:
        t_exit_dec = _ns(t_limite)
        last = iend - 1
    ix = int(np.searchsorted(a.t, t_exit_dec, side="left"))
    complete = ix < n
    if complete:
        exit_px, t_exit = a.o[ix], int(a.t[ix])
    else:
        exit_px, t_exit = a.c[-1], int(a.t[-1]) + MIN.value
        last = n - 1 if j is None else last
    if last >= i0:
        mx, mn = a.h[i0:last + 1].max(), a.l[i0:last + 1].min()
    else:
        mx = mn = entry
    mfe = mx / entry - 1.0 if lado > 0 else 1.0 - mn / entry
    mae = mn / entry - 1.0 if lado > 0 else 1.0 - mx / entry
    gross = lado * (exit_px / entry - 1.0)
    costs = cost * (1.0 + exit_px / entry)
    f0 = int(np.searchsorted(a.ft, int(a.t[i0]), side="right"))
    f1 = int(np.searchsorted(a.ft, t_exit, side="right"))
    funding = 0.0
    if f1 > f0:
        k = np.searchsorted(a.t, a.ft[f0:f1], side="left") - 1
        funding = float(-lado * np.sum(a.fr[f0:f1] * a.c[np.clip(k, 0, None)]) / entry)
    pnl = gross - costs + funding
    return {"t_entrada": pd.Timestamp(int(a.t[i0]), tz="UTC"), "precio_entrada": float(entry), "barrera": barrera,
            "t_decision_salida": pd.Timestamp(t_exit_dec, tz="UTC"), "t_salida": pd.Timestamp(t_exit, tz="UTC"),
            "precio_salida": float(exit_px), "pnl_bruto": float(gross), "costos": float(costs),
            "funding": funding, "pnl_neto": float(pnl), "etiqueta": int(pnl > 0), "mae": float(mae),
            "mfe": float(mfe), "ventana_completa": bool(complete)}


# ---- 3. pesos por unicidad ------------------------------------------------------------------------------


def uniqueness_weights(t0: pd.Series | np.ndarray, t1: pd.Series | np.ndarray) -> np.ndarray:
    """Promedio temporal de 1/concurrencia sobre [t0, t1) de cada evento (AFML cap. 4)."""
    a = pd.DatetimeIndex(t0).as_unit("ns").asi8
    b = pd.DatetimeIndex(t1).as_unit("ns").asi8
    if not len(a):
        return np.array([], dtype=float)
    b = np.maximum(b, a + 1)  # vida minima de 1 ns (no deberia pasar: la salida es >= 1 min despues)
    pts = np.unique(np.r_[a, b])
    ia, ib = np.searchsorted(pts, a), np.searchsorted(pts, b)
    delta = np.zeros(len(pts), dtype=float)
    np.add.at(delta, ia, 1.0)
    np.add.at(delta, ib, -1.0)
    conc = np.cumsum(delta)[:-1]
    inv = np.where(conc > 0, 1.0 / np.where(conc > 0, conc, 1.0), 0.0)
    integ = np.r_[0.0, np.cumsum(inv * np.diff(pts).astype(float))]
    return (integ[ib] - integ[ia]) / (pts[ib] - pts[ia]).astype(float)


# ---- 4. armado -----------------------------------------------------------------------------------------


def _ema_levels(rule, dev: dict[str, MarketData], params: dict[str, Any]):
    """Corre la regla capturando sus niveles de stop/objetivo: (senal, {(simbolo, t_entrada): (sl, tp)})."""
    with capture_exits() as rec:
        sig = rule(dev, params)
    by_id = {id(md.klines): s for s, md in dev.items()}
    levels: dict[tuple[str, pd.Timestamp], tuple[float | None, float | None]] = {}
    for r in rec:
        sym = by_id.get(r["key"])
        for tr in r["trades"]:
            levels[(sym, tr["entry"])] = (tr.get("sl"), tr.get("tp"))
    return sig, levels


def build_primaria(primaria: str, data: dict[str, MarketData], holdout_start: pd.Timestamp,
                   k_sigma: float, dev_start: pd.Timestamp = DEV_START,
                   stats: dict[str, int] | None = None) -> pd.DataFrame:
    """Eventos etiquetados de una primaria. `stats` (opcional) recibe los conteos de lo que no entra."""
    stats = stats if stats is not None else {}
    stats.update({"sin_sigma": 0, "sin_vela_de_entrada": 0, "renovaciones_sin_cambio": 0})
    spec, rule = CATALOG[primaria]
    dev = development(data, holdout_start)  # el holdout nunca llega a la regla ni a las etiquetas
    params = dict(spec.params)
    levels: dict = {}
    if primaria.startswith("ema-cruce"):
        sig, levels = _ema_levels(rule, dev, params)
    else:
        sig = rule(dev, params)
    rows = []
    for sym in dev:
        s = sig.get(sym)
        if s is None or not len(s):
            continue
        sd_ = s.sort_index().fillna(0.0)
        sd_ = sd_[~sd_.index.duplicated(keep="last")]
        # la regla renueva la misma posicion (p. ej. max-ruptura encadena dias): el simulador no opera, no es apertura
        stats["renovaciones_sin_cambio"] += int(((sd_ != 0) & (sd_ == sd_.shift(1)) & (sd_.index >= dev_start)).sum())
        evs = [e for e in signal_events(s, rebalances=primaria == "tsmom-diario-v1") if e["t_decision"] >= dev_start]
        if not evs:
            continue
        arr = _Arrays(dev[sym])
        sig_d = daily_sigma(dev[sym].klines, [e["t_decision"] for e in evs])
        for e, sd in zip(evs, sig_d):
            t = e["t_decision"]
            i0 = int(np.searchsorted(arr.t, _ns(t), side="left"))
            if i0 >= len(arr.t):
                stats["sin_vela_de_entrada"] += 1
                continue
            entry = arr.o[i0]
            if primaria.startswith("ema-cruce"):
                sl, tp = levels.get((sym, t), (None, None))
                t_lim = t + pd.Timedelta(hours=params["max_hold_hours"])
            else:
                if not np.isfinite(sd) or sd <= 0:
                    stats["sin_sigma"] += 1  # falta historia: se cuenta en el resumen
                    continue
                lo_, hi_ = entry * np.exp(-k_sigma * sd), entry * np.exp(k_sigma * sd)
                sl, tp = (lo_, hi_) if e["lado"] > 0 else (hi_, lo_)
                if primaria == "max-ruptura-10d-v1":
                    t_lim = t + pd.Timedelta(days=params["hold_days"])
                else:
                    fin = e["t_fin_regla"]
                    t_lim = t + MAX_HORIZON if pd.isna(fin) else min(fin, t + MAX_HORIZON)
            lab = triple_barrier(arr, t, e["lado"], sl, tp, t_lim)
            if lab is None:
                continue
            rows.append({"primaria": primaria, "symbol": sym, **e, "sigma_diaria": float(sd),
                         "nivel_sl": np.nan if sl is None else float(sl),
                         "nivel_tp": np.nan if tp is None else float(tp), "t_limite": t_lim, **lab})
    df = pd.DataFrame(rows)
    if df.empty:
        return pd.DataFrame(columns=COLUMNS)
    df = df.sort_values(["t_decision", "symbol"]).reset_index(drop=True)
    df["peso_unicidad"] = uniqueness_weights(df["t_decision"], df["t_salida"])
    return df[COLUMNS]


def summarize(df: pd.DataFrame, prereg_hash: str, extra: dict[str, Any] | None = None) -> dict[str, Any]:
    out: dict[str, Any] = {"version": VERSION, "preregistro_sha256": prereg_hash, "n": int(len(df)), "primarias": {}}
    for p, g in df.groupby("primaria", sort=False):
        out["primarias"][p] = {
            "n": int(len(g)),
            "por_tipo": {k: int(v) for k, v in g["tipo"].value_counts().items()},
            "tasa_etiqueta_1": round(float(g["etiqueta"].mean()), 4),
            "barreras": {k: int(v) for k, v in g["barrera"].value_counts().items()},
            "ventana_incompleta": int((~g["ventana_completa"]).sum()),
            "t_decision_min": str(g["t_decision"].min()), "t_decision_max": str(g["t_decision"].max()),
            "pnl_neto_medio": round(float(g["pnl_neto"].mean()), 6),
            "peso_unicidad_medio": round(float(g["peso_unicidad"].mean()), 4),
            "holdout_start": str(HOLDOUT_START.get(p)),
        }
    if extra:
        out.update(extra)
    return out


def build(data: dict[str, MarketData] | None = None, out_dir: Path | None = None,
          primarias: Iterable[str] = PRIMARIAS, holdout_start: dict[str, pd.Timestamp] | None = None,
          dev_start: pd.Timestamp = DEV_START) -> tuple[pd.DataFrame, dict[str, Any]]:
    prereg, h = load_prereg()
    holdout_start = holdout_start or HOLDOUT_START
    primarias = list(primarias)
    for p in primarias:  # las reglas tienen que ser las congeladas del pre-registro
        got = CATALOG[p][0].spec_hash()
        if prereg["primarias"].get(p) != got:
            raise RuntimeError(f"{p}: spec_hash {got} != pre-registro {prereg['primarias'].get(p)}")
    if data is None:
        needs = tuple(sorted({n for p in primarias for n in CATALOG[p][0].needs}))
        data = load_market(SEVEN, needs, end=max(holdout_start[p] for p in primarias))
    k = float(prereg["etiquetas"]["k_sigma"])
    frames, descartes = [], {}
    for p in primarias:
        log.info("%s: armando eventos", p)
        descartes[p] = {}
        frames.append(build_primaria(p, data, holdout_start[p], k, dev_start, descartes[p]))
    df = pd.concat([f for f in frames if len(f)], ignore_index=True) if any(len(f) for f in frames) \
        else pd.DataFrame(columns=COLUMNS)
    summary = summarize(df, h, {"k_sigma": k, "dev_start": str(dev_start), "max_horizonte_dias": MAX_HORIZON.days,
                                "costo_bps_por_lado": COST_PER_SIDE * 1e4, "funding": "incluido (convencion lab/sim.py)",
                                "descartes": descartes})
    if out_dir is not None:
        out_dir.mkdir(parents=True, exist_ok=True)
        df.to_parquet(out_dir / f"{VERSION}.parquet", index=False)
        (out_dir / f"{VERSION}_resumen.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False))
    return df, summary


def main(argv: Iterable[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m heatsignal.lab.meta.dataset")
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build", help="arma events_v1.parquet + resumen (no es un intento)")
    b.add_argument("--out", type=Path, default=Path.home() / "heatsignal-lab" / "meta")
    args = ap.parse_args(list(argv) if argv is not None else None)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    _, summary = build(out_dir=args.out.expanduser())
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
