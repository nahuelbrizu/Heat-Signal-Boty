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

## Contexto macro y diario de lecciones (2026-10-19)

- **Bloque "Macro y eventos"** (`macro/context.py`, `macro-context-v2`, con PAYEMS): el ciclo (`pipeline/analysis.py`, parámetro
  `macro`) lo arma point-in-time y lo pone en `state["macro_context"]`; SOLO el Analista de Contexto de Mercado lo
  recibe, una vez, al final de su mensaje. **Mismas llamadas por ciclo** (test). Tope `MAX_BLOCK_CHARS` = 1150
  (~320 tokens medidos con datos reales; estimación conservadora 3,2 car./token). Contiene: FRED (DFF, CPIAUCSL,
  DGS10, DTWEXBGS, VIXCLS) con fecha de publicación, eventos de las próximas 48 h (FOMC/CPI/NFP) y probabilidades de
  Polymarket con su variación de 24 h. Apagable con `HEATSIGNAL_MACRO=0`.
- Adaptado de TradingAgents (Apache-2.0): vintage de FRED por `realtime_start`, parseo de Gamma. Diferencia: acá se
  guarda append-only y el grafo no llama herramientas (lo arma el código).
- **Diario de lecciones** (`evaluation/lessons.py`, inspirado en `memory/reflection.py`): una lección por hipótesis
  resuelta, escrita por el job de evaluación con el LLM barato (o por código, marcada). **No se reinyecta**: a
  diferencia de TradingAgents, ninguna lección entra a ningún prompt ni decisión (test `test_lessons.py` que lo verifica
  estática y dinámicamente). Motivo: cambiaría el sistema sin pre-registro ni conteo de intentos.

## Estrategias de días laterales (2026-10-19)

- Spec: `decisions.md`, "3 estrategias del grafo para días laterales". JSON en `backend/strategies/lateralidad-1h/`
  (family `1h-lateralidad`, `daily_trend_in: ["lateral"]`, `alignment: "lateral"`, `levels_model_id: htf-levels-v1`).
- Niveles: `HtfContext.levels` (`features/htf.py`, `htf-levels-v1`, versión aparte para NO tocar `htf-trend-v1`, con
  la que están registradas las 5 de tendencia): rango de Asia (8 velas 1h de 00–08 UTC), máx/mín del día UTC
  anterior, VWAP del día (precio típico × volumen de velas 1m, sumado por hora en `history/hourly.py`:
  `Candle1h.vwap_pv/vwap_vol`; una hora sin 1m → `sin_datos`), ADX14 1h de Wilder, mechas y rango de la última vela.
  Las banderas (`falsa_ruptura_asia`, `rechazo_dia_anterior`, `extremo_vwap`, con la dirección) solo se calculan en
  día lateral; si se cumplen LONG y SHORT a la vez no hay bandera.
- Entrada por código (`evaluation/timeframes._check_lateral`): día lateral, versión de niveles, sesión de la CORRIDA
  (solo la falsa ruptura: europa/eeuu), contexto de la última vela recién cerrada, bandera presente y misma dirección.
- Tope: `entry_conditions.max_per_symbol_session` (1) por (family, símbolo, sesión del día) en el Sintetizador,
  contando lo ya registrado en la base (`PendingHypothesisStore.session_hypotheses`) más la corrida. Sin sesión de
  corrida → rechazo. Mismas llamadas LLM por ciclo.
- Prompts: el Analista de Contexto ve una línea "niveles (código …)" por símbolo (~70 tokens); el digest suma
  "lateral (código): …" solo en días laterales; +~85 tokens de sistema en el Sintetizador y ~50 en el Analista.
- Panel: Moneda/contexto 1h dibuja Asia, ayer y VWAP sobre las velas de 1h y muestra los niveles; Hipótesis filtra
  por `family`.
- Ojo con el horario: en BST (hasta el 25-10) Londres abre 07:00 UTC y la corrida de Europa es a las 08:00 UTC con la
  vela 07–08 (dentro del rango de Asia): la falsa ruptura no puede darse en esa corrida, solo en la de EE.UU. (o en
  Europa desde el horario de invierno, 09:00 UTC).

## CIO y CRO — graph-v2-cio (2026-10-21)

- Orden nuevo: analistas → debate (N rondas) → **CIO** (`agents/cio.py`, LLM fuerte) → **CRO** (`agents/cro.py`, sin LLM)
  → **registro** (`synthesizer.make_registrar_node`, código) → redactor. El CIO reemplaza la llamada LLM del Sintetizador:
  **mismas llamadas por ciclo** (tests `test_cio.py::test_llm_calls_per_cycle_unchanged…` y `test_budget_session_context…`).
- CIO: una llamada batcheada (`CioBatch`). Recibe digest de analistas, transcript y, por símbolo, los **candidatos
  verificados por código** (`code_candidates`: estrategia activa + dirección cuya entrada `timeframes.check_htf_entry` da
  por cumplida y cuyo horizonte entra; sin lo que ya tiene hipótesis abierta). Devuelve postura (alcista/bajista/empate),
  `peso` 0–1, tomar candidato o no operar, 2–4 razones. Código (`validate_verdicts`) rechaza: setup fuera de los
  candidatos, dirección distinta del candidato, dirección contraria a la postura, empate con "tomar", peso fuera de [0,1].
- CRO (reglas fijas, en orden): hueco REAL (`features/gaps.py`), FOMC/CPI/NFP en −1 h…+2 h (`macro/calendar.py`; calendario
  ilegible → veto), tope por sesión (`max_per_symbol_session`), exposición correlacionada (> 2 en la misma dirección en
  altcoins contando abiertas + emitidas en la sesión + nuevas; las nuevas por `peso` descendente).
- Registro: `apply_proposals` (defensa en profundidad), `store.add` con `graph_version="graph-v2-cio"` (columna aditiva en
  `hypothesis_context`), `confidence_score` = `peso`. Escribe `cio_verdicts` (append-only) en CADA ciclo, se opere o no.
- Evaluación (job, `evaluation/cio_verdicts.py`, tabla `cio_verdict_outcomes`): postura vs retorno 4 h con velas 1h
  cerradas a mark, acierto + IC de Wilson y Brier del `peso`. Descriptivo; no vuelve al grafo.
- Panel: tabla "Veredicto del CIO" en el detalle de `#/ciclos`, tarjeta "¿Acierta el CIO?" en `#/analisis`
  (`static/app/cio-ui.js`, `/api/cio/accuracy`, `/api/cio/verdicts/{run_id}`, `cio_verdicts` en `/api/cycles/{run_id}`).
