"""Tests del panel interno: estado, features en vivo y pagina, sin red ni LLM."""

from __future__ import annotations

import re
from pathlib import Path

import httpx
import pytest

from heatsignal.api.panel import PanelContext, create_panel_app
from heatsignal.evaluation.pending_store import PendingHypothesisStore
from heatsignal.evaluation.strategy_registry import StrategyHypothesis, StrategyRegistry
from heatsignal.ingestion.health import CountingSink, HealthRegistry
from heatsignal.pipeline.analysis import AnalysisLoop
from heatsignal.schema.events import Exchange, Side
from heatsignal.storage.memory import MemoryWindowSink

from features_helpers import SYM, liq, ticker, trade


def _client(ctx: PanelContext) -> httpx.AsyncClient:
    # Mismo event loop que el test (como en produccion): el registry SQLite no es thread-safe.
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=create_panel_app(ctx)), base_url="http://panel")


async def _ctx(tmp_path, *, with_analysis: bool = True) -> PanelContext:
    import time

    now = int(time.time() * 1000)
    memory = MemoryWindowSink()
    health = HealthRegistry()
    sink = CountingSink(memory, health, "binance")
    await sink.write(trade(150.0, 2.0, side=Side.SELL, ts=now - 5_000))
    await sink.write(ticker(Exchange.BINANCE, now - 4_000, mark=150.1))
    await sink.write(ticker(Exchange.OKX, now - 3_000, mark=150.2))
    await sink.write(liq(149.0, 25_000.0, ts=now - 2_000))
    if not with_analysis:
        return PanelContext(health=health, memory=memory, config={"symbols": ["SOLUSDT"]})
    registry = StrategyRegistry(tmp_path / "eval.sqlite")
    registry.register(StrategyHypothesis(
        description="rebote", entry_conditions={}, exit_conditions={"horizon_s": 3600}, author="tests",
    ))
    store = PendingHypothesisStore(tmp_path / "eval.sqlite", registry)
    loop = AnalysisLoop(memory, [SYM], registry)
    return PanelContext(health=health, memory=memory, analysis=loop, registry=registry, store=store,
                        config={"symbols": ["SOLUSDT"]})


async def test_status_reports_workers_memory_and_strategies(tmp_path) -> None:
    client = _client(await _ctx(tmp_path))
    body = (await client.get("/api/status")).json()
    assert body["workers"][0]["worker"] == "binance"
    sym = body["memory"]["symbols"][SYM]
    assert sym["counts"] == {"liquidation": 1, "trade": 1, "ticker": 2}
    assert sym["mark_by_source"] == {"okx": 150.2, "binance": 150.1}
    assert sym["last_trade"]["side"] == "sell"
    assert sym["last_liquidation"]["notional_usd"] == 25_000.0
    assert body["analysis"] == {"enabled": True, "graph_enabled": False, "llm_providers": [], "llm_cooldowns": {}, "last_cycle": None, "last_ran": None,
                                "history": [], "history_counts": {}, "per_symbol": {}}
    ev = body["evaluation"]
    assert ev["available"] and len(ev["strategies"]) == 1 and ev["strategies"][0]["horizon_s"] == 3600
    assert ev["hypotheses"] == [] and ev["pending_count"] == 0
    assert ev["strategies"][0]["hypotheses"] == {"total": 0, "pending": 0, "wins": 0}


async def test_live_features(tmp_path) -> None:
    client = _client(await _ctx(tmp_path))
    body = (await client.get("/api/features")).json()
    fs = body["features"][SYM]
    assert fs["context"]["mark_price"] is not None
    assert fs["order_flow"]["n_trades"] == 1
    assert any("calentamiento" in g for g in fs["data_gaps"])


async def test_without_analysis_features_are_503_and_eval_unavailable(tmp_path) -> None:
    client = _client(await _ctx(tmp_path, with_analysis=False))
    assert (await client.get("/api/features")).status_code == 503
    assert (await client.get("/api/status")).json()["evaluation"]["available"] is False


async def test_index_serves_html(tmp_path) -> None:
    client = _client(await _ctx(tmp_path))
    r = (await client.get("/"))
    assert r.status_code == 200 and "HeatSignal" in r.text
    assert "http" not in r.text.split("<script>")[0].lower().replace("http-equiv", "")  # sin recursos externos


async def test_klines_passthrough_validates_and_reports_upstream_errors(tmp_path) -> None:
    calls: list[str] = []

    def fake_get(url: str, timeout_s: float) -> tuple[int, bytes]:
        calls.append(url)
        if "limit=2" in url:
            return 200, b'[[1792108800000,"1.3343","1.3528","1.2884","1.3109","474559415.8",0,"0",1,"0","0","0"]]'
        return 503, b"mantenimiento"

    ctx = await _ctx(tmp_path, with_analysis=False)
    ctx.http_get = fake_get
    client = _client(ctx)

    r = await client.get("/api/klines", params={"symbol": "SOL-USDT", "interval": "1d", "limit": 2})
    assert r.status_code == 200
    assert r.json()["candles"] == [{"t": 1792108800, "o": 1.3343, "h": 1.3528, "l": 1.2884, "c": 1.3109, "v": 474559415.8}]
    assert "symbol=SOLUSDT" in calls[0] and "interval=1d" in calls[0]

    assert (await client.get("/api/klines", params={"symbol": "ETHUSDT"})).status_code == 400  # no configurado
    assert (await client.get("/api/klines", params={"symbol": "SOLUSDT", "interval": "3m"})).status_code == 400
    assert (await client.get("/api/klines", params={"symbol": "SOLUSDT", "limit": 1501})).status_code == 422
    assert (await client.get("/api/klines", params={"symbol": "SOLUSDT", "limit": 5})).status_code == 502
    assert len(calls) == 2  # lo invalido no llega a Binance


async def test_charts_library_is_served_locally(tmp_path) -> None:
    client = _client(await _ctx(tmp_path, with_analysis=False))
    r = await client.get("/static/lightweight-charts.js")
    assert r.status_code == 200 and "Lightweight Charts" in r.text[:300]
    assert '<script src="/static/lightweight-charts.js">' in (await client.get("/")).text


async def test_page_is_never_cached_and_status_carries_build_id(tmp_path) -> None:
    ctx = await _ctx(tmp_path, with_analysis=False)
    client = _client(ctx)
    page = await client.get("/")
    assert page.headers["cache-control"] == "no-store"
    main = (await client.get("/static/app/main.js")).text
    assert "location.reload()" in main and "s.build !== state.build" in main  # la SPA se recarga si cambia el build
    build = (await client.get("/api/status")).json()["build"]
    assert len(build) == 12 and (await client.get("/api/status")).json()["build"] == build
    ctx.started_at += 1  # otro proceso (deploy/reinicio) -> otro build: la pagina abierta se recarga
    assert (await _client(ctx).get("/api/status")).json()["build"] != build


async def test_lab_summary_is_served_when_published(tmp_path) -> None:
    import json

    ctx = await _ctx(tmp_path, with_analysis=False)
    client = _client(ctx)
    assert (await client.get("/api/status")).json()["lab"] is None  # sin archivo -> nada
    path = tmp_path / "lab_summary.json"
    path.write_text(json.dumps({"generated_at": "2026-10-17T00:00:00Z", "strategies": [{"strategy_id": "x", "dsr": 0.5}]}))
    ctx.lab_summary_path = path
    lab = (await client.get("/api/status")).json()["lab"]
    assert lab["strategies"][0]["strategy_id"] == "x"
    # la vista del laboratorio de la SPA se sirve y consume /api/lab
    view = await client.get("/static/app/views/laboratorio.js")
    assert view.status_code == 200 and "/api/lab" in view.text


async def test_resolved_hypothesis_shows_real_outcome_and_track_record(tmp_path) -> None:
    from datetime import datetime, timedelta, timezone
    from decimal import Decimal

    from heatsignal.evaluation.pending_store import ResolutionMetrics
    from heatsignal.schema.hypotheses import HypothesisStatus, PendingHypothesis, Resolution
    from heatsignal.schema.signals import Direction

    ctx = await _ctx(tmp_path)
    sid = ctx.registry.get_all()[0].strategy_id
    t0 = datetime(2026, 10, 1, tzinfo=timezone.utc)
    win = PendingHypothesis(strategy_id=sid, symbol=SYM, created_at=t0, direction=Direction.LONG, confidence_score=0.6,
                            entry_reference_price=Decimal("100"), horizon_s=3600, rationale="r")
    loss = PendingHypothesis(strategy_id=sid, symbol=SYM, created_at=t0 + timedelta(hours=2), direction=Direction.SHORT,
                             confidence_score=0.6, entry_reference_price=Decimal("100"), horizon_s=3600, rationale="r")
    for h in (win, loss):
        ctx.store.add(h)
    due = lambda h: h.created_at + timedelta(seconds=h.horizon_s)  # noqa: E731
    ctx.store.resolve(Resolution(hypothesis_id=win.hypothesis_id, status=HypothesisStatus.RESOLVED, resolved_at=due(win),
                                 exit_price=Decimal("102"), pnl_pct=1.9, win=True),
                      ResolutionMetrics(gross_pnl_pct=2.0, cost_bps=10, benchmark_pnl_pct=2.0, alpha_pct=-0.1,
                                        exit_price_ts=due(win)))
    ctx.store.resolve(Resolution(hypothesis_id=loss.hypothesis_id, status=HypothesisStatus.RESOLVED, resolved_at=due(loss),
                                 exit_price=Decimal("103"), pnl_pct=-3.1, win=False),
                      ResolutionMetrics(gross_pnl_pct=-3.0, cost_bps=10, benchmark_pnl_pct=3.0, alpha_pct=-6.1,
                                        exit_price_ts=due(loss)))
    ev = (await _client(ctx).get("/api/status")).json()["evaluation"]
    w = next(h for h in ev["hypotheses"] if h["hypothesis_id"] == str(win.hypothesis_id))
    assert w["win"] is True and w["real_move_pct"] == pytest.approx(2.0) and w["alpha_pct"] == pytest.approx(-0.1)
    assert w["due_at"].startswith("2026-10-01T01:00")
    r = ev["resolved"]
    assert r["n"] == 2 and r["wins"] == 1 and r["rate"] == 0.5 and r["mean_pnl_pct"] == pytest.approx(-0.6)
    assert 0 < r["ci95"][0] < 0.5 < r["ci95"][1] < 1
    assert ev["strategies"][0]["track_record"] == {"resolved": 2, "wins": 1, "rate": 0.5, "ci95": r["ci95"]}


# ---------------------------------------------------------------------------- SPA: archivos estaticos propios

APP_DIR = Path(__file__).resolve().parents[1] / "src" / "heatsignal" / "api" / "static" / "app"


def _app_files() -> list[Path]:
    return sorted(p for p in APP_DIR.rglob("*") if p.is_file())


async def test_spa_shell_loads_local_modules_only(tmp_path) -> None:
    client = _client(await _ctx(tmp_path, with_analysis=False))
    page = (await client.get("/")).text
    assert '<script type="module" src="/static/app/main.js">' in page
    assert '<link rel="stylesheet" href="/static/app/app.css">' in page
    assert re.search(r"(src|href)=\"(https?:)?//", page) is None  # ningun recurso externo (sin CDN)


async def test_spa_static_files_are_served_with_type_and_revalidation(tmp_path) -> None:
    client = _client(await _ctx(tmp_path, with_analysis=False))
    files = _app_files()
    assert {"main.js", "core.js", "app.css"} <= {f.name for f in files}
    for f in files:
        r = await client.get("/static/app/" + f.relative_to(APP_DIR).as_posix())
        assert r.status_code == 200, f
        assert r.headers["cache-control"] == "no-cache"
        expected = {".js": "application/javascript", ".css": "text/css"}[f.suffix]
        assert r.headers["content-type"].startswith(expected)


@pytest.mark.parametrize("path", ["../panel.py", "..%2Fpanel.py", "views/../../panel.html", "main.py", "MAIN.js",
                                  "nope.js", "views/", "app.css.map", "%2e%2e/panel.py"])
async def test_spa_static_rejects_traversal_and_unknown_files(tmp_path, path) -> None:
    client = _client(await _ctx(tmp_path, with_analysis=False))
    assert (await client.get("/static/app/" + path)).status_code == 404


def test_spa_module_imports_resolve() -> None:
    # Cada import relativo apunta a un archivo que existe y exporta los nombres pedidos (sin build: un error
    # aca rompe la vista en el navegador).
    exports: dict[Path, set[str]] = {}
    for f in _app_files():
        if f.suffix == ".js":
            exports[f] = set(re.findall(r"export\s+(?:async\s+)?(?:function|const|class|let)\s+(\w+)", f.read_text()))
    for f in exports:
        src = f.read_text()
        for names, target in re.findall(r'import\s*\{([^}]*)\}\s*from\s*"([^"]+)"', src):
            t = (f.parent / target).resolve()
            assert t in exports, f"{f.name}: importa {target} que no existe"
            for n in (x.strip().split(" as ")[0] for x in names.split(",") if x.strip()):
                assert n in exports[t], f"{f.name}: {target} no exporta {n}"
    views = {p.stem for p in (APP_DIR / "views").glob("*.js")}
    mods = set(re.findall(r'mod: "(\w+)"', (APP_DIR / "main.js").read_text()))
    assert mods and mods <= views  # cada ruta del router tiene su vista


def test_spa_never_injects_html_or_loads_remote_code() -> None:
    # Texto de terceros (noticias, razonamiento del LLM) siempre como nodos de texto; nada desde un CDN.
    for f in _app_files():
        src = f.read_text()
        assert "innerHTML" not in src and "outerHTML" not in src and "insertAdjacentHTML" not in src, f
        assert "document.write" not in src and "eval(" not in src, f
        remote = re.findall(r"https?://[^\s\"'`)]+", src)
        assert all(u.startswith("http://www.w3.org/") for u in remote), (f, remote)


def test_spa_actions_send_the_action_header() -> None:
    core = (APP_DIR / "core.js").read_text()
    assert '"X-HeatSignal-Action": "1"' in core and 'method: "POST"' in core
    actions = (APP_DIR / "actions.js").read_text()
    assert 'apiPost("/api/analyze"' in actions and 'apiPost("/api/lab/requests"' in actions
    assert "showModal" in actions  # pedir al laboratorio exige confirmacion
    polling = re.findall(r"POLL_MS = (\d+)", (APP_DIR / "main.js").read_text())
    assert polling == ["5000"]


async def test_build_id_changes_when_a_spa_file_changes(tmp_path, monkeypatch) -> None:
    from heatsignal.api import panel

    ctx = await _ctx(tmp_path, with_analysis=False)
    build = (await _client(ctx).get("/api/status")).json()["build"]
    fake = tmp_path / "app"
    (fake / "views").mkdir(parents=True)
    for f in _app_files():
        dst = fake / f.relative_to(APP_DIR)
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_bytes(f.read_bytes())
    monkeypatch.setattr(panel, "_APP_DIR", fake)
    assert (await _client(ctx).get("/api/status")).json()["build"] == build  # mismo contenido -> mismo build
    (fake / "main.js").write_text((fake / "main.js").read_text() + "\n// cambio\n")
    assert (await _client(ctx).get("/api/status")).json()["build"] != build  # un .js nuevo recarga las paginas
