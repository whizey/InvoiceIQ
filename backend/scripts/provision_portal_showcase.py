"""Explicit, idempotent provisioning into a NEW dedicated showcase workspace.

Never run this script as part of app startup or during customer registration.
The caller must validate the target database and deliberately set
INVOICEIQ_PORTAL_SHOWCASE=true. It cannot touch legacy accounting tables.
"""
import asyncio
import os
import random
import uuid
from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.engine import make_url

from app.core.db import async_session_factory, engine
from app.api.portal import SHOWCASE_ID
from app.models.portal import PortalWorkspace, PortalInvoice, PortalPayment


def validate_target():
    if os.getenv("INVOICEIQ_PORTAL_SHOWCASE", "").lower() != "true":
        raise RuntimeError("Set INVOICEIQ_PORTAL_SHOWCASE=true explicitly")
    database = make_url(os.environ["DATABASE_URL"]).database
    if database not in {"invoiceiq_demo", "invoiceiq_demo_test", "invoiceiq_portal_test"}:
        raise RuntimeError("STOP: showroom provisioning is restricted to disposable demo databases")


async def main():
    validate_target()
    try:
        async with async_session_factory() as db:
            workspace = await db.get(PortalWorkspace, SHOWCASE_ID)
            if workspace and workspace.kind != "showcase":
                raise RuntimeError("Existing workspace is not a showcase")
            if not workspace:
                db.add(PortalWorkspace(id=SHOWCASE_ID, name="InvoiceIQ Showcase", kind="showcase"))
                await db.flush()
            count = await db.scalar(select(func.count()).select_from(PortalInvoice).where(PortalInvoice.workspace_id == SHOWCASE_ID))
            if count:
                print(f"Showcase already populated: {count} invoices")
                return
            rng = random.Random(26015)
            today = date.today()
            # Coherent records, reproducible within a given cutoff.
            # Probabilities are omitted: do not claim an unexecuted ML model produced scores.
            for i in range(1500):
                posted = today - timedelta(days=rng.randint(5, 420))
                term = rng.choice((15, 30, 45, 60, 90))
                amount = Decimal(rng.randint(150, 12500) * 100)
                fraction = rng.choice((Decimal("0"), Decimal("0"), Decimal("0.25"),
                                       Decimal("0.5"), Decimal("1"), Decimal("1")))
                invoice = PortalInvoice(
                    id=uuid.uuid5(SHOWCASE_ID, f"invoice:{i}"), workspace_id=SHOWCASE_ID,
                    invoice_number=f"IQ-{posted.year}-{i+1:05d}",
                    counterparty=f"Account {i % 97 + 1:03d}",
                    issue_date=posted, posting_date=posted,
                    due_date=posted + timedelta(days=term),
                    amount_total=amount, amount_paid=(amount * fraction).quantize(Decimal(".01")),
                    risk_probability=None, risk_provenance=None,
                )
                db.add(invoice)
                if fraction > 0:
                    paid_on = min(today, posted + timedelta(days=term + rng.randint(-12, 55)))
                    db.add(PortalPayment(id=uuid.uuid5(SHOWCASE_ID, f"payment:{i}"),
                                         workspace_id=SHOWCASE_ID, invoice_id=invoice.id,
                                         paid_on=paid_on, amount=invoice.amount_paid))
                if i % 200 == 0:
                    await db.flush()
            await db.commit()
            print("Provisioned 1500 isolated illustrative invoices (no fake ML scores)")
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
