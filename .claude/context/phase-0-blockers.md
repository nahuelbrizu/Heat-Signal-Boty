# Contexto: Fase 0 — Bloqueantes (antes de codear)

> Cargar al arrancar el proyecto. Ningún código de producto empieza hasta cerrar estos tres bloqueantes.

## Bloqueante A — Términos de uso de datos (MITIGADO al cambiar a WS nativos)
**Estado: mayormente resuelto por decisión de arquitectura.** Al pasar la fuente primaria a los WS
públicos de los exchanges (Binance/Bybit/OKX) en vez de un agregador pago, desaparece la cláusula de
redistribución comercial que tenía CoinGlass (uso comercial = tier Standard ~299 USD/mes). Los datos
públicos de mercado de los exchanges no tienen esa restricción.
- [ ] Lectura rápida de los términos de uso de la API de cada exchange (Binance/Bybit/OKX) — riesgo menor,
      pero confirmar que no exigen atribución o prohíben algún uso puntual.
- [ ] Si a futuro se ofrece CoinGlass como fuente **BYOK opcional**, el usuario usa su propia clave y su
      propio plan → la responsabilidad de licencia recae en él, no en HeatSignal.
- Ya NO es un bloqueante duro para el dashboard público.

## Bloqueante B — Diseño de la capa de ingesta (WS nativos)
**Estado: implementado 2026-10-17.** Fuente = WS públicos de Binance + Bybit + OKX. Detalle en
`phase-1-ingestion.md`; corre 24/7 en la VM de prueba (ver `estado.md`).
- [x] Endpoints verificados contra doc oficial de cada exchange (liquidaciones/trades/ticker).
- [x] Decisión: **1 worker por exchange** (multiplexa canales), no 1 por stream.
- [x] Reconexión con backoff+jitter, replay de suscripciones al reconectar, heartbeat por exchange y
      watchdog de socket zombie. El "checkpointing" de la ingesta no aplica en modo memory (cada reinicio
      vacía la ventana y se declara como calentamiento).
- [x] Adaptadores de normalización por exchange a esquema común UTC (`schema/events.py`).
- ⚠️ Cobertura de liquidaciones no uniforme: solo `allLiquidation` de Bybit es FULL; Binance (`forceOrder`,
      la última por símbolo cada 1000ms) y OKX son PARTIAL. Marcado por registro en `coverage`.

## Bloqueante C — Baseline/benchmark del win-rate (RESUELTO)
**Estado: cerrado 2026-07-10.** Implementado en `backend/src/heatsignal/evaluation/`
(`strategy_registry.py`, `baseline.py`, `evaluator.py`, `hypothesis_log.py`), 46 tests pasando. Sin
baseline, el win-rate no significa nada (si BTC sube el 65% de los días, un agente "long-only" supera el
50% sin edge real) — ya no es el caso: todo pasa por el Evaluador antes de reportarse.
- [x] Definir un **benchmark pasivo**: `baseline.py` — `RandomBaseline`, `AlwaysLongBaseline`,
      `MomentumBaseline`, todos reproducibles por seed.
- [x] Definir cómo se calcula n (tamaño de muestra) e intervalo de confianza: `evaluator.py` —
      `wilson_ci()` (IC de Wilson 95%), `n>=30` como mínimo (`Evaluator.PASS_MIN_N`), significancia vía
      test binomial (scipy) contra H0: win_rate=0.5.
- [x] Pre-registrar qué estrategias/reglas usa cada agente (con `strategy_id`) **antes** de ver
      resultados: `strategy_registry.py` — `StrategyRegistry`, pre-registro inmutable sobre SQLite
      append-only (triggers a nivel de motor bloquean `UPDATE`/`DELETE`, no es solo convención).
- [x] Reemplazar el umbral plano de 0.5% por normalización por ATR/volatilidad del activo — **no es del
      Evaluador**: vive en la capa de features. ATR Wilder en `features/volatility.py`; el heatmap
      `liqcluster-v1` usa bin = 0.25 × ATR y expone `distance_atr` por cluster (fallback % del mark solo sin
      ATR, declarado en `data_gaps`).
- Criterio de aprobación implementado: `n>=30 AND p_value<0.05 AND wilson_ci[0]>0.50 AND sharpe>1.0 AND`
  superar los 3 baselines en un criterio **compuesto** (avg_return mayor + sharpe mayor + max_drawdown
  menor en valor absoluto — `_beats_baseline`, no alcanza con ganar en una sola métrica). `n<30` siempre
  resuelve en `passed=False, reason="insufficient_data"` — sin fallos silenciosos.
- El log de resultados (`hypothesis_log.py`) nunca pisa una evaluación previa: hipótesis fallidas quedan
  tan registradas como las exitosas (anti-p-hacking).

## Checklist de higiene (rápida, hacer también en Fase 0)
- [x] Gestión de secretos: `.env` fuera de git (`.env.example` versionado sin valores). La única clave
      del core es `ANTHROPIC_API_KEY`, que vive solo en el `.env` de la VM. Plan de rotación: pendiente.
- [x] Retención de datos crudos: resuelta por la decisión "datos de mercado solo en memoria" (no se
      guardan). Si se vuelve a modo `persistent`, la política está en `storage/timescale.py`.
- ~~Decidir plan de CoinGlass a contratar.~~ No aplica al core (ver `decisions.md`).

## Salida de Fase 0
Un documento corto que responda: (a) ¿se puede publicar contenido derivado? (b) diseño de ingesta WS
confirmado, (c) benchmark definido. Sin las tres, no se pasa a Fase 1.
