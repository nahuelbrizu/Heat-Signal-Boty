"""
Job macro: `python -m heatsignal.macro <comando>` (corre en la VM, sin pandas).

    run                    cada hora (heatsignal-macro.timer): Polymarket siempre; FRED si la ultima corrida OK
                           tiene >= 20 h (o con --force-fred); despues exporta los dias UTC cerrados que falten a
                           data/export/macro/YYYY/MM/DD/*.csv.gz (el timer de historia los sube a Drive con rclone).
    backfill-fred          vintages historicos de FRED desde --since (para el laboratorio). Necesita FRED_API_KEY.
    backfill-polymarket    serie de precios del CLOB de los mercados ya conocidos, --days hacia atras (source
                           'clob-history', fechada en el instante del precio; sin volumen ni liquidez).
    status                 estado de cada fuente en JSON.

Cada corrida deja una fila en `macro_runs` por fuente: ok | partial | error | off. FRED sin key = 'off' con el
motivo, nunca un exito vacio. Codigo de salida != 0 si alguna fuente fallo (que systemd lo vea).
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .calendar import load_calendar
from .context import build_block, estimate_tokens, source_status
from .fred import SERIES, FredClient, FredError, api_key_from_env, window_for
from .polymarket import PolymarketClient, PolymarketError, collect, labels, load_config
from .store import DEFAULT_DB, MacroStore, fred_today

log = logging.getLogger("heatsignal.macro.job")

FRED_EVERY_MS = 20 * 3_600_000
FRED_LOOKBACK_DAYS = 400           # 13 meses de CPI para la variacion a/a; los vintages repetidos se descartan
DAY_MS = 86_400_000
EXPORT_TABLES = {"fred_observations": "fetched_ms", "polymarket_snapshots": "fetched_ms"}


def _now_ms() -> int:
    return int(time.time() * 1000)


def run_fred(store: MacroStore, client: FredClient, *, now: datetime | None = None,
             lookback_days: int = FRED_LOOKBACK_DAYS, since: str | None = None) -> dict[str, Any]:
    started = _now_ms()
    if not client.configured:
        detail = "apagado: falta FRED_API_KEY en .env (key gratuita en https://fred.stlouisfed.org/docs/api/api_key.html)"
        store.add_run("fred", started, _now_ms(), "off", 0, detail)
        log.warning("FRED %s", detail)
        return {"status": "off", "detail": detail}
    today = fred_today(now)
    if since:
        rt_start = since
        obs_start = (date.fromisoformat(since) - timedelta(days=FRED_LOOKBACK_DAYS)).isoformat()
    else:
        rt_start, obs_start = window_for(today, lookback_days)
    n, errors = 0, []
    for s in SERIES:
        try:
            rows = client.vintages(s.series_id, rt_start, obs_start)
            n += store.add_fred(rows, _now_ms())
        except FredError as exc:
            errors.append(f"{s.series_id}: {exc}")
            log.error("FRED %s: %s", s.series_id, exc)
    status = "ok" if not errors else ("partial" if len(errors) < len(SERIES) else "error")
    store.add_run("fred", started, _now_ms(), status, n, {"errors": errors, "realtime_start": rt_start} if errors else
                  {"realtime_start": rt_start, "observation_start": obs_start})
    return {"status": status, "rows": n, "errors": errors}


def run_polymarket(store: MacroStore, client: PolymarketClient, now_ms: int | None = None,
                   config_path: Path | None = None) -> dict[str, Any]:
    started = _now_ms()
    try:
        cfg = load_config(config_path) if config_path else load_config()
    except Exception as exc:  # noqa: BLE001 - config rota = error explicito
        store.add_run("polymarket", started, _now_ms(), "error", 0, f"config ilegible: {exc}")
        return {"status": "error", "errors": [f"config ilegible: {exc}"]}
    try:
        rows, errors = collect(client, cfg, now_ms or _now_ms())
    except PolymarketError as exc:
        rows, errors = [], [str(exc)]
    n = store.add_polymarket(rows)
    status = "ok" if not errors else ("partial" if rows else "error")
    store.add_run("polymarket", started, _now_ms(), status, n,
                  {"config_id": cfg.config_id, "markets": len(rows), "errors": errors})
    for e in errors:
        log.warning("Polymarket: %s", e)
    return {"status": status, "rows": n, "errors": errors}


def export_days(store: MacroStore, out: Path, today: date, max_days: int = 60) -> list[str]:
    """Exporta los dias UTC cerrados que falten (una carpeta por dia; un dia ya exportado no se rehace).

    Como mucho `max_days` dias nuevos por corrida (el resto sale en las siguientes)."""
    from ..history.export import write_csv_gz

    starts = [m for m in (store.min_ts(t, c) for t, c in EXPORT_TABLES.items()) if m is not None]
    if not starts:
        return []
    d = datetime.fromtimestamp(min(starts) / 1000, tz=timezone.utc).date()
    done = []
    while d < today and len(done) < max_days:
        folder = out / f"{d:%Y}" / f"{d:%m}" / f"{d:%d}"
        if not folder.exists():
            start = int(datetime(d.year, d.month, d.day, tzinfo=timezone.utc).timestamp() * 1000)
            tmp = folder.with_name(folder.name + ".tmp")
            tmp.mkdir(parents=True, exist_ok=True)
            for table, col in EXPORT_TABLES.items():
                cols, rows = store.rows_between(table, col, start, start + DAY_MS)
                write_csv_gz(rows, cols, tmp / f"{table}.csv.gz")
            tmp.rename(folder)
            done.append(d.isoformat())
        d += timedelta(days=1)
    return done


def status_json(store: MacroStore | None, now_ms: int) -> dict[str, Any]:
    try:
        cal, err = load_calendar(), None
    except Exception as exc:  # noqa: BLE001
        cal, err = None, str(exc)
    st = source_status(store, now_ms, cal, err)
    block = build_block(store, now_ms, calendar=cal, cal_error=err, labels=_labels())
    return {"sources": st, "block": block, "block_chars": len(block), "block_tokens_est": estimate_tokens(block)}


def _labels() -> dict[str, str] | None:
    try:
        return labels(load_config())
    except Exception:  # noqa: BLE001
        return None


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m heatsignal.macro", description=__doc__.split("\n\n")[0])
    p.add_argument("command", choices=["run", "backfill-fred", "backfill-polymarket", "status"])
    p.add_argument("--db", default=os.environ.get("HEATSIGNAL_MACRO_DB", DEFAULT_DB))
    p.add_argument("--export-dir", default=os.path.join(os.environ.get("HEATSIGNAL_EXPORT_DIR", "data/export"), "macro"))
    p.add_argument("--force-fred", action="store_true")
    p.add_argument("--no-export", action="store_true")
    p.add_argument("--since", default="2021-01-01", help="backfill-fred: primer dia del vintage (YYYY-MM-DD)")
    p.add_argument("--days", type=int, default=90, help="backfill-polymarket: dias hacia atras")
    a = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    store = MacroStore(a.db)
    try:
        now = _now_ms()
        if a.command == "status":
            print(json.dumps(status_json(store, now), indent=2, ensure_ascii=False, default=str))
            return 0
        if a.command == "backfill-fred":
            res = run_fred(store, FredClient(api_key_from_env()), since=a.since)
            print(json.dumps(res, ensure_ascii=False))
            return 0 if res["status"] in ("ok", "partial") else 1
        if a.command == "backfill-polymarket":
            return _backfill_poly(store, PolymarketClient(), a.days)
        out: dict[str, Any] = {"polymarket": run_polymarket(store, PolymarketClient())}
        last = store.last_ok_ms("fred")
        if a.force_fred or last is None or now - last >= FRED_EVERY_MS:  # sin key: queda 'off' cada hora, visible
            out["fred"] = run_fred(store, FredClient(api_key_from_env()))
        if not a.no_export:
            today = datetime.fromtimestamp(now / 1000, tz=timezone.utc).date()
            out["exported"] = export_days(store, Path(a.export_dir), today)
        print(json.dumps(out, ensure_ascii=False, default=str))
        failed = [k for k, v in out.items() if isinstance(v, dict) and v.get("status") == "error"]
        return 1 if failed else 0
    finally:
        store.close()


def _backfill_poly(store: MacroStore, client: PolymarketClient, days: int) -> int:
    """Precios historicos del CLOB para los mercados que ya leimos (necesita su token_id)."""
    started = _now_ms()
    end_s = started // 1000
    start_s = end_s - days * 86400
    rows, errors = [], []
    seen = {r["market_id"]: r for r in store.poly_latest(started)}
    for mid, r in seen.items():
        if not r.get("token_id"):
            errors.append(f"{mid}: sin token_id")
            continue
        try:
            for t_ms, prob in client.price_history(r["token_id"], start_s, end_s):
                rows.append({"market_id": mid, "fetched_ms": t_ms, "source": "clob-history", "slug": r["slug"],
                             "event_slug": r["event_slug"], "config_key": r["config_key"], "question": r["question"],
                             "outcome": r["outcome"], "prob": prob, "end_date": r["end_date"], "token_id": r["token_id"]})
        except PolymarketError as exc:
            errors.append(f"{mid}: {exc}")
    n = store.add_polymarket(rows)
    status = "ok" if not errors else ("partial" if n else "error")
    store.add_run("polymarket-backfill", started, _now_ms(), status, n, {"errors": errors[:20], "days": days})
    print(json.dumps({"status": status, "rows": n, "errors": errors[:20]}, ensure_ascii=False))
    return 0 if status != "error" else 1


if __name__ == "__main__":
    sys.exit(main())
