"""Test del esqueleto HTTP: solo confirma que la app levanta y el healthcheck responde."""

from __future__ import annotations

from fastapi.testclient import TestClient

from heatsignal.api.app import create_app


def test_health_endpoint_returns_ok() -> None:
    client = TestClient(create_app())

    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
