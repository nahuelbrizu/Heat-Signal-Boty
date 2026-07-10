"""
Esqueleto HTTP mínimo (FastAPI — decisión cerrada en `decisions.md`, 2026-07-10).

Placeholder de Fase 4: hoy solo expone un healthcheck para probar que el
esqueleto levanta. El dashboard, el reporte diario y la API pública de
señales (rate limiting propio, no filtrar API keys ni datos crudos que violen
el ToS de un exchange — ver `phase-4-product.md`) se construyen más adelante,
sobre esta misma app.

Correr en desarrollo:
    uvicorn heatsignal.api.app:app --reload
"""

from __future__ import annotations

from fastapi import FastAPI


def create_app() -> FastAPI:
    """Factory en vez de una instancia de módulo suelta: facilita testear con
    distinta configuración (ej. distintos sinks/clientes) sin reimportar el módulo."""
    app = FastAPI(title="HeatSignal API", version="0.1.0")

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    return app


app = create_app()
