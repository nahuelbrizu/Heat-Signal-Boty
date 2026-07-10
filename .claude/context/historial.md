# Contexto: Historial de sesiones

> Registro cronológico de avances, revisiones y decisiones tomadas sesión a sesión. Se actualiza al
> cierre de cada sesión de trabajo relevante (no cada mensaje). No reemplaza a `decisions.md` — ahí van
> las decisiones cerradas/abiertas en sí; acá va **cuándo y en qué contexto** se tocó cada cosa.

## Cómo usar este archivo
- Agregar una entrada nueva arriba de todo (orden descendente, más reciente primero).
- Cada entrada: fecha, qué se hizo, qué quedó pendiente/próximo paso.
- Si una sesión cierra un bloqueante o una decisión, referenciarlo (no duplicar el contenido).

## Entradas

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
