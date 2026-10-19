"""
Etapa 3, subtarea C: meta-modelo LightGBM (las 4 configuraciones pre-registradas G1-G4) con la evaluacion 3c.

Especificacion (decisions.md, "Meta-modelo 3c pre-registrada (2026-10-19 ...)"), sin variantes:
- Misma etiqueta, mismo preproceso (``train_baseline``: mediana + indicador de faltante > 5 %, estandarizacion,
  one-hot de las categoricas, todo DENTRO del fold), mismo marco (purged k-fold k = 5, embargo 5 d, calibracion
  ``FoldCalibrated``, pesos de unicidad, primaria como categorica, ``pm_*`` fuera) y solo desarrollo.
- G1 = nucleo, max_depth 3, num_leaves 7, n_estimators 200, learning_rate 0,03, min_child_samples 100,
  reg_lambda 5, subsample 0,8 (bagging_freq 1), colsample_bytree 0,8, semilla 20261019.
  G2 = nucleo, max_depth 2, num_leaves 3, n_estimators 300, learning_rate 0,03, min_child_samples 200,
  reg_lambda 10, subsample 0,8, colsample_bytree 0,8 (bagging_freq 1 y la semilla de G1: el pre-registro no las
  repite; sin bagging_freq el subsample no tiene efecto en LightGBM). G3 = las 78 con G1; G4 = las 78 con G2.
  ``verbose=-1`` y ``n_jobs`` (<= 8) no entran en el hash.
- Evaluacion 3c (``validation``): umbral = tasa base ponderada del train de cada fold (``b_fold``), filtrada = p > b,
  tamano = clip((p - b) / b, 0, 1). Economia con ``pnl_regla`` (salida real de la regla, imputada al dia de
  ``t_salida_regla``); al lado, la misma con el PnL de barreras (``pnl_neto``).
- Registro: ``MetaTrialRegistry.run`` (el diario filtrado que se guarda es el de ``pnl_regla``, conjunto).
- DSR con K = configuraciones registradas (8 con L1-L4). PBO: las predicciones OOS de L1-L4 NO se guardaron
  (``meta_trials`` solo tiene su diario filtrado con umbral 0,5 y PnL de barreras, que no es comparable), asi que
  el PBO es sobre G1-G4 con la evaluacion 3c. L1-L4 no se re-entrenan.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from collections.abc import Iterable, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from lightgbm import LGBMClassifier
from sklearn.base import BaseEstimator, ClassifierMixin
from sklearn.compose import ColumnTransformer
from sklearn.impute import SimpleImputer
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import OneHotEncoder, StandardScaler

from heatsignal.lab.diagnostics import pbo_cscv
from heatsignal.lab.meta import features as F
from heatsignal.lab.meta import train_baseline as B
from heatsignal.lab.meta import validation as V

log = logging.getLogger("heatsignal.lab.meta.train_lgbm")

N_JOBS = 8
HP_G1: dict[str, Any] = {"max_depth": 3, "num_leaves": 7, "n_estimators": 200, "learning_rate": 0.03,
                         "min_child_samples": 100, "reg_lambda": 5.0, "subsample": 0.8, "subsample_freq": 1,
                         "colsample_bytree": 0.8, "random_state": 20261019}
HP_G2: dict[str, Any] = {"max_depth": 2, "num_leaves": 3, "n_estimators": 300, "learning_rate": 0.03,
                         "min_child_samples": 200, "reg_lambda": 10.0, "subsample": 0.8, "subsample_freq": 1,
                         "colsample_bytree": 0.8, "random_state": 20261019}


def _cfg(features: Sequence[str], hp: dict[str, Any]) -> V.MetaConfig:
    return V.MetaConfig("lightgbm", ("primaria", *features), dict(hp))


CONFIGS: dict[str, V.MetaConfig] = {
    "G1": _cfg(B.NUCLEO, HP_G1),
    "G2": _cfg(B.NUCLEO, HP_G2),
    "G3": _cfg(B.TODAS, HP_G1),
    "G4": _cfg(B.TODAS, HP_G2),
}
PREREG_HASHES = frozenset(c.hash for c in CONFIGS.values())
EVAL_REGLA = {"base_col": "b_fold", "pnl_col": "pnl_regla", "exit_col": "t_salida_regla"}
EVAL_BARRERAS = {"base_col": "b_fold", "pnl_col": "pnl_neto", "exit_col": "t_salida"}


def parse_configs(text: str) -> list[str]:
    names = [s.strip().upper() for s in text.split(",") if s.strip()]
    bad = [n for n in names if n not in CONFIGS]
    if bad or not names:
        raise ValueError(f"configuraciones no pre-registradas: {bad or text!r} (solo {sorted(CONFIGS)})")
    return list(dict.fromkeys(names))


class MetaLGBM(ClassifierMixin, BaseEstimator):
    """Mismo preproceso que ``train_baseline.MetaLogit`` + ``LGBMClassifier`` (re-ajustado entero en cada fold)."""

    def __init__(self, hiper: dict[str, Any] | None = None, cat_idx: tuple[int, ...] = (), n_features: int = 0,
                 n_jobs: int = N_JOBS) -> None:
        self.hiper = hiper
        self.cat_idx = cat_idx
        self.n_features = n_features
        self.n_jobs = n_jobs

    def _pipeline(self) -> Pipeline:
        cat = list(self.cat_idx)
        num = [i for i in range(self.n_features) if i not in set(cat)]
        pre = ColumnTransformer([
            ("num", Pipeline([("imp", B.MedianIndicator()), ("std", StandardScaler())]), num),
            ("cat", Pipeline([("fill", SimpleImputer(strategy="constant", fill_value=-999.0)),
                              ("oh", OneHotEncoder(handle_unknown="ignore", sparse_output=False))]), cat),
        ])
        gbm = LGBMClassifier(**dict(self.hiper or {}), n_jobs=min(int(self.n_jobs), N_JOBS), verbose=-1)
        return Pipeline([("pre", pre), ("gbm", gbm)])

    def fit(self, X: Any, y: Any, sample_weight: Any = None) -> MetaLGBM:
        self.pipe_ = self._pipeline()
        self.pipe_.fit(X, y, gbm__sample_weight=sample_weight)
        self.classes_ = self.pipe_.classes_
        return self

    def predict_proba(self, X: Any) -> np.ndarray:
        return self.pipe_.predict_proba(X)

    def predict(self, X: Any) -> np.ndarray:
        return self.pipe_.predict(X)


def make_model_factory(cfg: V.MetaConfig, n_jobs: int = N_JOBS):
    feats = list(cfg.features)
    cat_idx = tuple(i for i, f in enumerate(feats) if f in B.CATEGORICAS)
    return lambda: MetaLGBM(hiper=dict(cfg.hiper), cat_idx=cat_idx, n_features=len(feats), n_jobs=n_jobs)


def evaluate_oos(oos: pd.DataFrame, cfg_hash: str, n_boot: int) -> dict[str, Any]:
    """Metricas 3c sobre las predicciones OOS (``primaria`` ya por nombre)."""
    por_prim = {str(p): V.statistical_metrics(g["etiqueta"], g["p"], g["w"])
                for p, g in oos.groupby("primaria", sort=True)}
    return {"config": cfg_hash, "estadisticas": V.statistical_metrics(oos["etiqueta"], oos["p"], oos["w"]),
            "estadisticas_por_primaria": por_prim,
            "economicas": V.economic_metrics(oos, n_boot=n_boot, **EVAL_REGLA),
            "economicas_barreras": V.economic_metrics(oos, n_boot=n_boot, **EVAL_BARRERAS),
            "umbral_por_fold": oos.groupby("fold")["b_fold"].first().to_dict(),
            "calibracion_por_fold": oos.groupby("fold")["calibracion"].first().to_dict()}


def _evaluate_named(ev: pd.DataFrame, cfg: V.MetaConfig, n_boot: int, n_jobs: int) -> dict[str, Any]:
    oos = V.oos_predictions(ev, cfg.features, make_model_factory(cfg, n_jobs))
    oos["primaria"] = oos["primaria_nombre"]
    return evaluate_oos(oos, cfg.hash, n_boot)


def criterio(m: dict[str, Any], pbo: float | None) -> dict[str, Any]:
    """Criterio pre-registrado para la etapa 4 (conjunto, con pnl_regla)."""
    st, ec = m["estadisticas"], m["economicas"]["_conjunto"]
    checks = {"auc_mayor_0_55": st["auc"] is not None and st["auc"] > 0.55,
              "brier_mejor_que_base": st["brier"] < st["brier_base"],
              "filtrada_mayor_sin_filtrar": (ec["filtrada"]["sharpe"] or -np.inf) > (ec["sin_filtrar"]["sharpe"]
                                                                                    or -np.inf),
              "p_bootstrap_menor_igual_0_05": ec["p_mejora_filtrada"] <= 0.05,
              "pbo_menor_0_5": pbo is not None and pbo < 0.5}
    return {**checks, "cumple": all(checks.values())}


def run(names: Sequence[str], db: Path = V.DEFAULT_DB, data_dir: Path = F.DEFAULT_DIR,
        out_dir: Path = F.DEFAULT_DIR, n_boot: int = 2000, n_jobs: int = N_JOBS) -> Path:
    for n in names:
        if CONFIGS[n].hash not in PREREG_HASHES:
            raise ValueError(n)
    t_all = time.time()
    ev, dropped = B.load_events(data_dir)
    missing = [c for c in (*B.TODAS, "pnl_regla", "t_salida_regla") if c not in ev]
    if missing:
        raise RuntimeError(f"faltan columnas en el dataset: {missing}")
    reg = V.MetaTrialRegistry(db)
    res: dict[str, Any] = {}
    try:
        for n in names:
            cfg = CONFIGS[n]
            repetida = reg.check(cfg)
            t0 = time.time()
            metrics = reg.run(cfg, lambda cfg=cfg: _evaluate_named(ev, cfg, n_boot, n_jobs))
            res[n] = {"hash": cfg.hash, "hiper": cfg.hiper, "n_features": len(cfg.features),
                      "repeticion_mismo_hash": repetida, "segundos": round(time.time() - t0, 1), "metrics": metrics}
            log.info("%s (%s) listo en %.1f s", n, cfg.hash, time.time() - t0)
        k = reg.k
        for n in names:
            daily = res[n]["metrics"]["economicas"]["_daily"]["filtrada"]
            try:
                res[n]["dsr_filtrada_conjunto"] = V.meta_dsr(daily, reg)
            except ValueError as e:
                res[n]["dsr_filtrada_conjunto"] = {"error": str(e)}
        first = reg.results()  # primer resultado de cada hash (no la mejor corrida)
        g_hashes = [CONFIGS[n].hash for n in CONFIGS if CONFIGS[n].hash in first]
        mat = pd.concat({h: first[h]["daily"] for h in g_hashes}, axis=1).sort_index().fillna(0.0)
        pbo = pbo_cscv(mat, n_blocks=16) if mat.shape[1] >= 2 else {"error": "menos de 2 configuraciones G"}
        pbo["sobre"] = "G1-G4 (evaluacion 3c, pnl_regla filtrada, conjunto); L1-L4 sin predicciones OOS guardadas"
        hashes = reg.hashes()
    finally:
        reg.close()
    pbo_val = pbo.get("pbo")
    for n in names:
        m = res[n]["metrics"]
        m["economicas"] = V._clean(m["economicas"])
        m["economicas_barreras"] = V._clean(m["economicas_barreras"])
        res[n]["criterio"] = criterio(m, pbo_val)
    out = {"generado": datetime.now(timezone.utc).isoformat(), "prereg_hash": V.PREREG_HASH,
           "dataset": {"n": int(len(ev)), "descartados_post_desarrollo": dropped, "dev_end": str(B.DEV_END),
                       "por_primaria": ev["primaria_nombre"].value_counts().sort_index().to_dict()},
           "evaluacion": {"umbral": "tasa base ponderada del train de cada fold (p > b)",
                          "tamano": "clip((p - b) / b, 0, 1)", "pnl": "pnl_regla (economicas); pnl_neto "
                          "(economicas_barreras)", "dsr_k": k},
           "k_meta": k, "hashes_registrados": hashes, "pbo": pbo, "configuraciones": res,
           "alguna_cumple_criterio": any(r["criterio"]["cumple"] for r in res.values()),
           "segundos_total": round(time.time() - t_all, 1)}
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"lgbm_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.json"
    path.write_text(json.dumps(V._clean(out), indent=2, ensure_ascii=False, default=str))
    return path


def main(argv: Iterable[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m heatsignal.lab.meta.train_lgbm")
    ap.add_argument("--configs", default="G1,G2,G3,G4", help="subconjunto de G1,G2,G3,G4 (nada mas)")
    ap.add_argument("--db", type=Path, default=V.DEFAULT_DB)
    ap.add_argument("--data", type=Path, default=F.DEFAULT_DIR)
    ap.add_argument("--out", type=Path, default=F.DEFAULT_DIR)
    ap.add_argument("--n-jobs", type=int, default=N_JOBS)
    args = ap.parse_args(list(argv) if argv is not None else None)
    try:
        names = parse_configs(args.configs)
    except ValueError as e:
        ap.error(str(e))
    if not 1 <= args.n_jobs <= N_JOBS:
        ap.error(f"--n-jobs entre 1 y {N_JOBS}")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    path = run(names, args.db.expanduser(), args.data.expanduser(), args.out.expanduser(), n_jobs=args.n_jobs)
    print(path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
