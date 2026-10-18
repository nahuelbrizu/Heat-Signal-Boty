"""
Features macro para el meta-modelo (plan-entrenamiento.md, Etapa 2): FRED point-in-time y Polymarket. PC, con pandas.

Solo CARGA: no entrena nada ni elige features. Reglas del laboratorio (`lab/clock.py`): toda serie se indexa por
el instante en que el dato estuvo DISPONIBLE.
  - FRED: `available_ms` de cada vintage (00:00 de Chicago del dia siguiente a su `realtime_start`; ver
    `macro/store.py`). En el instante t se usa, de cada observacion, el ultimo vintage disponible <= t, y la
    observacion mas reciente. Una revision posterior NUNCA cambia el valor visto en t.
  - Polymarket: `fetched_ms` (cuando se leyo; en el backfill del CLOB, el instante del precio). Con tolerancia: un
    valor de hace mas de `poly_max_age` no se usa (NaN), para no arrastrar un mercado que se dejo de leer.

Fuentes aceptadas: la base `heatsignal_macro.sqlite` (copiada de la VM o del respaldo) o la carpeta de export
(`data/export/macro/YYYY/MM/DD/*.csv.gz`, la que sube el timer de historia a Drive). Backfill: en la VM,
`python -m heatsignal.macro backfill-fred --since 2021-01-01` y `backfill-polymarket --days 90`.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import numpy as np
import pandas as pd

FRED_COLS = ["series_id", "obs_date", "value", "realtime_start", "realtime_clipped", "available_ms"]
POLY_COLS = ["market_id", "fetched_ms", "source", "config_key", "question", "prob"]


def _read(source: str | Path, table: str, cols: list[str]) -> pd.DataFrame:
    p = Path(source)
    if p.is_file():
        with sqlite3.connect(f"file:{p}?mode=ro", uri=True) as conn:
            return pd.read_sql_query(f"SELECT {', '.join(cols)} FROM {table}", conn)
    files = sorted(p.glob(f"*/*/*/{table}.csv.gz"))
    if not files:
        return pd.DataFrame(columns=cols)
    df = pd.concat([pd.read_csv(f, usecols=lambda c: c in cols) for f in files], ignore_index=True)
    return df[cols].drop_duplicates()


def load_fred_vintages(source: str | Path) -> pd.DataFrame:
    df = _read(source, "fred_observations", FRED_COLS)
    df["available"] = pd.to_datetime(df["available_ms"].astype("int64"), unit="ms", utc=True)
    df["obs_date"] = pd.to_datetime(df["obs_date"])
    return df.sort_values(["series_id", "available", "obs_date"]).reset_index(drop=True)


def load_polymarket(source: str | Path) -> pd.DataFrame:
    df = _read(source, "polymarket_snapshots", POLY_COLS)
    df["t"] = pd.to_datetime(df["fetched_ms"].astype("int64"), unit="ms", utc=True)
    return df.dropna(subset=["prob"]).sort_values(["market_id", "t"]).reset_index(drop=True)


def fred_point_in_time(vintages: pd.DataFrame, series_id: str) -> pd.DataFrame:
    """Serie escalonada: en cada instante de publicacion, (valor de la ultima observacion conocida, su fecha)."""
    rows = vintages[vintages["series_id"] == series_id]
    known: dict[pd.Timestamp, float] = {}
    out_t, out_v, out_d = [], [], []
    for t, grp in rows.groupby("available", sort=True):
        for d, v in zip(grp["obs_date"], grp["value"]):
            known[d] = v  # el vintage nuevo de esa observacion pisa al anterior DESDE t
        valid = {d: v for d, v in known.items() if v is not None and not (isinstance(v, float) and np.isnan(v))}
        if not valid:
            continue
        last = max(valid)
        out_t.append(t), out_v.append(float(valid[last])), out_d.append(last)
    return pd.DataFrame({"value": out_v, "obs_date": out_d}, index=pd.DatetimeIndex(out_t, name="available"))


def fred_features(vintages: pd.DataFrame, index: pd.DatetimeIndex) -> pd.DataFrame:
    """Columnas `fred_<ID>` (valor) y `fred_<ID>_age_d` (dias desde la fecha de la observacion) en cada t de `index`."""
    idx = _utc(index)
    out = pd.DataFrame(index=idx)
    for sid in sorted(vintages["series_id"].unique()):
        pit = fred_point_in_time(vintages, sid)
        if pit.empty:
            continue
        aligned = pit.reindex(pit.index.union(idx)).ffill().reindex(idx)
        out[f"fred_{sid}"] = aligned["value"].astype(float)
        obs = pd.to_datetime(aligned["obs_date"]).dt.tz_localize("UTC")
        out[f"fred_{sid}_age_d"] = (pd.Series(idx, index=idx) - obs).dt.total_seconds() / 86400.0
    return out


def polymarket_features(snaps: pd.DataFrame, index: pd.DatetimeIndex, *, poly_max_age: str = "3h",
                        change_window: str = "24h") -> pd.DataFrame:
    """`pm_<config_key>_<market_id>` (prob. leida <= t, NaN si es mas vieja que `poly_max_age`) y su cambio en 24 h."""
    idx = _utc(index)
    out = pd.DataFrame(index=idx)
    max_age = pd.Timedelta(poly_max_age)
    for (key, mid), g in snaps.groupby(["config_key", "market_id"], sort=True):
        s = g.drop_duplicates("t", keep="last").set_index("t")["prob"].astype(float).sort_index()
        col = f"pm_{key}_{mid}"
        out[col] = _asof(s, idx, max_age)
        out[col + "_chg24h"] = out[col] - _asof(s, idx - pd.Timedelta(change_window), max_age).values
    return out


def load_macro_features(source: str | Path, index: pd.DatetimeIndex, **kw) -> pd.DataFrame:
    """FRED + Polymarket alineados a `index` (instantes de decision, UTC), sin look-ahead."""
    fred = fred_features(load_fred_vintages(source), index)
    poly = polymarket_features(load_polymarket(source), index, **kw)
    return fred.join(poly, how="left")


def _utc(index: pd.DatetimeIndex) -> pd.DatetimeIndex:
    index = pd.DatetimeIndex(index)
    index = index.tz_localize("UTC") if index.tz is None else index.tz_convert("UTC")
    return index.as_unit("ns")  # misma resolucion que las series (asi8 compara enteros)


def _asof(s: pd.Series, idx: pd.DatetimeIndex, max_age: pd.Timedelta) -> pd.Series:
    if s.empty:
        return pd.Series(np.nan, index=idx)
    s = s.copy()
    s.index = pd.DatetimeIndex(s.index).as_unit("ns")
    idx = pd.DatetimeIndex(idx).as_unit("ns")
    pos = s.index.searchsorted(idx, side="right") - 1
    vals = np.where(pos >= 0, s.values[np.clip(pos, 0, None)], np.nan)
    ts = np.where(pos >= 0, s.index.asi8[np.clip(pos, 0, None)], np.iinfo("int64").min)
    stale = (idx.asi8 - ts) > max_age.value
    vals = np.where(stale | (pos < 0), np.nan, vals)
    return pd.Series(vals, index=idx)
