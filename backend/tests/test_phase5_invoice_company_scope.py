"""Phase 5 invoice and payment company-isolation tests."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.core.config import settings
from app.core.db import get_db
from app.core.invoice_company_scope import authorize_invoice_mutation
from app.main import app


def request_with_scope(*company_ids):
    return SimpleNamespace(
        state=SimpleNamespace(
            analytics_scope=company_ids
        )
    )


def test_authorized_invoice_company(monkeypatch):
    company = uuid4()
    monkeypatch.setenv("ANALYTICS_API_KEYS", "configured")
    monkeypatch.setenv("PUBLIC_READ_ONLY", "false")

    authorize_invoice_mutation(
        request_with_scope(company),
        company,
    )


def test_cross_company_invoice_denied(monkeypatch):
    company_a = uuid4()
    company_b = uuid4()

    monkeypatch.setenv("ANALYTICS_API_KEYS", "configured")
    monkeypatch.setenv("PUBLIC_READ_ONLY", "false")

    with pytest.raises(HTTPException) as error:
        authorize_invoice_mutation(
            request_with_scope(company_a),
            company_b,
        )

    assert error.value.status_code == 404


def test_missing_invoice_scope_fails_closed(monkeypatch):
    monkeypatch.setenv("ANALYTICS_API_KEYS", "configured")
    monkeypatch.setenv("PUBLIC_READ_ONLY", "false")

    with pytest.raises(HTTPException) as error:
        authorize_invoice_mutation(
            SimpleNamespace(state=SimpleNamespace()),
            uuid4(),
        )

    assert error.value.status_code == 401


def test_public_mode_blocks_invoice_mutation(monkeypatch):
    monkeypatch.setenv("PUBLIC_READ_ONLY", "true")

    with pytest.raises(HTTPException) as error:
        authorize_invoice_mutation(
            request_with_scope(uuid4()),
            uuid4(),
        )

    assert error.value.status_code == 403


@pytest.mark.parametrize("operation", ["create", "payment"])
def test_http_cross_company_financial_mutation_denied(
    monkeypatch, operation
):
    company_a = uuid4()
    company_b = uuid4()
    invoice_id = uuid4()

    monkeypatch.setattr(settings, "env", "development")
    monkeypatch.setenv("PUBLIC_READ_ONLY", "false")
    monkeypatch.setenv(
        "ANALYTICS_API_KEYS",
        json.dumps({
            "phase5-financial-test": {
                "token": "phase5-financial-test-token",
                "company_ids": [str(company_a)],
            }
        }),
    )

    db = AsyncMock()

    db.execute.return_value = SimpleNamespace(
        scalar_one_or_none=lambda: SimpleNamespace(
            id=invoice_id,
            company_id=company_b,
        )
    )

    async def fake_db():
        yield db

    previous = app.dependency_overrides.copy()

    try:
        app.dependency_overrides[get_db] = fake_db

        headers = {
            "x-invoiceiq-analytics-key":
                "phase5-financial-test-token",
        }

        with TestClient(app) as client:
            if operation == "create":
                response = client.post(
                    "/invoices",
                    headers=headers,
                    json={
                        "company_id": str(company_b),
                        "invoice_number": "P5-CROSS-COMPANY",
                        "amount_total": "100.00",
                        "currency": "INR",
                        "issue_date": "2026-01-01",
                        "posting_date": "2026-01-01",
                        "due_date": "2026-01-31",
                        "payment_term": 30,
                        "payment_method_description": "NEFT",
                    },
                )
            else:
                response = client.post(
                    f"/invoices/{invoice_id}/simulate-payment",
                    headers=headers,
                    json={"amount": "10.00"},
                )

    finally:
        app.dependency_overrides.clear()
        app.dependency_overrides.update(previous)

    assert response.status_code == 404, response.text

    if operation == "create":
        db.get.assert_not_awaited()
        db.execute.assert_not_awaited()
    else:
        db.execute.assert_awaited_once()

    db.commit.assert_not_awaited()
    db.add.assert_not_called()
