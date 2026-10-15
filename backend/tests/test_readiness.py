from unittest.mock import AsyncMock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import health, ml_intelligence


def test_readiness_distinguishes_primary_and_optional_dependencies(monkeypatch):
    app = FastAPI()
    app.include_router(health.router)
    client = TestClient(app)
    monkeypatch.setattr(health, "check_db_connection", AsyncMock(return_value=True))
    monkeypatch.setattr(ml_intelligence, "list_models", lambda: {
        "primary_model": "b2b_v2", "models": [
            {"available": True, "version": "test-version"}, {"available": False},
        ],
    })
    assert client.get("/health/ready").status_code == 200
    assert client.get("/health/ready").json()["bpi_available"] is False
    monkeypatch.setattr(ml_intelligence, "list_models", lambda: {"models": [{"available": False}]})
    assert client.get("/health/ready").status_code == 503
    monkeypatch.setattr(health, "check_db_connection", AsyncMock(side_effect=ConnectionError("private-host")))
    response = client.get("/health/ready")
    assert response.status_code == 503
    assert "private-host" not in response.text
    assert client.get("/health").status_code == 200
