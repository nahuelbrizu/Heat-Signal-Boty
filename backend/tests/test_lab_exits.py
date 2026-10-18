"""Ficha de salidas (lab/exits.py) y atribucion de cada cierre a su motivo. Sin red.

- La ficha cubre todo el CATALOG y sus valores numericos coinciden con spec.params (no inventa nada).
- La reconstruccion de motivos coincide EXACTAMENTE con strategies.exits_with_levels: stop, objetivo, empate -> stop,
  tiempo, y un caso aleatorio grande.
- Envolver las reglas para capturar las salidas no cambia ni su resultado ni ningun spec_hash.
"""

from __future__ import annotations

import ast
import json
import subprocess
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from heatsignal.lab import exits as E
from heatsignal.lab import strategies as S
from heatsignal.lab.strategies import CATALOG, MarketData

sys.path.insert(0, str(Path(__file__).parent))
from test_lab_run import T0, _md  # noqa: E402

ROOT = Path(__file__).parents[2]
MIN = pd.Timedelta(minutes=1)


# ---------------------------------------------------------------------------- ficha


def test_ficha_covers_whole_catalog_and_matches_params() -> None:
    assert set(E.EXITS) == set(CATALOG)
    for sid, (spec, _) in CATALOG.items():
        assert E.check_params(sid, spec.params) == [], sid
        f = E.EXITS[sid]
        assert {"stop", "take_profit", "tiempo", "otra", "nota"} <= set(f), sid
        assert f["cierre"] in E.MOTIVOS and f["giro"] in E.MOTIVOS
        assert len(f["nota"]) < 140
        assert E.ficha_json(sid)["chips"]


def test_ficha_known_values() -> None:
    p = CATALOG["ema-cruce-v1"][0].params
    f = E.EXITS["ema-cruce-v1"]
    assert f["stop"]["valor"] == p["sl_atr"] == 1.5
    assert f["take_profit"]["valor"] == p["rr"] * p["sl_atr"] == 3.0
    assert f["tiempo"]["valor"] == p["max_hold_hours"] == 720
    assert E.ficha_json("ema-cruce-v1")["resumen"] == "stop 1,5 ATR · objetivo 3 ATR · máx. 30 d"
    assert E.ficha_json("donchian-ensemble-v1")["resumen"] == "sin stop: canal"
    assert E.EXITS["rango-reversion-v1"]["stop"]["valor"] == CATALOG["rango-reversion-v1"][0].params["sl_atr"]
    assert E.EXITS["pares-eth-btc-v1"]["stop"]["valor"] == CATALOG["pares-eth-btc-v1"][0].params["stop_z"]
    assert E.EXITS["pares-eth-btc-v1"]["stop"]["velas_1m"] is False
    assert E.ficha_json("max-ruptura-10d-v1")["resumen"] == "sin stop: tiempo 1 d"


def test_check_params_detects_a_mismatch() -> None:
    p = dict(CATALOG["vwap-reversion-v1"][0].params, sl_atr=2.0)
    assert E.check_params("vwap-reversion-v1", p)


def test_cartera_inherits_its_components() -> None:
    comps = list(CATALOG["cartera-tendencia-v1"][0].params["components"])
    assert E.EXITS["cartera-tendencia-v1"]["hereda"] == comps
    fj = E.ficha_json("cartera-tendencia-v1")
    assert [c["strategy_id"] for c in fj["componentes"]] == comps
    assert fj["componentes"][2]["resumen"] == E.ficha_json("ema-cruce-v1")["resumen"]


def test_exits_module_does_not_import_pandas_and_specs_unchanged() -> None:
    code = ("import builtins, sys\n_imp = builtins.__import__\n"
            "def g(n, *a, **k):\n    if n.split('.')[0] in ('pandas', 'numpy'): raise ImportError('bloqueado')\n"
            "    return _imp(n, *a, **k)\nbuiltins.__import__ = g\n"
            "from heatsignal.lab import exits as E\nprint(E.ficha_json('ema-cruce-v1')['resumen'])\n")
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env={"PYTHONPATH": str(ROOT / "backend/src")})
    assert r.returncode == 0, r.stderr
    assert dataclass_fields_unchanged()


def dataclass_fields_unchanged() -> bool:
    # la ficha vive aparte: el Spec no gano campos (si los ganara, cambiarian todos los spec_hash)
    from dataclasses import fields

    return [f.name for f in fields(S.Spec)] == ["strategy_id", "family", "description", "timeframe", "direction", "horizon",
                                               "params", "grid", "needs", "backtestable", "source"]


def _ft_attrs(path: Path, cls: str) -> dict:
    tree = ast.parse(path.read_text())
    consts = {}
    for n in tree.body:
        if isinstance(n, ast.Assign) and isinstance(n.targets[0], ast.Name):
            try:
                consts[n.targets[0].id] = ast.literal_eval(n.value)
            except ValueError:
                pass
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == cls:
            out = {}
            for st in node.body:
                if isinstance(st, ast.Assign) and isinstance(st.targets[0], ast.Name):
                    name = st.targets[0].id
                    if name in ("stoploss", "minimal_roi", "trailing_stop"):
                        v = st.value
                        out[name] = consts[v.id] if isinstance(v, ast.Name) else ast.literal_eval(v)
            return out
    raise AssertionError(cls)


def test_freqtrade_exits_match_the_strategy_files() -> None:
    don = _ft_attrs(ROOT / "freqtrade/user_data/strategies/DonchianEnsembleV1.py", "DonchianEnsembleV1")
    hyp = _ft_attrs(ROOT / "freqtrade/user_data_hyp/strategies/HypothesisBridgeV1.py", "HypothesisBridgeV1")
    assert don["stoploss"] == E.FREQTRADE_EXITS["DonchianEnsembleV1"]["stoploss"] == -0.99
    assert don["minimal_roi"] == E.FREQTRADE_EXITS["DonchianEnsembleV1"]["minimal_roi"]
    assert hyp["stoploss"] == E.FREQTRADE_EXITS["HypothesisBridgeV1"]["stoploss"] == -0.20
    assert hyp["minimal_roi"] == E.FREQTRADE_EXITS["HypothesisBridgeV1"]["minimal_roi"]
    x = E.freqtrade_exits("HypothesisBridgeV1")
    assert x["source"] == "estrategia" and x["stop_text"] == "stop −20 %" and x["roi_text"] == "ROI desactivado"
    assert E.freqtrade_exits("DonchianEnsembleV1", {"stoploss": -0.99, "minimal_roi": {"0": 100}})["source"] == "api"
    assert E.freqtrade_exits("DonchianEnsembleV1")["stop_text"].startswith("sin stop")


# ---------------------------------------------------------------------------- atribucion vs exits_with_levels


def _flat(n: int, price: float = 100.0) -> pd.DataFrame:
    idx = pd.date_range(T0, periods=n, freq="1min")
    return pd.DataFrame({"open": price, "high": price + 0.1, "low": price - 0.1, "close": price}, index=idx)


def _check(entries, k, horizon, tp=None, sl=None):
    ref = S.exits_with_levels(entries, k, horizon, tp=tp, sl=sl)
    trades = E.exit_reasons(entries, k, horizon, tp=tp, sl=sl)
    assert E.series_from_trades(trades).equals(ref)
    return trades


def test_stop_target_tie_and_time_match_exits_with_levels() -> None:
    k = _flat(600)
    t = T0 + pd.Timedelta(minutes=10)
    e = pd.Series([1.0], index=[t])
    tp, sl = pd.Series([101.0], index=[t]), pd.Series([99.0], index=[t])
    # stop: el minimo toca 99 en el minuto 15
    ks = k.copy()
    ks.loc[T0 + pd.Timedelta(minutes=15), "low"] = 98.9
    tr = _check(e, ks, pd.Timedelta(hours=4), tp, sl)
    assert tr[0]["motivo"] == "stop" and tr[0]["exit"] == T0 + pd.Timedelta(minutes=16)
    # objetivo: el maximo toca 101 en el minuto 20 (y el stop despues)
    ko = k.copy()
    ko.loc[T0 + pd.Timedelta(minutes=20), "high"] = 101.2
    ko.loc[T0 + pd.Timedelta(minutes=30), "low"] = 98.0
    tr = _check(e, ko, pd.Timedelta(hours=4), tp, sl)
    assert tr[0]["motivo"] == "objetivo" and tr[0]["exit"] == T0 + pd.Timedelta(minutes=21)
    # empate en la misma vela 1m -> stop (conservador)
    kt = k.copy()
    kt.loc[T0 + pd.Timedelta(minutes=25), ["high", "low"]] = [101.5, 98.5]
    tr = _check(e, kt, pd.Timedelta(hours=4), tp, sl)
    assert tr[0]["motivo"] == "stop" and tr[0]["exit"] == T0 + pd.Timedelta(minutes=26)
    # tiempo: no toca nada
    tr = _check(e, k, pd.Timedelta(hours=2), tp, sl)
    assert tr[0]["motivo"] == "tiempo" and tr[0]["exit"] == t + pd.Timedelta(hours=2)
    # short: stop arriba, objetivo abajo
    es = pd.Series([-1.0], index=[t])
    ks2 = k.copy()
    ks2.loc[T0 + pd.Timedelta(minutes=12), "low"] = 98.9
    tr = _check(es, ks2, pd.Timedelta(hours=4), pd.Series([99.0], index=[t]), pd.Series([101.0], index=[t]))
    assert tr[0]["motivo"] == "objetivo"
    # solo stop (sin objetivo), con NaN en los niveles de las velas sin entrada
    tr = _check(e, ks, pd.Timedelta(hours=4), None, pd.Series([99.0, np.nan], index=[t, t + MIN]))
    assert tr[0]["motivo"] == "stop"


def test_level_at_the_decision_candle_counts() -> None:
    # el nivel se evalua desde la vela que abre EN la decision (la primera post-decision)
    k = _flat(300)
    t = T0 + pd.Timedelta(minutes=5)
    k.loc[t, "low"] = 98.0
    tr = _check(pd.Series([1.0], index=[t]), k, pd.Timedelta(hours=1), None, pd.Series([99.0], index=[t]))
    assert tr[0]["exit"] == t + MIN and tr[0]["motivo"] == "stop"


def test_random_cases_match_exactly() -> None:
    rng = np.random.default_rng(11)
    k = _md(6, 3).klines
    for _ in range(6):
        idx = k.index[rng.choice(len(k) - 500, 120, replace=False)]
        side = rng.choice([-1.0, 0.0, 1.0], len(idx))
        e = pd.Series(side, index=idx).sort_index()
        c = k["close"].reindex(e.index)
        width = rng.uniform(0.001, 0.01, len(e))
        tp = c * (1 + np.sign(e) * width * rng.uniform(0.5, 2))
        sl = c * (1 - np.sign(e) * width)
        horizon = pd.Timedelta(minutes=int(rng.integers(30, 600)))
        trades = _check(e, k, horizon, tp, sl)
        assert {t["motivo"] for t in trades} <= {"stop", "objetivo", "tiempo"}
        assert len(trades) > 5


def test_hold_reasons_match_hold_events() -> None:
    k = _flat(2000)
    e = pd.Series([1.0, -1.0, 1.0, -1.0], index=k.index[[10, 40, 300, 360]])
    ref = S.hold_events(e, pd.Timedelta(hours=1), k.index)
    tr = E.hold_reasons(e, pd.Timedelta(hours=1))
    assert E.series_from_trades(tr).equals(ref) and len(tr) == 3  # la de las 06:00 entra justo al cerrar la anterior


# ---------------------------------------------------------------------------- captura sobre reglas reales


def _market(days: int = 12) -> dict[str, MarketData]:
    return {s: _md(days, seed) for s, seed in (("BNBUSDT", 1), ("TRXUSDT", 2), ("XLMUSDT", 3))}


def test_capture_does_not_change_the_rule_and_verifies() -> None:
    data = _market()
    spec, rule = CATALOG["rango-reversion-v1"]
    before = rule(data, dict(spec.params))
    with E.capture_exits() as rec:
        during = rule(data, dict(spec.params))
    assert S.exits_with_levels.__name__ == "exits_with_levels" and S.hold_events.__name__ == "hold_events"
    assert all(before[s].equals(during[s]) for s in before)
    assert rec and all(c["coincide"] for c in rec)
    r = E.attribute("rango-reversion-v1", rule, data, dict(spec.params))
    assert r["verificacion"]["coincide"] and r["verificacion"]["llamadas"] == 3
    assert set(r["por_motivo"]) == set(E.MOTIVOS)
    assert r["trades"] == sum(r["por_motivo"].values()) and r["trades"] > 0
    assert r["por_motivo"]["senal"] == r["por_motivo"]["rebalanceo"] == 0


def test_attribute_time_and_transition_rules() -> None:
    data = {s: _md(15, i) for i, s in enumerate(("BTCUSDT", "ETHUSDT", "SOLUSDT"), 1)}
    spec, rule = CATALOG["max-ruptura-10d-v1"]
    r = E.attribute("max-ruptura-10d-v1", rule, data, dict(spec.params))
    assert r["verificacion"]["coincide"] and r["por_motivo"]["tiempo"] == r["trades"]
    spec, rule = CATALOG["donchian-ensemble-v1"]
    r = E.attribute("donchian-ensemble-v1", rule, data, {**spec.params, "channels": [5, 10], "vol_window": 10})
    assert r["metodo"].startswith("transiciones") and r["por_motivo"]["stop"] == 0
    assert set(r["por_motivo"]) == set(E.MOTIVOS)


def test_attribute_cartera_sums_components(monkeypatch) -> None:
    calls = []

    def fake(sid, rule, data, params, ficha=None):
        calls.append(sid)
        return {"por_motivo": {**E._empty_counts(), "tiempo": 2}, "abiertas_al_final": 1, "ajustes": 0,
                "verificacion": {"coincide": True, "llamadas": 1}}

    orig = E.attribute
    monkeypatch.setattr(E, "attribute", fake)
    r = orig("cartera-tendencia-v1", None, {}, {})
    assert calls == E.EXITS["cartera-tendencia-v1"]["hereda"]
    assert r["por_motivo"]["tiempo"] == 6 and r["trades"] == 6 and set(r["componentes"]) == set(calls)


def test_spec_hashes_unchanged_after_using_exits() -> None:
    before = {sid: spec.spec_hash() for sid, (spec, _) in CATALOG.items()}
    data = _market(8)
    spec, rule = CATALOG["vwap-reversion-v1"]
    E.attribute("vwap-reversion-v1", rule, {"BNBUSDT": data["BNBUSDT"]}, dict(spec.params))
    assert {sid: spec.spec_hash() for sid, (spec, _) in CATALOG.items()} == before
    # los congelados del lote 1 que fija decisions.md
    assert CATALOG["donchian-ensemble-v1"][0].spec_hash() == "86061813c7d1691c"
    assert json.dumps(E.ficha_json("vwap-reversion-v1"), ensure_ascii=False)


def test_diagnose_salidas_does_not_use_the_holdout() -> None:
    from heatsignal.lab import diagnostics as D

    spec, rule = CATALOG["rango-reversion-v1"]
    market = _market(16)
    holdout = T0 + pd.Timedelta(days=10)
    result = {"strategy_id": spec.strategy_id, "spec_hash": spec.spec_hash(), "symbols": list(market),
              "data": {"holdout_start": str(holdout)}, "walk_forward": {"chosen": []}}
    poison = {}
    for s, md in market.items():
        k = md.klines.copy()
        k.loc[k.index >= holdout, ["open", "high", "low", "close"]] *= 3
        poison[s] = MarketData(k, md.funding)
    a = D.diagnose("salidas", spec, rule, result, market)
    b = D.diagnose("salidas", spec, rule, result, poison)
    assert a["salidas"]["por_motivo"] == b["salidas"]["por_motivo"]
    assert a["salidas"]["holdout"] == "NO USADO" and a["salidas"]["adds_trials"] is False
    assert "pbo" not in a and "robustez" not in a
    with pytest.raises(ValueError):
        D.diagnose("otra", spec, rule, result, market)
