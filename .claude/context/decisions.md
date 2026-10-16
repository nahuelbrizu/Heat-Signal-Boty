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
- **Datos de mercado en la etapa de prueba → solo en memoria (pass-through).** Sobre persistirlos en
  TimescaleDB/Redis. Razón: es un proyecto de prueba; el usuario pidió que los datos solo pasen, sin
  guardarse ni exponerse en otro lado. También evita disco/RAM extra en la VM e2-small y no guarda ni
  redistribuye datos crudos de los exchanges. `HEATSIGNAL_STORAGE=memory` (default) usa
  `storage/memory.py::MemoryWindowSink`: ventana en RAM por tipo y símbolo (trades 30 min, tickers 2 h,
  liquidaciones 24 h) con tope duro de eventos. Costo aceptado: cada reinicio vacía la ventana
  (calentamiento) y no hay historia para recalcular features con un heatmap nuevo. Sin fallos silenciosos:
  `data_gaps()` declara el calentamiento, la retención menor a la ventana pedida y los huecos por tope.
  **Lo que SÍ persiste:** hipótesis y resoluciones (`pending_store`, SQLite append-only). Sin eso no hay
  win-rate medible y se rompe el anti-p-hacking. El precio de salida sale de klines REST de Binance, no de
  datos guardados. Los features agregados (no los eventos crudos) se envían al proveedor LLM cuando corre
  el grafo. El modo `persistent` (compose) queda en el código, apagado. Fecha: 2026-10-17.

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
  real (no hay key en la máquina de desarrollo; todo está testeado con modelos falsos). El grafo ya
  está cableado en la VM y arranca solo cuando aparezca `ANTHROPIC_API_KEY` en su `.env`.
- **Símbolos iniciales.** Corriendo BTCUSDT + SOLUSDT; propuesta sumar ETHUSDT. Falta confirmación.
- **Supuesto de costos publicado.** 10 bps round-trip (taker 5 bps × 2, sin slippage ni funding). Decidir
  si se publica así o se modela slippage/funding antes de mostrar números.
- **Umbral de régimen (`regime-v1`).** 2 ATR de desplazamiento en 20 barras de 60 s; calibrar con datos
  del shadow (cambiarlo = nuevo id de versión).

## Plantilla para nuevas decisiones
```
- **[Título] → [elección].** Sobre [alternativa]. Razón: [...]. Fecha: [...].
```
