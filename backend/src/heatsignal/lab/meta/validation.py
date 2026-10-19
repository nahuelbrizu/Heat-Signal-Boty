"""
Marco de validacion del meta-modelo (plan-entrenamiento.md, etapa 3, subtarea A). NO entrena con datos reales.

Todo lo fijo sale de ``preregistro.json`` (k = 5, embargo 5 dias, umbral de calibracion 1.000, presupuesto 24,
semilla). El umbral de decision de la primaria filtrada es 0,5 FIJO (plan), nunca optimizado.

1. Purged k-fold con embargo (AFML cap. 7)
------------------------------------------
- Los eventos se ordenan por ``t_decision`` y se cortan en k bloques CONTIGUOS (sin mezclar). Los eventos con el
  mismo ``t_decision`` (varias monedas/primarias a la vez) caen siempre en el mismo bloque.
- Ventana de test = [min t_decision del bloque, max t_salida del bloque].
- Purga: sale del train todo evento cuya vida [t_decision, t_salida] toca la ventana de test.
- Embargo: sale ademas del train todo evento que EMPIEZA en (fin de test, fin de test + embargo].
- El solape es por tiempo sobre TODO el dataset (todas las primarias y monedas juntas): un evento de BTC de
  donchian se purga si se solapa con un test de ETH de tsmom. Es conservador a proposito (el mercado es comun).

2. Calibracion dentro de cada fold
----------------------------------
``FoldCalibrated`` envuelve cualquier clasificador de sklearn: isotonica si el train del fold tiene >= 1.000
eventos, si no Platt (sigmoide). La calibracion usa ``CalibratedClassifierCV(ensemble=False)`` con un purged
k-fold interno (3 bloques, mismo embargo) sobre el train del fold: el test del fold no se ve nunca.

3. Registro de configuraciones (``meta_trials``) contra el presupuesto de 24
--------------------------------------------------------------------------
- Base propia ``~/heatsignal-lab/meta_trials.sqlite`` (no ``trials.sqlite``): separacion fisica. El K de las
  estrategias del laboratorio (``lab.publish.program`` / ``lab.program``) se cuenta sobre ``lab_trials`` de
  ``trials.sqlite``; los intentos del meta-modelo son OTRO programa con su propio K y NO suman ahi. En otra base
  no hay forma de mezclarlos por accidente (copias, consultas sobre todas las tablas, backups de la VM).
- Tabla append-only (triggers bloquean UPDATE y DELETE). Una configuracion = hash de (familia, features
  ordenadas, hiperparametros). Cada corrida escribe una fila ``inicio`` ANTES de entrenar (si el entrenamiento se
  cae, el intento igual cuenta) y una ``resultado`` con las metricas OOS y los retornos diarios filtrados.
- K del meta-modelo = cantidad de hashes distintos. Repetir una configuracion identica agrega filas pero no suma
  K. La configuracion 25 (hash nuevo con 24 ya registrados) levanta ``BudgetExceeded`` sin escribir nada.
- Para DSR y PBO vale el PRIMER resultado de cada hash (la repeticion no puede elegir la mejor corrida).

4. Metricas sobre las predicciones OOS concatenadas
---------------------------------------------------
- Estadisticas (con pesos por unicidad, ``meta.dataset.uniqueness_weights``): Brier, log loss y AUC, contra la
  tasa base (p constante = media ponderada de la etiqueta), curva de calibracion por deciles de p.
- Economicas, por primaria y para el conjunto (promedio de las primarias): sin filtrar (s = 1), filtrada
  (s = 1 si p >= 0,5) y tamano por probabilidad (s = clip(2 (p - 0,5), 0, 1)). Sharpe neto anualizado
  (``stats.sharpe_ratio``, 365), retorno (suma de retornos diarios, nocional fijo como el simulador), max DD
  (``stats.max_drawdown`` sin capitalizar) y n de operaciones (s > 0).
- Retornos diarios: el ``pnl_neto`` de cada evento (fraccion del nocional, ya con costos y funding) se multiplica
  por s y por |tamano| / (monedas de la primaria) (capital repartido igual entre monedas) y se imputa ENTERO al
  dia UTC de ``t_salida`` (PnL realizado; no se reparte entre los dias de vida de la operacion, porque el dataset
  no trae la curva intradia). Los dias sin salidas valen 0, en el calendario [primer t_decision, ultima
  t_salida] de la primaria, igual para las tres variantes. Diferencia con ``lab.sim`` (marca a mercado por
  minuto, agregado con resample 1D UTC): concentra la varianza en los dias de cierre; afecta igual a filtrada y
  sin filtrar, asi que la comparacion es justa.
- Mejora de Sharpe: bootstrap estacionario pareado de los dias (p-valor de SR_filtrada - SR_sin_filtrar <= 0).
- DSR con el K del meta-modelo (``stats.dsr_from_returns`` con los Sharpe filtrados de todas las configuraciones
  registradas) y PBO por CSCV (``diagnostics.pbo_cscv``) sobre la matriz dias x configuraciones de retornos
  filtrados (conjunto), alineada por fecha con 0 donde falta.
"""
from __future__ import annotations

import hashlib
import json
import math
import sqlite3
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from sklearn.base import BaseEstimator, ClassifierMixin, clone
from sklearn.calibration import CalibratedClassifierCV
from sklearn.metrics import brier_score_loss, log_loss, roc_auc_score

from heatsignal.lab import stats
from heatsignal.lab.diagnostics import pbo_cscv
from heatsignal.lab.meta.dataset import load_prereg, uniqueness_weights

PREREG, PREREG_HASH = load_prereg()
K_FOLDS: int = int(PREREG["validacion"]["k"])
EMBARGO = pd.Timedelta(days=int(PREREG["validacion"]["embargo_dias"]))
CALIB_MIN_ISOTONIC = 1000  # preregistro["calibracion"]: isotonica si el train del fold tiene >= 1000 eventos
BUDGET: int = int(PREREG["presupuesto_intentos"]["max_configuraciones"])
SEED: int = int(PREREG["semilla"])
THRESHOLD = 0.5  # umbral FIJO del plan; nunca se optimiza
FAMILIAS = ("logistica", "lightgbm")  # preregistro["modelos_permitidos"]
DEFAULT_DB = Path.home() / "heatsignal-lab" / "meta_trials.sqlite"


# ---- 1. purged k-fold con embargo ----------------------------------------------------------------------


@dataclass(frozen=True)
class Fold:
    train: np.ndarray  # posiciones (0..n-1) en el orden original
    test: np.ndarray
    test_start: pd.Timestamp
    test_end: pd.Timestamp


def _ts(x: Any) -> np.ndarray:
    return pd.DatetimeIndex(pd.to_datetime(x, utc=True)).as_unit("ns").asi8


def purged_kfold(t0: Any, t1: Any, k: int = K_FOLDS, embargo: pd.Timedelta = EMBARGO) -> list[Fold]:
    """k bloques contiguos por ``t0`` (t_decision); train purgado + embargo. Ver el docstring del modulo."""
    a, b = _ts(t0), _ts(t1)
    n = len(a)
    if n != len(b):
        raise ValueError("t0 y t1 tienen que tener el mismo largo")
    if n < k:
        raise ValueError(f"muy pocos eventos ({n}) para k = {k}")
    b = np.maximum(a, b)
    uniq, inv, counts = np.unique(a, return_inverse=True, return_counts=True)
    before = np.r_[0, np.cumsum(counts)[:-1]]  # eventos con t_decision estrictamente anterior
    fold_of_time = np.minimum((before * k) // n, k - 1)
    fold = fold_of_time[inv]
    emb = int(pd.Timedelta(embargo).value)
    out: list[Fold] = []
    for f in range(k):
        test = np.flatnonzero(fold == f)
        if not len(test):
            raise ValueError(f"el bloque {f} quedo vacio (demasiados eventos con el mismo t_decision)")
        lo, hi = a[test].min(), b[test].max()
        overlap = (a <= hi) & (b >= lo)
        embargoed = (a > hi) & (a <= hi + emb)
        train = np.flatnonzero((fold != f) & ~overlap & ~embargoed)
        out.append(Fold(train, test, pd.Timestamp(lo, tz="UTC"), pd.Timestamp(hi, tz="UTC")))
    return out


# ---- 2. calibracion dentro del fold --------------------------------------------------------------------


def calibration_method(n_train: int, min_isotonic: int = CALIB_MIN_ISOTONIC) -> str:
    """'isotonic' si el train del fold tiene >= 1000 eventos, si no 'sigmoid' (Platt)."""
    return "isotonic" if n_train >= min_isotonic else "sigmoid"


class FoldCalibrated(ClassifierMixin, BaseEstimator):
    """Envuelve un clasificador de sklearn y lo calibra SOLO con el train que recibe (purged k-fold interno)."""

    def __init__(self, estimator: Any, min_isotonic: int = CALIB_MIN_ISOTONIC, inner_k: int = 3,
                 embargo: pd.Timedelta = EMBARGO) -> None:
        self.estimator = estimator
        self.min_isotonic = min_isotonic
        self.inner_k = inner_k
        self.embargo = embargo

    def fit(self, X: Any, y: Any, sample_weight: Any = None, t0: Any = None, t1: Any = None) -> FoldCalibrated:
        y = np.asarray(y)
        self.method_ = calibration_method(len(y), self.min_isotonic)
        if t0 is not None and t1 is not None:
            cv: Any = [(f.train, f.test) for f in purged_kfold(t0, t1, self.inner_k, self.embargo)]
        else:
            cv = self.inner_k
        self.calibrated_ = CalibratedClassifierCV(clone(self.estimator), method=self.method_, cv=cv,
                                                  ensemble=False)
        self.calibrated_.fit(X, y, sample_weight=sample_weight)
        self.classes_ = self.calibrated_.classes_
        return self

    def predict_proba(self, X: Any) -> np.ndarray:
        return self.calibrated_.predict_proba(X)

    def predict(self, X: Any) -> np.ndarray:
        return self.classes_[(self.predict_proba(X)[:, 1] >= THRESHOLD).astype(int)]


def oos_predictions(events: pd.DataFrame, features: Sequence[str], make_model: Callable[[], Any],
                    k: int = K_FOLDS, embargo: pd.Timedelta = EMBARGO, label: str = "etiqueta",
                    weight: str | None = "peso_unicidad") -> pd.DataFrame:
    """p OOS de cada evento (concatenado de los k folds), con el fold y el metodo de calibracion usado."""
    ev = events.reset_index(drop=True)
    X = ev[list(features)].to_numpy(dtype=float)
    y = ev[label].to_numpy(dtype=int)
    w = (ev[weight].to_numpy(dtype=float) if weight and weight in ev
         else uniqueness_weights(ev["t_decision"], ev["t_salida"]))
    p = np.full(len(ev), np.nan)
    fold_id = np.full(len(ev), -1)
    method = np.empty(len(ev), dtype=object)
    for i, f in enumerate(purged_kfold(ev["t_decision"], ev["t_salida"], k, embargo)):
        m = FoldCalibrated(make_model(), embargo=embargo)
        tr = f.train
        m.fit(X[tr], y[tr], sample_weight=w[tr], t0=ev["t_decision"].iloc[tr], t1=ev["t_salida"].iloc[tr])
        p[f.test] = m.predict_proba(X[f.test])[:, 1]
        fold_id[f.test] = i
        method[f.test] = m.method_
    out = ev.copy()
    out["p"], out["fold"], out["calibracion"], out["w"] = p, fold_id, method, w
    return out


# ---- 3. metricas ---------------------------------------------------------------------------------------


def calibration_deciles(y: Any, p: Any, w: Any, n_bins: int = 10) -> list[dict[str, float]]:
    y, p, w = np.asarray(y, float), np.asarray(p, float), np.asarray(w, float)
    edges = np.unique(np.quantile(p, np.linspace(0, 1, n_bins + 1)))
    idx = np.clip(np.searchsorted(edges, p, side="right") - 1, 0, max(len(edges) - 2, 0))
    rows = []
    for b in np.unique(idx):
        m = idx == b
        rows.append({"p_media": float(np.average(p[m], weights=w[m])), "tasa": float(np.average(y[m], weights=w[m])),
                     "n": int(m.sum()), "peso": float(w[m].sum())})
    return rows


def statistical_metrics(y: Any, p: Any, w: Any) -> dict[str, Any]:
    """Brier, log loss y AUC ponderados, contra la tasa base (p constante)."""
    y, p, w = np.asarray(y, int), np.clip(np.asarray(p, float), 1e-6, 1 - 1e-6), np.asarray(w, float)
    base = float(np.average(y, weights=w))
    pb = np.full_like(p, min(max(base, 1e-6), 1 - 1e-6))
    brier, brier_b = brier_score_loss(y, p, sample_weight=w), brier_score_loss(y, pb, sample_weight=w)
    ll, ll_b = log_loss(y, p, sample_weight=w, labels=[0, 1]), log_loss(y, pb, sample_weight=w, labels=[0, 1])
    auc = roc_auc_score(y, p, sample_weight=w) if len(np.unique(y)) == 2 else float("nan")
    return {"n": int(len(y)), "tasa_base": base, "brier": float(brier), "brier_base": float(brier_b),
            "brier_skill": float(1 - brier / brier_b) if brier_b > 0 else float("nan"),
            "logloss": float(ll), "logloss_base": float(ll_b), "auc": float(auc), "auc_base": 0.5,
            "calibracion_deciles": calibration_deciles(y, p, w)}


def size_filtered(p: Any, threshold: float = THRESHOLD) -> np.ndarray:
    return (np.asarray(p, float) >= threshold).astype(float)


def size_by_probability(p: Any) -> np.ndarray:
    return np.clip(2.0 * (np.asarray(p, float) - 0.5), 0.0, 1.0)


def _escala(ev: pd.DataFrame) -> np.ndarray:
    tam = ev["tamaño"].abs().fillna(1.0).to_numpy(float) if "tamaño" in ev else np.ones(len(ev))
    n_sym = ev.groupby("primaria")["symbol"].transform("nunique").to_numpy(float) if "symbol" in ev else 1.0
    return tam / n_sym


def daily_returns(ev: pd.DataFrame, s: Any, calendar: pd.DatetimeIndex | None = None) -> pd.Series:
    """Retorno diario (dia UTC de t_salida) de un conjunto de eventos de UNA primaria con tamanos s en [0, 1]."""
    contrib = ev["pnl_neto"].to_numpy(float) * _escala(ev) * np.asarray(s, float)
    day = pd.to_datetime(ev["t_salida"], utc=True).dt.floor("D")
    r = pd.Series(contrib, index=day.to_numpy()).groupby(level=0).sum()
    if calendar is None:
        calendar = _calendar(ev)
    return r.reindex(calendar, fill_value=0.0)


def _calendar(ev: pd.DataFrame) -> pd.DatetimeIndex:
    lo = pd.to_datetime(ev["t_decision"], utc=True).min().floor("D")
    hi = pd.to_datetime(ev["t_salida"], utc=True).max().floor("D")
    return pd.date_range(lo, hi, freq="1D")


def _summary(r: pd.Series, s: np.ndarray) -> dict[str, Any]:
    return {"sharpe": stats.sharpe_ratio(r), "retorno": float(r.sum()), "max_dd": stats.max_drawdown(r),
            "n_operaciones": int((s > 0).sum()), "dias": int(len(r))}


def sharpe_improvement_pvalue(r_new: pd.Series, r_base: pd.Series, n_boot: int = 2000, seed: int = SEED,
                              block_len: float = 10.0) -> float:
    """P-valor (bootstrap estacionario pareado) de H0: SR(r_new) - SR(r_base) <= 0."""
    a, b = np.asarray(r_new, float), np.asarray(r_base, float)
    idx = stats.stationary_bootstrap_indices(len(a), block_len, n_boot, np.random.default_rng(seed))

    def sr(m: np.ndarray) -> np.ndarray:
        sd = m.std(axis=1, ddof=1)
        return np.where(sd > 0, m.mean(axis=1) / np.where(sd > 0, sd, 1.0), 0.0)
    diff = sr(a[idx]) - sr(b[idx])
    return float(np.mean(diff <= 0))


def economic_metrics(oos: pd.DataFrame, p_col: str = "p", threshold: float = THRESHOLD,
                     n_boot: int = 2000) -> dict[str, Any]:
    """Sin filtrar / filtrada (p >= 0,5) / tamano por probabilidad, por primaria y en conjunto."""
    out: dict[str, Any] = {}
    daily: dict[str, dict[str, pd.Series]] = {}
    for prim, ev in oos.groupby("primaria", sort=True):
        cal = _calendar(ev)
        p = ev[p_col].to_numpy(float)
        sizes = {"sin_filtrar": np.ones(len(ev)), "filtrada": size_filtered(p, threshold),
                 "tamano_prob": size_by_probability(p)}
        rs = {k: daily_returns(ev, s, cal) for k, s in sizes.items()}
        daily[str(prim)] = rs
        res = {k: _summary(rs[k], sizes[k]) for k in sizes}
        res["p_mejora_filtrada"] = sharpe_improvement_pvalue(rs["filtrada"], rs["sin_filtrar"], n_boot)
        res["p_mejora_tamano_prob"] = sharpe_improvement_pvalue(rs["tamano_prob"], rs["sin_filtrar"], n_boot)
        out[str(prim)] = res
    agg = {k: combined_daily(daily, k) for k in ("sin_filtrar", "filtrada", "tamano_prob")}
    n_ops = {k: sum(out[pr][k]["n_operaciones"] for pr in daily) for k in agg}
    out["_conjunto"] = {k: {**_summary(agg[k], np.zeros(0)), "n_operaciones": n_ops[k]} for k in agg}
    out["_conjunto"]["p_mejora_filtrada"] = sharpe_improvement_pvalue(agg["filtrada"], agg["sin_filtrar"], n_boot)
    out["_conjunto"]["p_mejora_tamano_prob"] = sharpe_improvement_pvalue(agg["tamano_prob"], agg["sin_filtrar"],
                                                                        n_boot)
    out["_daily"] = agg  # series del conjunto (se sacan antes de guardar en JSON)
    return out


def combined_daily(daily: dict[str, dict[str, pd.Series]], variant: str) -> pd.Series:
    """Promedio por dia de las primarias (capital repartido igual; dia sin datos de una primaria = 0)."""
    m = pd.concat({k: v[variant] for k, v in daily.items()}, axis=1).fillna(0.0)
    return m.mean(axis=1)


# ---- 4. registro de configuraciones (append-only, presupuesto 24) ------------------------------------


class BudgetExceeded(RuntimeError):
    """Se intento registrar una configuracion nueva con el presupuesto del pre-registro ya agotado."""


@dataclass(frozen=True)
class MetaConfig:
    familia: str
    features: tuple[str, ...]
    hiper: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.familia not in FAMILIAS:
            raise ValueError(f"familia no pre-registrada: {self.familia!r} (permitidas {FAMILIAS})")
        if self.familia == "lightgbm" and not 1 <= int(self.hiper.get("max_depth", 0)) <= 4:
            raise ValueError("LightGBM chico: max_depth entre 1 y 4 (preregistro)")
        if not self.features:
            raise ValueError("lista de features vacia")

    def canonical(self) -> str:
        return json.dumps({"familia": self.familia, "features": sorted(set(self.features)),
                           "hiper": self.hiper}, sort_keys=True, default=str, separators=(",", ":"))

    @property
    def hash(self) -> str:
        return hashlib.sha256(self.canonical().encode()).hexdigest()[:16]


_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta_trials (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    config_hash  TEXT NOT NULL,
    fase         TEXT NOT NULL CHECK (fase IN ('inicio', 'resultado')),
    familia      TEXT NOT NULL,
    features     TEXT NOT NULL,
    hiper        TEXT NOT NULL,
    metrics      TEXT,
    daily        TEXT,
    prereg_hash  TEXT NOT NULL,
    created_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_meta_trials_hash ON meta_trials (config_hash);
CREATE TRIGGER IF NOT EXISTS trg_meta_trials_no_update BEFORE UPDATE ON meta_trials
BEGIN SELECT RAISE(ABORT, 'meta_trials es append-only: UPDATE no permitido'); END;
CREATE TRIGGER IF NOT EXISTS trg_meta_trials_no_delete BEFORE DELETE ON meta_trials
BEGIN SELECT RAISE(ABORT, 'meta_trials es append-only: DELETE no permitido'); END;
"""


def _clean(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {str(k): _clean(v) for k, v in obj.items() if not isinstance(v, pd.Series)}
    if isinstance(obj, (list, tuple)):
        return [_clean(v) for v in obj]
    if isinstance(obj, (np.floating, float)):
        return float(obj) if math.isfinite(obj) else None
    if isinstance(obj, np.integer):
        return int(obj)
    return obj


class MetaTrialRegistry:
    """Intentos del meta-modelo. Su K NO suma al K de las estrategias del lab (otra base; ver docstring)."""

    def __init__(self, db_path: str | Path = DEFAULT_DB, budget: int = BUDGET) -> None:
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self.budget = budget
        self._conn = sqlite3.connect(str(db_path), timeout=60)
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    def hashes(self) -> list[str]:
        rows = self._conn.execute("SELECT config_hash FROM meta_trials GROUP BY config_hash ORDER BY MIN(id)")
        return [r[0] for r in rows]

    @property
    def k(self) -> int:
        """K del meta-modelo = configuraciones distintas registradas."""
        return len(self.hashes())

    def check(self, cfg: MetaConfig) -> bool:
        """True si la configuracion ya existe (no suma K). ``BudgetExceeded`` si es nueva y no hay lugar."""
        if cfg.hash in self.hashes():
            return True
        if self.k >= self.budget:
            raise BudgetExceeded(f"presupuesto agotado: {self.k}/{self.budget} configuraciones; "
                                 f"{cfg.hash} no se corre")
        return False

    def _insert(self, cfg: MetaConfig, fase: str, metrics: dict[str, Any] | None = None,
                daily: pd.Series | None = None) -> int:
        d = None if daily is None else json.dumps({ix.strftime("%Y-%m-%d"): float(v) for ix, v in daily.items()})
        cur = self._conn.execute(
            "INSERT INTO meta_trials (config_hash, fase, familia, features, hiper, metrics, daily, prereg_hash,"
            " created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (cfg.hash, fase, cfg.familia, json.dumps(list(cfg.features)), json.dumps(cfg.hiper, sort_keys=True,
             default=str), None if metrics is None else json.dumps(_clean(metrics)), d, PREREG_HASH,
             datetime.now(timezone.utc).isoformat()))
        self._conn.commit()
        return int(cur.lastrowid)

    def run(self, cfg: MetaConfig, fn: Callable[[], dict[str, Any]]) -> dict[str, Any]:
        """Chequea presupuesto, registra ``inicio`` (cuenta aunque falle), corre ``fn`` y registra el resultado.

        ``fn`` devuelve metricas; si trae ``economicas["_daily"]["filtrada"]`` se guarda para PBO/DSR.
        """
        self.check(cfg)
        self._insert(cfg, "inicio")
        metrics = fn()
        daily = metrics.get("economicas", {}).get("_daily", {}).get("filtrada")
        self._insert(cfg, "resultado", metrics, daily)
        return metrics

    def results(self) -> dict[str, dict[str, Any]]:
        """Primer resultado de cada configuracion: {hash: {"metrics": ..., "daily": Series}}."""
        out: dict[str, dict[str, Any]] = {}
        for h, m, d in self._conn.execute(
                "SELECT config_hash, metrics, daily FROM meta_trials WHERE fase = 'resultado' ORDER BY id"):
            if h in out:
                continue
            dd = json.loads(d) if d else {}
            s = pd.Series(dd, dtype=float)
            s.index = pd.to_datetime(s.index, utc=True)
            out[h] = {"metrics": json.loads(m) if m else {}, "daily": s}
        return out

    def daily_matrix(self) -> pd.DataFrame:
        res = self.results()
        return pd.concat({h: r["daily"] for h, r in res.items()}, axis=1).sort_index().fillna(0.0)


def meta_dsr(daily: pd.Series, registry: MetaTrialRegistry) -> dict[str, float]:
    """DSR del retorno diario elegido con K = configuraciones registradas y V = varianza de sus Sharpe."""
    sharpes = [stats.sharpe_ratio(r["daily"]) for r in registry.results().values()]
    return stats.dsr_from_returns(daily, trial_sharpes_annual=sharpes, n_trials=float(registry.k))


def meta_pbo(registry: MetaTrialRegistry, n_blocks: int = 16) -> dict[str, Any]:
    """PBO por CSCV (``diagnostics.pbo_cscv``) sobre los retornos diarios filtrados de las configuraciones."""
    return pbo_cscv(registry.daily_matrix(), n_blocks=n_blocks)


def evaluate(events: pd.DataFrame, cfg: MetaConfig, make_model: Callable[[], Any],
             n_boot: int = 2000) -> dict[str, Any]:
    """OOS + metricas estadisticas y economicas de una configuracion (sin registrar)."""
    oos = oos_predictions(events, cfg.features, make_model)
    return {"config": cfg.hash, "estadisticas": statistical_metrics(oos["etiqueta"], oos["p"], oos["w"]),
            "economicas": economic_metrics(oos, n_boot=n_boot),
            "calibracion_por_fold": oos.groupby("fold")["calibracion"].first().to_dict()}


def run_config(registry: MetaTrialRegistry, events: pd.DataFrame, cfg: MetaConfig,
               make_model: Callable[[], Any], n_boot: int = 2000) -> dict[str, Any]:
    """Punto de entrada de la subtarea 3b: presupuesto -> OOS -> metricas -> registro append-only."""
    return registry.run(cfg, lambda: evaluate(events, cfg, make_model, n_boot))
