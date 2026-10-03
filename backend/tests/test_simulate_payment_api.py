"""Direct API tests for POST /invoices/{id}/simulate-payment.

Previously this endpoint was only exercised indirectly, by mutating
invoice.amount_paid straight in the DB inside workflow tests — this tests
the actual HTTP contract (status codes, response shape, edge cases).

Each test creates its own throwaway company/invoice (no dependency on the
shared narrative seed data) and gets its own `with TestClient(app)` block
(its own event-loop portal), so the engine's connection pool is disposed
after every handoff between a plain asyncio.run() call and a TestClient
block — otherwise a pooled asyncpg connection can end up bound to an
already-closed loop. See tests/test_api_read_endpoints.py for the same
lesson at module scope.
"""

import asyncio
import contextlib
from datetime import timedelta
from decimal import Decimal

from fastapi.testclient import TestClient

from app.core.db import async_session_factory, engine
from app.main import app
from app.models import Company, Invoice
from app.models.enums import CompanySegment, InvoiceStatus
from app.core.time import utc_today

NIL_UUID = "00000000-0000-0000-0000-000000000000"


@contextlib.contextmanager
def api_client():
    with TestClient(app) as client:
        yield client
    asyncio.run(engine.dispose())


async def _create_invoice_async(amount_total: Decimal, amount_paid: Decimal, invoice_number: str) -> str:
    async with async_session_factory() as session:
        company = Company(
            name=f"Payment Test Co {invoice_number}", industry="Testing", segment=CompanySegment.SMB
        )
        session.add(company)
        await session.flush()

        due = utc_today() - timedelta(days=5)
        invoice = Invoice(
            company_id=company.id,
            invoice_number=invoice_number,
            amount_total=amount_total,
            amount_paid=amount_paid,
            issue_date=due - timedelta(days=30),
            due_date=due,
            status=InvoiceStatus.OVERDUE if amount_paid < amount_total else InvoiceStatus.PAID,
        )
        session.add(invoice)
        await session.commit()
        return str(invoice.id)


def create_invoice(amount_total: str, amount_paid: str, invoice_number: str) -> str:
    # Dispose first, clearing out any connection left bound to a just-closed
    # TestClient portal loop from a previous test, before this fresh
    # asyncio.run() loop touches the pool.
    asyncio.run(engine.dispose())
    invoice_id = asyncio.run(
        _create_invoice_async(Decimal(amount_total), Decimal(amount_paid), invoice_number)
    )
    asyncio.run(engine.dispose())
    return invoice_id


def test_simulate_payment_full_amount():
    invoice_id = create_invoice("50000.00", "0.00", "INV-PAYTEST-A001")
    with api_client() as client:
        resp = client.post(f"/invoices/{invoice_id}/simulate-payment")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "PAID"
    assert Decimal(body["amount_paid"]) == Decimal("50000.00")


def test_simulate_payment_partial_amount():
    invoice_id = create_invoice("50000.00", "0.00", "INV-PAYTEST-B001")
    with api_client() as client:
        resp = client.post(f"/invoices/{invoice_id}/simulate-payment", json={"amount": "20000.00"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "PARTIALLY_PAID"
    assert Decimal(body["amount_paid"]) == Decimal("20000.00")


def test_simulate_payment_overpayment_is_capped_at_outstanding():
    invoice_id = create_invoice("50000.00", "0.00", "INV-PAYTEST-C001")
    with api_client() as client:
        resp = client.post(f"/invoices/{invoice_id}/simulate-payment", json={"amount": "999999.00"})
    assert resp.status_code == 200
    body = resp.json()
    assert Decimal(body["amount_paid"]) == Decimal("50000.00")
    assert body["status"] == "PAID"


def test_simulate_payment_404_for_missing_invoice():
    with api_client() as client:
        resp = client.post(f"/invoices/{NIL_UUID}/simulate-payment")
    assert resp.status_code == 404


def test_simulate_payment_400_when_already_fully_paid():
    invoice_id = create_invoice("50000.00", "50000.00", "INV-PAYTEST-D001")
    with api_client() as client:
        resp = client.post(f"/invoices/{invoice_id}/simulate-payment")
    assert resp.status_code == 400


def test_simulate_payment_400_for_non_positive_amount():
    invoice_id = create_invoice("50000.00", "0.00", "INV-PAYTEST-E001")
    with api_client() as client:
        resp = client.post(f"/invoices/{invoice_id}/simulate-payment", json={"amount": "-100.00"})
    assert resp.status_code == 400
