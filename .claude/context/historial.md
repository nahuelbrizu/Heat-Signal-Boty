# Contexto: Historial de sesiones

> Registro cronológico de avances, revisiones y decisiones tomadas sesión a sesión. Se actualiza al
> cierre de cada sesión de trabajo relevante (no cada mensaje). No reemplaza a `decisions.md` — ahí van
> las decisiones cerradas/abiertas en sí; acá va **cuándo y en qué contexto** se tocó cada cosa.

## Cómo usar este archivo
- Agregar una entrada nueva arriba de todo (orden descendente, más reciente primero).
- Cada entrada: fecha, qué se hizo, qué quedó pendiente/próximo paso.
- Si una sesión cierra un bloqueante o una decisión, referenciarlo (no duplicar el contenido).

## Entradas

### 2026-10-27
- Deploy de SL/TP (commit fa2bbdd, 2026-10-26 20:03 UTC). Túnel: puertos 18001/18081 y WSL mirrored (conexion.md).
- Noticias del mundo: 12 feeds generalistas + temas `newstopics-v1` + bloque "Mundo" para el Analista de Contexto
  (decisions.md). Tests `test_news_world.py`.

### 2026-10-26 (noche)
- Lote 7 (grilla long con stop por compra, opción a del usuario): `lab/grilla.py`, `lab/regime.py` (htf-trend-v1
  vectorizado, verificado contra el vivo), tests `test_lab_grilla.py` y `test_lab_regime.py`. Se corrigió el modelo de
  llenado antes de ver retornos (declarado en decisions.md). Resultado: no pasa ninguna (Sharpe −0,03 y −0,29).
  Falta republicar el laboratorio para verlo en el panel (`python -m heatsignal.lab.publish --upload`).

### 2026-10-26 (tarde)
- Revisión de infinity-grid (bot de grilla para Kraken spot): `research/infinity-grid.md`. Sirve como idea de familia
  nueva (grilla, activable en días laterales) y como patrones de robustez para la etapa de plata real (intención escrita
  antes de la orden, órdenes pendientes, kill switch por precio viejo, tope de inversión). No tiene stop ni backtest;
  licencia no comercial (tomar ideas, no copiar código). Propuesta de pre-registro del lote 7 a la espera del usuario.

### 2026-10-26
- SL/TP obligatorio en todas las operaciones nuevas (pedido del usuario) como **-v2 en paralelo** de las 30 del bot de
  paper: mismas entradas que la v1, salida solo por stop 3 ATR (8–20 %) u objetivo 2× (decisions.md, "SL/TP
  obligatorio"). Código: `lab/sltp.py` (niveles, guardia de liquidación, `advance`), specs en `lab/strategies.py`,
  fichas en `lab/exits.py`, ejecución con estado en `paperbot/engine.py` (`_sltp_run`, tabla `sltp_trades`), rol `sltp`
  en `params.json`, panel `#/paperbot` (sección "Con SL/TP obligatorio" con la v1 al lado; en el detalle, operaciones
  abiertas con stop, objetivo, liquidación y distancia desde la marca, y cerradas con su motivo). Tests: `test_sltp.py`.
  Simulación local de 60 corridas: ~5,5 s y 206 MB pico por corrida (antes ~3 s / 147 MB en la VM; MemoryMax 400M).
- Desplegado el mismo día a las 20:03 UTC (commit fa2bbdd; ver estado.md).
  Freqtrade sin -v2 (decidir si se suma una instancia o alcanza con el paperbot).

### 2026-10-19 (noche)
- Investigación de matemática del trading (MQL5, dos hilos de X, Rankia): `research/mql5-matematica-trading.md`,
  partes 1 y 2, con lo que ya teníamos, lo nuevo útil y lo descartado.
- Implementado el punto 1 del orden propuesto: bloque `trade_quality` en el laboratorio + tarjeta en el panel (ver
  `estado.md`). Suite completa: 934 tests pasan (1 omitido).
- Próximo: Monte Carlo en bloques (DD P95, P(ruina)) y estacionalidad horaria con FDR.

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
- Push de la rama `ingestion-binance-bybit` a GitHub (sin PR).
- Panel interno (`api/panel.py`): FastAPI + HTML propio en el mismo proceso de ingesta, 127.0.0.1:8000,
  acceso por túnel SSH. Exchanges, ventana, ciclo, features en vivo, estrategias e hipótesis.

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

## Archivo de estado.md (movido el 2026-11-18 al achicar estado.md; texto original sin cambios)

## Novedades 2026-10-27 (desplegado 2026-10-26 20:18 UTC, commit 0400836)

- **Noticias del mundo:** 16 feeds (4 cripto + 12 generalistas: guerras, gobiernos, economía, tecnología) con temas
  `newstopics-v1`; bloque "Mundo" para el Analista de Contexto desde el próximo ciclo (Asia 01:01 UTC). Panel
  `#/noticias` con "Mundo: lo que recibe el grafo". Verificado en la VM: 16 feeds sin error, 12 titulares en el bloque.
  Revisar en una semana: economía con pocos titulares y política con ruido local (ver decisions.md).

## Novedades 2026-10-26 (desplegado 2026-10-26 20:03 UTC, commit fa2bbdd)

- **SL/TP obligatorio (-v2 en paralelo):** cada una de las 30 estrategias del bot de paper tiene su -v2 (rol `sltp`)
  con las mismas entradas y salida SOLO por stop 3 ATR (8–20 %) u objetivo 2×, con guardia de liquidación y estado en
  `sltp_trades`. Arrancan planas desde su primera corrida. Panel `#/paperbot`. Ver `decisions.md`. Desplegado: paperbot
  `MemoryMax=700M`, ingesta reiniciada, corrida de prueba en la VM con base temporal: 60/60 ok, 152 MB, 15 s. Primera
  corrida real con las -v2: 2026-10-26 21:02 UTC. Respaldo previo: `~/deploy-backups/pre-fa2bbdd-*.tgz` en la VM.
  Freqtrade todavía sin -v2.

## Novedades 2026-10-17 (tarde)

- **Panel = SPA** (`api/static/app/`, vanilla, sin build ni CDN; `panel.html` es solo el cascarón). Vistas:
  resumen, moneda (todo filtrado por una cripto + "Analizar ahora" y "Mandar al laboratorio"), mercado,
  análisis, ciclos, estrategias, hipótesis (acertó/falló contra la realidad), laboratorio (ficha, holdout,
  pedidos), noticias, datos guardados, sistema. API documentada en `api/API.md`.
- **Cola del laboratorio:** los pedidos se guardan en la VM (`lab_requests`, append-only); el worker de la PC
  (`systemctl --user heatsignal-lab-worker.timer`, cada 5 min) los toma por SSH, corre el backtest (cada corrida
  cuenta como intentos; el holdout nunca se abre desde la cola) y sube el resultado. Con la PC apagada,
  los pedidos esperan. Si un pedido queda colgado en `running`, liberarlo con `python -m heatsignal.evaluation.lab_requests fail ID`.
- **Paper trading:** `heatsignal-freqtrade.service` en la VM (dry-run de donchian-ensemble-v1). FreqUI con `hs-ft`,
  `hs-ft-open` y `hs-ft-pass`.
- **Paper vs backtest (2026-10-18, sin desplegar):** `lab/paper_compare.py` (PC) lee la base del dry-run por SSH en
  solo lectura, re-simula la regla congelada y descompone la diferencia (timing, tamano, fill, comisiones, slippage,
  funding); sube `paper_summary.json`. Timer semanal en `deploy/pc/` (domingos 01:00, falta instalarlo). Panel: vista
  `#/paper`, `GET /api/paper` y `GET /api/paper/compare` (requiere deploy del codigo y reinicio de la ingesta).
- **Bot propio de paper del lote 3 (2026-10-18, sin desplegar):** `heatsignal.paperbot` (corrida horaria a los :02 UTC,
  `deploy/heatsignal-paperbot.{service,timer}`, `MemoryMax=400M`; pico medido 143 MB). Llama a las mismas reglas de
  `lab/strategies.py`, simula fills/costos/funding en `data/paperbot.sqlite` (append-only) y registra huecos sin rellenarlos.
  Panel: vista `#/paperbot` (`GET /api/paperbot`, con el veredicto del backtest al lado). Falta: instalar pandas en el venv
  de la VM (`requirements-paperbot.txt`), deploy del código, reiniciar la ingesta y activar el timer; el archivado tiene que
  conservar >= 64 días de velas 1m (`--klines-keep-days 90` en `heatsignal-archive.service`).
- **LLM:** Anthropic (sin crédito) → gemini-3.5 → gemini-3.8-flash / 3.1-flash-lite. Rotar ambas keys (quedaron en el chat).
- **Laboratorio:** resultados del lote 1 y holdout en `decisions.md`.

- **Almacenamiento por niveles (2026-10-18, sin desplegar):** `history/archive.py` + `heatsignal-archive.timer` (cada
  hora: al 80 % de disco sube lo viejo a `gdrive:archive/`, verifica md5 y borra) y lectura fría en el panel
  (`/api/history/storage`, badge "desde Drive"). Respaldo semanal `heatsignal-backup.timer`. Falta: deploy del código,
  instalar los timers y reiniciar la ingesta (nuevo `ReadWritePaths` para rclone). Ver `decisions.md`.

- **Velas de 1h + tendencia del día + sesiones (2026-10-18, sin desplegar):** contexto multi-temporalidad por código
  (`features/htf.py`, regla fija `htf-trend-v1`; velas de 1h CERRADAS desde `klines`/`bars_1m` de la base, REST de Binance
  solo para huecos, declarado; cache incremental en `history/hourly.py`). Sesiones Asia/Europa/EE.UU./cierre con DST
  (`features/sessions.py`): al cerrar la 1ª vela de 1h de cada una se guarda un análisis por código en el diario
  append-only `session_journal` (base de evaluación) y el grafo recibe el diario del día. Modo `HEATSIGNAL_ANALYSIS_MODE=sessions`:
  4 corridas del grafo por día con TODAS las criptos batcheadas (sin llamadas extra). Hipótesis con `timeframe`,
  `daily_trend`, `trend_alignment`, `session` (tabla nueva `hypothesis_context`, migración aditiva); las de 1h vencen en
  un cierre de vela y se resuelven con la vela de 1h (mark). Panel: vista `#/sesiones`, tarjeta de 1h en Moneda, columnas
  y corte por tendencia/sesión (n e IC) en Hipótesis. 5 estrategias nuevas en `backend/strategies/tendencia-diaria-1h/`
  **sin registrar** (las registra el usuario). Falta: deploy + `HEATSIGNAL_ANALYSIS_MODE=sessions` + reinicio.

## Novedades 2026-10-19 (sin desplegar)

- **max-ruptura-10d-v1 en el bot propio de paper.** `paperbot/params.json` ahora define qué corre (6 del lote 3 + max-ruptura,
  spec 39cb9dee6d3e469a, sin grilla). El modo (`inicio`/`consecutiva`/`hueco`) se decide por estrategia: la nueva entra
  "desde hoy" (orden `inicio` al objetivo de la regla, 10.000 US$ y canasta propios desde su primera corrida) sin tocar la
  historia de las otras. Sin cambios de esquema. Panel `#/paperbot`: veredicto del backtest + del holdout (no confirmó)
  y "en paper desde". `lab/publish.py` publica `holdout_result` con ventana, canasta y p azar (republicar con
  `python -m heatsignal.lab.publish --upload` desde la PC para verlos). Corrida en seco en la VM: 147 MB, 3 s.
- **Revisión semanal** `python -m heatsignal.paper.weekly` + `deploy/heatsignal-weekly.{service,timer}` (lunes 00:30 UTC):
  cierra la semana UTC anterior por código (sin LLM) y guarda una fila append-only por semana en `weekly_reviews`
  (base de evaluación). Fuentes: bot propio, Freqtrade Donchian (+ paper_compare), Freqtrade de hipótesis, hipótesis del
  grafo (n, IC de Wilson, alfa, cortes) y salud (bars_1m/klines, ciclos, sesiones con LLM; las llamadas al LLM no se
  registran: falta). Panel `#/semanal`, `GET /api/weekly` y `/api/weekly/{week}`. Corrida en seco en la VM: 28 MB, 6–10 s.
  Falta: deploy del código, reiniciar la ingesta (endpoint nuevo) e instalar el timer.

- **Macro y eventos + diario de lecciones (2026-10-19, sin desplegar):** módulo `heatsignal/macro/` (FRED
  point-in-time por fecha de publicación, calendario FOMC/CPI/NFP versionado en `macro/config/calendar.json`,
  Polymarket configurable en `macro/config/polymarket_markets.json`), base append-only `data/heatsignal_macro.sqlite`,
  timer `heatsignal-macro.timer` (cada hora; FRED 1 vez/día), export a `data/export/macro/` (lo sube el timer de
  historia). El Analista de Contexto recibe el bloque "Macro y eventos" (~320 tokens, sin llamadas extra). Diario de
  lecciones (`hypothesis_lessons` en la base de evaluación, lo escribe el job de evaluación con el LLM barato; NO vuelve
  a ningún prompt). Panel: `#/macro`, `#/lecciones` y la lección en el detalle de cada hipótesis. Carga para el
  laboratorio: `lab/macro_features.py`. Falta: deploy, instalar el timer macro, recargar el eval.service (MemoryMax 400M)
  y reiniciar la ingesta. `calendar.json` hay que actualizarlo cuando el BLS/Fed publiquen fechas nuevas (el panel avisa).

- **Stop loss y take profit en todo el sistema (2026-10-19, sin desplegar):** ficha de salidas por estrategia
  `lab/exits.py` (`EXITS`, mapa aparte del Spec: ningún spec_hash cambia; test de que los valores coinciden con
  `spec.params`); la cartera hereda de sus componentes. Atribución de salidas (cuántas cerraron por stop, objetivo,
  tiempo, señal o rebalanceo) con `python -m heatsignal.lab.diagnostics salidas <id>` (también dentro de `ambos`):
  reconstruye los motivos con la misma lógica de `exits_with_levels` y verifica que coincida; sin holdout, no suma
  intentos. Panel: columna "Salidas" y tarjeta "Cómo sale" en Laboratorio; ficha, cierres por motivo y motivo por
  trade en Bot paper (tabla nueva append-only `exit_reasons` en `paperbot.sqlite`, solo para lo nuevo); stop/ROI de
  cada Freqtrade y `exit_reason` por trade en Paper; regla de salida + MAE/MFE en el detalle de cada hipótesis (tabla
  append-only `hypothesis_excursions` en la base de evaluación, la llena el job de evaluación) con el corte "¿un stop
  de X ATR la hubiera sacado antes?" marcado como NO pre-registrado; motivos de cierre por fuente en Semanal.
  **Plata real:** toda orden lleva stop del lado del exchange (STOP_MARKET reduce-only), obligatorio aunque la regla
  salga por tiempo (`plan-entrenamiento.md`, etapa 5). Falta: deploy del código, reiniciar la ingesta (endpoints),
  republicar el laboratorio y correr `diagnostics salidas --all` en la PC.

- **Calidad de los trades en el laboratorio (2026-10-19, sin desplegar):** `lab/stats.py` suma win-rate mínimo para no
  perder (`1/(1+payoff)`), win-rate de azar por barreras (ruina del jugador, solo si stop y objetivo están en ATR),
  test de rachas, AHPR/GHPR, R² con signo de la curva y estabilidad Mann–Whitney (primer 70 % vs. último 30 %). Va en
  `oos.trade_quality` y `holdout.trade_quality` (`lab/run.py`), y en el panel como tarjeta "Calidad de los trades" en el
  detalle de cada estrategia y del holdout. Son diagnósticos: no entran al veredicto ni a la selección. Tests en
  `tests/test_lab_trade_quality.py`. Falta: correr de nuevo el laboratorio (los resultados viejos no traen el bloque;
  el panel lo avisa) y republicar. Investigación: `research/mql5-matematica-trading.md`.

## Estado por fase

| Fase | Estado | Dónde |
|---|---|---|
| 0 — Bloqueantes | A mitigado (falta leer ToS de exchanges), B implementado, C cerrado | `phase-0-blockers.md` |
| 1 — Ingesta | **En producción de prueba** (VM, modo memory, 24/7) | `phase-1-ingestion.md` |
| 2 — Agentes | Implementado y cableado; **inactivo** hasta tener API key + estrategias | `phase-2-agents.md`, `agents.md` |
| 3 — Evaluación | Implementado; timer cada 5 min en la VM, sin hipótesis todavía | `phase-3-evaluation.md` |
| 4 — Producto | Panel **interno** listo (no público); dashboard/API pública/reporte no iniciados | `phase-4-product.md` |

Código: rama `ingestion-binance-bybit`, pusheada a GitHub (sin PR).
Tests: 923 pasando, 1 skipped (2026-10-19, con salidas) (`backend/`, `.venv/bin/pytest -q`).

## Qué corre hoy (VM de prueba)

- GCP e2-small `instance-20261016-191100`, `asia-northeast1-a`, proyecto `vocal-operand-271906`.
  Debian 13, 2 vCPU, 1.9 GB RAM + 2 GB swap, disco 10 GB. Código en `~/heatsignal` (copiado con
  `git archive` + `gcloud compute scp`, no es un clon de git). venv Python 3.13 en `~/heatsignal/backend/.venv`.
- `heatsignal-ingestion.service`: ingesta Binance + Bybit + OKX + Hyperliquid (BTC, SOL, XRP, ETH, XLM, BNB, TRX) en **modo memory**
  (datos de mercado solo en RAM) + ciclo de análisis cada 15 min. `MemoryMax=700M`, `Restart=always`.
- `heatsignal-eval.timer` → `heatsignal-eval.service` cada 5 min (resolución + evaluación).
- Persistido: `data/heatsignal_eval.sqlite` (estrategias + hipótesis) y `data/heatsignal_history.sqlite`
  (agregados por minuto, liquidaciones, noticias, velas 1m). `heatsignal-history.timer` (00:20 UTC):
  velas nuevas + export diario + respaldo a Drive. Con API
  key se suma `heatsignal_checkpoints.sqlite` (checkpoints del grafo).
- `ANTHROPIC_API_KEY` cargada en `~/heatsignal/.env` (2026-10-17): grafo habilitado. Con la primera
  estrategia registrada (`xrp-acumulacion-v1`) el grafo corre cada 15 min y gasta LLM.
- Logs: `sudo journalctl -u heatsignal-ingestion -f`, `sudo journalctl -u heatsignal-eval -n 50`.
- **Panel interno** (`api/panel.py` + `panel.html`): dentro del proceso de ingesta, escucha solo en
  `127.0.0.1:8000` de la VM (sin puerto abierto en el firewall). Incluye gráfico de velas (Binance REST
  pedido al abrir, no se guarda) con hipótesis y clusters encima. Abrir con túnel SSH desde la PC:
  `gcloud compute ssh --zone asia-northeast1-a instance-20261016-191100 --project vocal-operand-271906 -- -N -L 8000:localhost:8000`
  y después `http://localhost:8000`. Muestra exchanges, ventana en memoria, último ciclo, features en
  vivo, estrategias e hipótesis. Solo lectura.

## Pendientes — los define el usuario (actualizado 2026-10-19)

- [ ] **Listón del programa (SR0):** elegir (a) como está, (b) K efectivo por correlación, (c) por familia
      (`decisions.md`, "Abiertas"). Decidir antes de mirar qué opción hace pasar a alguna estrategia.
- [ ] **Seguridad de la VM (a hacer):**
  - [ ] Activar *deletion protection* (hoy apagada).
  - [ ] Borrar la regla de firewall `default-allow-rdp` (3389 abierto a internet, inútil en Linux).
  - [ ] Más adelante: SSH solo por IAP (regla para 35.235.240.0/20, `--tunnel-through-iap` en nuestros
        comandos y en `lab/publish.py`), y Secure Boot.
- [ ] **Rotar keys pegadas en el chat:** `ANTHROPIC_API_KEY` (ya rotada una vez), la key vieja de Gemini (AI
      Studio) y la de Vertex (`AQ.`, en uso). Cargar la nueva en `~/heatsignal/.env` de la VM.
- [ ] **Crédito de Anthropic:** cuando se cargue, Claude vuelve a ser el primario (Haiku/Sonnet): medir costo.
- [ ] **Métricas del grafo** (tokens y costo por ciclo y por modelo, en el panel): pedido pendiente.
- [ ] **Plan de entrenamiento de bots:** `plan-entrenamiento.md` (para revisar y aprobar por etapas).
- [ ] **METAL**: no existe como perpetuo; ¿es MTL (Metal DAO, solo Binance/Bybit) u otro token?
- [ ] **ToS de Binance/Bybit/OKX**: lectura rápida (`phase-0-blockers.md`, Bloqueante A).
- [x] Estrategias del grafo pre-registradas: las 5 de 1h (2026-10-18). `xrp-acumulacion-v1/v2` retiradas (XRP
      pasa a la wallet).
- [x] VM pasada a e2-medium (4 GB) con disco de 20 GB (2026-10-19).

## Pendientes — código / técnicos

Cuando el grafo empiece a correr:
- [x] Grafo validado contra la API real (2026-10-17 03:01 UTC): 8 llamadas, ~90 s, `estado=ran`,
      los 3 símbolos `no_hypothesis`. Falta medir el costo real por ciclo en la consola de Anthropic.
- [ ] Manejar refusals del modelo y modelo de fallback (hoy un fallo del LLM = ciclo `error`, sin reintento
      de otro modelo).
- [ ] Medir la RAM real de la ventana de 30 min de trades en la VM (estimado 160–250 MB).

Calibración / estadística (preguntas abiertas de los agentes de implementación):
- [ ] Baseline momentum del Evaluador mezcla símbolos cuando una estrategia opera varios.
- [ ] Supuesto de costos de 10 bps round-trip (sin slippage ni funding): decidir si se publica así.
- [ ] Calibrar el umbral de `regime-v1` (2 ATR en 20 barras) con datos del shadow.
- [ ] Definición de `price_mid` del cluster (hoy punto medio del rango del cluster).

Noticias:
- [ ] Sin fuente primaria de Ripple (no publica RSS). Opción: monitorear su sala de prensa de otra forma.
- [ ] El etiquetado por palabras clave es simple (`newsrss-v1`): medir falsos positivos con lo que se acumule.

Ingesta:
- [ ] Open interest de Binance: el stream `markPrice` no lo trae; la suma de OI sale solo de Bybit + OKX
      (declarado en `data_gaps`). Opción: poll REST `openInterest` de Binance.
- [ ] `reconnects` de health se infiere parseando logs; reemplazar por un hook explícito en los workers.
- [ ] Modo `persistent`: `TimescaleSink` nunca se probó contra Postgres real; `prune_plain()` no está
      agendado. Irrelevante mientras se use modo memory.

Producto (Fase 4):
- [x] Panel interno de operación (túnel SSH, solo lectura).
- [ ] Endpoints públicos FastAPI (reportes `public_view()` por estrategia).
- [ ] Dashboard Next.js.
- [ ] Reporte diario anclado en UTC.

Ejecución (sin plan todavía, ver `decisions.md`):
- [ ] Si se decide operar: plan por etapas shadow → paper → dinero chico → escalar. XRPL DEX entra acá
      (Hummingbot `xrpl`).

Operación:
- [ ] Reloj de la WSL de desarrollo adelantado ~1.9 s (`sudo hwclock -s`); la VM está bien (NTP).
- [ ] Deploy repetible: hoy es `git archive` + `scp` manual; documentar o automatizar.
