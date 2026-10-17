"""Almacenamiento por niveles (history/archive.py, cold.py, remote.py) y su lectura desde el panel.

Sin red ni Drive real: `archive_fakes.FakeRclone` simula gdrive: con un directorio local.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import sqlite3
from datetime import date, datetime, timezone
from pathlib import Path

import httpx
import pytest

from heatsignal.api import panel, panel_api
from heatsignal.api.panel import PanelContext, create_panel_app
from heatsignal.history import archive as arch
from heatsignal.history import export as export_mod
from heatsignal.history.cold import ColdCache, ColdReader
from heatsignal.history.recorder import MinuteRecorder
from heatsignal.history.store import HistoryStore, MinuteBar
from heatsignal.ingestion.health import HealthRegistry

from archive_fakes import fake_remote

DAY = 86_400_000
NOW = int(datetime(2026, 10, 18, 12, tzinfo=timezone.utc).timestamp() * 1000)
TODAY0 = NOW - NOW % DAY
OLD0 = TODAY0 - 70 * DAY   # dia viejo (> 60 dias): se archiva
OLD1 = TODAY0 - 65 * DAY   # otro dia viejo
SYM = "SOL-USDT"
MB = 1024 * 1024


@pytest.fixture(autouse=True)
def _clear_caches():
    for c in (panel_api._CACHE, panel._HISTORY_CACHE, panel._PER_SYMBOL_CACHE, panel._LAB_CACHE):
        c.clear()
    yield


def _md5(p: Path) -> str:
    return hashlib.md5(p.read_bytes()).hexdigest()  # noqa: S324


def _bars(day0: int, n: int = 30, exchanges=("binance", "bybit"), sym: str = SYM) -> list[MinuteBar]:
    out = []
    for ex in exchanges:
        for i in range(n):
            m = day0 + 3_600_000 + i * 60_000
            out.append(MinuteBar(ex, sym, m, open=100 + i, high=101 + i, low=99 + i, close=100.5 + i, buy_usd=10.0,
                                 sell_usd=4.0, n_trades=2, liq_long_usd=5.0 if i == 2 else 0.0, n_liqs=int(i == 2),
                                 mark=100.0 + i, funding_8h=0.0001 if ex == "bybit" else None))
    return out


def _setup(tmp_path: Path) -> dict:
    data = tmp_path / "data"
    data.mkdir()
    db = data / "heatsignal_history.sqlite"
    st = HistoryStore(db)
    st.upsert_bars(_bars(OLD0) + _bars(OLD1) + _bars(OLD1, sym="XRP-USDT") + _bars(TODAY0 - DAY))
    st.add_liquidations([("bybit", SYM, OLD0 + 1000, "sell", 100.0, 2.0, 200.0, "full"),
                         ("binance", SYM, OLD1 + 2000, "buy", 101.0, 100.0, 10_100.0, "partial"),
                         ("okx", SYM, TODAY0 - DAY + 5000, "sell", 99.0, 1.0, 99.0, "partial")])
    st.add_news([("n-vieja", "coindesk", 0, "vieja, con coma", "linea1\nlinea2", "https://x.test/1", OLD0, OLD0 + 5,
                  "SOL", 0),
                 ("n-nueva", "coindesk", 0, "nueva", "", "https://x.test/2", TODAY0 - DAY, TODAY0 - DAY, "SOL", 0)])
    kl = [("binance", SYM, "1m", t, 1.0, 2.0, 0.5, 1.5, 10.0, 15.0, 3, 7.0)
          for t in range(TODAY0 - 100 * DAY, TODAY0, 6 * 3_600_000)]  # 4 por dia, 100 dias
    st.add_klines(kl)
    st.close()
    ck = data / "heatsignal_checkpoints.sqlite"
    c = sqlite3.connect(ck)
    c.execute("CREATE TABLE checkpoints (thread_id TEXT NOT NULL, checkpoint_ns TEXT NOT NULL DEFAULT '', "
              "checkpoint_id TEXT NOT NULL, parent_checkpoint_id TEXT, type TEXT, checkpoint BLOB, metadata BLOB, "
              "PRIMARY KEY (thread_id, checkpoint_ns, checkpoint_id))")
    c.execute("CREATE TABLE writes (thread_id TEXT NOT NULL, checkpoint_ns TEXT NOT NULL DEFAULT '', checkpoint_id TEXT "
              "NOT NULL, task_id TEXT NOT NULL, task_path TEXT NOT NULL DEFAULT '', idx INTEGER NOT NULL, channel TEXT "
              "NOT NULL, type TEXT, value BLOB, PRIMARY KEY (thread_id, checkpoint_ns, checkpoint_id, task_id, idx))")
    for ms in (OLD0 + 500, TODAY0 - DAY):
        tid = f"SOL-USDT:cycle-{ms}"
        c.executemany("INSERT INTO checkpoints VALUES (?, '', ?, NULL, 'msgpack', ?, ?)",
                      [(tid, f"id{i}", b"\x00blob" * 10, b"{}") for i in range(3)])
        c.execute("INSERT INTO writes VALUES (?, '', 'id0', 't', '', 0, 'ch', 'msgpack', ?)", (tid, b"w"))
    c.commit()
    c.close()
    # export diario local de un dia viejo, ya "subido" a Drive (mismo contenido en el remoto falso)
    exp = data / "export"
    hs = HistoryStore(db)
    export_mod.export_day(hs, exp, date.fromtimestamp(OLD0 / 1000) if False else
                          datetime.fromtimestamp(OLD0 / 1000, tz=timezone.utc).date())
    hs.close()
    remote, fake = fake_remote(tmp_path / "drive")
    for f in exp.rglob("*.csv.gz"):
        dst = fake.root / f.relative_to(exp)
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_bytes(f.read_bytes())
    # bases protegidas: nunca se tocan
    (data / "heatsignal_eval.sqlite").write_bytes(b"EVAL" * 1000)
    (data / "freqtrade_dryrun.sqlite").write_bytes(b"FT" * 1000)
    return {"data": data, "db": db, "ck": ck, "exp": exp, "remote": remote, "fake": fake}


class FakeDisk:
    """Disco de 1 GB. `pct` fija el uso; `drop_per_entry` baja el uso por cada fila del catalogo (para probar
    que el archivado se detiene al llegar al objetivo)."""

    def __init__(self, pct: float, db: Path | None = None, drop_per_entry: float = 0.0, total_mb: int = 1024) -> None:
        self.pct, self.db, self.drop = pct, db, drop_per_entry
        self.total = total_mb * MB

    def __call__(self, path: Path) -> tuple[int, int, int]:
        pct = self.pct
        if self.drop and self.db is not None:
            c = sqlite3.connect(self.db)
            try:
                n = c.execute("SELECT COUNT(*) FROM archive_catalog").fetchone()[0]
            except sqlite3.OperationalError:
                n = 0
            c.close()
            pct -= n * self.drop
        used = int(self.total * pct / 100)
        return self.total, used, self.total - used


def _cfg(env: dict, **kw) -> arch.ArchiveConfig:
    base = dict(history_db=env["db"], checkpoints_db=env["ck"], export_dir=env["exp"], remote_base="gdrive:",
                vacuum="never")
    base.update(kw)
    return arch.ArchiveConfig(**base)


def _count(db: Path, sql: str) -> int:
    c = sqlite3.connect(db)
    try:
        return c.execute(sql).fetchone()[0]
    finally:
        c.close()


# ---------------------------------------------------------------------------- utilidades

def test_uuid6_and_thread_time() -> None:
    assert arch.uuid6_ms("1f1c9d71-8afc-688f-bfff-27b17da47c12") == 1792206109452
    assert arch.thread_ms("BTC-USDT,SOL-USDT:cycle-1792206109022", None) == 1792206109022
    assert arch.thread_ms("X:otro", "1f1c9d71-8afc-688f-bfff-27b17da47c12") == 1792206109452
    assert arch.uuid6_ms("no-es-uuid") is None


def test_config_refuses_protected_databases(tmp_path) -> None:
    for name in arch.PROTECTED_NAMES:
        with pytest.raises(arch.ArchiveError):
            arch.Archiver(arch.ArchiveConfig(history_db=tmp_path / name))
        with pytest.raises(arch.ArchiveError):
            arch.Archiver(arch.ArchiveConfig(history_db=tmp_path / "h.sqlite", checkpoints_db=tmp_path / name))
    with pytest.raises(arch.ArchiveError):
        arch.Archiver(arch.ArchiveConfig(history_db=tmp_path / "h.sqlite", threshold_pct=70, target_pct=80))


# ---------------------------------------------------------------------------- check / umbral

def test_check_is_read_only_and_plans_in_order(tmp_path) -> None:
    env = _setup(tmp_path)
    before = {p.name: _md5(p) for p in env["data"].glob("*.sqlite")}
    a = arch.Archiver(_cfg(env, target_pct=1.0), remote=env["remote"], disk=FakeDisk(72.0), clock_ms=lambda: NOW)
    r = a.check()
    assert r["would_run"] is False and "solo informativo" in r["plan_note"]
    stages = [u["stage"] for u in r["plan"]]
    assert stages == sorted(stages, key=arch.STAGES.index)  # export -> klines -> history -> checkpoints
    assert set(r["candidates"]) == {"export", "klines", "history", "checkpoints"}
    assert r["candidates"]["history"]["units"] == 2 and r["candidates"]["checkpoints"]["units"] == 1
    assert r["candidates"]["klines"]["rows"]["klines_1m"] == 40 * 4  # dias 100..61 atras
    assert {p.name: _md5(p) for p in env["data"].glob("*.sqlite")} == before  # ni crea el catalogo
    assert _count(env["db"], "SELECT COUNT(*) FROM archive_catalog") == 0
    assert env["fake"].calls == []
    assert arch.Archiver(_cfg(env), disk=FakeDisk(85.0), clock_ms=lambda: NOW).check()["would_run"] is True


def test_run_below_threshold_does_nothing(tmp_path) -> None:
    env = _setup(tmp_path)
    before = {p.name: _md5(p) for p in env["data"].glob("*.sqlite")}
    r = arch.Archiver(_cfg(env), remote=env["remote"], disk=FakeDisk(79.9), clock_ms=lambda: NOW).run()
    assert r["status"] == "skipped" and r["archived"] == []
    assert {p.name: _md5(p) for p in env["data"].glob("*.sqlite")} == before and env["fake"].calls == []


# ---------------------------------------------------------------------------- run: archivado + verificacion + borrado

def test_run_archives_verifies_deletes_and_catalogs(tmp_path) -> None:
    env = _setup(tmp_path)
    protected = {n: _md5(env["data"] / n) for n in arch.PROTECTED_NAMES}
    bars_before = _count(env["db"], "SELECT COUNT(*) FROM bars_1m")
    a = arch.Archiver(_cfg(env, target_pct=1.0), remote=env["remote"], disk=FakeDisk(90.0), clock_ms=lambda: NOW)
    r = a.run()
    assert r["status"] == "partial"  # el objetivo de 1 % es inalcanzable: archivo todo lo elegible
    stages = [u["stage"] for u in r["archived"]]
    assert stages[0] == "export" and stages[-1] == "checkpoints" and stages.count("history") == 2
    # lo viejo ya no esta en la base; lo reciente si
    assert _count(env["db"], f"SELECT COUNT(*) FROM bars_1m WHERE minute_ms < {TODAY0 - 60 * DAY}") == 0
    assert _count(env["db"], "SELECT COUNT(*) FROM bars_1m") == 60 and bars_before == 240
    assert _count(env["db"], "SELECT COUNT(*) FROM liquidations") == 1
    assert _count(env["db"], "SELECT COUNT(*) FROM news") == 1
    assert _count(env["db"], f"SELECT MIN(open_time_ms) FROM klines") >= TODAY0 - 60 * DAY
    assert _count(env["ck"], "SELECT COUNT(*) FROM checkpoints") == 3  # queda el hilo reciente
    assert _count(env["ck"], "SELECT COUNT(*) FROM writes") == 1
    assert not (env["exp"] / f"{datetime.fromtimestamp(OLD0 / 1000, tz=timezone.utc):%Y/%m/%d}").exists()
    # catalogo: cada archivo en Drive con md5 verificado y filas = las borradas
    c = sqlite3.connect(env["db"])
    rows = c.execute("SELECT kind, table_name, rows, remote_path, md5, bytes FROM archive_catalog").fetchall()
    c.close()
    by = {}
    for kind, t, n, path, md5, size in rows:
        by.setdefault(t, []).append(n)
        f = env["fake"].root / path.removeprefix("gdrive:")
        assert f.is_file() and _md5(f) == md5 and f.stat().st_size == size
    assert sum(by["bars_1m"]) == 180 and sum(by["liquidations"]) == 2 and sum(by["news"]) == 1
    assert sum(by["klines_1m"]) == 160 and by["checkpoints"] == [3] and len(by["export"]) == 4
    assert any(p.startswith("archive/history/") and p.endswith("bars_1m.csv.gz") for p in env["fake"].files())
    # el archivo frio tiene el mismo formato que el export (y la noticia con coma y salto de linea sobrevive)
    news = next(env["fake"].root.glob("archive/history/*/*/*/news.csv.gz"))
    with gzip.open(news, "rt") as fh:
        text = fh.read()
    assert text.startswith("item_id,source,") and "linea1\nlinea2" in text
    # las protegidas, intactas
    assert {n: _md5(env["data"] / n) for n in arch.PROTECTED_NAMES} == protected
    # catalogo y corridas: solo-agregar
    c = sqlite3.connect(env["db"])
    with pytest.raises(sqlite3.IntegrityError):
        c.execute("DELETE FROM archive_catalog")
    with pytest.raises(sqlite3.IntegrityError):
        c.execute("UPDATE archive_runs SET status='x'")
    assert c.execute("SELECT status FROM archive_runs").fetchall() == [("partial",)]
    c.close()
    # el export diario no vuelve a generar el dia cuya carpeta se archivo
    hs = HistoryStore(env["db"])
    pend = export_mod.pending_days(hs, env["exp"], datetime.fromtimestamp(NOW / 1000, tz=timezone.utc).date())
    hs.close()
    assert datetime.fromtimestamp(OLD0 / 1000, tz=timezone.utc).date() not in pend
    # y `export --day` de un dia archivado se rechaza (saldria vacio y pisaria Drive)
    rc = export_mod.main(["--db", str(env["db"]), "--out", str(env["exp"]), "--no-upload",
                          "--day", f"{datetime.fromtimestamp(OLD0 / 1000, tz=timezone.utc):%Y-%m-%d}"])
    assert rc == 2


def test_run_stops_when_target_reached(tmp_path) -> None:
    env = _setup(tmp_path)
    disk = FakeDisk(85.0, env["db"], drop_per_entry=3.0)  # cada archivo catalogado baja 3 puntos
    r = arch.Archiver(_cfg(env, target_pct=70.0), remote=env["remote"], disk=disk, clock_ms=lambda: NOW).run()
    assert r["status"] == "ok"
    # export (4 archivos = -12) + 1 mes de klines (-3) = 70 %: no sigue con history ni checkpoints
    assert [u["stage"] for u in r["archived"]] == ["export", "klines"]
    assert _count(env["db"], "SELECT COUNT(*) FROM bars_1m") == 240


@pytest.mark.parametrize("failure", ["upload", "corrupt", "stat"])
def test_failed_verification_deletes_nothing(tmp_path, failure) -> None:
    env = _setup(tmp_path)
    if failure == "corrupt":
        env["fake"].corrupt_upload = True
    else:
        env["fake"].fail.add(failure)
    before = {p.name: _md5(p) for p in env["data"].glob("*.sqlite")}
    a = arch.Archiver(_cfg(env, target_pct=1.0, stages=("klines", "history", "checkpoints")), remote=env["remote"],
                      disk=FakeDisk(90.0), clock_ms=lambda: NOW)
    r = a.run()
    assert r["status"] == "error" and r["archived"] == [] and r["errors"]
    assert _count(env["db"], "SELECT COUNT(*) FROM bars_1m") == 240
    assert _count(env["db"], "SELECT COUNT(*) FROM klines") == 400
    assert _count(env["db"], "SELECT COUNT(*) FROM archive_catalog") == 0
    assert _count(env["ck"], "SELECT COUNT(*) FROM checkpoints") == 6
    for n in arch.PROTECTED_NAMES:
        assert _md5(env["data"] / n) == before[n]


def test_export_not_in_drive_is_kept(tmp_path) -> None:
    env = _setup(tmp_path)
    for f in list(env["fake"].root.rglob("news.csv.gz")):
        f.unlink()  # el dia no termino de subirse
    r = arch.Archiver(_cfg(env, target_pct=1.0, stages=("export",)), remote=env["remote"], disk=FakeDisk(90.0),
                      clock_ms=lambda: NOW).run()
    assert r["skipped"] and r["archived"] == []
    assert list(env["exp"].rglob("news.csv.gz"))


def test_verification_without_md5_downloads_and_existing_identical_is_reused(tmp_path) -> None:
    env = _setup(tmp_path)
    env["fake"].no_md5 = True
    r = arch.Archiver(_cfg(env, target_pct=1.0, stages=("history",)), remote=env["remote"], disk=FakeDisk(90.0),
                      clock_ms=lambda: NOW).run()
    assert len(r["archived"]) == 2
    assert any(c[1] == "copyto" and c[2].startswith("gdrive:") for c in env["fake"].calls)  # bajo para verificar


def test_never_overwrites_a_different_cold_file(tmp_path) -> None:
    env = _setup(tmp_path)
    d = datetime.fromtimestamp(OLD0 / 1000, tz=timezone.utc)
    other = env["fake"].root / f"archive/history/{d:%Y/%m/%d}/bars_1m.csv.gz"
    other.parent.mkdir(parents=True)
    other.write_bytes(b"otro contenido")
    r = arch.Archiver(_cfg(env, target_pct=1.0, stages=("history",)), remote=env["remote"], disk=FakeDisk(90.0),
                      clock_ms=lambda: NOW).run()
    assert len(r["archived"]) == 2 and other.read_bytes() == b"otro contenido"
    assert any(".arch-" in p for p in env["fake"].files())


def test_incremental_vacuum_returns_space(tmp_path) -> None:
    env = _setup(tmp_path)
    st = HistoryStore(env["db"])  # mas volumen para que se note
    st.upsert_bars([b for k in range(20) for b in _bars(OLD0 + 0, n=60, sym=f"S{k}-USDT")])
    st.close()
    c = sqlite3.connect(env["db"])
    c.execute("PRAGMA auto_vacuum = INCREMENTAL")
    c.execute("VACUUM")
    c.close()
    size0 = env["db"].stat().st_size
    a = arch.Archiver(_cfg(env, target_pct=1.0, stages=("history",)), remote=env["remote"], disk=FakeDisk(90.0),
                      clock_ms=lambda: NOW)
    a.run()
    c = sqlite3.connect(env["db"])
    assert c.execute("PRAGMA freelist_count").fetchone()[0] == 0
    c.close()
    assert env["db"].stat().st_size < size0


def test_full_vacuum_migrates_when_space_allows(tmp_path) -> None:
    env = _setup(tmp_path)
    a = arch.Archiver(_cfg(env, target_pct=1.0, stages=("history",), vacuum="auto"), remote=env["remote"],
                      disk=FakeDisk(90.0, total_mb=10_240), clock_ms=lambda: NOW)  # 1 GB libre: alcanza
    r = a.run()
    assert r["vacuum"] and r["vacuum"][0]["result"].startswith("ok")
    assert _count(env["db"], "PRAGMA auto_vacuum") == 2
    # sin espacio: no lo intenta y lo dice
    tight = arch.Archiver(_cfg(env), disk=FakeDisk(99.99), clock_ms=lambda: NOW)
    assert "sin espacio" in tight.full_vacuum(env["db"], dry_run=True)["result"]


def test_status_and_cli_check(tmp_path, capsys, monkeypatch) -> None:
    env = _setup(tmp_path)
    arch.Archiver(_cfg(env, target_pct=1.0, stages=("history",)), remote=env["remote"], disk=FakeDisk(90.0),
                  clock_ms=lambda: NOW).run()
    s = arch.status(_cfg(env))
    assert s["cold"]["bars_1m"]["files"] == 2 and s["runs"][0]["status"] == "partial"
    assert s["hot"]["bars_1m"]["rows"] == 60 and s["disk"]["threshold_pct"] == 80.0
    json.dumps(s)
    monkeypatch.setenv("HEATSIGNAL_DRIVE_REMOTE", "gdrive:")
    rc = arch.main(["check", "--dry-run", "--db", str(env["db"]), "--checkpoints-db", str(env["ck"]),
                    "--export-dir", str(env["exp"]), "--json"])
    assert rc == 0 and "plan" in json.loads(capsys.readouterr().out)


# ---------------------------------------------------------------------------- cache LRU

def test_cold_cache_is_lru_with_cap(tmp_path) -> None:
    env = _setup(tmp_path)
    arch.Archiver(_cfg(env, target_pct=1.0, stages=("history",)), remote=env["remote"], disk=FakeDisk(90.0),
                  clock_ms=lambda: NOW).run()
    reader = ColdReader(env["db"], ColdCache(tmp_path / "cache", cap_bytes=10**9), env["remote"])
    entries = reader.entries("bars_1m", None, None)
    assert len(entries) == 2
    sizes = [e.bytes for e in entries]
    cache = ColdCache(tmp_path / "cache2", cap_bytes=max(sizes) + 1)  # entra uno solo
    p0, hit0 = cache.get(entries[0], env["remote"])
    p0b, hit0b = cache.get(entries[0], env["remote"])
    assert (hit0, hit0b) == (False, True) and p0 == p0b
    p1, _ = cache.get(entries[1], env["remote"])
    assert p1.is_file() and not p0.exists()  # expulso el menos usado
    assert cache.stats()["bytes"] <= cache.cap_bytes


def test_cold_cache_rejects_tampered_file(tmp_path) -> None:
    env = _setup(tmp_path)
    arch.Archiver(_cfg(env, target_pct=1.0, stages=("history",)), remote=env["remote"], disk=FakeDisk(90.0),
                  clock_ms=lambda: NOW).run()
    reader = ColdReader(env["db"], ColdCache(tmp_path / "cache"), env["remote"])
    e = reader.entries("bars_1m", None, None)[0]
    (env["fake"].root / e.remote_path.removeprefix("gdrive:")).write_bytes(b"alterado")
    from heatsignal.history.cold import ColdError

    with pytest.raises(ColdError):
        reader.fetch([e])
    assert not list((tmp_path / "cache").glob("*"))


# ---------------------------------------------------------------------------- panel: lectura fria combinada

def _ctx(env: dict, reader: ColdReader | None) -> PanelContext:
    return PanelContext(health=HealthRegistry(), history=MinuteRecorder(HistoryStore(env["db"])), archive=reader,
                        checkpoint_db=env["ck"], db_paths={"history": env["db"]})


def _client(ctx: PanelContext) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=create_panel_app(ctx)), base_url="http://panel")


async def test_panel_reads_cold_and_local_combined(tmp_path) -> None:
    env = _setup(tmp_path)
    reader = ColdReader(env["db"], ColdCache(env["data"] / "cold_cache"), env["remote"])
    params = {"symbol": "SOLUSDT", "tf": "1h", "since": str(OLD1), "until": str(TODAY0)}
    before = (await _client(_ctx(env, reader)).get("/api/history/bars", params=params)).json()
    assert before["source"] == "local" and before["count"] > 0
    arch.Archiver(_cfg(env, target_pct=1.0, stages=("history",)), remote=env["remote"], disk=FakeDisk(90.0),
                  clock_ms=lambda: NOW).run()
    panel._HISTORY_CACHE.clear()
    c = _client(_ctx(env, reader))
    after = (await c.get("/api/history/bars", params=params)).json()
    assert after["source"] == "mixed" and after["archive"]["files"] == 1 and after["archive"]["downloaded"] == 1
    assert after["partial"] is False and "Drive" in after["archive"]["note"]
    assert after["bars"] == before["bars"]  # mismo resultado que antes de archivar
    again = (await c.get("/api/history/bars", params=params)).json()
    assert again["archive"]["cache_hits"] == 1 and again["archive"]["downloaded"] == 0
    only_cold = (await c.get("/api/history/bars", params={**params, "until": str(OLD1 + DAY), "combine": "true"})).json()
    assert only_cold["source"] == "drive" and only_cold["count"] > 0
    # liquidaciones: con since se combinan; sin since solo local + aviso
    liq = (await c.get("/api/history/liquidations", params={"since": str(OLD0), "limit": 2})).json()
    assert liq["total"] == 3 and liq["source"] == "mixed" and len(liq["items"]) == 2
    assert liq["items"][0]["ts_ms"] > liq["items"][1]["ts_ms"]
    assert liq["totals"]["long_usd"] == 299.0 and liq["totals"]["short_usd"] == 10_100.0
    page2 = (await c.get("/api/history/liquidations", params={"since": str(OLD0), "limit": 2, "offset": 2})).json()
    assert [i["ts_ms"] for i in page2["items"]] == [OLD0 + 1000]
    nos = (await c.get("/api/history/liquidations")).json()
    assert nos["total"] == 1 and nos["source"] == "local" and "since" in nos["archive_note"]
    # storage
    stg = (await c.get("/api/history/storage")).json()
    t = {x["table"]: x for x in stg["tables"]}
    assert t["bars_1m"]["cold"]["files"] == 2 and t["bars_1m"]["hot"]["rows"] == 60
    assert stg["disk"]["threshold_pct"] == 80.0 and stg["last_run"]["status"] == "partial"
    assert stg["cache"]["files"] >= 1 and set(stg["protected"]) == set(arch.PROTECTED_NAMES)


async def test_panel_503_when_drive_fails_and_partial_on_request(tmp_path) -> None:
    env = _setup(tmp_path)
    arch.Archiver(_cfg(env, target_pct=1.0, stages=("history",)), remote=env["remote"], disk=FakeDisk(90.0),
                  clock_ms=lambda: NOW).run()
    env["fake"].fail.add("download")
    reader = ColdReader(env["db"], ColdCache(env["data"] / "cold_cache"), env["remote"])
    c = _client(_ctx(env, reader))
    params = {"symbol": "SOLUSDT", "tf": "1h", "since": str(OLD0), "until": str(TODAY0)}
    r = await c.get("/api/history/bars", params=params)
    assert r.status_code == 503 and "Drive" in r.json()["detail"]
    p = (await c.get("/api/history/bars", params={**params, "allow_partial": "true"})).json()
    assert p["partial"] is True and len(p["partial_reasons"]) == 2 and p["count"] > 0  # lo local si
    r = await c.get("/api/history/liquidations", params={"since": str(OLD0)})
    assert r.status_code == 503
    # sin remoto configurado: tambien 503 con el motivo
    c2 = _client(_ctx(env, ColdReader(env["db"], ColdCache(tmp_path / "c2"), None, remote_error="sin remoto")))
    r = await c2.get("/api/history/bars", params=params)
    assert r.status_code == 503 and "sin remoto" in r.json()["detail"]
    # un rango local puro no toca Drive aunque este caido
    ok = await c.get("/api/history/bars", params={**params, "since": str(TODAY0 - DAY)})
    assert ok.status_code == 200 and ok.json()["source"] == "local"


async def test_cycle_report_read_from_archived_checkpoints(tmp_path) -> None:
    from heatsignal.agents.checkpoint import make_run_config, sqlite_checkpointer
    from heatsignal.agents.graph import build_graph
    from heatsignal.evaluation.strategy_registry import StrategyHypothesis

    from _agent_fakes import ScriptedChatModel, full_script, make_feature_set

    env = _setup(tmp_path)
    env["ck"].unlink()
    run_id = "cycle-1792206109022"
    async with sqlite_checkpointer(env["ck"]) as saver:
        graph = build_graph(ScriptedChatModel(full_script(["XRP-USDT"])), checkpointer=saver)
        state = {"run_id": run_id, "features_by_symbol": {"XRP-USDT": make_feature_set("XRP-USDT")},
                 "allowed_strategies": [StrategyHypothesis(description="s", entry_conditions={}, exit_conditions={},
                                                           author="t")]}
        await graph.ainvoke(state, make_run_config(["XRP-USDT"], run_id))
    later = 1792206109022 + 40 * DAY
    r = arch.Archiver(_cfg(env, target_pct=1.0, stages=("checkpoints",)), remote=env["remote"], disk=FakeDisk(90.0),
                      clock_ms=lambda: later).run()
    assert [u["stage"] for u in r["archived"]] == ["checkpoints"]
    assert _count(env["ck"], "SELECT COUNT(*) FROM checkpoints") == 0
    reader = ColdReader(env["db"], ColdCache(env["data"] / "cold_cache"), env["remote"])
    det = (await _client(_ctx(env, reader)).get(f"/api/cycles/{run_id}")).json()
    assert det["report_source"] == "checkpoint_drive" and det["report"]["symbols"]["XRP-USDT"] is not None
    assert any("Drive" in n for n in det["notes"])
