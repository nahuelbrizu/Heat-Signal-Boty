"""
CLI de las alertas de oportunidad.

    python -m heatsignal.alerts run                 # una corrida (la dispara heatsignal-alerts.timer cada 5 min)
    python -m heatsignal.alerts status [--n 20]     # ultimas corridas y alertas
    python -m heatsignal.alerts auto STRATEGY_ID on|off
    python -m heatsignal.alerts enviar ALERT_ID     # mandarla a la demo (la ejecuta la fase 4)

Env: HEATSIGNAL_DATA_DIR (default ~/heatsignal/data), HEATSIGNAL_ALERTS_DB, HEATSIGNAL_HISTORY_DB, HEATSIGNAL_PAPERBOT_DB.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys

from .store import AlertError, AlertStore


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m heatsignal.alerts")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("run")
    s = sub.add_parser("status")
    s.add_argument("--n", type=int, default=20)
    a = sub.add_parser("auto")
    a.add_argument("strategy_id")
    a.add_argument("estado", choices=["on", "off"])
    e = sub.add_parser("enviar")
    e.add_argument("alert_id")
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    from .engine import default_config

    cfg = default_config()
    if args.cmd == "run":
        from .engine import run_once

        out = run_once(cfg)
        print(json.dumps(out, ensure_ascii=False, default=str))
        return 0 if out.get("status") in ("ok", "skipped") else 1
    store = AlertStore(cfg.alerts_db)
    try:
        if args.cmd == "status":
            print(json.dumps({"corridas": store.runs(args.n), "alertas": store.recent(limit=args.n)},
                             ensure_ascii=False, indent=2, default=str))
        elif args.cmd == "auto":
            store.set_auto(args.strategy_id, args.estado == "on", origin="cli")
            print(json.dumps({"strategy_id": args.strategy_id, "auto": args.estado == "on"}))
        else:
            print(json.dumps(store.act(args.alert_id, "enviada", origin="cli"), ensure_ascii=False, default=str))
    except AlertError as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False))
        return 2
    finally:
        store.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
