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
**En diseño.** Fuente = WS públicos de Binance + Bybit + OKX. Diseño detallado en `phase-1-ingestion.md`.
- [x] Endpoints verificados contra doc oficial de cada exchange (liquidaciones/trades/ticker).
- [x] Decisión: **1 worker por exchange** (multiplexa canales), no 1 por stream.
- [ ] Implementar reconexión/backoff+jitter, replay de suscripciones, heartbeat, checkpointing.
- [ ] Implementar adaptadores de normalización por exchange (formatos distintos) a esquema común UTC.
- ⚠️ Cobertura de liquidaciones no uniforme: Binance y `allLiquidation` de Bybit dan todo; OKX y el Bybit
      viejo dan ≤1/seg por símbolo. Marcar granularidad/origen por registro.
- Corre 24/7 desde el MVP.

## Bloqueante C — Baseline/benchmark del win-rate
Sin baseline, el win-rate no significa nada (si BTC sube el 65% de los días, un agente "long-only" supera
el 50% sin edge real).
- [ ] Definir un **benchmark pasivo** (ej. estrategia direccional/aleatoria en el mismo período) contra el
      que se compara cada agente.
- [ ] Definir cómo se calcula n (tamaño de muestra) e intervalo de confianza en el ranking.
- [ ] Pre-registrar qué estrategias/reglas usa cada agente (con `strategy_id`) **antes** de ver resultados.
- [ ] Reemplazar el umbral plano de 0.5% por normalización por ATR/volatilidad del activo.
- **Definir esto antes de escribir el Agente Evaluador.**

## Checklist de higiene (rápida, hacer también en Fase 0)
- [ ] Gestión de secretos: API key de CoinGlass en `.env` (fuera de git), plan de rotación, dónde vive.
- [ ] Política de retención de `raw_snapshots` (tick-by-tick crece rápido — dimensionar antes de que el
      storage bill escale).
- [ ] Decidir plan de CoinGlass a contratar (ver `decisions.md`).

## Salida de Fase 0
Un documento corto que responda: (a) ¿se puede publicar contenido derivado? (b) diseño de ingesta WS
confirmado, (c) benchmark definido. Sin las tres, no se pasa a Fase 1.
