"""lab/paper_compare.py: paper (base falsa de Freqtrade) contra la re-simulacion del laboratorio, sin red ni SSH."""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from heatsignal.lab import paper_compare as pc
from heatsignal.lab.sim import Costs, simulate
from heatsignal.lab.strategies import MarketData
from heatsignal.paper import freqtrade_db as ftdb
from paper_fakes import make_db

SYMS = ["AAAUSDT", "BBBUSDT"]
START = pd.Timestamp("2026-02-08 16:45:37", tz="UTC")
CAP = 10_000.0


@pytest.fixture(scope="module")
def data():
    rng = np.random.default_rng(5)
    idx = pd.date_range("2026-01-01", "2026-02-15", freq="1min", tz="UTC", inclusive="left")
    out = {}
    for k, s in enumerate(SYMS):
        r = rng.normal(0.00003, 0.0008, len(idx)) + 0.00004 * np.sin(np.arange(len(idx)) / (4000 + 900 * k))
        c = 50.0 * (k + 1) * np.exp(np.cumsum(r))
        o = np.r_[c[0], c[:-1]]
        df = pd.DataFrame({"open": o, "high": np.maximum(o, c) * 1.0003, "low": np.minimum(o, c) * 0.9997, "close": c}, index=idx)
        f = pd.Series(rng.normal(0.0001, 0.00005, 45 * 3), index=pd.date_range("2026-01-01", periods=45 * 3, freq="8h", tz="UTC"))
        out[s] = MarketData(df, f, None)
    return out


def _sim(data):
    end = min(md.klines.index[-1] for md in data.values()) + pd.Timedelta(minutes=1)
    _, _, st, _ = pc.lab_signals(data, end, START)
    prices = {s: data[s].klines[data[s].klines.index >= START] for s in SYMS}
    fund = {s: data[s].funding[data[s].funding.index >= START] for s in SYMS}
    return simulate(prices, st, fund, Costs(), CAP)


def _paper_db(tmp_path: Path, res, *, delay_s=0, price_bps=0.0, size=1.0, skip_small=0.0, funding="lab") -> Path:
    """Base falsa armada desde los fills del laboratorio, con perturbaciones controladas."""
    trades, cur, tid = [], {}, 0
    base = CAP / len(SYMS)
    pos = {s: 0.0 for s in SYMS}
    for r in res.fills.itertuples():
        s = r.symbol
        entering, exiting = pos[s] == 0 and r.units_to != 0, r.units_to == 0
        delta = (r.units_to * size) - pos[s] if not exiting else -pos[s]
        if not (entering or exiting) and abs(delta) * r.price < skip_small * base:
            continue
        ms = int(r.time.value // 1_000_000) + delay_s * 1000
        if entering:
            tid += 1
            cur[s] = {"id": tid, "pair": s[:3] + "/USDT:USDT", "fills": [], "funding": 0.0}
            trades.append(cur[s])
        cur[s]["fills"].append((ms, delta, float(r.price) * (1 + price_bps / 1e4), "x"))
        pos[s] += delta
        if exiting:
            pos[s] = 0.0
    if funding == "lab":
        for t in trades:
            sym = t["pair"][:3] + "USDT"
            et = pd.Timestamp(t["fills"][0][0] - delay_s * 1000, unit="ms", tz="UTC")
            tr = res.trades[(res.trades.symbol == sym) & (res.trades.entry_time == et)]
            t["funding"] = float(tr.funding.iloc[0])
    db = tmp_path / "ft.sqlite"
    db.unlink(missing_ok=True)
    return make_db(db, trades, bot_start_ms=int(START.value // 1_000_000))


def test_exact_replica_differs_only_by_slippage(tmp_path, data) -> None:
    res = _sim(data)
    assert len(res.fills) > 10
    d = ftdb.dump(_paper_db(tmp_path, res))
    out = pc.compare(d, data=data, symbols=SYMS)
    assert out["status"] == "ok" and out["window"]["start"] == START.isoformat()
    assert out["window"]["start_source"].startswith("bot_start_time")
    dc = out["decomposition"]
    assert out["totals"]["sim"]["total"] == pytest.approx(res.breakdown["total"])
    for k in ("timing", "tamano", "precio_fill", "comisiones", "funding_posicion", "funding_contable"):
        assert dc[k] == pytest.approx(0.0, abs=1e-6), k
    assert dc["slippage"] == pytest.approx(res.breakdown["slippage"])
    assert out["totals"]["diff"] == pytest.approx(res.breakdown["slippage"])
    assert abs(dc["residual"]) < 1e-6
    for p in out["by_pair"]:
        assert p["paper"]["ordenes"] == p["sim"]["ordenes"]
        assert p["fill_bps"]["max_abs"] == pytest.approx(0.0, abs=1e-9)
        assert all(t["match"] == "ambos" and t["entry_delta_min"] == 0 for t in p["trades"])
        assert p["paper"]["realizado"] + p["paper"]["no_realizado"] == pytest.approx(p["paper"]["precio"])
    pts = out["curves"]["points"]
    assert out["curves"]["columns"] == ["paper", "sim", "canasta", "buy_and_hold_medio"]
    assert pts[-1][1] == pytest.approx(out["totals"]["paper"]["total"] / CAP, abs=1e-9)
    assert pts[-1][2] == pytest.approx(out["totals"]["sim"]["total"] / CAP)
    assert {d_["id"] for d_ in out["design_differences"]} >= {"rebalanceo_5", "sin_slippage", "reinversion"}
    json.dumps(out, allow_nan=False)  # JSON estricto


def test_perturbed_decomposition_is_exact_and_attributes(tmp_path, data) -> None:
    res = _sim(data)
    d = ftdb.dump(_paper_db(tmp_path, res, delay_s=150, price_bps=4.0, size=0.97, skip_small=0.05, funding="cero"))
    out = pc.compare(d, data=data, symbols=SYMS)
    dc = out["decomposition"]
    assert abs(dc["residual"]) < 1e-6 * CAP
    assert dc["total"] == pytest.approx(sum(dc[k] for k in pc.COMPONENTS))
    assert dc["precio_fill"] != 0 and dc["tamano"] != 0 and dc["timing"] != 0
    assert dc["funding_contable"] == pytest.approx(-sum(p["paper"]["funding_recalculado"] for p in out["by_pair"]))
    for p in out["by_pair"]:
        # +4 bps sobre el precio del fill (el fill llega 2,5 min tarde)
        assert p["fill_bps"]["n"] == p["paper"]["ordenes"]
        assert p["paper"]["ajustes"] < p["sim"]["ajustes"]  # umbral de rebalanceo
        assert p["decomposition"]["total"] == pytest.approx(p["diff"])
        assert all(t["entry_delta_min"] == pytest.approx(2.5) for t in p["trades"] if t["match"] == "ambos")


def test_fills_after_window_are_excluded_and_reported(tmp_path, data) -> None:
    res = _sim(data)
    db = _paper_db(tmp_path, res)
    d = ftdb.dump(db)
    end = pd.Timestamp("2026-02-12", tz="UTC")
    out = pc.compare(d, data=data, symbols=SYMS, end=end)
    assert out["window"]["end"] == end.isoformat()
    assert out["data"]["fills_after_window"] > 0
    assert any("despues de la ultima vela" in n for n in out["notes"])


def test_no_candles_yet(tmp_path, data) -> None:
    db = make_db(tmp_path / "ft.sqlite", [], bot_start_ms=int(pd.Timestamp("2026-03-01", tz="UTC").value // 1_000_000))
    out = pc.compare(ftdb.dump(db), data=data, symbols=SYMS)
    assert out["status"] == "sin_datos" and out["notes"]


def test_since_overrides_start_and_keeps_prior_position(tmp_path, data) -> None:
    res = _sim(data)
    d = ftdb.dump(_paper_db(tmp_path, res))
    since = pd.Timestamp("2026-02-11", tz="UTC")
    out = pc.compare(d, start=since, data=data, symbols=SYMS, start_source="--since")
    assert out["window"]["start"] == since.isoformat() and out["window"]["start_source"] == "--since"
    assert abs(out["decomposition"]["residual"]) < 1e-6 * CAP
    # el paper llega con posicion abierta y el lab arranca con la posicion objetivo vigente: el timing es chico
    assert any(p["paper"]["unidades_inicio"] != 0 for p in out["by_pair"])


def test_avg_cost_realized() -> None:
    assert pc.avg_cost_realized(0, 0, [(2, 10), (2, 20), (-3, 30)]) == pytest.approx(3 * (30 - 15))
    assert pc.avg_cost_realized(1, 10, [(-1, 12)]) == pytest.approx(2)
    assert pc.avg_cost_realized(0, 0, [(1, 10), (-2, 8), (1, 6)]) == pytest.approx(-2 + 2)  # short abierto a 8 y cerrado a 6


def test_write_outputs_history_one_row_per_date(tmp_path) -> None:
    def res(ts, diff):
        return {"generated_at": ts, "status": "ok", "window": {"days": 1}, "totals": {"paper": {"total": 1, "ordenes": 2},
                "sim": {"total": 1 - diff, "ordenes": 3}, "diff": diff}, "decomposition": {"timing": diff}}
    pc.write_outputs(res("2026-10-11T01:00:00+00:00", 1.0), tmp_path)
    pc.write_outputs(res("2026-10-18T01:00:00+00:00", 2.0), tmp_path)
    files = pc.write_outputs(res("2026-10-18T09:00:00+00:00", float("nan")), tmp_path)
    assert [f.name for f in files] == ["compare_2026-10-18.json", "paper_summary.json"]
    s = json.loads((tmp_path / "paper_summary.json").read_text())
    assert [h["date"] for h in s["history"]] == ["2026-10-11", "2026-10-18"]
    assert s["history"][-1]["diff"] is None and s["latest"]["generated_at"].startswith("2026-10-18T09")
    (tmp_path / "paper_summary.json").write_text("roto")
    pc.write_outputs(res("2026-10-25T01:00:00+00:00", 3.0), tmp_path)
    assert len(json.loads((tmp_path / "paper_summary.json").read_text())["history"]) == 1


class FakeRemote:
    def __init__(self, d):
        self.d, self.uploaded = d, []

    def dump(self):
        return self.d

    def upload(self, files):
        self.uploaded.append([Path(f).name for f in files])


def test_run_with_fake_remote_and_upload(tmp_path, data, monkeypatch) -> None:
    res = _sim(data)
    d = ftdb.dump(_paper_db(tmp_path, res))
    monkeypatch.setattr(pc, "load_window", lambda symbols, start, warmup, root=None: {s: data[s] for s in SYMS})
    import heatsignal.lab.strategies as strat
    monkeypatch.setattr(strat, "SEVEN", SYMS)
    remote = FakeRemote(d)
    out = pc.run(remote=remote, out_dir=tmp_path / "paper", update=False, upload=True)
    assert out["status"] == "ok"
    assert remote.uploaded == [[f"compare_{out['generated_at'][:10]}.json", "paper_summary.json"]]
    assert (tmp_path / "paper" / "paper_summary.json").is_file()


def test_update_market_data_reports_errors(tmp_path) -> None:
    calls = []

    def run_job(job, root, today):
        calls.append((job.dataset, job.symbol, job.month))
        if job.symbol == "BBBUSDT":
            raise RuntimeError("404 raro")
        return job, 10

    def funding_fn(sym, root):
        if sym == "AAAUSDT":
            raise RuntimeError("timeout")
        return 3

    from datetime import date
    out = pc.update_market_data(SYMS, pd.Timestamp("2026-09-20", tz="UTC"), root=tmp_path, today=date(2026, 10, 18),
                                run_job=run_job, funding_fn=funding_fn)
    assert ("klines", "AAAUSDT", "2026-10") in calls and ("funding", "AAAUSDT", "2026-09") in calls
    assert not any(c[0] == "funding" and c[2] == "2026-10" for c in calls)  # el mes en curso va por REST
    assert out["rows"] > 0 and any("BBBUSDT" in e for e in out["errors"]) and any("timeout" in e for e in out["errors"])
