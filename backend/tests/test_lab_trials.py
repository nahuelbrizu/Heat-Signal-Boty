"""Tests del registro de intentos del laboratorio: append-only real y conteo K para el DSR."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from heatsignal.lab.trials import HoldoutAlreadyOpenedError, TrialRegistry


@pytest.fixture
def reg(tmp_path: Path):
    r = TrialRegistry(tmp_path / "trials.sqlite")
    yield r
    r.close()


def _add(reg: TrialRegistry, sid: str, sharpe: float | None, window: str = "test", **kw) -> int:
    return reg.register(
        strategy_id=sid,
        spec_hash=kw.get("spec_hash", "abc123"),
        params=kw.get("params", {"n": 20}),
        window=window,
        symbols=["BTCUSDT", "ETHUSDT"],
        metrics={"sharpe": sharpe} if sharpe is not None else {"error": "sin trades"},
        window_start="2025-01-01",
        window_end="2025-06-30",
    )


def test_register_and_read_back(reg):
    tid = _add(reg, "donchian-ensemble-v1", 1.2, params={"n": 40})
    t = reg.trials("donchian-ensemble-v1")[0]
    assert t.id == tid and t.params == {"n": 40} and t.metrics == {"sharpe": 1.2}
    assert t.symbols == ["BTCUSDT", "ETHUSDT"] and t.window == "test"


def test_counts_and_sharpes(reg):
    for s in (0.5, 1.0, -0.2):
        _add(reg, "a-v1", s)
    _add(reg, "a-v1", None)  # un intento fallido cuenta en K aunque no tenga Sharpe
    _add(reg, "b-v1", 0.3, window="train")
    assert reg.count("a-v1") == 4
    assert reg.count() == 5
    assert reg.count(window="train") == 1
    assert reg.count_by_strategy() == {"a-v1": 4, "b-v1": 1}
    assert reg.sharpes("a-v1") == [0.5, 1.0, -0.2]
    assert sorted(reg.sharpes()) == [-0.2, 0.3, 0.5, 1.0]


def test_nan_metrics_stored_as_null(reg):
    _add(reg, "a-v1", float("nan"))
    assert reg.sharpes("a-v1") == []
    assert reg.count("a-v1") == 1


def test_update_and_delete_blocked_by_triggers(reg, tmp_path):
    _add(reg, "a-v1", 0.5)
    conn = sqlite3.connect(str(tmp_path / "trials.sqlite"))
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute("UPDATE lab_trials SET metrics = '{}'")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute("DELETE FROM lab_trials")
    conn.close()
    assert reg.count() == 1


def test_holdout_opens_once(reg):
    _add(reg, "a-v1", 0.5, window="holdout")
    with pytest.raises(HoldoutAlreadyOpenedError):
        _add(reg, "a-v1", 0.9, window="holdout", spec_hash="otro")
    _add(reg, "b-v1", 0.1, window="holdout")  # otra estrategia si puede
    assert reg.count(window="holdout") == 2


def test_invalid_window(reg):
    with pytest.raises(ValueError):
        _add(reg, "a-v1", 0.5, window="validacion")


def test_persists_across_connections(tmp_path):
    r1 = TrialRegistry(tmp_path / "t.sqlite")
    _add(r1, "a-v1", 0.5)
    r1.close()
    r2 = TrialRegistry(tmp_path / "t.sqlite")
    assert r2.count() == 1
    r2.close()
