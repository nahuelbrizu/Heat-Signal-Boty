"""Prueba hacia adelante en el bot de paper (2026-10-20): historia minima por spec, velas diarias (1m agregadas en SQL
+ REST /fapi/v1/klines interval=1d con un servidor falso), entrar "desde hoy" sin tocar a las existentes, "sin datos"
explicito, balance de fuerzas, roles y criterio pre-registrado en `view` y en el endpoint, render. Sin red."""

from __future__ import annotations

import json
import shutil
import sqlite3
import subprocess
import sys
import urllib.parse
from pathlib import Path

import httpx
import numpy as np
import pandas as pd
import pytest

from heatsignal.api.panel import PanelContext, create_panel_app
from heatsignal.history import klines as hk
from heatsignal.history.store import HistoryStore
from heatsignal.ingestion.health import HealthRegistry
from heatsignal.lab.strategies import CATALOG, LOTE3, MarketData
from heatsignal.paperbot import daily as DL
from heatsignal.paperbot import params as P
from heatsignal.paperbot import view as V
from heatsignal.paperbot.engine import EngineConfig, run_once, target_at
from heatsignal.paperbot.store import PaperbotStore
from paperbot_fakes import DAY, H, MIN, T0, fill_funding, walk

sys.path.insert(0, str(Path(__file__).parent))
from test_paperbot import toy  # noqa: E402

SYMS = ["BTCUSDT", "ETHUSDT"]
HIST_DAYS = 130
FAM = "l6-familia-ruptura-long-v1"
NETA = "l6-familia-ruptura-neta-v1"
TS = "l6-tsmom-l28-vol-long-v1"
VIEW = Path(__file__).parents[1] / "src/heatsignal/api/static/app/views/paperbot.js"
HARNESS = Path(__file__).parent / "js" / "render_view.mjs"


# ---- historia minima por spec ---------------------------------------------------------------------------------


def test_history_days_per_spec():
    hd = {sid: DL.history_days(*CATALOG[sid]) for sid in P.load()}
    assert all(hd[s] is None for s in (*LOTE3, "max-ruptura-10d-v1"))  # siguen con la ventana 1m de 64 dias
    assert hd["l6-ruptura-n55-h5-long-v1"] == 55 + DL.CHAIN_MARGIN_D
    assert hd["l6-ruptura-n10-h1-long-v1"] == 10 + DL.CHAIN_MARGIN_D
    assert hd["l6-tsmom-l7-fijo-long-v1"] == 7 + 1 + 5
    assert hd["l6-tsmom-l28-vol-long-v1"] == 31 + 5  # vol de 30 d = 31 cierres > 29 del momentum
    assert hd["l6-reg-adx25-long-v1"] == 28 + DL.EWM_WARMUP_D + DL.CHAIN_MARGIN_D
    assert hd["l6-reg-atrp50-ambos-v1"] == 365 + 1 + 14 + DL.EWM_WARMUP_D + DL.CHAIN_MARGIN_D
    assert hd[FAM] == hd["l6-familia-ruptura-short-v1"] == hd[NETA] == 55 + DL.CHAIN_MARGIN_D
    assert max(v for v in hd.values() if v) == hd["l6-reg-atrp50-ambos-v1"] < 1000  # una pagina de REST por simbolo


# ---- datos: 1m recientes en la base, lo viejo solo por REST diario ----------------------------------------------


def _agg_daily(arr: np.ndarray) -> list[list]:
    """Velas diarias de Binance (formato /fapi/v1/klines) agregadas de las 1m del mismo random walk."""
    t = arr[:, 0].astype(np.int64)
    out = []
    for d in np.unique(t // DAY):
        sel = arr[t // DAY == d]
        if len(sel) != 1440:
            continue
        o = int(d) * DAY
        out.append([o, str(sel[0, 1]), str(sel[:, 2].max()), str(sel[:, 3].min()), str(sel[-1, 4]), "1.0", o + DAY - 1,
                    "1.0", 1, "0.5", "0.5", "0"])
    return out


def fake_daily_server(arrays: dict[str, np.ndarray], calls: list, *, status: int = 200):
    daily = {s: _agg_daily(a) for s, a in arrays.items()}

    def get(url: str, timeout: float):
        calls.append(url)
        q = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))
        if "/fapi/v1/klines" in url and q.get("interval") == "1d":
            if status != 200:
                return status, b'{"code":-1003,"msg":"falso"}'
            start, limit = int(q["startTime"]), int(q["limit"])
            return 200, json.dumps([k for k in daily[q["symbol"]] if k[0] >= start][:limit]).encode()
        if "/fapi/v1/fundingRate" in url or "/fapi/v1/klines" in url:
            return 200, b"[]"  # 1m y funding ya estan en las bases
        return 404, b"{}"

    return get


@pytest.fixture
def fwd(tmp_path):
    end = T0 + 6 * H
    arrays = {s: walk(s, T0 - HIST_DAYS * DAY, end, vol=0.002) for s in SYMS}
    hist = HistoryStore(tmp_path / "hist.sqlite")
    for s, a in arrays.items():
        a = a[a[:, 0] >= T0 - 5 * DAY]  # la base historica tiene solo 5 dias de 1m (lo demas se archivo)
        hist.add_klines([(hk.EXCHANGE, hk.internal_symbol(s), "1m", int(r[0]), *map(float, r[1:5]), 1.0, float(r[4]), 1,
                          float(r[4]) / 2) for r in a])
    hist.close()
    fill_funding(tmp_path / "pb.sqlite", SYMS, T0 - 3 * DAY, end)
    return tmp_path, arrays


def _entries(*sids):
    return {sid: {"spec_hash": CATALOG[sid][0].spec_hash(), "universe": SYMS, "params": {}, "desde": "2026-10-20",
                  "rol": "principal" if "familia" in sid else "seguimiento"} for sid in sids}


def _cfg(d, entries, catalog=None, **kw):
    return EngineConfig(paperbot_db=d / "pb.sqlite", history_db=d / "hist.sqlite", window_days=2, funding_days=3,
                        entries=entries, catalog=catalog, **kw)


def _rows(d, sql, args=()):
    c = sqlite3.connect(d / "pb.sqlite")
    c.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in c.execute(sql, args)]
    finally:
        c.close()


def test_rest_daily_cache_only_closed_and_incremental(tmp_path):
    arr = walk("BTCUSDT", T0 - 10 * DAY, T0 + 6 * H)
    calls: list = []
    get = fake_daily_server({"BTCUSDT": arr}, calls)
    st = PaperbotStore(tmp_path / "pb.sqlite")
    now = T0 + 5 * H
    n = DL.update_daily_cache(st, "BTCUSDT", T0 - 10 * DAY, T0, now, http_get=get, sleep=lambda s: None)
    assert n == 10 and len(calls) == 1
    q = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(calls[0]).query))
    assert calls[0].startswith("https://fapi.binance.com/fapi/v1/klines?") and q["interval"] == "1d" and q["limit"] == "1000"
    rows = st.daily_klines("BTCUSDT", 0, T0 + DAY)
    assert [r[0] for r in rows] == [T0 - k * DAY for k in range(10, 0, -1)]  # la vela de hoy (abierta) no se guarda
    day0 = arr[(arr[:, 0] >= T0 - 10 * DAY) & (arr[:, 0] < T0 - 9 * DAY)]
    assert rows[0][1:] == pytest.approx((day0[0, 1], day0[:, 2].max(), day0[:, 3].min(), day0[-1, 4]))
    assert DL.update_daily_cache(st, "BTCUSDT", T0 - 10 * DAY, T0, now, http_get=get) == 0 and len(calls) == 1
    with pytest.raises(DL.DailyFetchError, match="HTTP 418"):
        DL.update_daily_cache(st, "BTCUSDT", T0 - 20 * DAY, T0, now, http_get=fake_daily_server({"BTCUSDT": arr}, [], status=418))
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        st.conn.execute("DELETE FROM daily_klines")
    st.close()


def test_sql_daily_aggregates_only_complete_days(fwd):
    d, arrays = fwd
    hist = HistoryStore(d / "hist.sqlite")
    got = DL.sql_daily(hist, "BTCUSDT", T0 - 10 * DAY, T0 + DAY)
    hist.close()
    assert sorted(got) == [T0 - k * DAY for k in range(5, 0, -1)]  # el dia en curso (6 h) no esta completo
    a = arrays["BTCUSDT"]
    day = a[(a[:, 0] >= T0 - DAY) & (a[:, 0] < T0)]
    assert got[T0 - DAY] == pytest.approx((day[0, 1], day[:, 2].max(), day[:, 3].min(), day[-1, 4]))


def test_daily_strategies_match_lab_rules_and_enter_from_today(fwd):
    """Las diarias entran "desde hoy" al lado de una existente sin tocarla, con historia de REST + 1m agregadas, y su
    objetivo en H es el MISMO que el de la regla del laboratorio sobre 130 dias de 1m."""
    d, arrays = fwd
    cat_old, ent_old = toy([("BTCUSDT", T0 + H, 1.0)])
    assert run_once(_cfg(d, ent_old, cat_old, download=False), now_ms=T0 + 3 * H + 2 * MIN)["status"] == "ok"
    assert run_once(_cfg(d, ent_old, cat_old, download=False), now_ms=T0 + 4 * H + 2 * MIN)["status"] == "ok"
    before = _rows(d, "SELECT * FROM equity WHERE strategy_id='toy-v1' ORDER BY hour_ms")

    ent = {**ent_old, **_entries(FAM, NETA, TS)}
    cat = {**cat_old, **{s: CATALOG[s] for s in (FAM, NETA, TS)}}
    calls: list = []
    now = T0 + 5 * H + 2 * MIN
    r = run_once(_cfg(d, ent, cat, download=True), http_get=fake_daily_server(arrays, calls), now_ms=now)
    assert r["status"] == "ok", r["reason"]
    est = r["detail"]["estrategias"]
    assert est["toy-v1"]["modo"] == "consecutiva" and {est[s]["modo"] for s in (FAM, NETA, TS)} == {"inicio"}
    assert est[FAM]["rol"] == "principal" and est[TS]["historia_dias"] == 36
    vd = r["detail"]["velas_diarias"]
    assert vd["dias"] == 115 and vd["por_simbolo"]["BTCUSDT"]["dias_1m"] == 5
    assert vd["por_simbolo"]["BTCUSDT"]["dias_rest"] == 110 and vd["por_simbolo"]["BTCUSDT"]["dias_contiguos"] == 115
    assert sum("interval=1d" in u for u in calls) == 2  # un pedido por simbolo
    # la existente no cambio
    assert _rows(d, "SELECT * FROM equity WHERE strategy_id='toy-v1' ORDER BY hour_ms")[:2] == before

    Hh = now - now % H
    nonzero = 0
    for sid in (FAM, NETA, TS):
        spec, rule = CATALOG[sid]
        for s in SYMS:
            a = arrays[s]
            a = a[a[:, 0] < Hh]
            k = pd.DataFrame(a[:, 1:5], columns=["open", "high", "low", "close"],
                             index=pd.DatetimeIndex(pd.to_datetime(a[:, 0].astype(np.int64), unit="ms", utc=True)))
            want = target_at(rule({s: MarketData(k)}, dict(spec.params)).get(s), Hh)
            assert est[sid]["targets"][s] == pytest.approx(want, abs=1e-12), (sid, s)
            nonzero += abs(want) > 0
        orders = _rows(d, "SELECT * FROM orders WHERE strategy_id=?", (sid,))
        assert all(o["reason"] == "inicio" and o["decision_ms"] == Hh for o in orders)
    assert nonzero >= 1
    # balance de fuerzas: votos long/short por simbolo y la neta resultante = objetivo de la regla
    for sid in (FAM, NETA):
        for s, v in est[sid]["votos"].items():
            assert v["componentes"] == (12 if sid == FAM else 24)
            assert v["neta"] == pytest.approx(est[sid]["targets"][s]) == pytest.approx((v["long"] - v["short"]) / 12)
    assert est[FAM]["votos"]["BTCUSDT"]["short"] == 0

    # la hora siguiente: consecutivas y sin volver a pedir velas diarias (cache + 1m)
    calls.clear()
    r2 = run_once(_cfg(d, ent, cat, download=True), http_get=fake_daily_server(arrays, calls), now_ms=now + H)
    assert r2["status"] == "ok" and {r2["detail"]["estrategias"][s]["modo"] for s in ent} == {"consecutiva"}
    assert not any("interval=1d" in u for u in calls)


def test_missing_daily_history_is_sin_datos_not_silent(fwd):
    d, arrays = fwd
    cat_old, ent_old = toy([("BTCUSDT", T0 + H, 1.0)])
    ent = {**ent_old, **_entries(FAM, TS)}
    cat = {**cat_old, FAM: CATALOG[FAM], TS: CATALOG[TS]}
    r = run_once(_cfg(d, ent, cat, download=True), http_get=fake_daily_server(arrays, [], status=500),
                 now_ms=T0 + 5 * H + 2 * MIN)
    assert r["status"] == "ok" and "SIN DATOS" in r["reason"] and FAM in r["reason"]
    est = r["detail"]["estrategias"]
    assert est["toy-v1"]["estado"] == "ok" and est[FAM]["estado"] == est[TS]["estado"] == "sin_datos"
    assert "115 dias contiguos" in est[FAM]["motivo"] and "HTTP 500" in est[FAM]["motivo"]
    assert _rows(d, "SELECT * FROM equity WHERE strategy_id=?", (FAM,)) == []
    assert set(r["detail"]["sin_datos"]) == {FAM, TS}
    ov = V.overview(d / "pb.sqlite", now_ms=T0 + 5 * H + 3 * MIN)
    sd = {x["strategy_id"]: x for x in ov["sin_datos"]}
    assert sd[FAM]["rol"] == "principal" and sd[FAM]["nunca_corrio"] is True
    assert any("Sin datos" in n and FAM in n for n in ov["notes"])


# ---- estadistica del criterio ------------------------------------------------------------------------------------


def test_sharpe_bootstrap_and_months():
    rng = np.random.default_rng(1)
    good = list(rng.normal(0.01, 0.01, 180))
    noise = list(rng.normal(0.0, 0.01, 180))
    assert V.block_bootstrap_p(good) < 0.01
    assert V.block_bootstrap_p(noise) > 0.05 or abs(np.mean(noise)) > 0.0015
    assert V.block_bootstrap_p(good) == V.block_bootstrap_p(good)  # determinista
    assert V.block_bootstrap_p([0.1] * 5) is None
    assert V.sharpe([0.01, 0.01]) is None and V.sharpe([0.01, -0.01, 0.02]) > 0
    jan31 = int(pd.Timestamp("2026-01-31", tz="UTC").value // 1_000_000)
    assert V._iso(V._add_months(jan31, 1)).startswith("2026-02-28")
    oct20 = int(pd.Timestamp("2026-10-20", tz="UTC").value // 1_000_000)
    assert V._iso(V._add_months(oct20, 3)).startswith("2027-01-20") and V._iso(V._add_months(oct20, 6)).startswith("2027-04-20")


def _synthetic_db(path: Path, days: int) -> None:
    """Base del bot con equity horaria sintetica (cada 6 h) para 5 estrategias de los 3 roles."""
    st = PaperbotStore(path)
    rng = np.random.default_rng(5)
    start = T0
    plan = {FAM: (0.004, 0.001), "l6-familia-ruptura-short-v1": (-0.001, 0.001), NETA: (0.002, 0.001),
            TS: (0.001, 0.0), "tsmom-diario-v1": (0.0, 0.0)}
    eq = {s: 10_000.0 for s in plan}
    beq = 10_000.0
    for k in range(days * 4 + 1):
        hour = start + k * 6 * H
        b = float(rng.normal(0.0002, 0.003))
        beq *= 1 + b
        rows = []
        for s, (mu, beta) in plan.items():
            eq[s] *= 1 + mu / 4 + beta * 0 + float(rng.normal(0, 0.002))
            rows.append({"run_id": f"r{k}", "hour_ms": hour, "strategy_id": s, "equity": eq[s], "basket_equity": beq,
                         "realized": 0.0, "unrealized": eq[s] - 10_000, "funding": 0.0, "fees": 0.0, "slippage": 0.0,
                         "exposure": 1.0})
        detail = {"estrategias": {s: {"rol": ("principal" if "familia" in s else "seguimiento" if s.startswith("l6-")
                                               else "paper_previo"), "estado": "ok", "spec_hash": "x",
                                      "votos": {"BTCUSDT": {"long": 5, "short": 2, "componentes": 24, "neta": 0.25}}
                                      if s == NETA else None} for s in plan}}
        st.record_run({"run_id": f"r{k}", "hour_ms": hour, "started_ms": hour, "finished_ms": hour, "status": "ok",
                       "reason": "ok", "detail": detail, "peak_rss_kb": 1}, equity=rows)
    st.close()


def test_view_roles_sharpe_and_criterion(tmp_path):
    _synthetic_db(tmp_path / "pb.sqlite", 100)
    ov = V.overview(tmp_path / "pb.sqlite", now_ms=T0 + 100 * DAY)
    by = {s["strategy_id"]: s for s in ov["strategies"]}
    assert [r["rol"] for r in ov["roles"]] == ["principal", "seguimiento", "paper_previo"]
    assert by[FAM]["rol"] == "principal" and by[FAM]["rol_label"] == "Hipótesis principal (decide)"
    assert by[TS]["rol"] == "seguimiento" and by[TS]["sharpe"]["marca"] == "exploratorio; corrección BH al evaluar"
    assert by["tsmom-diario-v1"]["rol"] == "paper_previo" and by["tsmom-diario-v1"]["criterio"] is None
    c = by[FAM]["criterio"]
    assert c["dias"] == 100 and c["hito_3m_at"].startswith(V._iso(V._add_months(T0, 3))[:10])
    v3 = c["veredicto_3m"]
    assert v3 is not None and c["veredicto_6m"] is None and v3["pasa"] is True and v3["sharpe"] > v3["sharpe_referencia"]
    assert c["estado"] == "3m: pasa" and "p ≤ 0,05" in c["texto"]
    cs = by["l6-familia-ruptura-short-v1"]["criterio"]
    assert cs["lado"] == "short" and "−Sharpe de la canasta" in cs["texto"] and cs["veredicto_3m"]["pasa"] is False
    # la referencia de la short es la canasta en corto
    assert cs["veredicto_3m"]["sharpe_referencia"] == pytest.approx(-v3["sharpe_referencia"])
    assert by[NETA]["votos"]["BTCUSDT"]["long"] == 5 and "beta" in ov["lectura_conjunta"]
    # criterios de parada pre-registrados: solo las principales, con semaforo y umbrales del JSON congelado
    pa = by[FAM]["parada"]
    assert pa["estado"] in ("ok", "alerta", "parar") and pa["dias"] == 100 and pa["hash"]
    assert 0 < pa["drawdown"]["p95"] < pa["drawdown"]["p99"]
    assert by[TS]["parada"] is None and by[NETA]["parada"] is not None
    # antes de 3 meses: en curso, sin veredicto
    _synthetic_db(tmp_path / "pb2.sqlite", 40)
    c2 = {s["strategy_id"]: s for s in V.overview(tmp_path / "pb2.sqlite")["strategies"]}[FAM]["criterio"]
    assert c2["veredicto_3m"] is None and c2["estado"] == "en curso" and c2["en_curso"]["dias"] == 40
    # a los 6 meses: con p del bootstrap pareado
    _synthetic_db(tmp_path / "pb3.sqlite", 190)
    c3 = {s["strategy_id"]: s for s in V.overview(tmp_path / "pb3.sqlite")["strategies"]}[FAM]["criterio"]
    assert c3["veredicto_6m"]["p"] is not None and c3["veredicto_6m"]["p"] <= 0.05 and c3["estado"] == "6m: pasa"


def _client(ctx: PanelContext) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=create_panel_app(ctx)), base_url="http://panel")


async def test_endpoint_roles_and_render(tmp_path):
    _synthetic_db(tmp_path / "pb.sqlite", 100)
    ctx = PanelContext(health=HealthRegistry(), paperbot_db=tmp_path / "pb.sqlite")
    async with _client(ctx) as c:
        ov = (await c.get("/api/paperbot")).json()
        det = (await c.get(f"/api/paperbot/{FAM}")).json()
    by = {s["strategy_id"]: s for s in ov["strategies"]}
    assert by[FAM]["rol"] == "principal" and by[FAM]["criterio"]["veredicto_3m"]["pasa"] is True
    assert by[TS]["rol"] == "seguimiento" and by["tsmom-diario-v1"]["rol"] == "paper_previo"
    assert det["rol"] == "principal" and det["criterio"]["dias"] == 100
    if shutil.which("node") is None:
        pytest.skip("sin node")
    f = tmp_path / "data.json"
    f.write_text(json.dumps({"overview": ov, "detail": det}))
    out = subprocess.run(["node", str(HARNESS), str(VIEW), str(f)], capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    res = json.loads(out.stdout)
    o = res["overview"]
    assert o.index("Hipótesis principal (decide)") < o.index("Seguimiento (exploratorio, no decide)") < o.index("Paper previo")
    assert "exploratorio; corrección BH al evaluar" in o and "Lectura conjunta" in o
    assert "Balance de fuerzas" in o and "5 long · 2 short" in o
    assert "3m: pasa" in o and "días desde el inicio" in o
    assert "Criterio pre-registrado" in res["detail"]


def test_weekly_review_has_roles(tmp_path):
    from heatsignal.paper import weekly as W

    _synthetic_db(tmp_path / "pb.sqlite", 100)
    we = T0 + 98 * DAY
    pb = W.paperbot_week(tmp_path / "pb.sqlite", we - 7 * DAY, we)
    by = {s["strategy_id"]: s for s in pb["strategies"]}
    assert by[FAM]["rol"] == "principal" and by[FAM]["criterio"]["veredicto_3m"]["pasa"] is True
    assert by[TS]["rol"] == "seguimiento" and by[TS]["sharpe_cum"]["marca"] == V.SEGUIMIENTO_MARCA
    assert by["tsmom-diario-v1"]["rol"] == "paper_previo" and by["tsmom-diario-v1"]["criterio"] is None
    text = W.summary_text({"week": "2027-W04", "week_start_at": V._iso(we - 7 * DAY), "week_end_ms": we,
                           "paperbot": {**pb, "available": True}})
    i, j, k = (text.index(W._ROLE_TITLE[r]) for r in ("principal", "seguimiento", "paper_previo"))
    assert i < j < k and "criterio: 3m: pasa" in text and "corrección BH al evaluar" in text
    assert "Lectura conjunta" in text
    assert by[FAM]["parada"]["estado"] in ("ok", "alerta", "parar") and "parada pre-registrada:" in text
