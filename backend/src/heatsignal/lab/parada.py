"""
Criterios de parada PRE-REGISTRADOS de la prueba hacia adelante (decisions.md, consulta al trader 2026-10-20).

    python -m heatsignal.lab.parada            # escribe paperbot/parada_preregistrada.json (versionado)
    python -m heatsignal.lab.parada --check    # recalcula y compara contra el JSON congelado (no escribe)

Se genera UNA vez, antes de que haya datos vivos significativos, y queda congelado (hash fijado en un test).

Serie de retornos diarios del backtest de cada familia principal, con los resultados OOS (walk-forward) de sus
componentes (`results/<componente>/2*.json`, `oos.equity_daily` = retorno acumulado por suma simple -> se diferencia):
  - long  = media de las 12 rupturas long (1/12 cada una);
  - short = media de las 12 rupturas short;
  - neta  = long + short (cada componente a 1/12).
APROXIMACION: la neta real netea posiciones por simbolo (una long y una short activas se cancelan y no pagan costos);
aca se suman los retornos NETOS de costos de cada pata, asi que los costos estan sobrecontados -> la distribucion de
drawdown de la neta es conservadora (mas profunda que la real).

Bootstrap estacionario (Politis-Romano; bloques de largo medio 20 dias, B = 5000, semilla fija) de caminos de h dias
(h = 30, 90, 180, 365) remuestreados de la serie entera. Por camino:
  - drawdown maximo con la convencion del lab (`stats.max_drawdown`, sin componer): equity = 1 + cumsum(r), arranca
    en 1, DD = max (pico - equity) / pico;
  - duracion maxima bajo el agua: la racha mas larga de dias seguidos con equity < pico previo (incluido el 1 inicial).
Se guardan P50, P95 y P99 de cada una.

Regla (la evalua `paperbot/parada.py`): drawdown en vivo desde el inicio > P95 del horizonte transcurrido
(interpolado) -> "alerta: tamano a la mitad"; > P99 -> "parar". Igual con la duracion bajo el agua. En paper NO
cambia operaciones (se muestra y se registra); en plata real es regla obligatoria.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np

GRID = [(n, h) for n in (5, 10, 20, 55) for h in (1, 3, 5)]
LONG = [f"l6-ruptura-n{n}-h{h}-long-v1" for n, h in GRID]
SHORT = [f"l6-ruptura-n{n}-h{h}-short-v1" for n, h in GRID]
FAMILIES = {"l6-familia-ruptura-long-v1": "long", "l6-familia-ruptura-short-v1": "short",
            "l6-familia-ruptura-neta-v1": "neta"}
HORIZONS = (1, 3, 7, 14, 30, 90, 180, 365)  # enmienda 2026-10-21: horizontes cortos (antes se interpolaba linealmente desde 0)
QUANTILES = (50, 95, 99)
BLOCK_MEAN = 20.0
N_BOOT = 5000
SEED = 20261020
OUT = Path(__file__).resolve().parents[1] / "paperbot" / "parada_preregistrada.json"


def daily_from_equity(equity_daily: list[list[Any]]) -> dict[str, float]:
    """[[fecha, acumulado], ...] (suma simple) -> {fecha: retorno del dia}."""
    out, prev = {}, 0.0
    for d, v in equity_daily:
        out[str(d)] = float(v) - prev
        prev = float(v)
    return out


def _latest(results_dir: Path, sid: str) -> Path:
    runs = sorted((results_dir / sid).glob("2*.json"))
    if not runs:
        raise FileNotFoundError(f"sin resultado para {sid} en {results_dir / sid}")
    return runs[-1]


def family_series(results_dir: Path) -> tuple[list[str], dict[str, np.ndarray], dict[str, str]]:
    """Fechas comunes y retornos diarios de las tres familias; + {componente: archivo usado}."""
    comps, used = {}, {}
    for sid in LONG + SHORT:
        p = _latest(results_dir, sid)
        used[sid] = p.name
        comps[sid] = daily_from_equity(json.loads(p.read_text())["oos"]["equity_daily"])
    dates = sorted(set().union(*[set(c) for c in comps.values()]))
    mat = {sid: np.array([c.get(d, 0.0) for d in dates]) for sid, c in comps.items()}
    long = np.mean([mat[s] for s in LONG], axis=0)
    short = np.mean([mat[s] for s in SHORT], axis=0)
    return dates, {"long": long, "short": short, "neta": long + short}, used


def stationary_indices(n_src: int, h: int, block_mean: float, n_boot: int, rng: np.random.Generator) -> np.ndarray:
    """(n_boot x h) indices del bootstrap estacionario sobre una serie de largo n_src (circular)."""
    p = 1.0 / block_mean
    new_block = rng.random((n_boot, h)) < p
    new_block[:, 0] = True
    starts = rng.integers(0, n_src, size=(n_boot, h))
    pos = np.broadcast_to(np.arange(h), (n_boot, h))
    last = np.where(new_block, pos, 0)
    np.maximum.accumulate(last, axis=1, out=last)
    return (np.take_along_axis(starts, last, axis=1) + (pos - last)) % n_src


def path_stats(paths: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Drawdown maximo (convencion del lab, sin componer) y duracion maxima bajo el agua (dias) de cada fila."""
    eq = np.concatenate([np.ones((paths.shape[0], 1)), 1.0 + np.cumsum(paths, axis=1)], axis=1)
    peak = np.maximum.accumulate(eq, axis=1)
    with np.errstate(divide="ignore", invalid="ignore"):
        dd = np.where(peak > 0, (peak - eq) / peak, np.nan)
    mdd = np.nanmax(dd, axis=1)
    under = eq[:, 1:] < peak[:, 1:] - 1e-12
    run = np.zeros(paths.shape[0], dtype=np.int64)
    best = np.zeros(paths.shape[0], dtype=np.int64)
    for j in range(under.shape[1]):
        run = np.where(under[:, j], run + 1, 0)
        np.maximum(best, run, out=best)
    return mdd, best


def bootstrap(returns: np.ndarray, horizons=HORIZONS, block_mean: float = BLOCK_MEAN, n_boot: int = N_BOOT,
              seed: int = SEED) -> dict[str, dict[str, dict[str, float]]]:
    """{str(h): {"drawdown": {"p50":..}, "duracion_dias": {...}}} con un generador por serie (semilla fija)."""
    r = np.asarray(returns, dtype=float)
    rng = np.random.default_rng(seed)
    out = {}
    for h in horizons:
        idx = stationary_indices(len(r), int(h), block_mean, n_boot, rng)
        mdd, dur = path_stats(r[idx])
        out[str(h)] = {
            "drawdown": {f"p{q}": round(float(np.percentile(mdd, q)), 6) for q in QUANTILES},
            "duracion_dias": {f"p{q}": round(float(np.percentile(dur, q)), 2) for q in QUANTILES},
        }
    return out


def content_hash(doc: dict[str, Any]) -> str:
    """sha256 (16 hex) del JSON canonico sin los campos `hash` y `generado_at`."""
    body = {k: v for k, v in doc.items() if k not in ("hash", "generado_at")}
    return hashlib.sha256(json.dumps(body, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()[:16]


def build(results_dir: Path) -> dict[str, Any]:
    dates, series, used = family_series(results_dir)
    fams = {}
    for fid, side in FAMILIES.items():
        r = series[side]
        fams[fid] = {
            "lado": side,
            "serie": {"dias": len(r), "media_diaria": round(float(r.mean()), 8), "desvio_diario": round(float(r.std(ddof=1)), 8),
                      "drawdown_max_historico": round(float(path_stats(r[None, :])[0][0]), 6)},
            "horizontes": bootstrap(r),
        }
    doc = {
        "version": 1,
        "fecha": "2026-10-21",
        "pre_registrado": "Antes de que haya datos vivos significativos (decisions.md: consulta a un trader profesional).",
        "metodo": {
            "bootstrap": "estacionario (Politis-Romano), circular", "bloque_medio_dias": BLOCK_MEAN, "B": N_BOOT,
            "semilla": SEED, "horizontes_dias": list(HORIZONS), "percentiles": list(QUANTILES),
            "drawdown": "convencion del lab (stats.max_drawdown sin componer): equity = 1 + cumsum(r) desde 1; "
                        "DD = max (pico - equity) / pico",
            "duracion": "racha maxima de dias seguidos con equity < pico previo",
            "serie": "OOS walk-forward de los componentes (oos.equity_daily acumulada por suma, diferenciada); long = "
                     "media de las 12 long, short = media de las 12 short, neta = long + short (cada componente 1/12)",
            "aproximacion": "la neta real netea posiciones por simbolo y paga menos costos; aca se suman las patas "
                            "netas de costos -> drawdown sobrecontado: umbrales conservadores",
            "periodo": [dates[0], dates[-1]],
            "componentes": used,
        },
        "regla": {
            "alerta": "drawdown o duracion bajo el agua en vivo desde el inicio > P95 del horizonte transcurrido "
                      "(interpolado linealmente entre 0 -> 0, 30, 90, 180 y 365 dias; despues de 365 se usa el de 365) "
                      "-> 'alerta: tamano a la mitad'",
            "parar": "> P99 -> 'parar'",
            "paper": "en paper NO cambia operaciones: se muestra en el panel y en la revision semanal y se registra",
            "plata_real": "en la etapa de plata real es regla OBLIGATORIA: alerta = tamano a la mitad, parar = cerrar y "
                          "no reabrir sin una decision nueva registrada",
        },
        "familias": fams,
    }
    doc["hash"] = content_hash(doc)
    doc["generado_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    return doc


def main(argv: list[str] | None = None) -> int:
    from .diagnostics import _results_dir

    p = argparse.ArgumentParser(description="Criterios de parada pre-registrados (bootstrap del drawdown)")
    p.add_argument("--results", default=None)
    p.add_argument("--out", default=str(OUT))
    p.add_argument("--check", action="store_true", help="recalcula y compara contra el JSON existente; no escribe")
    p.add_argument("--force", action="store_true", help="sobrescribir un JSON congelado (NO: esta pre-registrado)")
    a = p.parse_args(argv)
    doc = build(Path(a.results) if a.results else _results_dir())
    out = Path(a.out)
    if a.check:
        old = json.loads(out.read_text())
        ok = old.get("hash") == doc["hash"]
        print(f"hash congelado {old.get('hash')} / recalculado {doc['hash']}: {'igual' if ok else 'DISTINTO'}")
        return 0 if ok else 1
    if out.exists() and not a.force:
        print(f"{out} ya existe (congelado, hash {json.loads(out.read_text()).get('hash')}); no se sobrescribe")
        return 1
    out.write_text(json.dumps(doc, indent=1, ensure_ascii=False) + "\n")
    for fid, f in doc["familias"].items():
        print(fid, {h: (v["drawdown"], v["duracion_dias"]) for h, v in f["horizontes"].items()})
    print(f"-> {out} hash {doc['hash']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
