"""Isolated browser fixture. Never serves or mutates a non-test database."""

import asyncio
import json
import os
from pathlib import Path
import sys
from datetime import datetime, timedelta
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from sqlalchemy.engine import make_url
from app.core.config import settings

url = make_url(settings.database_url).set(database="invoiceiq_dashboard_test")
settings.database_url = url.render_as_string(hide_password=False)
os.environ["DATABASE_URL"] = settings.database_url
settings.scheduler_enabled = False
settings.outbox_dispatch_enabled = False
from app.core.db import async_session_factory, engine  # noqa: E402
from app.models import Company, Invoice, Payment  # noqa: E402
from app.models.enums import CompanySegment, InvoiceStatus, PaymentMethod, PaymentStatus  # noqa: E402
from sqlalchemy import delete  # noqa: E402
from zoneinfo import ZoneInfo  # noqa: E402


async def seed():
    today = datetime.now(ZoneInfo(settings.business_timezone)).date()
    async with async_session_factory() as db:
        company = Company(name="Browser test customer", segment=CompanySegment.SMB)
        db.add(company)
        await db.flush()
        for i in range(26):
            invoice = Invoice(
                company_id=company.id,
                invoice_number="BROWSER-" + uuid4().hex[:12],
                amount_total=1000 + i * 100,
                amount_paid=200 if i % 2 == 0 else 0,
                currency="INR",
                issue_date=today - timedelta(days=i),
                posting_date=today - timedelta(days=i),
                due_date=today - timedelta(days=i * 3),
                status=InvoiceStatus.PARTIALLY_PAID if i % 2 == 0 else InvoiceStatus.SENT,
            )
            db.add(invoice)
            await db.flush()
            if i % 2 == 0:
                db.add(
                    Payment(
                        invoice_id=invoice.id,
                        amount=200,
                        currency="INR",
                        payment_date=datetime.now(ZoneInfo(settings.business_timezone)),
                        method=PaymentMethod.OTHER,
                        status=PaymentStatus.SUCCESS,
                    )
                )
        await db.commit()
        return company.id


async def run():
    import uvicorn

    cid = await seed()
    os.environ["ANALYTICS_API_KEYS"] = json.dumps(
        {"browser-test": {"token": "isolated-browser-fixture", "company_ids": [str(cid)]}}
    )
    from app.main import app

    try:
        await uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=8017, log_level="warning")).serve()
    finally:
        async with async_session_factory() as db:
            await db.execute(delete(Company).where(Company.id == cid))
            await db.commit()
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(run())
