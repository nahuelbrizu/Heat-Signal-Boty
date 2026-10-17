"""Conversion de archivos de data.binance.vision (sin red): encabezados, tiempos en ms, orden, checksum."""

from __future__ import annotations

import hashlib
import io
import zipfile
from datetime import date

import pytest

from heatsignal.lab import vision
from heatsignal.lab.vision import (
    VisionError,
    fetch_csv,
    funding_frame,
    klines_frame,
    metrics_frame,
    months,
    plan,
    premium_frame,
)

K_OLD = [["1580515200000", "0.2396", "0.2399", "0.2395", "0.2397", "132089.7", "1580515259999", "31665.39290",
          "33", "44141.3", "10585.12891", "0"]]


def test_klines_and_premium_frames() -> None:
    df = klines_frame(K_OLD + K_OLD)  # duplicado -> una fila
    assert len(df) == 1 and df.loc[0, "open_time"] == 1580515200000
    assert df.loc[0, "taker_buy_quote_volume"] == pytest.approx(10585.12891) and df["count"].dtype == "int64"
    prem = premium_frame([["1788220800000", "-0.0005", "0", "-0.0006", "-0.00052697", "0", "1788220859999",
                           "0", "12", "0", "0", "0"]])
    assert list(prem.columns) == ["open_time", "premium"] and prem.loc[0, "premium"] == pytest.approx(-0.00052697)


def test_metrics_frame_sorts_and_uses_ms() -> None:
    rows = [["2026-10-15 04:25:00", "XRPUSDT", "301779771.0", "397762794.77", "3.27", "1.85", "2.76", "2.59"],
            ["2026-10-15 00:00:00", "XRPUSDT", "300000000.0", "390000000.0", "3.0", "1.8", "2.7", "2.5"]]
    df = metrics_frame(rows)
    assert list(df["ts"]) == [1792022400000, 1792038300000]  # ms UTC y ordenado (el archivo viene desordenado)
    assert df.loc[1, "sum_open_interest_value"] == pytest.approx(397762794.77)


def test_funding_frame_rounds_offset_ms() -> None:
    df = funding_frame([["1788220800005", "8", "0.00008579"], ["1788249600001", "8", "-0.0001"]])
    assert list(df["funding_time"]) == [1788220800000, 1788249600000]
    assert list(df["interval_h"]) == [8, 8] and df.loc[1, "rate"] == pytest.approx(-0.0001)


def _zip(text: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("x.csv", text)
    return buf.getvalue()


def test_fetch_csv_verifies_checksum_and_strips_header(monkeypatch) -> None:
    blob = _zip("open_time,open\n1,2\n")
    good = f"{hashlib.sha256(blob).hexdigest()}  x.zip".encode()
    store = {"u": blob, "u.CHECKSUM": good}
    monkeypatch.setattr(vision, "_get", lambda url, *a, **k: store.get(url))
    assert fetch_csv("u") == [["1", "2"]]
    store["u.CHECKSUM"] = b"0" * 64
    with pytest.raises(VisionError, match="checksum"):
        fetch_csv("u")
    assert fetch_csv("no-publicado") is None  # 404 -> None, se reintenta en otra corrida


def test_plan_skips_closed_months_already_converted(tmp_path) -> None:
    assert months("2026-08", date(2026, 10, 17)) == ["2026-08", "2026-09", "2026-10"]
    (tmp_path / "klines" / "XRPUSDT").mkdir(parents=True)
    (tmp_path / "klines" / "XRPUSDT" / "2026-09.parquet").write_bytes(b"x")
    jobs = plan(["XRPUSDT"], ["klines", "funding"], date(2026, 10, 17), tmp_path, since="2026-09")
    got = {(j.dataset, j.month, j.current) for j in jobs}
    # klines 2026-09 ya esta; el mes en curso se rehace; funding del mes en curso va por REST
    assert got == {("klines", "2026-10", True), ("funding", "2026-09", False)}
