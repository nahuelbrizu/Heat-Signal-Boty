# Contexto: Arquitectura de Agentes

> Cargar cuando se trabaje sobre cualquier agente o el grafo LangGraph.
> Patrón adaptado de TauricResearch/TradingAgents (LangGraph, salida estructurada, log de decisiones
> con reflexión resuelta por resultado). Adaptamos el esqueleto del grafo; cambiamos el contenido de los
> nodos porque nuestro dominio es microestructura de derivados, no equities.

## Grafo (pipeline secuencial con debate)

```
[Ingesta: WS + REST]  ← servicios, fuera del grafo LLM
        │
        ▼
┌─────────────────────────────────────────┐
│  ANALISTAS (paralelo)                     │
│  • Analista de Liquidaciones              │
│  • Analista de Order Flow                 │
│  • Contexto de Mercado                    │
└─────────────────────────────────────────┘
        │
        ▼
┌─────────────────────────────────────────┐
│  DEBATE (bull vs bear, N rondas)          │
│  • Estratega-Alcista                      │
│  • Estratega-Bajista                      │
│    (max_debate_rounds configurable)       │
└─────────────────────────────────────────┘
        │
        ▼
   [Estratega/Sintetizador]  → emite hipótesis con strategy_id versionado (salida Pydantic)
        │
        ▼
   [Evaluador/Juez]  → benchmark pasivo, n, IC, calibración, reflexión resuelta por resultado
        │
        ▼
   [Redactor]  → reporte con lenguaje histórico-no-predictivo
```

## Los agentes

### Capa de analistas (paralelo)
- **Analista de Liquidaciones** — interpreta clusters del heatmap **normalizados por ATR/volatilidad**
  del activo (nunca umbral plano de 0.5%). Fuente: liquidaciones crudas normalizadas de los WS de
  exchanges + el modelo de heatmap propio (`heatmap_model_id` versionado), no un heatmap de terceros.
- **Analista de Order Flow** — lee buffers agregados en ventanas de los streams WS de trades spot/futures.
  Detecta desequilibrios comprador/vendedor. Aporte de dominio propio (sin equivalente en TradingAgents).
- **Contexto de Mercado** — régimen actual (tendencia/rango) desde el ticker snapshot (funding, OI).
  Define el régimen para que el benchmark pasivo tenga sentido.

### Capa de debate (dialéctica anti-sesgo)
- **Estratega-Alcista / Estratega-Bajista** — argumentan sobre las mismas señales antes de sintetizar.
  Reduce el sesgo direccional (el agente que solo emite "long" en bull market). `max_debate_rounds`
  da la palanca costo/profundidad.

### Capa de síntesis y juicio
- **Estratega/Sintetizador** — emite la hipótesis final con `strategy_id` **versionado y pre-registrado**,
  en salida estructurada Pydantic (`with_structured_output`).
- **Evaluador/Juez** — compara contra benchmark pasivo, calcula significancia (n, IC) y calibración.
  Adopta el patrón de "resolver pendientes con retorno realizado", reemplazando alfa-vs-SPY por
  **alfa-vs-benchmark-pasivo-cripto**.
- **Redactor** — reporte con guardrails de lenguaje histórico-no-predictivo.

## Mecanismos heredados de TradingAgents (adoptar tal cual)

1. **Salida estructurada Pydantic** en los agentes de decisión (Sintetizador, Evaluador, Redactor)
   vía `llm.with_structured_output(Schema)`. Nada de parsear texto libre.
2. **Log de decisiones persistente con reflexión.** Cada corrida guarda su decisión; la siguiente corrida
   del mismo par resuelve las pendientes con el retorno realizado + alfa vs benchmark + reflexión.
   **Las pendientes nunca se podan** (esto es el anti-p-hacking).
3. **Checkpoint resume por nodo (SQLite por par).** Una corrida interrumpida se reanuda desde el último
   nodo exitoso. Resuelve idempotencia del pipeline.
4. **Contrato de datos verificado.** La lista de fuentes es la cadena de resolución exacta (sin fallback
   silencioso); los fallos salen por una taxonomía de error tipada; datos viejos se rechazan, no se
   reportan como actuales.

## Estado de implementación (Fase 2)

Código en `backend/src/heatsignal/agents/` (`graph.py` tiene el diagrama). Desvíos/precisiones respecto
del diseño de arriba:
- El **Evaluador/Juez no es un nodo de este grafo**: el Sintetizador persiste `PendingHypothesis` vía el
  protocolo `PendingHypothesisStore.add()` y la Fase 3 (`evaluation/`) las resuelve fuera del grafo.
  Orden real: analistas (paralelo) → debate (N rondas) → sintetizador → redactor.
- Los analistas leen `FeatureSet` (`schema/features.py`), no eventos crudos. Los números
  (notional, imbalance, régimen, precio de entrada) los pone el código; el LLM interpreta.
- El Sintetizador solo elige entre `allowed_strategies` (pre-registradas); cualquier `strategy_id`
  desconocido se rechaza y loguea en código. `entry_reference_price` = `context.mark_price`.
- El Redactor tiene un guardrail en código (`guardrails.py`): 1 reintento con feedback y, si vuelve a
  fallar, el reporte queda `guardrail_failed` (no publicable).
- Checkpoint: `thread_id = "{símbolos ordenados}:{run_id}"`, `AsyncSqliteSaver` (`checkpoint.py`).
- Ruteo de modelos en `llm_factory.py` (proveedor: decisión abierta, ver `decisions.md`).

## Lo que NO copiamos de TradingAgents
- Su capa de datos (yfinance/Alpha Vantage por fecha) es point-in-time sobre equities. No sirve.
- Sus analistas de fundamentals/earnings/Reddit — no aplican a derivados cripto.

## Contexto multi-temporalidad y sesiones (2026-10-18)

- `FeatureSet.htf` (`features/htf.py`, `htf-trend-v1`): últimas 48 velas de 1h CERRADAS, día UTC (apertura, máx/mín,
  cambio en % y en ATR(1h), posición en el rango), EMA20/SMA50 de 1h y pendiente, EMA10/SMA20 diarias, ATR14 1h,
  `daily_trend` (regla fija, la pone el CÓDIGO) y `setups` (banderas por código). Los huecos van a `data_gaps`.
- `FeatureSet.session` (`features/sessions.py`, `session-v1`): último análisis de sesión del día para ese símbolo.
- Estado del grafo: `session` (sesión de la corrida) y `day_journal` (diario del día: sesiones previas, código + Redactor).
- Prompts: el Analista de Contexto recibe el bloque de 1h (6 velas compactas, no las 48) y la última sesión; el
  digest que leen el debate, el Sintetizador y el Redactor suma una línea de tendencia por símbolo y el diario del día
  al principio. **Mismas llamadas por ciclo** (test `test_budget_session_context_adds_no_llm_calls`).
- Sintetizador: estrategias con `exit_conditions.timeframe = "1h"` → horizonte alineado al cierre de vela
  (`evaluation/timeframes.py`) y entrada verificada por código (`htf_setup`, `daily_trend_in`, `alignment`, `sessions`).
  Toda hipótesis nueva registra `daily_trend`, `trend_alignment`, `htf_snapshot` y la sesión de la que salió.
- `entry_conditions.scope = "rotation"`: la estrategia aplica a los símbolos que el ciclo ya analiza y no obliga a
  mandar todos al grafo.
- Modo `sessions` (`pipeline/analysis.py`): el grafo corre al cerrar la 1ª vela de 1h de Asia/Europa/EE.UU. y la última
  del día UTC (4 corridas/día, todos los símbolos). Sin LLM o sin estrategias, el análisis por código se guarda igual y
  la fila `llm` del diario queda `sin_llm`.
