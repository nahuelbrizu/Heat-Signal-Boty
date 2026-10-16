# Contexto: Decisiones (ADR liviano)

> Registro de decisiones. Al cerrar una, actualizar aquí en el mismo commit.

## Cerradas
- **Fuente de datos → WS nativos de exchanges (Binance + Bybit + OKX).** Sobre CoinGlass pago. Razón:
  gratis, sin cláusula de redistribución comercial (elimina el Bloqueante A), y control total sobre el
  modelo de heatmap. Costo: hay que agregar/normalizar cada exchange y construir el heatmap propio.
  CoinGlass queda como **fuente BYOK opcional** a futuro (el usuario pone su clave). Fecha: 2026-07-08.
- **Orquestación → LangGraph.** Sobre CrewAI. Razón: durabilidad de producción, manejo de estado preciso,
  y mejor performance para grafos de 5+ agentes / 10+ pasos. Además el checkpoint resume por nodo resuelve
  la idempotencia del pipeline gratis. (Ref: patrón TradingAgents.)
- **Ingesta = servicios, no agentes LLM.** 4 workers WS + 1 poller REST. Razón: los recolectores no
  razonan; gastar tokens ahí no aporta y escala mal.
- **Bloqueante C (benchmark/baseline) → resuelto.** Implementado en `backend/src/heatsignal/evaluation/`:
  3 baselines pasivos reproducibles por seed (`Random`/`AlwaysLong`/`Momentum`), Evaluador con IC de
  Wilson, p-value binomial (scipy) y Sharpe anualizado, y un criterio de aprobación fijo
  (n>=30, p<0.05, wilson_ci[0]>0.50, sharpe>1.0, y superar los 3 baselines). Razón para el criterio
  "supera baseline" compuesto (avg_return + sharpe + max_drawdown, no solo avg_return): un avg_return más
  alto con volatilidad/drawdown peores no es edge real, es más riesgo. Pre-registro de hipótesis inmutable
  (`StrategyRegistry`) y log de resultados que nunca pisa una evaluación previa (`HypothesisLog`), ambos
  sobre SQLite append-only con triggers a nivel de motor. Fecha: 2026-07-10.
- **Backend → FastAPI.** Sobre NestJS. Razón: mismo lenguaje que el resto del stack de agentes
  (LangGraph, evaluation/, ingestion/ ya son Python) — un solo runtime, sin puente entre proceso
  Node y proceso Python para el mismo dominio. Esqueleto mínimo en
  `backend/src/heatsignal/api/app.py` (`create_app()` + `GET /health`); el dashboard, el reporte diario
  y la API pública de señales quedan para Fase 4. Fecha: 2026-07-10.

## Abiertas
- ~~Plan de CoinGlass a contratar.~~ **Ya no aplica al core** — la fuente primaria pasó a WS nativos.
  Solo se retomaría si se implementa la fuente BYOK opcional (y ahí el plan lo elige el usuario).
- **Proveedor LLM del grafo → Anthropic (PROPUESTA, falta confirmación humana).** Implementado como
  default en `backend/src/heatsignal/agents/llm_factory.py` (`make_llms`) vía `langchain-anthropic`:
  `claude-haiku-5-5` (analistas, modelo económico) y `claude-sonnet-5-5` (debate, sintetizador,
  redactor). IDs confirmados contra la tabla de modelos vigente del skill `claude-api` (2026-10).
  Structured output con `method="json_schema"` (Sonnet 5.5 rechaza `tool_choice` forzado, que es lo
  que usa el método por defecto de langchain). Los nodos reciben el LLM inyectado, así que cambiar de
  proveedor no toca el grafo. Pendiente: confirmar proveedor + presupuesto, y validar con una API key
  real (no hay key en la máquina de desarrollo; todo está testeado con modelos falsos).

## Plantilla para nuevas decisiones
```
- **[Título] → [elección].** Sobre [alternativa]. Razón: [...]. Fecha: [...].
```
