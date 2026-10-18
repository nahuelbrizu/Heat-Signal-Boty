"""
Pre-registro de estrategias: `python -m heatsignal.evaluation.register_strategy`.

    --list                     muestra las estrategias registradas y su estado
    archivo.json [...]         registra la(s) estrategia(s) de los archivos (inmutables una vez guardadas)
    --dry-run archivo.json     valida y muestra lo que se registraria, sin tocar la base

Estrategias de velas de 1h (exit_conditions.timeframe = "1h"): se valida que el horizonte sea horizon_bars >= 1 o
horizon = "end_of_day_utc" y que `htf_setup` sea uno conocido (`evaluation/timeframes.py`).

Formato del JSON (ver `StrategyHypothesis`):
    {
      "description": "Rebote tras cluster de longs liquidados a < 2 ATR del mark",
      "entry_conditions": {"cluster_side": "SELL", "max_distance_atr": 2.0},
      "exit_conditions": {"horizon_s": 14400},
      "author": "nahue"
    }

Anti-p-hacking: registrar ANTES de ver resultados. Un `strategy_id` no se puede pisar ni borrar;
para cambiar una estrategia se registra una nueva (otra version) y queda constancia de ambas.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from .job import DEFAULT_DB_PATH
from .strategy_registry import StrategyHypothesis, StrategyRegistry
from .timeframes import SETUPS, hourly_horizon, is_hourly


def validate_hourly(strategy: StrategyHypothesis) -> str | None:
    """Motivo si una estrategia de 1h esta mal pre-registrada (None si esta bien o no es de 1h)."""
    if not is_hourly(strategy.exit_conditions):
        return None
    probe = hourly_horizon(strategy.exit_conditions, datetime(2026, 1, 1, 0, 30, tzinfo=timezone.utc))
    if isinstance(probe, str):
        return f"horizonte de 1h invalido: {probe}"
    setup = strategy.entry_conditions.get("htf_setup")
    if setup is not None and setup not in SETUPS:
        return f"htf_setup desconocido: {setup!r} (validos: {', '.join(SETUPS)})"
    return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m heatsignal.evaluation.register_strategy")
    parser.add_argument("file", nargs="*", help="JSON(s) con la estrategia a registrar")
    parser.add_argument("--dry-run", action="store_true", help="validar y mostrar, sin registrar")
    parser.add_argument("--db", default=os.environ.get("HEATSIGNAL_EVAL_DB", DEFAULT_DB_PATH))
    parser.add_argument("--list", action="store_true", help="listar estrategias registradas")
    args = parser.parse_args(argv)
    if not args.file and not args.list:
        parser.error("indicar un archivo JSON o --list")

    strategies = []
    for f in args.file:
        data = json.loads(Path(f).read_text(encoding="utf-8"))
        for forbidden in ("strategy_id", "registered_at", "status"):
            if forbidden in data:
                parser.error(f"{f}: {forbidden!r} lo asigna el sistema, sacarlo del JSON")
        strategy = StrategyHypothesis(**data)
        problem = validate_hourly(strategy)
        if problem:
            parser.error(f"{f}: {problem}")
        if "horizon_s" not in strategy.exit_conditions and not is_hourly(strategy.exit_conditions):
            print(f"aviso: {f}: exit_conditions sin 'horizon_s' -> se usara el horizonte por defecto (4 h)",
                  file=sys.stderr)
        strategies.append(strategy)
    if args.dry_run:
        for st in strategies:
            print(json.dumps(st.model_dump(mode="json", exclude={"strategy_id", "registered_at", "status"}),
                             ensure_ascii=False, indent=2))
        print(f"dry-run: {len(strategies)} estrategia(s) validas, no se registro nada", file=sys.stderr)
        return 0

    db_path = Path(args.db)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    registry = StrategyRegistry(db_path)
    try:
        for strategy in strategies:
            registry.register(strategy)
            print(f"registrada {strategy.strategy_id}: {strategy.description}")
        if args.list:
            for s in registry.get_all():
                print(json.dumps(s.model_dump(mode="json"), ensure_ascii=False))
    finally:
        registry.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
