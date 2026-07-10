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
