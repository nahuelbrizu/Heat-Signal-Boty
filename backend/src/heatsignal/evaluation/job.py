"""
Job de evaluacion (Fase 3): `python -m heatsignal.evaluation.job`.

1. Resuelve las hipotesis vencidas con una `PriceSource` (default: klines REST de
   Binance USDS-M, ver `price_source.py`).
2. Evalua TODAS las estrategias pre-registradas (Evaluador + calibracion + shadow),
   logueando cada evaluacion en `HypothesisLog` y el reporte completo en `ReportLog`.
3. Imprime un JSON con el resumen de resolucion y los reportes.

Por defecto registry, pendientes, log y reportes viven en el mismo archivo SQLite
(tablas distintas): `--db` o la variable de entorno `HEATSIGNAL_EVAL_DB`.
Pensado para correr periodicamente (cron / scheduler), es idempotente: una
hipotesis ya resuelta no se vuelve a resolver.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .hypothesis_log import HypothesisLog
from .pending_store import PendingHypothesisStore
from .price_source import BinanceKlinePriceSource, PriceSource
from .report import (
    DEFAULT_MAX_EXPIRED_RATE,
    DEFAULT_SHADOW_PERIOD,
    ReportLog,
    StrategyReporter,
)
from .resolver import DEFAULT_COST_BPS, DEFAULT_NO_DATA_GRACE, Resolver
from .strategy_registry import StrategyRegistry

DEFAULT_DB_PATH = "data/heatsignal_eval.sqlite"


@dataclass
class JobConfig:
    cost_bps: float = DEFAULT_COST_BPS
    no_data_grace: timedelta = DEFAULT_NO_DATA_GRACE
    shadow_period: timedelta = DEFAULT_SHADOW_PERIOD
    max_expired_rate: float = DEFAULT_MAX_EXPIRED_RATE


def run_once(
    *,
    registry: StrategyRegistry,
    store: PendingHypothesisStore,
    hypothesis_log: HypothesisLog,
    report_log: ReportLog | None,
    price_source: PriceSource,
    config: JobConfig | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Una corrida completa: resolver vencidas y evaluar todas las estrategias."""
    config = config or JobConfig()
    now = now or datetime.now(timezone.utc)
    resolver = Resolver(
        store, price_source, cost_bps=config.cost_bps, no_data_grace=config.no_data_grace
    )
    summary = resolver.resolve_due(now)
    reporter = StrategyReporter(
        store,
        registry,
        hypothesis_log,
        report_log=report_log,
        shadow_period=config.shadow_period,
        max_expired_rate=config.max_expired_rate,
    )
    reports = reporter.evaluate_all(now)
    return {
        "ran_at": now.isoformat(),
        "resolution": summary.model_dump(mode="json"),
        "pending_count": store.pending_count(),
        "reports": [r.model_dump(mode="json") for r in reports],
        "public": [r.public_view() for r in reports],
    }


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="python -m heatsignal.evaluation.job", description=__doc__.split("\n\n")[0])
    parser.add_argument("--db", default=os.environ.get("HEATSIGNAL_EVAL_DB", DEFAULT_DB_PATH))
    parser.add_argument("--cost-bps", type=float, default=DEFAULT_COST_BPS)
    parser.add_argument("--shadow-days", type=float, default=DEFAULT_SHADOW_PERIOD.total_seconds() / 86400)
    parser.add_argument("--max-expired-rate", type=float, default=DEFAULT_MAX_EXPIRED_RATE)
    parser.add_argument("--no-data-grace-min", type=float, default=DEFAULT_NO_DATA_GRACE.total_seconds() / 60)
    parser.add_argument("--interval", default="1m", help="intervalo de kline de Binance")
    parser.add_argument("--max-staleness-s", type=float, default=120.0)
    parser.add_argument("--price-kind", choices=["last", "mark"], default="mark")
    parser.add_argument("--public-only", action="store_true", help="imprimir solo la vista publica")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    db_path = Path(args.db)
    db_path.parent.mkdir(parents=True, exist_ok=True)

    registry = StrategyRegistry(db_path)
    store = PendingHypothesisStore(db_path, registry)
    hypothesis_log = HypothesisLog(db_path)
    report_log = ReportLog(db_path)
    price_source = BinanceKlinePriceSource(
        interval=args.interval,
        max_staleness=timedelta(seconds=args.max_staleness_s),
        price_kind=args.price_kind,
    )
    config = JobConfig(
        cost_bps=args.cost_bps,
        no_data_grace=timedelta(minutes=args.no_data_grace_min),
        shadow_period=timedelta(days=args.shadow_days),
        max_expired_rate=args.max_expired_rate,
    )
    try:
        result = run_once(
            registry=registry,
            store=store,
            hypothesis_log=hypothesis_log,
            report_log=report_log,
            price_source=price_source,
            config=config,
        )
    finally:
        for closable in (report_log, hypothesis_log, store, registry):
            closable.close()

    output = {"ran_at": result["ran_at"], "public": result["public"]} if args.public_only else result
    print(json.dumps(output, indent=2, default=str))
    # Exit code != 0 si hubo errores transitorios de la fuente: que el scheduler lo vea.
    return 1 if result["resolution"]["errors"] else 0


if __name__ == "__main__":
    sys.exit(main())
