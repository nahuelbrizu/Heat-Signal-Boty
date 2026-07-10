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

## Abiertas
- ~~Plan de CoinGlass a contratar.~~ **Ya no aplica al core** — la fuente primaria pasó a WS nativos.
  Solo se retomaría si se implementa la fuente BYOK opcional (y ahí el plan lo elige el usuario).
- **Backend: FastAPI vs NestJS.** Sin decidir. FastAPI encaja mejor si el resto del stack de agentes es
  Python (LangGraph lo es). Sesgo tentativo hacia FastAPI por esa razón, pero no cerrado.

## Plantilla para nuevas decisiones
```
- **[Título] → [elección].** Sobre [alternativa]. Razón: [...]. Fecha: [...].
```
