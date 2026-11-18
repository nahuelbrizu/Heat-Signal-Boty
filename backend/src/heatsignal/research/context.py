"""
Contexto que recibe el agente de investigacion semanal (/mnt/session/uploads/contexto.md).

Se arma por codigo desde lab_summary.json (que se probo, por familia, y si paso los criterios del laboratorio) mas
un bloque fijo con los resultados del programa, los datos disponibles y las reglas. Asi el agente no propone lo que
ya se probo. Se vuelve a subir solo cuando cambia (ver managed.sync).
"""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from pathlib import Path
from typing import Any

FIXED = """\
## Resultados del programa (resumen)

- Laboratorio: 123 pruebas (lotes 1 a 6 + short). Ninguna pasa el listón del programa (bootstrap Romano-Wolf con
  todos los intentos contados; mejor p = 0,145). "Pasa" en la tabla de abajo es el criterio propio del laboratorio,
  más flojo que el listón del programa.
- Patrón consistente: tendencia LONG (rupturas de 10 a 55 días, momentum de series de tiempo de 28 días) con Sharpe
  de 1 a 1,3 y robusta a costos; las 12 rupturas short, todas con Sharpe negativo.
- Holdouts abiertos (donchian-ensemble-v1, max-ruptura-10d-v1, cartera-tendencia-v1): ninguno confirmó.
- Meta-modelo (meta-labeling con triple barrera y validación purgada, logística y LightGBM): sin señal
  filtrable (AUC <= 0,53).
- Prueba hacia adelante en paper desde 2026-10-20 (rupturas long, short y neta), con un hueco de datos del
  2026-10-25 al 2026-11-18.

## Datos disponibles

- Velas de 1 minuto de perpetuos USDT de Binance desde 2021 para BTC, SOL, XRP, ETH, XLM, BNB y TRX.
- Funding liquidado de Binance (histórico) y open interest (Bybit y OKX en vivo; Binance por REST cada 5 min).
- En vivo desde octubre de 2026, de Binance, Bybit, OKX y Hyperliquid: trades, liquidaciones, tickers y
  agregados por minuto (pocas semanas de historia: no alcanza para un backtest largo).
- Macro point-in-time de FRED por fecha de publicación: DFF, CPIAUCSL, DGS10, DTWEXBGS, VIXCLS. Calendario de
  FOMC, CPI y NFP. Algunos mercados de Polymarket.
- Noticias por RSS (cripto y generalistas), solo titulares.
- No hay: libro de órdenes histórico, datos on-chain, opciones, spot de otros exchanges, ni otras criptos.

## Reglas que tiene que respetar toda propuesta

- Pre-registro: la regla, los parámetros y el criterio de éxito se fijan ANTES de ver resultados, con un
  strategy_id versionado. Cada corrida cuenta como intento (corrección por comparaciones múltiples).
- No se ajustan parámetros de estrategias que fallaron; una variante nueva necesita una razón previa y otra versión.
- Toda estrategia se compara contra una canasta pasiva de las 7 criptos y contra entradas al azar.
- Costos supuestos: 5 bps de comisión y 2 bps de slippage por lado, más funding real.
"""


def build(summary: dict[str, Any] | None) -> str:
    """Texto del contexto. `summary` es lab_summary.json (o None si todavia no hay laboratorio publicado)."""
    lines = ["# Contexto de HeatSignal para la investigación semanal", ""]
    if summary:
        fams: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for s in summary.get("strategies") or []:
            fams[s.get("family") or "sin familia"].append(s)
        lines += ["## Familias ya probadas en el laboratorio", "",
                  f"Generado desde lab_summary.json ({summary.get('generated_at') or 'sin fecha'}).", "",
                  "| Familia | Probadas | Pasan el criterio del lab | Estrategias |", "|---|---|---|---|"]
        for fam in sorted(fams):
            items = sorted(fams[fam], key=lambda s: s.get("strategy_id") or "")
            ok = sum(1 for s in items if (s.get("verdict") or {}).get("pasa"))
            ids = ", ".join(s.get("strategy_id") or "?" for s in items)
            lines.append(f"| {fam} | {len(items)} | {ok} | {ids} |")
        lines.append("")
    else:
        lines += ["## Familias ya probadas en el laboratorio", "", "(todavía no hay resumen del laboratorio publicado)", ""]
    return "\n".join(lines) + FIXED


def load_summary(path: Path) -> dict[str, Any] | None:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()
