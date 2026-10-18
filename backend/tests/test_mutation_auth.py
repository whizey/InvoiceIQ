"""Regression tests: hosted mutation endpoints must fail closed."""

from fastapi import FastAPI
from fastapi.testclient import TestClient
from app.core.mutation_auth import HostedMutationAuthMiddleware

from app.core.config import settings
from app.core.mutation_auth import is_write_authorized

app = FastAPI()
app.add_middleware(HostedMutationAuthMiddleware)

@app.get("/health")
def health():
    return {"ok": True}

@app.post("/recovery-cases/detect-overdue")
def mutate():
    return {"unsafe": True}


def test_write_policy_is_fail_closed():
    key = "this-is-only-a-disposable-test-value"
    assert is_write_authorized(environment="production", configured_key=None, provided_key=key) == "misconfigured"
    assert is_write_authorized(environment="staging", configured_key="short", provided_key="short") == "misconfigured"
    assert is_write_authorized(environment="production", configured_key=key, provided_key=None) == "unauthorized"
    assert is_write_authorized(environment="production", configured_key=key, provided_key="wrong") == "unauthorized"
    assert is_write_authorized(environment="production", configured_key=key, provided_key=key) == "authorized"
    assert is_write_authorized(environment="test", configured_key=None, provided_key=None) == "local"


def test_hosted_unauthorized_write_never_reaches_the_database(monkeypatch):
    monkeypatch.setattr(settings, "env", "production")
    monkeypatch.delenv("INVOICEIQ_OPERATOR_API_KEY", raising=False)
    with TestClient(app) as client:
        response = client.post("/recovery-cases/detect-overdue")
        assert response.status_code == 503
        assert "password" not in response.text.lower()

        monkeypatch.setenv("INVOICEIQ_OPERATOR_API_KEY", "this-is-only-a-disposable-test-value")
        response = client.post("/recovery-cases/detect-overdue")
        assert response.status_code == 401
        assert response.json()["detail"] == "Operator authentication required"

        # No DB call is needed to exercise health reads and this auth gate.
        assert client.get("/health").status_code == 200
