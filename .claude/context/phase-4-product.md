# Contexto: Fase 4 — Producto (Dashboard + Reporte)

> Cargar al construir la capa de producto. Bloqueante A ya mayormente mitigado (fuente = WS nativos de
> exchanges, no CoinGlass) — igual confirmar los ítems livianos de ToS de exchange en `phase-0-blockers.md`
> antes de publicar.

**Estado (2026-10-17): no iniciado.** Solo existe `GET /health` (`api/app.py`). Pendientes en `estado.md`.

## Dashboard (Next.js)
- Señales rankeadas por win-rate calibrado, con n e intervalo de confianza visibles (no ocultar la
  incertidumbre).
- Marcar DEX de baja confianza distinto de exchanges mayores.
- Disclaimer banner presente, pero el texto generado ya debe ser histórico-no-predictivo por sí mismo.

## Reporte diario automatizado
- Flujo diario anclado en UTC.
- Redactor con guardrails: "clusters similares reaccionaron en X% de los casos", nunca "va a reaccionar".

## API pública
- Rate limiting propio; no filtrar API keys (propias ni, si existe BYOK, la del usuario) ni datos crudos
  que violen el ToS de un exchange.

## Salida de Fase 4
Producto público que respeta el ToS de la fuente y comunica incertidumbre honestamente.
