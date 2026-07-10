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

## Lo que NO copiamos de TradingAgents
- Su capa de datos (yfinance/Alpha Vantage por fecha) es point-in-time sobre equities. No sirve.
- Sus analistas de fundamentals/earnings/Reddit — no aplican a derivados cripto.
