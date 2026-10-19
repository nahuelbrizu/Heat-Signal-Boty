"""dias_hasta_* del grupo macro (etapa 2c, correccion): FOMC historico verificado, CPI/NFP desde vintages de FRED."""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from heatsignal.lab.meta import features as MF
from heatsignal.macro.calendar import HISTORICAL_PATH, load_historical, parse_calendar
from heatsignal.macro.fred import SERIES_BY_ID
from heatsignal.macro.store import available_ms_for

NY = "America/New_York"
DAY = pd.Timedelta(days=1)

# Verificadas el 2026-10-19 contra https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm (fila de cada
# reunion + link del comunicado monetaryYYYYMMDDa.htm). Dia del comunicado de las reuniones programadas.
FOMC_VERIFICADAS = {
    2022: ["2022-01-26", "2022-03-16", "2022-05-04", "2022-06-15", "2022-07-27", "2022-09-21", "2022-11-02",
           "2022-12-14"],
    2023: ["2023-02-01", "2023-03-22", "2023-05-03", "2023-06-14", "2023-07-26", "2023-09-20", "2023-11-01",
           "2023-12-13"],
    2024: ["2024-01-31", "2024-03-20", "2024-05-01", "2024-06-12", "2024-07-31", "2024-09-18", "2024-11-07",
           "2024-12-18"],
}


def _local(ms: int) -> pd.Timestamp:
    return pd.Timestamp(ms, unit="ms", tz="UTC").tz_convert(NY)


# ---- FOMC historico ----------------------------------------------------------------------------------------------


@pytest.mark.parametrize("anio", sorted(FOMC_VERIFICADAS))
def test_fomc_historico_coincide_con_la_fuente(anio) -> None:
    h = load_historical()
    got = [_local(e.ts_ms) for e in h.events if e.kind == "FOMC" and e.scheduled and _local(e.ts_ms).year == anio]
    assert [d.strftime("%Y-%m-%d") for d in got] == FOMC_VERIFICADAS[anio]
    assert all(d.strftime("%H:%M") == "14:00" for d in got)  # "For release at 2:00 p.m." (EST/EDT)


def test_fomc_historico_programadas_y_no_programadas() -> None:
    raw = json.loads(HISTORICAL_PATH.read_text(encoding="utf-8"))
    assert raw["sources"]["FOMC"].startswith("https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm")
    h = load_historical()
    prog = [e for e in h.events if e.scheduled]
    no_prog = [e for e in h.events if not e.scheduled]
    assert {y: sum(_local(e.ts_ms).year == y for e in prog) for y in range(2021, 2026)} == {y: 8 for y in range(2021, 2026)}
    assert [(_local(e.ts_ms).strftime("%Y-%m-%d %H:%M")) for e in no_prog] == ["2025-08-22 10:00"]
    # la no programada no se conocia de antemano: no cuenta como proximo comunicado
    t = pd.Timestamp("2025-08-01", tz="UTC")
    f = MF.calendar_features(parse_calendar({"sources": {}, "events": []}), pd.DatetimeIndex([t]), h)
    assert f["dias_hasta_fomc"].iloc[0] == pytest.approx((pd.Timestamp("2025-09-17 18:00", tz="UTC") - t) / DAY)


# ---- CPI / NFP desde vintages ------------------------------------------------------------------------------------


def _vint(rows: list[tuple[str, str, str, float, int]]) -> pd.DataFrame:
    df = pd.DataFrame(rows, columns=["series_id", "obs_date", "realtime_start", "value", "realtime_clipped"])
    df["available_ms"] = [available_ms_for(r) for r in df["realtime_start"]]
    df["available"] = pd.to_datetime(df["available_ms"], unit="ms", utc=True)
    df["obs_date"] = pd.to_datetime(df["obs_date"])
    return df


SINT = _vint([
    # recortados al borde de la ventana: solo siembran lo conocido (no son publicaciones)
    ("PAYEMS", "2023-10-01", "2024-01-01", 156_000, 1), ("PAYEMS", "2023-11-01", "2024-01-01", 156_200, 1),
    ("PAYEMS", "2023-12-01", "2024-01-05", 156_400, 0), ("PAYEMS", "2023-11-01", "2024-01-05", 156_150, 0),  # + revision
    ("PAYEMS", "2024-01-01", "2024-02-02", 156_700, 0),
    ("PAYEMS", "2023-12-01", "2024-02-20", 156_450, 0),  # solo revision de un mes viejo: NO es publicacion
    ("PAYEMS", "2024-02-01", "2024-03-08", 157_000, 0),
    # dos meses juntos el mismo dia (como tras el cierre de 2025): una sola publicacion
    ("PAYEMS", "2024-03-01", "2024-05-03", 157_200, 0), ("PAYEMS", "2024-04-01", "2024-05-03", 157_400, 0),
    ("CPIAUCSL", "2024-01-01", "2024-02-13", 309.0, 0), ("CPIAUCSL", "2024-02-01", "2024-03-12", 310.0, 0),
])


def test_fechas_de_publicacion_desde_vintages() -> None:
    nfp = [_local(x).strftime("%Y-%m-%d %H:%M") for x in MF.fred_release_dates(SINT, "PAYEMS")]
    assert nfp == ["2024-01-05 08:30", "2024-02-02 08:30", "2024-03-08 08:30", "2024-05-03 08:30"]
    cpi = MF.fred_release_dates(SINT, "CPIAUCSL")
    assert [pd.Timestamp(x, unit="ms", tz="UTC") for x in cpi] == [  # 08:30 EST -> 13:30 UTC; EDT -> 12:30
        pd.Timestamp("2024-02-13 13:30", tz="UTC"), pd.Timestamp("2024-03-12 12:30", tz="UTC")]
    assert MF.fred_release_dates(SINT, "NOEXISTE").size == 0


def test_calendario_bls_manda_y_fred_cubre_lo_anterior() -> None:
    cal = parse_calendar({"sources": {"NFP": "n"}, "events": [
        {"kind": "NFP", "date": "2024-03-08", "time": "08:30"}, {"kind": "NFP", "date": "2024-04-05", "time": "08:30"}]})
    ts = [_local(x).strftime("%Y-%m-%d") for x in MF.release_times(cal, "NFP", None, SINT)]
    # FRED solo antes de la primera fecha del calendario; desde ahi, el calendario BLS (aunque FRED diga otra cosa)
    assert ts == ["2024-01-05", "2024-02-02", "2024-03-08", "2024-04-05"]
    times = pd.DatetimeIndex([pd.Timestamp("2024-01-01", tz="UTC"), pd.Timestamp("2024-01-10", tz="UTC"),
                              pd.Timestamp("2024-04-06", tz="UTC")])
    f = MF.calendar_features(cal, times, None, SINT)
    assert np.isnan(f["dias_hasta_nfp"].iloc[0])  # antes de la primera fecha conocida
    assert f["dias_hasta_nfp"].iloc[1] == pytest.approx((pd.Timestamp("2024-02-02 13:30", tz="UTC") - times[1]) / DAY)
    assert np.isnan(f["dias_hasta_nfp"].iloc[2])  # sin proxima


def test_sin_look_ahead() -> None:
    """Solo fechas: nunca negativo, cambia de evento justo en el instante de publicacion, no depende de los valores."""
    pub = pd.Timestamp("2024-03-08 13:30", tz="UTC")
    times = pd.DatetimeIndex([pub - pd.Timedelta(minutes=1), pub, pub + pd.Timedelta(minutes=1)])
    f = MF.calendar_features(parse_calendar({"sources": {}, "events": []}), times, None, SINT)
    d = f["dias_hasta_nfp"].to_numpy()
    assert d[0] == pytest.approx(1 / 1440) and d[1] == 0.0
    assert d[2] == pytest.approx((pd.Timestamp("2024-05-03 12:30", tz="UTC") - times[2]) / DAY)
    otros = SINT.assign(value=SINT["value"] * -3.0)
    pd.testing.assert_frame_equal(f, MF.calendar_features(parse_calendar({"sources": {}, "events": []}), times, None, otros))
    assert (f.dropna(how="all").fillna(0).to_numpy() >= 0).all()


def test_payems_en_la_lista_de_fred() -> None:
    s = SERIES_BY_ID["PAYEMS"]
    assert s.frequency == "mensual"


# ---- build real: NaN de dias_hasta_* < 1 % -----------------------------------------------------------------------

_REAL = MF.DEFAULT_DIR / MF.GRUPOS["macro"].archivo


@pytest.mark.skipif(not _REAL.exists(), reason="sin el build real (~/heatsignal-lab/meta)")
def test_build_real_nan_menor_a_1_pct() -> None:
    df = pd.read_parquet(_REAL)
    nan = df[list(MF.CAL_KINDS.values())].isna().mean()
    assert (nan < 0.01).all(), nan.to_dict()
