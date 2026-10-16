# Contexto: Fase 3 — Evaluación, Benchmark y Modo Shadow

> Cargar al construir el Evaluador/Juez. Depende del Bloqueante C de Fase 0 (benchmark definido).
> Esta es la fase que hace que el win-rate signifique algo. No saltearla.

## Evaluador/Juez — qué calcula
1. **Resolución de pendientes.** Toma las hipótesis pendientes cuya ventana ya venció y les asigna
   `outcome` + `pnl_simulated_pct` con el retorno realizado. Patrón heredado de TradingAgents.
2. **Alfa vs benchmark pasivo.** Cada resultado se compara contra el benchmark definido en Fase 0.
   El win-rate crudo solo no cuenta; lo que importa es el edge sobre el baseline.
3. **Significancia.** n (tamaño de muestra) e intervalo de confianza por `strategy_id`. Un 70% con n=6
   no es distinguible de ruido — marcarlo como tal.
4. **Calibración.** Brier score y curvas de calibración por bucket de confidence. Objetivo: que el 72% de
   las señales con confidence≈0.72 efectivamente acierten. Sin esto el confidence_score es decorativo.

## Modo shadow (obligatorio antes de publicar)
- Correr 2-4 semanas en modo shadow (sin exponer números al público) para calibrar umbrales (ventanas de
  tiempo, normalización por ATR) con datos reales.
- Recién después de shadow un usuario ve un win-rate.

## KPIs (no solo win-rate)
- Win-rate calibrado + alfa vs benchmark.
- PnL simulado promedio y drawdown máximo simulado (en trading apalancado la magnitud importa más que la
  frecuencia).
- Brier score (calidad de calibración).
- Uptime por stream de ingesta.

## Salida de Fase 3
Ranking con números que un usuario (o un regulador) puede cuestionar y que resisten el escrutinio.

## Estado de implementacion (2026-10-17)
Implementado en `backend/src/heatsignal/evaluation/` (107 tests en `test_pending_store.py`,
`test_resolver.py`, `test_calibration.py`, `test_report.py`):
- `pending_store.py` — `PendingHypothesisStore` SQLite append-only (triggers bloquean UPDATE/DELETE en
  `pending_hypotheses` y `hypothesis_resolutions`). Rechaza `strategy_id` no pre-registrado. Una
  resolucion por hipotesis (UNIQUE), no antes de `created_at + horizon_s`. `add()` idempotente sobre
  (run_id, symbol, strategy_id) si hay run_id (re-ejecucion por checkpoint = no-op logueado).
  Cumple el protocolo `agents.hypothesis_store.PendingHypothesisStore`.
- `price_source.py` — protocolo `PriceSource.price_at(symbol, ts) -> Decimal | None` (precio en o antes de
  ts, nunca despues; None = sin dato confiable/stale; `PriceSourceError` = fallo transitorio, queda
  pendiente). `BinanceKlinePriceSource`: `GET fapi.binance.com/fapi/v1/klines?symbol&interval=1m&endTime&limit=1`
  (o `markPriceKlines`), usa el OPEN de la vela que contiene ts; staleness default 2 min.
- `resolver.py` — LONG `(exit-entry)/entry*100`, SHORT el negativo (lineal), neto = bruto - cost_bps/100.
  Default 10 bps round-trip (taker 5 bps x 2; sin slippage ni funding). win = neto > 0. Alfa por
  resolucion = neto - buy-and-hold (long sin costos, misma ventana). Sin precio tras 10 min de gracia ->
  `expired_no_data` (registrada, nunca descartada).
- `calibration.py` — Brier, Brier de referencia (tasa base) y skill, buckets de igual ancho con Wilson,
  ECE/MCE. n=0 -> None (no 0).
- `report.py` — `StrategyReporter`: Evaluador + calibracion + alfa + conteos. `publishable` solo si
  paso el shadow (default 14 dias desde la primera hipotesis de la estrategia) Y el Evaluador aprobo Y
  `expired_no_data` <= 20% de las vencidas. `public_view()` no expone win-rate/PnL/Brier si no es
  publicable. Cada evaluacion se loguea en `HypothesisLog`; el reporte completo en `ReportLog` (append-only).
- `job.py` — `python -m heatsignal.evaluation.job [--db ...]`: resuelve vencidas y evalua todas las
  estrategias; exit code 1 si hubo errores transitorios de la fuente.

Pendiente: baseline momentum del Evaluador recibe precios de entrada de senales consecutivas (mezcla
simbolos si la estrategia opera varios); exponer reportes en la API (Fase 4); `price_kind` default
`last` vs entrada en mark price.
