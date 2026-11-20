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


BLOCKS = """\
## Formato para proponer estrategias (estrategias.json)

Las estrategias se proponen SOLO combinando estos bloques (nadie escribe código). El sistema valida cada una,
la registra con un id que lleva el hash de la regla (cambiar un número = otra estrategia y otro intento) y la
manda al laboratorio. Lo que no respete el formato o los rangos se descarta con el motivo.

```json
[{"name": "rsi-tendencia-1h", "timeframe": "1h", "direction": "long",
  "symbols": ["BTCUSDT", "ETHUSDT"],
  "entry": {"type": "rsi", "period": 2, "low": 10, "high": 90},
  "filters": [{"type": "daily_trend", "ema": 50}],
  "exit": {"bars": 24, "stop_atr": 2.0, "target_atr": null},
  "description": "hipótesis en una línea + fuente [n]"}]
```

- name: 3-41 caracteres, minúsculas, números y guiones. symbols: opcional (default las 7).
- timeframe: "5min" | "15min" | "1h". direction: "long" | "short" | "both".
- entry (una):
  - breakout {n 5-500}: cierre = máximo de n cierres -> long; = mínimo -> short.
  - rsi {period 2-50, low 1-49, high 51-99}: RSI < low -> long; > high -> short.
  - ema_cross {fast 2-100 < slow 3-400}: cruce hacia arriba -> long; hacia abajo -> short.
  - vwap_dev {k 0,25-10}: cierre a más de k ATR por debajo del VWAP del día -> long; por encima -> short.
  - opening_range {session "asia"|"londres"|"ny", minutes múltiplo de la temporalidad, hasta 240}: primer cierre
    fuera del rango de apertura de la sesión ese día.
- filters (0 a 3, cada tipo una vez): daily_trend {ema 5-200} (long solo sobre la EMA diaria, short debajo);
  atr_pct {min, max} (ATR(14) en % del precio); session {name} (asia 00-08, londres 07-16, ny 13-21 UTC).
- exit: bars (obligatorio, hasta una semana) y opcionales stop_atr 0,25-20 / target_atr 0,25-40 (ATR(14) al entrar).
- Costos: ~14 bps ida y vuelta. En 5m y 15m el catálogo escrito a mano falló SIEMPRE por costos: preferí reglas
  que operen poco (filtros) y movimientos esperados grandes frente a 14 bps.
"""


def generated_section(generated: list[dict[str, Any]] | None, summary: dict[str, Any] | None) -> str:
    """Estrategias generadas ya registradas (para no repetirlas) con su resultado si el laboratorio ya las corrio."""
    if not generated:
        return "## Estrategias generadas ya registradas\n\n(ninguna todavía)\n"
    res = {s.get("strategy_id"): s for s in (summary or {}).get("strategies") or [] if isinstance(s, dict)}
    rows = ["## Estrategias generadas ya registradas (no repetir; una variante es otra estrategia y otro intento)", "",
            "| strategy_id | Regla | Origen | Sharpe OOS | Pasa |", "|---|---|---|---|---|"]
    for g in generated[-40:]:
        r = res.get(g["strategy_id"])
        sh = f"{r['sharpe']:.2f}" if r and isinstance(r.get("sharpe"), (int, float)) else "sin correr"
        ok = ("sí" if (r.get("verdict") or {}).get("pasa") else "no") if r else "-"
        rows.append(f"| {g['strategy_id']} | {g.get('describe', '')} | {g.get('origin', '')} | {sh} | {ok} |")
    return "\n".join(rows) + "\n"


def build(summary: dict[str, Any] | None, generated: list[dict[str, Any]] | None = None) -> str:
    """Texto del contexto. `summary` es lab_summary.json (o None si todavia no hay laboratorio publicado);
    `generated` = estrategias por bloques ya registradas ([{strategy_id, describe, origin}])."""
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
    return "\n".join(lines) + FIXED + "\n" + BLOCKS + "\n" + generated_section(generated, summary)


def load_generated(base: Path) -> list[dict[str, Any]]:
    """Registro de estrategias por bloques (data/lab_generated.jsonl) en el formato de `build`."""
    from ..lab import builder as B

    try:
        entries, _ = B.load_entries(base / "lab_generated.jsonl")
    except OSError:
        return []
    return [{"strategy_id": e["strategy_id"], "describe": B.describe(e["spec"]), "origin": e["origin"]} for e in entries]


def load_summary(path: Path) -> dict[str, Any] | None:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()
