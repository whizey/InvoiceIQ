"""Owned synthetic browser fixtures, restricted to a disposable *_test DB.

Prepare: .venv/bin/python scripts/browser_analytics_fixture.py
Cleanup: .venv/bin/python scripts/browser_analytics_fixture.py --cleanup
Credentials/config are written to private temporary files, never printed.
"""

import asyncio
from datetime import date, datetime, timezone
import json
import os
from pathlib import Path
import secrets
import sys
from uuid import UUID, uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.core.config import settings  # noqa: E402 - path setup precedes application imports
from scripts.test_database_safety import install_connection_guard, validate_url, verify  # noqa: E402

if "TEST_DATABASE_URL" not in os.environ:
    raise SystemExit("Set TEST_DATABASE_URL to the verified disposable invoiceiq_test database")
url = validate_url(os.environ["TEST_DATABASE_URL"])
if url.database != "invoiceiq_test":
    raise SystemExit("Browser fixtures require invoiceiq_test")
settings.database_url = url.render_as_string(hide_password=False)
verify(settings.database_url)
install_connection_guard(settings.database_url)
settings.scheduler_enabled = False
settings.outbox_dispatch_enabled = False
settings.google_api_key = settings.openai_api_key = settings.groq_api_key = None
from sqlalchemy import delete  # noqa: E402
from app.core.db import async_session_factory, engine  # noqa: E402
from app.models import Company, Invoice, RecoveryCase, InferenceRecord  # noqa: E402
from app.models.enums import CompanySegment, InvoiceStatus  # noqa: E402

state_file = Path("/private/tmp/invoiceiq-analytics-browser-state.json")


async def main():
    async with async_session_factory() as db:
        if "--cleanup" in sys.argv:
            if state_file.exists():
                state = json.loads(state_file.read_text())
                await db.execute(
                    delete(InferenceRecord).where(InferenceRecord.company_id == UUID(state["company_id"]))
                )
                await db.execute(delete(Company).where(Company.id == UUID(state["company_id"])))
                await db.commit()
                state_file.unlink()
                Path("/private/tmp/invoiceiq-analytics-browser.env").unlink(missing_ok=True)
            return
        c = Company(
            name="Synthetic analytics browser workspace", industry="Testing", segment=CompanySegment.SMB
        )
        db.add(c)
        await db.flush()
        for month, amount in [(5, 100), (8, 200), (9, 300)]:
            day = date(2026, month, 1)
            i = Invoice(
                company_id=c.id,
                invoice_number=f"BROWSER-{uuid4().hex}",
                amount_total=amount,
                amount_paid=0,
                currency="INR",
                issue_date=day,
                posting_date=day,
                due_date=day,
                status=InvoiceStatus.OVERDUE,
            )
            db.add(i)
            await db.flush()
            db.add(
                RecoveryCase(
                    invoice_id=i.id,
                    company_id=c.id,
                    revenue_at_risk=amount,
                    opened_at=datetime(2026, month, 1, tzinfo=timezone.utc),
                )
            )
        await db.commit()
        token = secrets.token_urlsafe(32)
        state_file.write_text(
            json.dumps({"company_id": str(c.id), "token": token, "database_url": settings.database_url})
        )
        state_file.chmod(0o600)
        env = {
            "DATABASE_URL": settings.database_url,
            "ANALYTICS_API_KEYS": json.dumps(
                {"browser-acceptance": {"token": token, "company_ids": [str(c.id)]}}
            ),
            "SCHEDULER_ENABLED": "false",
            "OUTBOX_DISPATCH_ENABLED": "false",
            "GOOGLE_API_KEY": "",
            "OPENAI_API_KEY": "",
            "GROQ_API_KEY": "",
            "RESEND_API_KEY": "",
            "SARVAM_API_KEY": "",
        }
        import shlex

        path = Path("/private/tmp/invoiceiq-analytics-browser.env")
        path.write_text("\n".join(f"export {key}={shlex.quote(value)}" for key, value in env.items()))
        path.chmod(0o600)
        print("Synthetic browser workspace prepared in isolated test database.")
    await engine.dispose()


asyncio.run(main())
