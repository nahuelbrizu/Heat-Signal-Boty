"""
CLI del bot de paper trading del lote 3 (simulado: sin plata real ni API keys).

    python -m heatsignal.paperbot run                  # una corrida (heatsignal-paperbot.timer, :02 UTC)
    python -m heatsignal.paperbot status               # ultimas corridas, huecos y equity por estrategia
    python -m heatsignal.paperbot params-from-lab ID [--detail data/lab_detail_ID.json] [--write]
    python -m heatsignal.paperbot reset-not-allowed    # explica por que no hay "reset" (base append-only)

Variables: HEATSIGNAL_PAPERBOT_DB (default data/paperbot.sqlite), HEATSIGNAL_HISTORY_DB
(default data/heatsignal_history.sqlite).
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from collections.abc import Iterable
from pathlib import Path

from .store import DEFAULT_DB

RESET_TEXT = """No hay reset: la base del paper trading es append-only (triggers que rechazan UPDATE y DELETE).
Borrar o reescribir el historial esconderia resultados malos, que es justo lo que el paper trading tiene que mostrar.

Si hay que empezar un paper NUEVO (otra version de las reglas o de los parametros), se archiva la base vieja y
se arranca otra; la vieja queda guardada:
    sudo systemctl stop heatsignal-paperbot.timer
    mv ~/heatsignal/data/paperbot.sqlite ~/heatsignal/data/paperbot-HASTA-$(date -u +%Y%m%d).sqlite
    sudo systemctl start heatsignal-paperbot.timer
(y anotarlo en decisions.md con el motivo)."""


def _paths() -> tuple[Path, Path]:
    db = Path(os.environ.get("HEATSIGNAL_PAPERBOT_DB", DEFAULT_DB))
    hist = Path(os.environ.get("HEATSIGNAL_HISTORY_DB", "data/heatsignal_history.sqlite"))
    return db, hist


def cmd_run(args: argparse.Namespace) -> int:
    from .engine import EngineConfig, run_once, summary_line

    db, hist = _paths()
    run = run_once(EngineConfig(paperbot_db=db, history_db=hist, download=not args.no_download))
    print(summary_line(run))
    return 0 if run["status"] in ("ok", "skipped") else 1


def cmd_status(args: argparse.Namespace) -> int:
    from . import view

    db, _ = _paths()
    try:
        ov = view.overview(db)
    except FileNotFoundError:
        print(f"todavia no hay base del bot en {db} (no corrio nunca)")
        return 1
    print(view.DISCLAIMER)
    for r in ov["runs"][:args.n]:
        print(f"  {r['hour_at']}  {r['status']:<7} {r['reason'] or ''}  rss={r['peak_rss_kb']} KB")
    for g in ov["gaps"][:10]:
        print(f"  HUECO {g['from_at']} -> {g['to_at']} ({g['hours']} h)")
    for s in ov["strategies"]:
        print(f"{s['strategy_id']:<20} equity={s['equity']} retorno={s['return']} canasta={s['basket_return']} "
              f"ordenes={s['n_orders']} trades={s['n_trades']}")
    return 0


def cmd_params_from_lab(args: argparse.Namespace) -> int:
    from ..lab.strategies import CATALOG
    from . import params as P

    if args.strategy_id not in CATALOG:
        print(f"estrategia desconocida: {args.strategy_id}")
        return 2
    spec = CATALOG[args.strategy_id][0]
    path = Path(args.detail or Path(_paths()[0]).parent / f"lab_detail_{args.strategy_id}.json")
    detail = json.loads(path.read_text(encoding="utf-8"))
    chosen, origin = P.params_from_lab_detail(detail, spec)
    print(json.dumps({"strategy_id": args.strategy_id, "params": chosen, "origen": origin}, ensure_ascii=False, indent=1))
    if args.write:
        P.write_choice(args.strategy_id, chosen, origin, spec)
        print(f"escrito en {P.PARAMS_PATH} (commitear el cambio y desplegar)")
    return 0


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m heatsignal.paperbot", description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="una corrida horaria")
    r.add_argument("--no-download", action="store_true", help="no bajar velas ni funding (solo pruebas)")
    s = sub.add_parser("status", help="estado del bot")
    s.add_argument("-n", type=int, default=24)
    p = sub.add_parser("params-from-lab", help="toma el selected_params del ultimo lab_detail publicado")
    p.add_argument("strategy_id")
    p.add_argument("--detail", default=None)
    p.add_argument("--write", action="store_true")
    sub.add_parser("reset-not-allowed", help="por que no hay reset")
    args = parser.parse_args(list(argv) if argv is not None else None)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if args.cmd == "run":
        return cmd_run(args)
    if args.cmd == "status":
        return cmd_status(args)
    if args.cmd == "params-from-lab":
        return cmd_params_from_lab(args)
    print(RESET_TEXT)
    return 2


if __name__ == "__main__":
    sys.exit(main())
