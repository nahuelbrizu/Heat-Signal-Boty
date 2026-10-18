# CLAUDE.md — HeatSignal

> Contexto raíz del proyecto. Claude Code lee este archivo automáticamente al inicio de cada sesión.
> Los archivos de etapa viven en `.claude/context/` y **NO** se cargan solos: pedímelos explícitamente
> (ej. "cargá el contexto de la Fase 0" → leé `.claude/context/phase-0-blockers.md`).

## Qué es HeatSignal

Plataforma web que combina **WS nativos de exchanges (Binance + Bybit + OKX)** con un **sistema
multi-agente** para identificar "puntos de calor" del mercado de derivados cripto (clusters de
liquidación propios, señales de order flow, snapshots de ticker) y evaluar **qué movimientos tienen la
mayor tasa de acierto histórica**. CoinGlass API v4 queda como fuente **BYOK opcional** a futuro (el
usuario pone su propia clave/plan), no como dependencia del core — ver `.claude/context/decisions.md`.

**Objetivo (cambiado el 2026-10-17):** herramienta **personal** que analiza semana a semana y mes a mes,
acumula datos propios, simula estrategias contra la realidad (velas 1m–1h) y, cuando una estrategia lo
demuestra fuera de muestra, opera por el usuario con límites de riesgo (bots propios y/o Hummingbot). Plan
por etapas en `decisions.md`. La integridad estadística sigue siendo el núcleo: nada opera con plata real
sin haber pasado simulación, cuenta de prueba y comparación con la realidad.

## Principios de trabajo (leer antes de escribir cualquier código)

1. **Grounding obligatorio.** Toda spec técnica sobre un exchange (Binance/Bybit/OKX) se verifica contra
   su doc oficial vigente antes de escribirse — los canales migran. Si se implementa el BYOK opcional de
   CoinGlass, ídem contra `https://docs.coinglass.com/llms.txt`. No inventar endpoints ni parámetros.
2. **Anti-p-hacking.** Toda hipótesis se guarda — incluidos los fallos. El sesgo de supervivencia
   invalida el win-rate. Las estrategias se pre-registran con `strategy_id` versionado antes de ver
   resultados. El Estratega no inventa variantes nuevas sin versionar.
3. **Honestidad estadística.** Ningún win-rate se publica sin: baseline/benchmark pasivo de referencia,
   tamaño de muestra (n) e intervalo de confianza, y calibración del confidence score (Brier).
4. **Lenguaje histórico, no predictivo.** El Redactor dice "clusters similares reaccionaron en X% de los
   casos", nunca "va a reaccionar". El disclaimer banner no alcanza por sí solo.
5. **Sin fallos silenciosos.** Los huecos de datos son tan dañinos como el sesgo. Todo fallo del pipeline
   es tipado y explícito; nada corre con datos parciales sin marcarlo.

## Stack

| Capa | Tecnología |
|---|---|
| Frontend | Next.js |
| Backend | **FastAPI** (decidido — ver `.claude/context/decisions.md`) |
| Base de datos | PostgreSQL + TimescaleDB |
| Cache / buffers | Redis |
| Orquestación de agentes | **LangGraph** (decidido — ver `.claude/context/agents.md`) |
| Datos | WS nativos de Binance + Bybit + OKX (core, gratis); CoinGlass API v4 como BYOK opcional |

## Arquitectura en dos capas (crítico)

**Capa de ingesta = servicios, NO agentes LLM.** Un worker WebSocket persistente por exchange (Binance,
Bybit, OKX) que multiplexa liquidaciones + trades + ticker por conexión, con reconexión/backoff+jitter,
replay de suscripciones y checkpointing, corriendo 24/7 desde el día 1. El heatmap ya no viene armado de
un poller REST de terceros: se construye propio a partir de liquidaciones crudas (`heatmap_model_id`
versionado). Gastar tokens de LLM acá no aporta y escala mal.

**Capa de agentes LLM = donde el razonamiento aporta.** Grafo LangGraph que corre encima de la ingesta.
Ver `.claude/context/agents.md`.

## Mapa de archivos de contexto (cargar bajo demanda)

| Archivo | Cuándo cargarlo |
|---|---|
| `.claude/context/estado.md` | **Primero**: estado actual por fase, qué corre en la VM y lista de pendientes |
| `.claude/context/agents.md` | Trabajar sobre cualquier agente o el grafo LangGraph |
| `.claude/context/phase-0-blockers.md` | Setup inicial y bloqueantes previos a codear |
| `.claude/context/phase-1-ingestion.md` | Construir la capa de ingesta (WS + REST) |
| `.claude/context/phase-2-agents.md` | Implementar los nodos de agentes |
| `.claude/context/phase-3-evaluation.md` | Evaluador, benchmark, calibración, modo shadow |
| `.claude/context/phase-4-product.md` | Dashboard, API pública, reporte diario |
| `.claude/context/decisions.md` | Decisiones abiertas y su estado (ADR liviano) |
| `.claude/context/plan-laboratorio.md` | Plan de etapas 1b–3: historia, laboratorio de backtest, lote de estrategias, Freqtrade |
| `.claude/context/plan-entrenamiento.md` | Plan para entrenar bots (meta-labeling, triple barrier, validación purgada) |
| `.claude/context/backlog-ui-datos.md` | Backlog: PnL, métricas y datos de mercado para la UI (análisis, sin implementar) |
| `.claude/context/research/` | Investigaciones: datos de Binance, ML4T, catálogo de hipótesis, motores de backtest |
| `.claude/context/historial.md` | Registro cronológico de sesiones y avances — actualizar al cierre de cada sesión relevante |

## Reglas para Claude Code en este repo

- Antes de escribir código de un canal WS de exchange, confirmar el canal/formato contra la doc oficial
  vigente de ese exchange (ver `.claude/context/phase-1-ingestion.md`). Si es CoinGlass (BYOK), `WebFetch`
  a `docs.coinglass.com/llms.txt`.
- Nunca commitear API keys. Los secretos van en `.env` (fuera de git) — ver `phase-0-blockers.md`.
- Al cerrar una decisión abierta, actualizá `.claude/context/decisions.md` en el mismo commit.
- Preferí llamadas LLM batcheadas (N pares en un call con schema estructurado) sobre 1 call por agente por par.
- Los tests de lógica estadística (benchmark, calibración, significancia) son no-negociables, no opcionales.
- Código fuente (no documentación) vive en `backend/src/heatsignal/` — `.claude/context/` es solo contexto
  para Claude Code, no un lugar para dejar código de producto.
