"""
Etapa 3, subtarea B: primer entrenamiento real del meta-modelo (las 4 configuraciones pre-registradas L1-L4).

Especificacion (decisions.md, "Meta-modelo 3a ... y 3b pre-registrada"), sin variantes:
- Un solo modelo para las 4 primarias; ``primaria`` entra como categorica.
- Preproceso DENTRO de cada fold (y de cada sub-fold de la calibracion), porque el estimador entero se re-ajusta
  con el train que recibe: mediana en las numericas (+ indicador de faltante solo en las columnas con > 5 % de NaN
  en ESE train), estandarizacion, one-hot de las categoricas listadas (el faltante de una categorica es una
  categoria mas).
- ``pm_*`` (Polymarket) excluidas. Pesos de unicidad (``peso_unicidad`` del dataset). Calibracion segun
  ``validation.FoldCalibrated``. Umbral 0,5 fijo (``validation.THRESHOLD``).
- L1 = logistica nucleo C = 1; L2 = nucleo C = 0,1; L3 = las 78 features C = 0,1; L4 = las 78 C = 1.
- Solo desarrollo: se descartan los eventos con ``t_decision`` >= 2026-07-16 (el inicio de holdout mas temprano
  de las primarias, comun a todas) y despues se verifica que no quede ninguno.

Cada configuracion se registra en ``meta_trials.sqlite`` con el mismo camino que ``validation.run_config``
(``MetaTrialRegistry.run``: presupuesto -> fila ``inicio`` -> OOS -> metricas -> fila ``resultado``); la unica
diferencia es que ademas guarda las metricas estadisticas por primaria, que ``run_config`` no expone.
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
from sklearn.base import BaseEstimator, ClassifierMixin, TransformerMixin
from sklearn.compose import ColumnTransformer
from sklearn.impute import SimpleImputer
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import OneHotEncoder, StandardScaler

from heatsignal.lab.meta import dataset as D
from heatsignal.lab.meta import features as F
from heatsignal.lab.meta import validation as V

log = logging.getLogger("heatsignal.lab.meta.train_baseline")

DEV_END = min(D.HOLDOUT_START.values())  # 2026-07-16: nada con t_decision >= esto entra
NAN_INDICATOR_FRAC = 0.05
CATEGORICAS = ("primaria", "lado", "tipo", "daily_trend", "daily_trend_dia_anterior", "btc_daily_trend",
               "btc_daily_trend_dia_anterior", "sesion", "cuadrante_oi_funding")
NUCLEO = ("dist_ema200_1d_atr", "adx_1d", "atr_pct_1d_pctl_1a", "ret_log_72h", "rsi_1d", "daily_trend_dia_anterior",
          "funding_z_30d", "delta_oi_24h", "cuadrante_oi_funding", "btc_daily_trend_dia_anterior", "corr_btc_30d",
          "fred_vixcls_pctl_1a")
TODAS = tuple(c for c in (F.FEATURES_BASICAS + F.FEATURES_DERIVADOS + F.FEATURES_MACRO + F.FEATURES_LATERALIDAD)
              if not c.startswith("pm_"))


def _cfg(features: Sequence[str], c: float) -> V.MetaConfig:
    return V.MetaConfig("logistica", ("primaria", *features), {"C": c})


CONFIGS: dict[str, V.MetaConfig] = {
    "L1": _cfg(NUCLEO, 1.0),
    "L2": _cfg(NUCLEO, 0.1),
    "L3": _cfg(TODAS, 0.1),
    "L4": _cfg(TODAS, 1.0),
}
PREREG_HASHES = frozenset(c.hash for c in CONFIGS.values())


def parse_configs(text: str) -> list[str]:
    """Solo L1-L4; cualquier otra cosa es un error (no hay variantes 'para probar')."""
    names = [s.strip().upper() for s in text.split(",") if s.strip()]
    bad = [n for n in names if n not in CONFIGS]
    if bad or not names:
        raise ValueError(f"configuraciones no pre-registradas: {bad or text!r} (solo {sorted(CONFIGS)})")
    return list(dict.fromkeys(names))


def development_only(ev: pd.DataFrame, dev_end: pd.Timestamp = DEV_END) -> tuple[pd.DataFrame, int]:
    """Descarta t_decision >= dev_end y verifica que no quede ninguno. Devuelve (eventos, descartados)."""
    t = pd.to_datetime(ev["t_decision"], utc=True)
    late = t >= dev_end
    out = ev.loc[~late].reset_index(drop=True)
    if (pd.to_datetime(out["t_decision"], utc=True) >= dev_end).any():
        raise RuntimeError("quedan eventos posteriores al desarrollo")
    return out, int(late.sum())


def encode_categoricals(ev: pd.DataFrame, cats: Iterable[str] = CATEGORICAS) -> pd.DataFrame:
    """Etiqueta -> codigo (float; NaN se mantiene). Es solo un renombre: el one-hot se ajusta dentro del fold."""
    out = ev.copy()
    for c in cats:
        if c not in out:
            continue
        vals = sorted(out[c].dropna().astype(str).unique())
        out[c] = out[c].map(lambda x, m={v: float(i) for i, v in enumerate(vals)}: np.nan if pd.isna(x)
                            else m[str(x)]).astype(float)
    return out


class MedianIndicator(TransformerMixin, BaseEstimator):
    """Mediana del train en todas las columnas + indicador de faltante solo donde el train tiene > 5 % NaN."""

    def __init__(self, frac: float = NAN_INDICATOR_FRAC) -> None:
        self.frac = frac

    def fit(self, X: Any, y: Any = None) -> MedianIndicator:
        X = np.asarray(X, float)
        with np.errstate(all="ignore"):
            med = np.nanmedian(X, axis=0) if len(X) else np.zeros(X.shape[1])
        self.medians_ = np.where(np.isfinite(med), med, 0.0)
        self.indicator_cols_ = np.flatnonzero(np.isnan(X).mean(axis=0) > self.frac)
        return self

    def transform(self, X: Any) -> np.ndarray:
        X = np.asarray(X, float)
        miss = np.isnan(X)
        filled = np.where(miss, self.medians_, X)
        return np.hstack([filled, miss[:, self.indicator_cols_].astype(float)])


class MetaLogit(ClassifierMixin, BaseEstimator):
    """Preproceso + logistica en un solo estimador (para que se re-ajuste entero en cada fold y sub-fold y reciba
    ``sample_weight``, que un ``Pipeline`` no acepta por nombre)."""

    def __init__(self, C: float = 1.0, cat_idx: tuple[int, ...] = (), n_features: int = 0) -> None:
        self.C = C
        self.cat_idx = cat_idx
        self.n_features = n_features

    def _pipeline(self) -> Pipeline:
        cat = list(self.cat_idx)
        num = [i for i in range(self.n_features) if i not in set(cat)]
        pre = ColumnTransformer([
            ("num", Pipeline([("imp", MedianIndicator()), ("std", StandardScaler())]), num),
            ("cat", Pipeline([("fill", SimpleImputer(strategy="constant", fill_value=-999.0)),
                              ("oh", OneHotEncoder(handle_unknown="ignore", sparse_output=False))]), cat),
        ])
        return Pipeline([("pre", pre), ("lr", LogisticRegression(C=self.C, max_iter=5000))])

    def fit(self, X: Any, y: Any, sample_weight: Any = None) -> MetaLogit:
        self.pipe_ = self._pipeline()
        self.pipe_.fit(X, y, lr__sample_weight=sample_weight)
        self.classes_ = self.pipe_.classes_
        return self

    def predict_proba(self, X: Any) -> np.ndarray:
        return self.pipe_.predict_proba(X)

    def decision_function(self, X: Any) -> np.ndarray:
        return self.pipe_.decision_function(X)

    def predict(self, X: Any) -> np.ndarray:
        return self.pipe_.predict(X)


def make_model_factory(cfg: V.MetaConfig):
    feats = list(cfg.features)
    cat_idx = tuple(i for i, f in enumerate(feats) if f in CATEGORICAS)
    return lambda: MetaLogit(C=float(cfg.hiper["C"]), cat_idx=cat_idx, n_features=len(feats))


def load_events(data_dir: Path = F.DEFAULT_DIR) -> tuple[pd.DataFrame, int]:
    ev = F.load_all(data_dir)
    ev, dropped = development_only(ev)
    prim_codes = sorted(ev["primaria"].unique())
    ev = encode_categoricals(ev)
    ev["primaria_nombre"] = ev["primaria"].map(dict(enumerate(prim_codes)))
    return ev, dropped


def run(names: Sequence[str], db: Path = V.DEFAULT_DB, data_dir: Path = F.DEFAULT_DIR,
        out_dir: Path = F.DEFAULT_DIR, n_boot: int = 2000) -> Path:
    for n in names:
        if CONFIGS[n].hash not in PREREG_HASHES:  # defensa: nunca registrar algo fuera de L1-L4
            raise ValueError(n)
    t_all = time.time()
    ev, dropped = load_events(data_dir)
    missing = [c for c in TODAS if c not in ev]
    if missing:
        raise RuntimeError(f"faltan features en el dataset: {missing}")
    reg = V.MetaTrialRegistry(db)
    res: dict[str, Any] = {}
    try:
        for n in names:
            cfg = CONFIGS[n]
            repetida = reg.check(cfg)
            t0 = time.time()

            metrics = reg.run(cfg, lambda cfg=cfg: _evaluate_named(ev, cfg, n_boot))
            res[n] = {"hash": cfg.hash, "hiper": cfg.hiper, "n_features": len(cfg.features),
                      "features": list(cfg.features), "repeticion_mismo_hash": repetida,
                      "segundos": round(time.time() - t0, 1), "metrics": metrics}
            log.info("%s (%s) listo en %.1f s", n, cfg.hash, time.time() - t0)
        for n in names:
            daily = res[n]["metrics"]["economicas"]["_daily"]["filtrada"]
            try:
                res[n]["dsr_filtrada_conjunto"] = V.meta_dsr(daily, reg)
            except ValueError as e:  # p.ej. la filtrada no opera nunca -> varianza 0: se reporta, no se inventa
                res[n]["dsr_filtrada_conjunto"] = {"error": str(e)}
        pbo = V.meta_pbo(reg)
        k = reg.k
        hashes = reg.hashes()
    finally:
        reg.close()
    for n in names:
        res[n]["metrics"]["economicas"] = V._clean(res[n]["metrics"]["economicas"])
    out = {"generado": datetime.now(timezone.utc).isoformat(), "prereg_hash": V.PREREG_HASH,
           "dataset": {"n": int(len(ev)), "descartados_post_desarrollo": dropped, "dev_end": str(DEV_END),
                       "t_decision_max": str(ev["t_decision"].max()),
                       "por_primaria": ev["primaria_nombre"].value_counts().sort_index().to_dict()},
           "k_meta": k, "hashes_registrados": hashes, "pbo": pbo, "configuraciones": res,
           "segundos_total": round(time.time() - t_all, 1)}
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"baseline_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.json"
    path.write_text(json.dumps(V._clean(out), indent=2, ensure_ascii=False, default=str))
    return path


def _evaluate_named(ev: pd.DataFrame, cfg: V.MetaConfig, n_boot: int) -> dict[str, Any]:
    """El modelo ve 'primaria' como codigo; las metricas por primaria la agrupan por nombre."""
    oos = V.oos_predictions(ev, cfg.features, make_model_factory(cfg))
    oos["primaria"] = oos["primaria_nombre"]
    por_prim = {str(p): V.statistical_metrics(g["etiqueta"], g["p"], g["w"])
                for p, g in oos.groupby("primaria", sort=True)}
    return {"config": cfg.hash, "estadisticas": V.statistical_metrics(oos["etiqueta"], oos["p"], oos["w"]),
            "estadisticas_por_primaria": por_prim,
            "economicas": V.economic_metrics(oos, n_boot=n_boot),
            "calibracion_por_fold": oos.groupby("fold")["calibracion"].first().to_dict()}


def main(argv: Iterable[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m heatsignal.lab.meta.train_baseline")
    ap.add_argument("--configs", default="L1,L2,L3,L4", help="subconjunto de L1,L2,L3,L4 (nada mas)")
    ap.add_argument("--db", type=Path, default=V.DEFAULT_DB)
    ap.add_argument("--data", type=Path, default=F.DEFAULT_DIR)
    ap.add_argument("--out", type=Path, default=F.DEFAULT_DIR)
    args = ap.parse_args(list(argv) if argv is not None else None)
    try:
        names = parse_configs(args.configs)
    except ValueError as e:
        ap.error(str(e))
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    path = run(names, args.db.expanduser(), args.data.expanduser(), args.out.expanduser())
    print(path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
