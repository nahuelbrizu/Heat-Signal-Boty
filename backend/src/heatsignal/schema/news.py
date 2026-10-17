"""
Noticias que entran al grafo como un dato mas del `FeatureSet` (decisions.md, "Noticias por RSS").

Son texto de terceros: el grafo las trata como DATOS, nunca como instrucciones. Viven solo en memoria
(ventana de 48 h), igual que los datos de mercado; lo que vio cada corrida queda en el checkpoint del
grafo (`run_id`), asi cada hipotesis puede rastrear que noticias tenia a la vista.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

NEWS_MODEL_ID = "newsrss-v1"


class NewsItem(BaseModel):
    item_id: str = Field(description="Hash estable de (fuente, guid/link)")
    source: str = Field(description="Nombre del feed, p.ej. 'coindesk', 'sec'")
    primary: bool = Field(description="True si la fuente es el emisor o el regulador (no un medio)")
    title: str
    summary: str = Field(description="Texto plano recortado; puede venir vacio")
    url: str
    published_ms: int = Field(description="pubDate del feed (lo que dice la fuente)")
    fetched_ms: int = Field(description="Cuando lo vimos por primera vez: el grafo solo ve items con fetched_ms <= ts")
    symbols: list[str] = Field(default_factory=list, description="Activos base mencionados, p.ej. ['XRP']")
    market_wide: bool = Field(default=False, description="Noticia de todo el mercado cripto (regulacion, ETF, macro)")
