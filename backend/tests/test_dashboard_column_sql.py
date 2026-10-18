"""Live SQL regression for full-cohort preserved dashboard filters.

Uses only the disposable CI database. No Neon or public production writes.
"""
import json
from datetime import datetime, timedelta
from decimal import Decimal
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete

from app.core.config import settings
from app.core.db import async_session_factory, engine
from app.main import app
from app.models import Company, Invoice, Payment, RecoveryCase
from app.models.enums import CompanySegment, InvoiceStatus, PaymentMethod, PaymentStatus


@pytest.mark.asyncio
async def test_column_filters_apply_before_metrics_sorting_and_pagination(monkeypatch):
    await engine.dispose()
    today = datetime.now(ZoneInfo(settings.business_timezone)).date()
    prefix = "COLUMN-CI-" + uuid4().hex[:12]
    ids = {}
    cid = None
    try:
        async with async_session_factory() as db:
            company = Company(name="Isolated column fixture " + prefix,
                              segment=CompanySegment.SMB)
            db.add(company)
            await db.flush()
            cid = company.id
            for label, amount, paid, overdue_days, status in (
                ("A", 100, 0, 30, InvoiceStatus.SENT),
                ("B", 200, 50, 15, InvoiceStatus.PARTIALLY_PAID),
                ("C", 300, 300, -10, InvoiceStatus.PAID),
            ):
                invoice = Invoice(
                    company_id=cid, invoice_number=prefix + "-" + label,
                    amount_total=Decimal(amount), amount_paid=Decimal(paid),
                    currency="INR", issue_date=today - timedelta(days=45),
                    posting_date=today - timedelta(days=25),
                    due_date=today - timedelta(days=overdue_days),
                    status=status,
                )
                db.add(invoice)
                await db.flush()
                ids[label] = invoice.id
                if paid:
                    db.add(Payment(
                        invoice_id=invoice.id, amount=Decimal(paid),
                        currency="INR", payment_date=datetime.now(ZoneInfo(settings.business_timezone)),
                        status=PaymentStatus.SUCCESS, method=PaymentMethod.OTHER,
                    ))
            db.add(RecoveryCase(company_id=cid, invoice_id=ids["B"], revenue_at_risk=150))
            await db.commit()

        monkeypatch.setenv("ANALYTICS_API_KEYS", json.dumps({
            "ci-columns": {"token": "ci-columns-test-only", "company_ids": [str(cid)]}
        }))
        headers = {"Authorization": "Bearer ci-columns-test-only"}
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://ci") as client:
            async def get(**params):
                response = await client.get("/dashboard/preserved", params=params, headers=headers)
                assert response.status_code == 200, response.text
                data = response.json()
                assert data["column_filters_version"] == 1
                assert data["total"] == data["metrics"]["invoice_count"]
                return data

            base = await get(sort="invoice_asc", page_size=2)
            assert base["total"] == 3
            assert [row["invoice_number"] for row in base["invoices"]] == [
                prefix + "-A", prefix + "-B"
            ]
            page = await get(sort="invoice_asc", page_size=2, page=2)
            assert page["total"] == 3
            assert [row["invoice_number"] for row in page["invoices"]] == [prefix + "-C"]
            assert Decimal(base["metrics"]["open"]) == 250
            assert Decimal(base["metrics"]["invoiced"]) == 600

            cases = [
                ({"invoice_number": prefix + "-A"}, {"A"}),
                ({"company_search": prefix}, {"A", "B", "C"}),
                ({"amount_min": "150", "amount_max": "300"}, {"B", "C"}),
                ({"paid_min": "1", "paid_max": "100"}, {"B"}),
                ({"balance_min": "110", "balance_max": "200"}, {"B"}),
                ({"days_min": "20"}, {"A"}),
                ({"status": "settled"}, {"C"}),
                ({"status": "overdue"}, {"A", "B"}),
                ({"case_presence": "yes"}, {"B"}),
                ({"case_presence": "no"}, {"A", "C"}),
                ({"posted_from": str(today - timedelta(days=30)),
                  "posted_to": str(today)}, {"A", "B", "C"}),
                ({"due_from": str(today - timedelta(days=17)),
                  "due_to": str(today + timedelta(days=30))}, {"B", "C"}),
            ]
            for filters, expected in cases:
                selected = await get(**filters, page_size=1, sort="invoice_desc")
                assert selected["total"] == len(expected), (filters, selected)
                assert selected["metrics"]["invoice_count"] == len(expected)
                # Full-cohort totals must not collapse to only the first page.
                assert selected["total"] >= len(selected["invoices"])
                expected_open = sum({"A": 100, "B": 150, "C": 0}[x] for x in expected)
                assert Decimal(selected["metrics"]["open"]) == expected_open, filters
                if len(expected) > 1:
                    second = await get(**filters, page_size=1, page=2, sort="invoice_desc")
                    assert second["total"] == selected["total"]
                    assert second["invoices"][0]["id"] != selected["invoices"][0]["id"]

            for params in [
                {"amount_min": "200", "amount_max": "100"},
                {"risk_min": "101"},
                {"page_size": "101"},
                {"status": "not-a-status"},
            ]:
                rejected = await client.get("/dashboard/preserved", params=params, headers=headers)
                assert rejected.status_code == 422, (params, rejected.text)

            export = await client.get(
                "/dashboard/preserved/export",
                params={"case_presence": "yes", "sort": "balance_desc"},
                headers=headers,
            )
            assert export.status_code == 200, export.text
            assert prefix + "-B" in export.text
            assert prefix + "-A" not in export.text
    finally:
        if cid is not None:
            async with async_session_factory() as db:
                await db.execute(delete(Company).where(Company.id == cid))
                await db.commit()
        await engine.dispose()
