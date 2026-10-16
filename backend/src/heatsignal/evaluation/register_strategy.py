"""
Pre-registro de estrategias: `python -m heatsignal.evaluation.register_strategy`.

    --list                     muestra las estrategias registradas y su estado
    archivo.json               registra la estrategia del archivo (inmutable una vez guardada)

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
from pathlib import Path

from .job import DEFAULT_DB_PATH
from .strategy_registry import StrategyHypothesis, StrategyRegistry


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m heatsignal.evaluation.register_strategy")
    parser.add_argument("file", nargs="?", help="JSON con la estrategia a registrar")
    parser.add_argument("--db", default=os.environ.get("HEATSIGNAL_EVAL_DB", DEFAULT_DB_PATH))
    parser.add_argument("--list", action="store_true", help="listar estrategias registradas")
    args = parser.parse_args(argv)
    if not args.file and not args.list:
        parser.error("indicar un archivo JSON o --list")

    db_path = Path(args.db)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    registry = StrategyRegistry(db_path)
    try:
        if args.file:
            data = json.loads(Path(args.file).read_text(encoding="utf-8"))
            for forbidden in ("strategy_id", "registered_at", "status"):
                if forbidden in data:
                    parser.error(f"{forbidden!r} lo asigna el sistema, sacarlo del JSON")
            strategy = StrategyHypothesis(**data)
            if "horizon_s" not in strategy.exit_conditions:
                print("aviso: exit_conditions sin 'horizon_s' -> se usara el horizonte por defecto (4 h)",
                      file=sys.stderr)
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
