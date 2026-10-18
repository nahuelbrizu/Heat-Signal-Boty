"""Salidas en todo el sistema (2026-10-19): MAE/MFE de hipotesis, motivo de cierre del bot de paper, stop/ROI de
Freqtrade, endpoints del panel y render con escape en node. Sin red."""

from __future__ import annotations

import json
import shutil
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import httpx
import pandas as pd
import pytest

from heatsignal.api.panel import PanelContext, create_panel_app
from heatsignal.evaluation import excursion as X
from heatsignal.evaluation.pending_store import PendingHypothesisStore
from heatsignal.evaluation.strategy_registry import StrategyHypothesis, StrategyRegistry
from heatsignal.history import klines as hk
from heatsignal.history.store import HistoryStore
from heatsignal.ingestion.health import HealthRegistry
from heatsignal.lab.strategies import Spec
from heatsignal.paper import freqtrade_db as ftdb
from heatsignal.paperbot import view as pbv
from heatsignal.paperbot.engine import EngineConfig, run_once
from heatsignal.schema.hypotheses import PendingHypothesis
from heatsignal.schema.signals import Direction

sys.path.insert(0, str(Path(__file__).parent))
import test_panel_api as TPA  # noqa: E402
from paper_fakes import make_db  # noqa: E402
from paperbot_fakes import DAY, H, MIN, T0, fill_funding, fill_history  # noqa: E402

HARNESS = Path(__file__).parent / "js" / "render_view.mjs"
EXITS_VIEW = Path(__file__).parent / "js" / "exits_view.mjs"
APP = Path(__file__).parents[1] / "src/heatsignal/api/static/app"
EVIL = "<img src=x onerror=alert(1)>"


# ---------------------------------------------------------------------------- MAE / MFE


def test_excursion_long_and_short() -> None:
    c = [(0, 100, 101, 98, 100), (60_000, 100, 104, 99, 103), (120_000, 103, 103.5, 96, 97)]
    lo = X.excursion("LONG", 100.0, c, atr=2.0)
    assert lo["mae_pct"] == pytest.approx(4.0) and lo["mfe_pct"] == pytest.approx(4.0)
    assert lo["mae_atr"] == pytest.approx(2.0) and lo["mfe_atr"] == pytest.approx(2.0)
    assert lo["mae_ms"] == 120_000 and lo["mfe_ms"] == 60_000
    hits = {s["atr"]: s for s in lo["stops"]}
    assert hits[1.0]["hit"] and hits[1.0]["hit_ms"] == 0          # 2 en contra = 1 ATR ya en la primera vela
    assert hits[1.5]["hit_ms"] == 120_000 and hits[2.0]["hit_ms"] == 120_000
    sh = X.excursion("SHORT", 100.0, c, atr=2.0)
    assert sh["mae_pct"] == pytest.approx(4.0) and sh["mfe_pct"] == pytest.approx(4.0)  # max high 104, min low 96
    assert sh["mae_ms"] == 60_000 and sh["mfe_ms"] == 120_000
    assert {s["atr"]: s["hit_ms"] for s in sh["stops"]}[1.5] == 60_000
    # nunca en contra: MAE 0; sin ATR: sin corte de stops
    up = X.excursion("LONG", 100.0, [(0, 100, 105, 100, 105)], atr=None)
    assert up["mae_pct"] == 0 and up["mae_atr"] is None and up["stops"] == []
    with pytest.raises(ValueError):
        X.excursion("LONG", 100.0, [], atr=1.0)


def test_wilder_atr_matches_htf() -> None:
    from heatsignal.features.htf import wilder_atr as htf_atr
    from heatsignal.schema.features import Candle1h

    cs = [(i * 3_600_000, 100 + i, 102 + i, 99 + i, 101 + i) for i in range(20)]
    c1 = [Candle1h(open_ms=t, open=o, high=h, low=lo, close=c, source="klines") for t, o, h, lo, c in cs]
    assert X.wilder_atr(cs) == pytest.approx(htf_atr(c1))


def _eval(tmp_path):
    reg = StrategyRegistry(tmp_path / "eval.sqlite")
    s = StrategyHypothesis(description="tendencia 1h", entry_conditions={}, exit_conditions={"horizon_s": 4 * 3600}, author="t")
    reg.register(s)
    store = PendingHypothesisStore(tmp_path / "eval.sqlite", reg)
    return reg, store, s


def _hyp(sid, created: datetime, direction=Direction.LONG, snap=None, symbol="BTC-USDT") -> PendingHypothesis:
    return PendingHypothesis(strategy_id=sid, symbol=symbol, created_at=created, direction=direction, confidence_score=0.6,
                             entry_reference_price=Decimal("100"), horizon_s=4 * 3600, rationale="r", timeframe="1h",
                             horizon_bars=4, htf_snapshot=snap)


def _resolve(store, h, win: bool):
    from heatsignal.evaluation.pending_store import ResolutionMetrics
    from heatsignal.schema.hypotheses import HypothesisStatus, Resolution

    due = h.created_at + timedelta(seconds=h.horizon_s)
    store.resolve(Resolution(hypothesis_id=h.hypothesis_id, status=HypothesisStatus.RESOLVED, resolved_at=due,
                             exit_price=Decimal("101" if win else "99"), pnl_pct=0.9 if win else -1.1, win=win),
                  ResolutionMetrics(gross_pnl_pct=1.0, cost_bps=10, benchmark_pnl_pct=1.0, alpha_pct=-0.1, exit_price_ts=due))


def test_compute_from_history_1m_and_from_rest_1h(tmp_path) -> None:
    created = datetime(2026, 10, 1, 10, tzinfo=timezone.utc)
    c0 = int(created.timestamp() * 1000)
    hist = HistoryStore(tmp_path / "hist.sqlite")
    rows = []
    for i in range(4 * 60):
        lo = 97.0 if i == 30 else 99.5
        rows.append((hk.EXCHANGE, "BTC-USDT", "1m", c0 + i * 60_000, 100.0, 103.0 if i == 200 else 100.5, lo, 100.0, 1.0, 100.0, 1, 50.0))
    hist.add_klines(rows)
    hist.close()
    calls = []

    def http_get(url, timeout):
        calls.append(url)
        q = dict(x.split("=") for x in url.split("?")[1].split("&"))
        start, end = int(q["startTime"]), int(q["endTime"])
        t0 = start - start % 3_600_000
        out = [[t, "100", "102", "98", "101", "1", t + 3_599_999] for t in range(t0, end + 1, 3_600_000)]
        return 200, json.dumps(out).encode()

    src = X.CandleSource(tmp_path / "hist.sqlite", http_get=http_get)
    now = c0 + 10 * 3_600_000
    h = _hyp("00000000-0000-0000-0000-000000000001", created, snap={"atr14_1h": 1.5})
    row = X.compute(h, src, now_ms=now)
    assert row["source"] == "klines_1m" and row["n_candles"] == 240 and not row["partial_before"]
    assert row["mae_pct"] == pytest.approx(3.0) and row["mfe_pct"] == pytest.approx(3.0)
    assert row["atr"] == 1.5 and row["mae_atr"] == pytest.approx(2.0) and "htf_snapshot" in row["atr_source"]
    assert {s["atr"]: s["hit"] for s in json.loads(row["stops"])} == {1.0: True, 1.5: True, 2.0: True}
    assert calls == []  # con la base completa no se pide nada a la red
    # otro simbolo sin base: 1h del REST; sin ATR en el snapshot: ATR de las velas previas
    h2 = _hyp("00000000-0000-0000-0000-000000000001", created + timedelta(minutes=17), direction=Direction.SHORT,
              symbol="ETH-USDT")
    row2 = X.compute(h2, src, now_ms=now)
    assert row2["source"] == "rest_1h" and row2["partial_before"] and row2["n_candles"] == 5
    assert row2["atr"] == pytest.approx(4.0) and "antes de la emision" in row2["atr_source"]
    assert row2["mae_pct"] == pytest.approx(2.0) and row2["mfe_pct"] == pytest.approx(2.0)
    assert any("interval=1h" in u for u in calls)
    # todavia no vencio
    assert X.compute(h2, src, now_ms=c0 + 3_600_000) is None


def test_write_missing_is_append_only_and_once(tmp_path) -> None:
    reg, store, s = _eval(tmp_path)
    created = datetime(2026, 10, 1, 10, tzinfo=timezone.utc)
    h = _hyp(s.strategy_id, created, snap={"atr14_1h": 2.0})
    pend = _hyp(s.strategy_id, created + timedelta(hours=1))
    store.add(h)
    store.add(pend)
    _resolve(store, h, win=True)

    class Src:
        def minutes(self, symbol, a, b):
            return [(a + i * 60_000, 100, 101, 97, 100) for i in range((b - a) // 60_000)]

        def hours(self, *a):
            raise AssertionError("no deberia pedir 1h")

    now = created + timedelta(hours=8)
    r = X.write_missing(tmp_path / "eval.sqlite", store, Src(), now=now)
    assert r == {"written": 1, "waiting": 0, "errors": []}
    assert X.write_missing(tmp_path / "eval.sqlite", store, Src(), now=now)["written"] == 0  # una sola vez
    ex = store.excursion(h.hypothesis_id)
    assert ex["mae_pct"] == pytest.approx(3.0) and ex["mae_atr"] == pytest.approx(1.5) and "NO pre-registrado" in ex["descriptivo"]
    assert store.excursion(pend.hypothesis_id) is None
    summ = store.excursion_summary()
    assert summ[0]["n"] == 1 and {x["atr"]: x["hit_won"] for x in summ[0]["stops"]} == {1.0: 1, 1.5: 1, 2.0: 0}
    con = sqlite3.connect(tmp_path / "eval.sqlite")
    with pytest.raises(sqlite3.DatabaseError):
        con.execute("UPDATE hypothesis_excursions SET mae_pct = 0")
    with pytest.raises(sqlite3.DatabaseError):
        con.execute("DELETE FROM hypothesis_excursions")


def test_job_runs_excursions_without_breaking(tmp_path) -> None:
    from heatsignal.evaluation import job
    from heatsignal.evaluation.hypothesis_log import HypothesisLog

    reg, store, _ = _eval(tmp_path)

    class P:
        def price_at(self, *a):
            return None

    out = job.run_once(registry=reg, store=store, hypothesis_log=HypothesisLog(tmp_path / "eval.sqlite"), report_log=None,
                       price_source=P(), excursion_writer=lambda now: (_ for _ in ()).throw(RuntimeError("red")))
    assert out["excursions"] == {"error": "RuntimeError: red"}


# ---------------------------------------------------------------------------- panel: hipotesis y laboratorio


def _client(ctx: PanelContext) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=create_panel_app(ctx)), base_url="http://panel")


async def test_hypothesis_detail_has_exit_rule_and_excursion(tmp_path) -> None:
    ctx, d = await TPA._full_ctx(tmp_path)
    win = d["win"]
    st = X.ExcursionStore(tmp_path / "eval.sqlite")
    st.add({"hypothesis_id": str(win.hypothesis_id), "model_id": X.MODEL_ID, "computed_at": "2026-10-01T02:00:00+00:00",
            "source": "rest_1h", "n_candles": 1, "window_start_ms": 0, "window_end_ms": 1, "entry": 100.0, "atr": 2.0,
            "atr_source": "htf_snapshot", "mae_pct": 1.0, "mfe_pct": 3.0, "mae_atr": 0.5, "mfe_atr": 1.5, "mae_ms": 0,
            "mfe_ms": 0, "stops": json.dumps([{"atr": 1.0, "hit": False, "hit_ms": None}]), "partial_before": 0, "note": "n"})
    st.close()
    c = _client(ctx)
    body = (await c.get(f"/api/hypotheses/{win.hypothesis_id}")).json()
    assert body["exit_rule"]["tipo"] == "tiempo" and body["exit_rule"]["texto"] == "por tiempo: 1 h"
    assert body["excursion"]["mfe_atr"] == 1.5 and body["excursion"]["stops"][0]["hit"] is False
    other = (await c.get(f"/api/hypotheses/{d['loss'].hypothesis_id}")).json()
    assert other["excursion"] is None
    summ = (await c.get("/api/hypotheses/excursions")).json()
    assert summ["descriptivo"] is True and summ["strategies"][0]["name"] == "rebote" and summ["strategies"][0]["n"] == 1
    assert (await c.get(f"/api/hypotheses/excursions?strategy_id={d['s2'].strategy_id}")).json()["strategies"] == []


def test_exit_rule_text_for_1h_bars() -> None:
    from heatsignal.api.panel_api import exit_rule

    h = _hyp("00000000-0000-0000-0000-000000000001", datetime(2026, 10, 1, tzinfo=timezone.utc))
    assert exit_rule(h)["texto"] == "por tiempo: 4 velas de 1h"


async def test_lab_endpoints_attach_the_exit_ficha(tmp_path) -> None:
    ctx, _ = await TPA._full_ctx(tmp_path)
    (tmp_path / "lab_summary.json").write_text(json.dumps({"generated_at": "2026-10-19T00:00:00Z", "strategies": [
        {"strategy_id": "ema-cruce-v1", "sharpe": 0.4}, {"strategy_id": "no-existe-v1"}]}))
    (tmp_path / "lab_detail_donchian-ensemble-v1.json").write_text(json.dumps({"strategy_id": "donchian-ensemble-v1",
        "diagnostics": {"salidas": {"por_motivo": {"senal": 3}}}}))
    c = _client(ctx)
    lab = (await c.get("/api/lab")).json()
    assert lab["strategies"][0]["exits"]["resumen"] == "stop 1,5 ATR · objetivo 3 ATR · máx. 30 d"
    assert lab["strategies"][1]["exits"] is None
    det = (await c.get("/api/lab/donchian-ensemble-v1")).json()
    assert det["exits"]["resumen"] == "sin stop: canal" and det["diagnostics"]["salidas"]["por_motivo"]["senal"] == 3


def test_publish_carries_the_salidas_block(tmp_path) -> None:
    from heatsignal.lab.publish import write_all

    d = tmp_path / "results" / "rango-reversion-v1"
    d.mkdir(parents=True)
    (d / "20260101T000000.json").write_text(json.dumps({"strategy_id": "rango-reversion-v1", "oos": {"sharpe": 0.1},
                                                         "data": {"holdout": "SELLADO"}}))
    (d / "diagnostics_20260102T000000.json").write_text(json.dumps({"run": "20260101T000000", "generated_at": "g",
        "salidas": {"por_motivo": {"stop": 5, "objetivo": 7, "tiempo": 2, "senal": 0, "rebalanceo": 0}, "trades": 14,
                    "abiertas_al_final": 0, "ajustes": 0, "verificacion": {"coincide": True, "llamadas": 3},
                    "holdout": "NO USADO", "por_simbolo": {"BNBUSDT": {"stop": 5}}}}))
    write_all(tmp_path)
    summ = json.loads((tmp_path / "lab_summary.json").read_text())["strategies"][0]
    det = json.loads((tmp_path / "lab_detail_rango-reversion-v1.json").read_text())
    assert summ["diagnostics"]["salidas"]["por_motivo"]["objetivo"] == 7 and "por_simbolo" not in summ["diagnostics"]["salidas"]
    assert det["diagnostics"]["salidas"]["por_simbolo"]["BNBUSDT"]["stop"] == 5
    assert det["diagnostics"]["pbo"] is None


# ---------------------------------------------------------------------------- bot de paper: motivo de cada cierre


def stop_rule(data, p):
    """Juguete: entradas fijas con stop en un nivel que se toca enseguida (via strategies.exits_with_levels)."""
    from heatsignal.lab import strategies as S

    out = {}
    for s, md in data.items():
        k = md.klines
        last = k.index[-1] + pd.Timedelta(minutes=1)
        ev = {pd.Timestamp(t, unit="ms", tz="UTC"): w for sym, t, w in p["events"] if sym == s}
        ev = {t: w for t, w in ev.items() if t <= last}
        e = pd.Series(list(ev.values()), index=pd.DatetimeIndex(list(ev.keys())), dtype=float).sort_index()
        if s == "BTCUSDT":
            sl = pd.Series(1e12, index=e.index)  # long con stop arriba del precio: toca en la primera vela
            out[s] = S.exits_with_levels(e, k, pd.Timedelta(hours=6), sl=sl)
        else:
            out[s] = S.hold_events(e, pd.Timedelta(minutes=30), k.index)
    return out


@pytest.fixture
def bot(tmp_path):
    syms = ["BTCUSDT", "ETHUSDT"]
    fill_history(tmp_path / "hist.sqlite", syms, T0 - 3 * DAY, T0 + 12 * H).close()
    fill_funding(tmp_path / "paperbot.sqlite", syms, T0 - 3 * DAY, T0 + 12 * H)
    spec = Spec("toy-stop-v1", "test", EVIL, "1h", "both", "h",
                {"events": [["BTCUSDT", T0 + H + 10 * MIN, 1.0], ["ETHUSDT", T0 + H + 5 * MIN, -1.0]]})
    cfg = EngineConfig(paperbot_db=tmp_path / "paperbot.sqlite", history_db=tmp_path / "hist.sqlite", window_days=2,
                       funding_days=3, download=False, catalog={"toy-stop-v1": (spec, stop_rule)},
                       entries={"toy-stop-v1": {"spec_hash": spec.spec_hash(), "universe": syms, "params": {}}})
    for h in (0, 1, 2, 3):
        run_once(cfg, now_ms=T0 + h * H + 2 * MIN)
    return tmp_path


def test_paperbot_records_the_exit_reason(bot) -> None:
    con = sqlite3.connect(bot / "paperbot.sqlite")
    con.row_factory = sqlite3.Row
    ex = {r["symbol"]: dict(r) for r in con.execute("SELECT * FROM exit_reasons")}
    assert ex["BTCUSDT"]["motivo"] == "stop" and ex["BTCUSDT"]["decision_ms"] == T0 + H + 11 * MIN
    assert ex["ETHUSDT"]["motivo"] == "tiempo" and ex["ETHUSDT"]["decision_ms"] == T0 + H + 35 * MIN
    with pytest.raises(sqlite3.DatabaseError):
        con.execute("UPDATE exit_reasons SET motivo = 'objetivo'")
    with pytest.raises(sqlite3.DatabaseError):
        con.execute("DELETE FROM exit_reasons")
    con.close()
    d = pbv.strategy_detail(bot / "paperbot.sqlite", "toy-stop-v1")
    assert {t["symbol"]: t["motivo"] for t in d["trades"]} == {"BTCUSDT": "stop", "ETHUSDT": "tiempo"}
    assert d["exit_counts"] == {"stop": 1, "tiempo": 1}
    assert d["exits"] is None  # el juguete no esta en la ficha


def test_paperbot_view_works_on_a_db_without_exit_reasons(tmp_path, bot) -> None:
    # base vieja (antes de la tabla): los trades salen como "sin registro", sin romper
    old = tmp_path / "old.sqlite"
    src = sqlite3.connect(bot / "paperbot.sqlite")
    dst = sqlite3.connect(old)
    src.backup(dst)
    src.close()
    dst.execute("DROP TRIGGER exit_reasons_no_delete")
    dst.execute("DROP TRIGGER exit_reasons_no_update")
    dst.execute("DROP TABLE exit_reasons")
    dst.commit()
    dst.close()
    d = pbv.strategy_detail(old, "toy-stop-v1")
    assert d["exit_counts"] == {"sin_dato": 2} and all(t["motivo"] is None for t in d["trades"])


async def test_paperbot_endpoint_carries_ficha_and_counts(bot) -> None:
    ctx = PanelContext(health=HealthRegistry(), paperbot_db=bot / "paperbot.sqlite")
    async with _client(ctx) as c:
        ov = (await c.get("/api/paperbot")).json()
        s = ov["strategies"][0]
        assert s["exit_counts"] == {"stop": 1, "tiempo": 1} and "exits" in s
        det = (await c.get("/api/paperbot/toy-stop-v1")).json()
        assert {t["motivo"] for t in det["trades"]} == {"stop", "tiempo"}


def test_weekly_counts_exit_reasons_per_source(bot) -> None:
    from heatsignal.paper import weekly as wk

    out = wk.paperbot_week(bot / "paperbot.sqlite", T0, T0 + 7 * DAY)
    assert out["strategies"][0]["exits_week"] == {"stop": 1, "tiempo": 1}


# ---------------------------------------------------------------------------- Freqtrade


def test_freqtrade_state_has_stoploss_roi_and_exit_reasons(tmp_path) -> None:
    t0 = 1_792_000_000_000
    db = make_db(tmp_path / "ft.sqlite", [
        {"id": 1, "pair": "BTC/USDT:USDT", "fills": [(t0, 0.01, 100_000.0, "e"), (t0 + 3_600_000, -0.01, 101_000.0, "x")]},
        {"id": 2, "pair": "SOL/USDT:USDT", "fills": [(t0, 1.0, 150.0, "e"), (t0 + 3_600_000, -1.0, 140.0, "x")],
         "exit_reason": "stop_loss"}])
    st = ftdb.paper_state(ftdb.dump(db), api={"ok": False}, now_ms=t0 + 5 * 3_600_000)
    assert st["exit_reasons"] == {"exit_signal": 1, "stop_loss": 1}
    assert st["exits"]["stoploss"] == -0.99 and st["exits"]["source"] == "estrategia" and st["exits"]["roi_text"] == "ROI desactivado"
    st2 = ftdb.paper_state(ftdb.dump(db), api={"ok": True, "show_config": {"strategy": "DonchianEnsembleV1", "stoploss": -0.99,
                                                                           "minimal_roi": {"0": 100}}}, now_ms=t0 + 5 * 3_600_000)
    assert st2["exits"]["source"] == "api"
    from heatsignal.paper import weekly as wk

    w = wk.freqtrade_week(db, t0 - 3_600_000, t0 + 7 * 86_400_000)
    assert w["exits_week"] == {"exit_signal": 1, "stop_loss": 1}


# ---------------------------------------------------------------------------- render en node (con escape)


def _node(view: Path, data: dict, tmp_path: Path) -> dict:
    f = tmp_path / "data.json"
    f.write_text(json.dumps(data))
    out = subprocess.run(["node", str(HARNESS), str(view), str(f)], capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


@pytest.mark.skipif(shutil.which("node") is None, reason="sin node")
def test_exits_ui_renders_and_escapes(tmp_path) -> None:
    from heatsignal.lab.exits import freqtrade_exits, ficha_json

    ficha = {**ficha_json("ema-cruce-v1"), "nota": EVIL}
    data = {"overview": {
        "ficha": ficha,
        "salidas": {"por_motivo": {"stop": 30, "objetivo": 10, "tiempo": 1, "senal": 0, "rebalanceo": 0}, "trades": 41,
                    "window": ["2021-01-01", "2026-07-15"], "holdout": "NO USADO", "verificacion": {"coincide": True, "llamadas": 7},
                    "componentes": {EVIL: {"por_motivo": {"stop": 1}}}},
        "ft": {**freqtrade_exits("HypothesisBridgeV1"), "nota": EVIL}, "ft_reasons": {"hyp_due": 4, EVIL: 1},
        "counts": {"stop": 2, "tiempo": 1},
        "summary": {"nota": "descriptivo", "strategies": [{"strategy_id": "x", "name": EVIL, "n": 3, "mae_atr_median": 0.8,
                                                           "mfe_atr_median": 1.2, "stops": [{"atr": 1.0, "n": 3, "hit": 2, "hit_won": 1}]}]}},
        "detail": {"status": "resolved", "exit_rule": {"texto": "por tiempo: 4 velas de 1h", "nota": EVIL},
                   "excursion": {"mae_pct": 1.234, "mfe_pct": 2.5, "mae_atr": 0.61, "mfe_atr": 1.25, "atr": 2.0, "atr_source": EVIL,
                                 "n_candles": 240, "source": "klines_1m", "stops": [{"atr": 1.5, "hit": True}], "note": EVIL,
                                 "descriptivo": "NO pre-registrado"}}}
    r = _node(EXITS_VIEW, data, tmp_path)
    ov, det = r["overview"], r["detail"]
    assert "stop 1,5 ATR" in ov and "objetivo 3 ATR" in ov and "máx. 30 d" in ov and "Cómo sale" in ov
    assert "stop −20 %" in ov and "ROI desactivado" in ov and "tiempo (vencimiento)" in ov
    assert "reconstrucción verificada" in ov and "73 %" in ov  # 30 de 41 por stop
    assert "por tiempo: 4 velas de 1h" in det and "NO pre-registrado" in det and "1,5 ATR" in det
    assert EVIL in ov and EVIL in det  # como texto, no como nodo
    assert "img" not in r["tags"]


@pytest.mark.skipif(shutil.which("node") is None, reason="sin node")
def test_laboratorio_and_paperbot_views_render_exits(tmp_path, bot) -> None:
    from heatsignal.lab.exits import ficha_json

    lab = {"overview": {"strategies": [{"strategy_id": "ema-cruce-v1", "exits": ficha_json("ema-cruce-v1"), "has_detail": True,
                                        "verdict": {"pasa": False, "checks": {}}}]},
           "detail": {"strategy_id": "ema-cruce-v1", "spec_hash": "x", "run": "r", "exits": ficha_json("cartera-tendencia-v1"),
                      "diagnostics": {"salidas": {"por_motivo": {"stop": 3, "objetivo": 1, "tiempo": 0, "senal": 2, "rebalanceo": 0},
                                                  "trades": 6, "componentes": {"ema-cruce-v1": {"por_motivo": {"stop": 3}}}}}}}
    r = _node(APP / "views/laboratorio.js", lab, tmp_path)
    assert "stop 1,5 ATR · " not in r["overview"]  # chips separados, no el resumen pegado
    assert "Salidas" in r["overview"] and "objetivo 3 ATR" in r["overview"]
    assert "Cómo sale" in r["detail"] and "hereda de 3 componentes" in r["detail"] and "donchian-ensemble-v1: sin stop: canal" in r["detail"]
    ov = pbv.overview(bot / "paperbot.sqlite")
    det = pbv.strategy_detail(bot / "paperbot.sqlite", "toy-stop-v1")
    r = _node(APP / "views/paperbot.js", {"overview": ov, "detail": {**det, "lab": None}}, tmp_path)
    assert "stop 1" in r["overview"] and "Cierres por motivo" in r["detail"] and "Motivo" in r["detail"]
    assert "img" not in r["tags"]
