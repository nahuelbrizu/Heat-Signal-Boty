"""
Protocolo minimo de persistencia de hipotesis pendientes que usa el Sintetizador.

El store real (append-only, SQLite/Postgres) lo construye la capa `evaluation/`;
el grafo solo depende de este protocolo (`add(h)`), asi no se acopla a esa
implementacion. `InMemoryPendingHypothesisStore` es para tests y desarrollo local:
tambien es append-only (no expone borrar ni pisar).
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from ..schema.hypotheses import PendingHypothesis


@runtime_checkable
class PendingHypothesisStore(Protocol):
    def add(self, h: PendingHypothesis) -> None:
        """Persiste una hipotesis pendiente. Debe fallar ruidosamente, nunca en silencio."""
        ...


class DuplicateHypothesisError(Exception):
    """Se intento agregar dos veces el mismo `hypothesis_id` (append-only)."""


class InMemoryPendingHypothesisStore:
    def __init__(self) -> None:
        self._items: list[PendingHypothesis] = []

    def add(self, h: PendingHypothesis) -> None:
        if any(existing.hypothesis_id == h.hypothesis_id for existing in self._items):
            raise DuplicateHypothesisError(f"hypothesis_id {h.hypothesis_id} ya existe")
        self._items.append(h)

    def open_positions(self) -> set[tuple[str, str]]:
        """En memoria nada se resuelve: toda hipotesis cuenta como abierta."""
        return {(str(h.strategy_id).lower(), h.symbol) for h in self._items}

    def all(self) -> list[PendingHypothesis]:
        return list(self._items)

    def __len__(self) -> int:
        return len(self._items)
