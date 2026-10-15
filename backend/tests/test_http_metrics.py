"""Metric access and label cardinality are enforced without requiring a running database."""

from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.requests import Request

from app.core.config import settings
from app.core.metrics import MetricsMiddleware, allow_metrics, metrics_response, route_family


def test_metric_route_never_includes_sensitive_identifiers():
    assert route_family("/recovery-cases/123e4567-e89b-12d3-a456-426614174000") == "recovery-cases"
    assert route_family("/some-private-customer-1234") == "other"
    assert route_family("/health/db") == "health"


def test_hosted_metrics_require_dedicated_secret():
    key = "disposable-only-for-this-test-collector-token"
    assert not allow_metrics(environment="staging", configured_token=None, provided_token=key)
    assert not allow_metrics(environment="production", configured_token=key, provided_token="wrong")
    assert allow_metrics(environment="staging", configured_token=key, provided_token=key)
    assert allow_metrics(environment="local", configured_token=None, provided_token=None)


def test_metrics_opt_in_and_private_in_hosted_environments(monkeypatch):
    application = FastAPI()
    application.add_middleware(MetricsMiddleware)

    @application.get("/health")
    def health():
        return {"status": "ok"}

    @application.get("/internal/metrics")
    def metrics(request: Request):
        return metrics_response(request)

    monkeypatch.setattr(settings, "env", "production")
    monkeypatch.setattr(settings, "metrics_enabled", False)
    with TestClient(application) as client:
        assert client.get("/internal/metrics").status_code == 404
        monkeypatch.setattr(settings, "metrics_enabled", True)
        monkeypatch.setattr(settings, "metrics_token", "disposable-only-for-this-test-collector-token")
        assert client.get("/internal/metrics").status_code == 403
        assert client.get("/health").status_code == 200
        response = client.get("/internal/metrics", headers={
            "x-invoiceiq-metrics-key": "disposable-only-for-this-test-collector-token"
        })
        assert response.status_code == 200
        assert "invoiceiq_http_requests_total" in response.text
