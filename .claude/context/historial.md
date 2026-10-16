# Contexto: Historial de sesiones

> Registro cronológico de avances, revisiones y decisiones tomadas sesión a sesión. Se actualiza al
> cierre de cada sesión de trabajo relevante (no cada mensaje). No reemplaza a `decisions.md` — ahí van
> las decisiones cerradas/abiertas en sí; acá va **cuándo y en qué contexto** se tocó cada cosa.

## Cómo usar este archivo
- Agregar una entrada nueva arriba de todo (orden descendente, más reciente primero).
- Cada entrada: fecha, qué se hizo, qué quedó pendiente/próximo paso.
- Si una sesión cierra un bloqueante o una decisión, referenciarlo (no duplicar el contenido).

## Entradas

### 2026-10-17
- Ola multi-agente (5 worktrees, mergeados en `ingestion-binance-bybit`): worker OKX (ctVal por REST,
  liquidaciones PARTIAL), persistencia (TimescaleSink, Fanout, Throttle, health, `ingestion/run.py`,
  compose), features (`liqcluster-v1`, ATR Wilder, order flow, `regime-v1`, `builder.py`), grafo LangGraph
  (analistas -> debate -> sintetizador -> redactor con guardrail) y evaluación Fase 3 (`pending_store`,
  resolver, calibración, reporte). Smoke test en vivo de los 3 exchanges sin reconexiones.
- Fixes: rutas nuevas de Binance Futures WS (`/market`, `/public`), `forceOrder` PARTIAL, lado de
  liquidación de Bybit invertido, precio de salida por mark.
- VM de prueba: GCP e2-small en asia-northeast1-a (Debian 13, 2 vCPU, 1.9 GB, disco 10 GB, sin swap).
  Los 3 exchanges responden desde Tokio.
- Decisión: datos de mercado solo en memoria (ver decisions.md). `MemoryWindowSink` +
  `HEATSIGNAL_STORAGE=memory` como default; unidad systemd en `deploy/`.
- Desplegado en la VM: swap 2 GB, venv (Python 3.13), servicio systemd `heatsignal-ingestion`.
- Cableado end-to-end: `pipeline/analysis.py` (AnalysisLoop) corre dentro del proceso de ingesta cada
  15 min: memoria -> `build_feature_set` -> grafo -> `PendingHypothesisStore`. Estados explicitos
  (`warmup`, `no_strategies`, `no_llm`, `ran`, `error`). Evaluacion por timer systemd cada 5 min.
  CLI `python -m heatsignal.evaluation.register_strategy` para pre-registrar estrategias.
- Pendiente: API key de Anthropic en el `.env` de la VM y estrategias pre-registradas por el usuario.
- Se actualizaron los estados de las fases y se creó `estado.md` (foto actual + lista de pendientes).

### 2026-10-17 — Worker de Bybit

- `ingestion/bybit_worker.py` (asyncio + `websockets`, sin conector de terceros): `allLiquidation` (FULL,
  lado invertido posición→orden), `publicTrade` (taker side + `is_block_trade`), `tickers` (merge
  snapshot+delta con cache por símbolo, limpiado en cada reconexión). Ping JSON cada 20s, timeout de
  mensajes como watchdog de socket zombie, backoff+jitter y re-suscripción en cada conexión.
  Canales verificados contra la doc oficial de Bybit V5; patrones tomados de NautilusTrader/Hummingbot.
- `TradeEvent.is_block_trade` (default False) agregado al esquema; `websockets>=12` explícito en
  `requirements.txt`.
- `test_bybit_worker.py`: 15 tests (payloads de la doc oficial). Total 96 pasando.
- Demo en vivo 40s (BTC/SOL): 2902 trades, 701 tickers, 1 liquidación, todo normalizado.
- El desfasaje `ts_ingest - ts_event` (~2s) es el reloj de WSL: +1.9s vs `fapi.binance.com/fapi/v1/time`.
  En producción hace falta NTP; no usar `ts_ingest_ms` para medir latencia hasta corregirlo.
- Pendiente: los tickers de Bybit llegan cada 100ms (~10/s por símbolo) — evaluar throttling antes de
  escribir a Redis/Timescale. Worker de OKX.

### 2026-10-17 — Binance Futures recibe datos reales: el "bloqueo de red" era la ruta legacy

- Revisando el feed de liquidaciones de Hummingbot (`data_feed/liquidations_feed/binance/constants.py`) se
  vio que usa `wss://fstream.binance.com/market/stream`. Binance separó los streams de Futures en
  `/market` (aggTrade, markPrice, forceOrder) y `/public` (depth, bookTicker); el path legacy `/ws`
  acepta el handshake y no manda nada. **El diagnóstico del 2026-07-11 (bloqueo de red) era incorrecto.**
- `binance_worker.py`: `stream_url=MARKET_STREAM_URL` (`wss://fstream.binance.com/market`). `_demo()` corrido
  45s contra Binance real: ~450 trades + tickers de BTC/SOL normalizados OK. Liquidaciones validadas con
  `!forceOrder@arr` en vivo (`_normalize` OK sobre payloads reales).
- **Corrección de cobertura:** `forceOrder` de Binance empuja solo la última liquidación por símbolo cada
  1000ms (docstring del conector oficial; coincide con el espaciado ~1s observado en vivo). Se pasó de
  `Coverage.FULL` a `Coverage.PARTIAL` en el worker, el test, `events.py`, `phase-0-blockers.md` y
  `phase-1-ingestion.md`. Solo Bybit `allLiquidation` es FULL.
- Revisado también el conector de Bybit de NautilusTrader: en `allLiquidation`, `S` es el lado de la
  **posición** liquidada (`Buy` = long liquidado), al revés que Binance (`S` = lado de la orden). Tenerlo
  en cuenta en el worker de Bybit. Hummingbot / NautilusTrader / Freqtrade quedan como referencia, no
  dependencia (Zennbot descartado: EA cerrado de MT5 forex).
- Observado: `ts_ingest_ms - ts_event_ms` ~1-2s; posible desfasaje del reloj de WSL, a revisar.
- 81 tests pasando.
- Próximo paso: worker de Bybit usando NautilusTrader/Hummingbot como referencia.

### 2026-07-11 — `_demo()` corrido contra Binance real: hallazgo de bloqueo de red al WS de Futures

- **Se corrió `binance_worker.py` de verdad** (`PYTHONPATH=src .venv/bin/python -m
  heatsignal.ingestion.binance_worker`, venv con `binance-futures-connector 4.2.0` ya instalado) contra
  `wss://fstream.binance.com` real, no simulado. Hasta esta sesión el archivo era "coherente por lectura"
  pero nunca se había ejecutado.
- **Resultado: la conexión y el ciclo de vida del worker funcionan correctamente, pero NUNCA llega ni un
  solo evento de datos** (`forceOrder`/`aggTrade`/`markPriceUpdate`) para BTCUSDT/SOLUSDT en 3 intentos de
  ~65s cada uno. El watchdog de socket zombie (`_watchdog`, `message_timeout_s=60s`) detectó correctamente
  la ausencia de mensajes, forzó reconexión, y el backoff+jitter también se comportó como se diseñó — esa
  parte del código quedó validada end-to-end contra infraestructura real.
- **Causa raíz: NO es un bug del código.** Se aisló con websockets crudos (sin el conector de Binance) en
  la misma sandbox:
  - `wss://fstream.binance.com/ws/btcusdt@aggTrade` (Binance **Futures** WS, el que usa este worker): el
    handshake HTTP completa (`on_open` dispara), pero **cero** frames de datos en ventanas de 10s, 15s y
    30s.
  - `wss://stream.binance.com:9443/ws/btcusdt@trade` (Binance **Spot** WS): 274 mensajes en 10s.
  - `wss://stream.bybit.com/v5/public/linear` (`publicTrade.BTCUSDT`): mensajes normales en 10s.
  - `wss://echo.websocket.org`: eco inmediato.
  - `https://fapi.binance.com/fapi/v1/ping` (REST de Futures): `200 OK`.
  - Conclusión: el egress de red de esta sandbox (o un filtro entre la sandbox y Binance) deja pasar el
    handshake WS y el tráfico REST de Futures, pero **descarta selectivamente el plano de datos del stream
    de Futures** (`fstream.binance.com`) — mientras que Spot, Bybit y un WS genérico andan sin problema.
    No se pudo determinar desde acá si el filtro está del lado de la red de la sandbox o es Binance
    limitando esta IP/rango específicamente para Futures WS; hace falta repetir la prueba desde una red sin
    esa restricción (máquina del usuario, VPS) para distinguirlo.
- **Impacto:** no se pudo confirmar en esta sesión que la normalización (`_normalize`) funcione contra
  payloads reales de Binance Futures — sigue siendo "coherente por lectura" en ese punto específico, aunque
  el ciclo de conexión/reconexión/watchdog sí quedó probado contra infraestructura real. Dado que
  `phase-1-ingestion.md` exige que los workers corran 24/7 en producción, este bloqueo de red **no puede
  reproducirse en este entorno de desarrollo** — el worker va a necesitar correr (o al menos probarse una
  vez) desde una red distinta antes de confiar en que ingesta datos reales.
- Sin cambios de código: no se tocó `binance_worker.py` porque no se encontró ningún bug atribuible al
  código en sí.
- Próximo paso sugerido: repetir el mismo test aislado (`wss://fstream.binance.com/ws/btcusdt@aggTrade`
  crudo) desde otra red (la del usuario, o el VPS/servidor donde correrá el worker en producción) para
  confirmar si ahí sí llegan datos. Si sí, el problema es 100% de esta sandbox y no bloquea nada. Si
  tampoco llegan datos ahí, es un problema real (geo-bloqueo de Binance a la IP, o algo del lado de
  Binance) que hay que resolver antes de depender de este worker en producción.

### 2026-07-10 — Redis Streams, watchdog, FastAPI y primer agente LangGraph
- **Redis Streams como sink real** (`schema/sink.py`): `RedisStreamSink` sobre `redis.asyncio`,
  implementa `EventSink` sin que `BinanceWorker` se entere. Stream key `heatsignal:{symbol}:{kind}`
  (ej. `heatsignal:BTC-USDT:liquidation`), payload `json.dumps(event.model_dump(mode="json"))`, `MAXLEN`
  aprox. configurable (default 10 000), pool de conexiones configurable (default 10 vía
  `max_connections`). `RedisStreamSink.connect()` es el punto de entrada de producción: prueba `PING`
  con timeout y cae a `ConsoleSink` si Redis no responde al arrancar (no crashea el proceso de ingesta);
  `write()` loguea y descarta si Redis se cae en operación, nunca propaga. Testeado con `fakeredis`
  (`redis>=5.0`, `fakeredis>=2.0` — el segundo solo en `requirements-dev.txt`), sin Redis real en tests.
  - Iteración: la primera versión agrupaba por `kind` solamente (`heatsignal:stream:{kind}`) y dejaba
    propagar excepciones de Redis al llamador; se corrigió a pedido a stream-por-símbolo y swallow interno
    de errores, con pool configurable y fallback a `ConsoleSink`.
- **Watchdog de socket zombie** (`ingestion/binance_worker.py`): `on_close` del conector no cubre el caso
  de un socket que queda "vivo" a nivel TCP pero deja de recibir datos. Se agregó `_watchdog()` (task
  separada, sondea cada `watchdog_poll_s`, default 5s) que fuerza `_connection_dead.set()` si no llega
  ningún mensaje en `message_timeout_s` (default 60s) — `_last_message_at` se actualiza en `_on_message`
  ante cualquier frame, incluso no-JSON.
- **Decisión FastAPI cerrada** (`decisions.md`, `CLAUDE.md`): esqueleto mínimo en
  `backend/src/heatsignal/api/app.py` (`create_app()` + `GET /health`). El dashboard/API pública quedan
  para Fase 4.
- **Primer agente LangGraph**: `agents/liquidation_analyst.py` (Analista de Liquidaciones, sin los otros
  2 analistas ni la capa de debate todavía) + `agents/state.py` (`GraphState`) + `agents/graph.py`
  (`build_graph()`, grafo de un solo nodo). Un call batcheado con salida estructurada Pydantic
  (`LiquidationAnalysisBatch`) para todos los símbolos de la ventana, no uno por símbolo. El LLM se
  inyecta (`BaseChatModel`), no se instancia adentro — el proveedor concreto sigue sin decidirse en
  `decisions.md`, y esto lo deja testeable con un modelo falso sin red ni API key.
- 81 tests pasando (74 previos + 7 nuevos: 6 del analista/grafo, 1 del healthcheck; el rediseño del sink
  de Redis sumó/reemplazó tests dentro de los 74).
- Pendiente: wiring real — nada todavía lee del Redis Stream para alimentar `liquidations_by_symbol` del
  grafo (el analista recibe el estado ya armado); falta decidir el proveedor de LLM concreto para poder
  correr el grafo fuera de tests; Order Flow y Contexto de Mercado (los otros 2 analistas paralelos) y la
  capa de debate siguen sin empezar.

### 2026-07-10 — Bloqueante C (benchmark/baseline estadístico)
- Se implementó el Bloqueante C completo (baseline/benchmark del win-rate, ver `phase-0-blockers.md` y
  `phase-3-evaluation.md`) en `backend/src/heatsignal/evaluation/`:
  - `strategy_registry.py` — `StrategyHypothesis` (Pydantic) + `StrategyRegistry` sobre SQLite. Pre-registro
    inmutable: triggers SQLite bloquean `UPDATE`/`DELETE` sobre `strategy_hypotheses` a nivel de motor, no
    solo por convención de la app. Los cambios de estado (`pending→running→evaluated`) se guardan como
    eventos nuevos en `strategy_status_events`, nunca como `UPDATE` de la hipótesis original.
  - `baseline.py` — `BaselineStrategy` (ABC) + `RandomBaseline`/`AlwaysLongBaseline`/`MomentumBaseline`,
    reproducibles por seed.
  - `evaluator.py` — `Evaluator.evaluate()` + `EvalResult`. Calcula win_rate, avg_return, sharpe anualizado
    (estimando trades/año desde el holding promedio de la muestra), max_drawdown, profit_factor, IC de
    Wilson (`wilson_ci`) y p-value (binomial test de scipy). Aprueba solo con n>=30, p<0.05, IC de Wilson
    con cota inferior >0.5, Sharpe>1.0 y superando los 3 baselines. `n<30` siempre resuelve en
    `passed=False, reason="insufficient_data"` (sin fallos silenciosos).
  - `hypothesis_log.py` — `HypothesisLog` sobre SQLite, también append-only por trigger. Cada
    `log_result()` agrega una fila nueva incluso si la misma estrategia se reevalúa (anti-p-hacking: el
    historial de fallos nunca se pisa).
  - `schema/signals.py` (nuevo) — `Signal`/`Direction`, no existía todavía.
  - **Ajuste post-implementación**: el criterio "supera un baseline" pasó de comparar solo `avg_return` a
    un criterio compuesto (`avg_return` mayor Y `sharpe_ratio` mayor Y `max_drawdown` menor en valor
    absoluto — ver `_beats_baseline` en `evaluator.py`), con fallback a las métricas disponibles cuando el
    Sharpe de un lado no está definido (varianza cero o <2 señales).
- Se agregó infraestructura de tests (no existía): `backend/pytest.ini` (`pythonpath = src`),
  `backend/requirements-dev.txt` (`pytest>=7`, separado del `requirements.txt` ya fijado por el usuario), y
  `backend/tests/` con 46 tests (`test_strategy_registry.py`, `test_hypothesis_log.py`, `test_baseline.py`,
  `test_evaluator.py`) — todos pasando, incluida cobertura explícita de `_normalize`, `wilson_ci` y del
  criterio compuesto `_beats_baseline`.
- Se inicializó el repo git de `heatsignal/` (`git init`, remote `origin` →
  `git@github.com:nahuelbrizu/Heat-Signal-Boty.git`) y se agregó `.gitignore` (`.venv/`, `__pycache__/`,
  `.env`, `*.db`) antes del primer commit.
- Pendiente: el Bloqueante C queda **implementado pero no integrado** — falta que el grafo LangGraph (Fase
  2, sin empezar) produzca `list[Signal]` reales vía Redis Streams para correr evaluaciones con datos
  posta; `decisions.md`/`phase-0-blockers.md` todavía no reflejan este avance (checkboxes del Bloqueante C
  sin tildar).
- Próximo paso sugerido: tildar el Bloqueante C en `phase-0-blockers.md` y registrar el cierre en
  `decisions.md`; después, decidir si se sigue con Bybit/OKX workers (Bloqueante B) o se arranca el
  esqueleto del grafo LangGraph (Fase 2).

### 2026-07-10
- **Detectado un cambio de arquitectura hecho fuera de esta conversación**: `phase-0-blockers.md`,
  `phase-1-ingestion.md`, `agents.md` y `decisions.md` ya estaban editados a mano con un giro grande —
  la fuente primaria de datos pasó de **CoinGlass API v4** a **WS nativos de Binance + Bybit + OKX**
  (gratis, sin cláusula de redistribución comercial). CoinGlass queda como fuente **BYOK opcional** a
  futuro. Esto mitiga el Bloqueante A (ya no es bloqueante duro para el dashboard público, solo queda
  revisar ToS livianos de cada exchange). Decisión ya registrada en `decisions.md` con fecha 2026-07-08.
  - **`CLAUDE.md` había quedado desactualizado** (seguía describiendo CoinGlass como fuente única/core,
    4 workers + poller REST). Se corrigió: "Qué es HeatSignal", tabla de Stack, "Arquitectura en dos
    capas" y "Reglas para Claude Code" ahora reflejan WS nativos como core + CoinGlass BYOK opcional.
  - `phase-4-product.md` también se actualizó (ya no depende en duro del Bloqueante A).
- Se agregaron 3 archivos de código Python (primer código de ingesta real, worker de Binance):
  `events.py`, `sink.py`, `binance_worker.py`. Habían sido subidos a `.claude/context/` (carpeta de
  documentación, no de código) — se movieron a una estructura de paquete real en
  `backend/src/heatsignal/`:
  - `backend/src/heatsignal/schema/events.py` — esquema Pydantic común (`LiquidationEvent`, `TradeEvent`,
    `TickerEvent`) con `Coverage` (full/partial) y `Exchange` como enums.
  - `backend/src/heatsignal/schema/sink.py` — protocolo `EventSink` + `ConsoleSink`/`BufferedSink` de
    desarrollo.
  - `backend/src/heatsignal/ingestion/binance_worker.py` — worker que multiplexa `forceOrder` (liquid.),
    `aggTrade` (trades) y `markPriceUpdate` (ticker) en una sola conexión WS, con reconexión
    backoff+jitter y normalización al esquema común. Implementa el patrón "1 worker por exchange"
    decidido en `phase-1-ingestion.md`/`phase-0-blockers.md`.
  - Se agregaron `__init__.py` vacíos para que el paquete y los imports relativos (`..schema.events`)
    funcionen, y `backend/requirements.txt` (`pydantic`, `binance-futures-connector`) — declarados pero
    **no instalados**.
  - Pendiente: el worker equivalente para Bybit y OKX (mismo patrón, canales ya documentados en
    `phase-1-ingestion.md`); wiring real a Redis/TimescaleDB (hoy el sink de ejemplo es solo consola/buffer
    en memoria); tests de los adaptadores de normalización.
- Próximo paso sugerido: decidir si se sigue con Bybit/OKX workers (completar Bloqueante B) o se cierra
  primero el Bloqueante C (benchmark), que sigue 100% abierto.

### 2026-07-08
- Se armó la estructura de carpetas del proyecto (`heatsignal/`, `.claude/context/`) a partir de los
  archivos `.md` provistos (CLAUDE.md, agents.md, decisions.md, phase-0..4, y este historial.md).
- Se revisaron los tres bloqueantes de Fase 0 (`phase-0-blockers.md`). Estado: **ninguno cerrado**.
  - Bloqueante A (ToS CoinGlass / redistribución pública) — sin respuesta de CoinGlass.
  - Bloqueante B (diseño ingesta WS) — confirmado por doc que Order Flow y Ticker Snapshot son
    WebSocket-only; falta diseñar el detalle (reconexión/backoff, checkpointing, ventanas, buffers).
  - Bloqueante C (baseline/benchmark del win-rate) — sin definir benchmark pasivo, cálculo de n/IC, ni
    pre-registro de estrategias.
- Próximo paso: decidir por cuál bloqueante seguir (candidato: Bloqueante B, es el único 100% técnico y
  no depende de terceros).
