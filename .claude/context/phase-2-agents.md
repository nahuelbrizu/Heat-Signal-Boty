# Contexto: Fase 2 — Implementación de Agentes

> Cargar junto con `agents.md` al implementar los nodos del grafo LangGraph.

## Orden de implementación sugerido
1. Esqueleto del grafo LangGraph con checkpoint resume por par (SQLite).
2. Los 3 analistas paralelos (Liquidaciones, Order Flow, Contexto de Mercado).
3. El par de debate (Alcista/Bajista) con `max_debate_rounds` configurable.
4. Estratega/Sintetizador con salida Pydantic y `strategy_id` versionado.
5. Redactor (dejar Evaluador para Fase 3).

## Reglas de implementación
- Agentes de decisión → `llm.with_structured_output(Schema)`, nunca parseo de texto libre.
- **Batchear**: un call con schema estructurado para N pares, no 1 call por agente por par. El costo no
  debe escalar linealmente con la cobertura (15-20 pares × 6 agentes reventaría el presupuesto).
- Usar modelos económicos para tareas repetitivas (analistas); reservar el modelo caro para síntesis/juicio.
- Cada agente: prompt propio, tool access propio, acceso a su porción del estado del grafo.
- El Estratega NO inventa variantes de estrategia nuevas sin versionar en `strategy_id`.

## Schema de señal (mínimo)
- `strategy_id` (versionado), `pair`, `timestamp_utc`, `hypothesis`, `direction`,
  `confidence_score`, `atr_normalized_trigger`, `outcome` (nullable hasta resolución),
  `pnl_simulated_pct`, `benchmark_result`, `run_status`.
- Guardar **todas** las hipótesis, incluidos fallos (anti-p-hacking).

## Salida de Fase 2
Grafo que produce hipótesis estructuradas y las persiste (pendientes), sin publicar aún ningún número.

## Estado (2026-10-17)
Grafo implementado (detalle en `agents.md`) y **cableado end-to-end**: `pipeline/analysis.py`
(`AnalysisLoop`) corre dentro del proceso de ingesta cada `HEATSIGNAL_ANALYSIS_INTERVAL_S` (900 s):
ventana en memoria -> `build_feature_set` -> grafo -> `PendingHypothesisStore` (SQLite). Cada ciclo
termina en un estado explícito: `warmup` (ventana incompleta tras reinicio, no se llama al LLM),
`no_strategies`, `no_llm` (sin `ANTHROPIC_API_KEY`), `ran` o `error`. Solo se ofrecen al Sintetizador
estrategias no `evaluated`. **Inactivo en la VM** hasta que haya API key y estrategias pre-registradas
(`estado.md`). Sin validar contra la API real.
