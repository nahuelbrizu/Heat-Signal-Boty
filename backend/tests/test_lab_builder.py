"""Constructor de estrategias por bloques (lab/builder.py, 2026-11-21): validacion, id = hash, registro append-only,
regla sin mirar el futuro y la vista del panel."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import numpy as np
import pandas as pd
import pytest

from heatsignal.lab import builder as B
from heatsignal.lab.strategies import MarketData

SPEC = {"name": "rsi-tendencia", "timeframe": "1h", "direction": "long",
        "entry": {"type": "rsi", "period": 2, "low": 10, "high": 90},
        "filters": [{"type": "daily_trend", "ema": 20}], "exit": {"bars": 24, "stop_atr": 2.0}}


def _klines(days: int = 120, seed: int = 7) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    idx = pd.date_range("2026-01-01", periods=days * 1440, freq="1min", tz="UTC")
    close = 100 * np.exp(np.cumsum(rng.normal(0.00001, 0.001, len(idx))))
    op = np.r_[close[0], close[:-1]]
    hi = np.maximum(op, close) * (1 + rng.uniform(0, 0.0005, len(idx)))
    lo = np.minimum(op, close) * (1 - rng.uniform(0, 0.0005, len(idx)))
    return pd.DataFrame({"open": op, "high": hi, "low": lo, "close": close, "volume": rng.uniform(1, 10, len(idx))}, index=idx)


def test_validate_normalizes_and_rejects_bad_specs() -> None:
    norm = B.validate({**SPEC, "symbols": ["ETHUSDT", "BTCUSDT"]})
    assert norm["symbols"] == ["BTCUSDT", "ETHUSDT"] and norm["exit"]["target_atr"] is None
    for bad, msg in [({**SPEC, "timeframe": "4h"}, "timeframe"), ({**SPEC, "entry": {"type": "magia"}}, "desconocido"),
                     ({**SPEC, "entry": {"type": "rsi", "period": 2, "low": 60, "high": 90}}, "fuera de rango"),
                     ({**SPEC, "entry": {"type": "ema_cross", "fast": 30, "slow": 20}}, "menor"),
                     ({**SPEC, "exit": {"bars": 10_000}}, "fuera de rango"), ({**SPEC, "codigo": "import os"}, "desconocidos"),
                     ({**SPEC, "timeframe": "15min", "entry": {"type": "opening_range", "session": "ny", "minutes": 20}},
                      "multiplo")]:
        with pytest.raises(B.SpecError, match=msg):
            B.validate(bad)


def test_id_is_the_hash_of_the_rule_not_of_the_description() -> None:
    a = B.strategy_id_for(B.validate(SPEC))
    assert a.startswith("gen-rsi-tendencia-") and a == B.strategy_id_for(B.validate({**SPEC, "description": "otra"}))
    assert a != B.strategy_id_for(B.validate({**SPEC, "exit": {"bars": 25, "stop_atr": 2.0}}))


def test_registry_is_append_only_and_detects_tampering(tmp_path: Path) -> None:
    p = tmp_path / "gen.jsonl"
    e = B.make_entry(SPEC, origin="agente", note="prueba")
    assert B.append(e, p) is True and B.append(B.make_entry(SPEC), p) is False  # misma regla: no se duplica
    lines = p.read_text().splitlines()
    rec = json.loads(lines[0])
    rec["spec"]["exit"]["bars"] = 99  # alguien edita la linea a mano
    p.write_text(json.dumps(rec) + "\n")
    entries, warns = B.load_entries(p)
    assert entries == [] and "no coinciden" in warns[0]


def test_rule_runs_and_does_not_look_ahead() -> None:
    k = _klines()
    data = {"BTCUSDT": MarketData(k)}
    cut = pd.Timestamp("2026-03-15", tz="UTC")
    specs = [SPEC,
             {"name": "ruptura", "timeframe": "15min", "direction": "both", "entry": {"type": "breakout", "n": 40},
              "filters": [{"type": "atr_pct", "min": 0.0, "max": 5.0}], "exit": {"bars": 8, "target_atr": 3}},
             {"name": "vwap", "timeframe": "5min", "direction": "both", "entry": {"type": "vwap_dev", "k": 2},
              "filters": [{"type": "session", "name": "ny"}], "exit": {"bars": 6}},
             {"name": "rango", "timeframe": "15min", "direction": "both",
              "entry": {"type": "opening_range", "session": "londres", "minutes": 30}, "exit": {"bars": 8}}]
    for s in specs:
        rule = B.compile_rule(B.validate(s))
        full = rule(data, {})["BTCUSDT"]
        assert (full != 0).sum() > 0, s["name"]
        assert set(np.unique(full.values)) <= {-1.0, 0.0, 1.0}
        part = rule({"BTCUSDT": MarketData(k[k.index < cut])}, {})["BTCUSDT"]
        early = lambda x: x[(x != 0) & (x.index < cut - pd.Timedelta(days=2))].index  # noqa: E731
        assert early(full).equals(early(part)), f"{s['name']} mira el futuro"


def test_long_only_never_shorts_and_session_filter_holds() -> None:
    k = _klines(60)
    s = B.validate({"name": "solo-long", "timeframe": "5min", "direction": "long", "entry": {"type": "vwap_dev", "k": 1},
                    "filters": [{"type": "session", "name": "asia"}], "exit": {"bars": 3}})
    e, _, _ = B.entry_signals(s, MarketData(k))
    assert (e >= 0).all() and (e > 0).any()
    hours = (e[e > 0].index - pd.Timedelta(minutes=5)).hour
    assert ((hours >= 0) & (hours < 8)).all()


def test_generated_catalog_builds_specs(tmp_path: Path) -> None:
    p = tmp_path / "gen.jsonl"
    B.append(B.make_entry(SPEC), p)
    cat = B.generated_catalog(p)
    (sid, (spec, rule)), = cat.items()
    assert spec.family == "generada" and spec.grid == {} and spec.timeframe == "1h" and callable(rule)
    assert "RSI(2)" in spec.description and spec.params["symbols"] == B.SEVEN


async def test_panel_lists_generated_with_lab_result(tmp_path: Path) -> None:
    from heatsignal.api.panel import PanelContext, create_panel_app
    from heatsignal.ingestion.health import HealthRegistry

    e = B.make_entry(SPEC, origin="agente")
    B.append(e, tmp_path / "lab_generated.jsonl")
    (tmp_path / "lab_summary.json").write_text(json.dumps({"generated_at": "2026-11-21T00:00:00Z", "strategies": [
        {"strategy_id": e["strategy_id"], "sharpe": -0.5, "trades": 40, "verdict": {"pasa": False}}]}))
    ctx = PanelContext(health=HealthRegistry(), lab_summary_path=tmp_path / "lab_summary.json")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_panel_app(ctx)), base_url="http://panel") as c:
        r = await c.get("/api/lab/generated")
    assert r.status_code == 200
    g = r.json()
    (s,) = g["estrategias"]
    assert s["origin"] == "agente" and s["lab"]["sharpe"] == -0.5 and "RSI(2)" in s["describe"]
    assert "opening_range" in g["bloques"]["entradas"]


def test_generated_exit_ficha_from_blocks() -> None:
    from heatsignal.lab.exits import chips, generated_ficha

    f = generated_ficha(B.validate({**SPEC, "exit": {"bars": 24, "stop_atr": 2.0, "target_atr": 4.0}}))
    assert f["stop"]["valor"] == 2.0 and f["take_profit"]["valor"] == 4.0 and f["tiempo"]["valor"] == 24.0
    assert f["stop"]["param"] is None and "Generada por bloques" in f["nota"] and chips(f)
    f5 = generated_ficha(B.validate({**SPEC, "timeframe": "5min", "exit": {"bars": 12}}))
    assert f5["stop"] is None and f5["take_profit"] is None and f5["tiempo"]["valor"] == 1.0
