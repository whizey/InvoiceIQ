"""Opt-in, read-only InvoiceIQ PostgreSQL integration tests."""

import asyncio
import os
import unittest

from collections import defaultdict
from datetime import date, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import func, select, text
from sqlalchemy.engine.url import make_url

from app.core.config import settings
from app.core.db import async_session_factory
from app.models import Invoice, Payment
from app.services.analytics_reconciliation import (
    build_ledger_evidence,
    reconciliation_for_interval,
)

START = date(2025, 8, 5)
END = date(2026, 9, 4)
TZ = ZoneInfo("Asia/Kolkata")


def total(report, field):
    return sum(
        (
            Decimal(values[field])
            for values in report.get("currencies", {}).values()
        ),
        Decimal(0),
    )


def status(value):
    return str(getattr(value, "value", value)).upper()


async def checks(t):
    if os.environ.get("INVOICEIQ_PHASE6_PG") != "1":
        raise unittest.SkipTest("PostgreSQL integration is opt-in")

    url = make_url(settings.database_url)

    assert (
        settings.env.lower() == "local"
        and url.get_backend_name() == "postgresql"
        and url.host in ("localhost", "127.0.0.1")
        and url.port == 5435
        and url.database == "recovery_db"
    ), "STOP: Unexpected database configuration"

    def interval(currency):
        return SimpleNamespace(
            start_date=START,
            end_date=END,
            currency=currency,
            source_timezone="Asia/Kolkata",
        )

    def identity(*company_ids):
        return SimpleNamespace(company_ids=company_ids)

    async with async_session_factory() as db:
        async with db.begin():
            await db.execute(text(
                "SET TRANSACTION ISOLATION LEVEL "
                "REPEATABLE READ, READ ONLY"
            ))
            await db.execute(text(
                "SET LOCAL statement_timeout = '15000ms'"
            ))

            posted = func.coalesce(
                Invoice.posting_date, Invoice.issue_date
            )

            tenants = (await db.execute(
                select(Invoice.company_id, func.count())
                .where(
                    Invoice.currency == "INR",
                    posted.between(START, END),
                )
                .group_by(Invoice.company_id)
                .order_by(func.count().desc())
                .limit(2)
            )).all()

            t.assertEqual(
                len(tenants), 2,
                "Two populated companies required"
            )

            (company_a, count_a), (company_b, count_b) = tenants

            a = await reconciliation_for_interval(
                db, interval("INR"),
                identity(company_a), cutoff=END,
            )

            b = await reconciliation_for_interval(
                db, interval("INR"),
                identity(company_b), cutoff=END,
            )

            combined = await reconciliation_for_interval(
                db, interval("INR"),
                identity(company_a, company_b), cutoff=END,
            )

            # 1. Company isolation
            for report, expected_count in (
                (a, count_a),
                (b, count_b),
            ):
                t.assertNotEqual(report["status"], "unavailable")
                t.assertEqual(report["invoice_rows"], expected_count)
                t.assertEqual(report["as_of"], END.isoformat())
                t.assertFalse(
                    report["provisional_issue_date_fallback"]
                )

                t.assertEqual(
                    expected_count,
                    report["verified_invoices"]
                    + report["unverifiable_count"]
                    + report["excluded_not_posted"],
                )

                t.assertTrue(
                    set(report["currencies"]).issubset({"INR"})
                )

                for values in report["currencies"].values():
                    t.assertEqual(
                        Decimal(values["face_amount"])
                        - Decimal(values["cash_received_as_of"]),
                        Decimal(values["outstanding_as_of"]),
                    )

                    t.assertEqual(
                        sum(
                            map(Decimal, values["aging"].values()),
                            Decimal(0),
                        ),
                        Decimal(values["outstanding_as_of"]),
                    )

            print("PASS: Company-scoped invoice counts")
            print("PASS: Financial balance reconciliation")
            print("PASS: Aging bucket reconciliation")

            # 2. Two companies must reconcile independently
            t.assertEqual(
                combined["invoice_rows"],
                count_a + count_b,
            )

            t.assertEqual(
                combined["verified_invoices"],
                a["verified_invoices"] + b["verified_invoices"],
            )

            for field in (
                "face_amount",
                "cash_received_as_of",
                "outstanding_as_of",
                "overdue_as_of",
            ):
                t.assertEqual(
                    total(combined, field),
                    total(a, field) + total(b, field),
                )

            print("PASS: Cross-company financial isolation")

            # 3. Unrelated company must return nothing
            nonexistent = UUID(
                "00000000-0000-0000-0000-000000000000"
            )

            existing = await db.scalar(
                select(func.count())
                .select_from(Invoice)
                .where(Invoice.company_id == nonexistent)
            )

            t.assertEqual(existing, 0)

            empty = await reconciliation_for_interval(
                db, interval("INR"),
                identity(nonexistent), cutoff=END,
            )

            t.assertEqual(empty["invoice_rows"], 0)
            t.assertEqual(empty["currencies"], {})

            print("PASS: Unrelated company cannot enter results")

            # 4. Currency must be correctly scoped
            expected_usd = await db.scalar(
                select(func.count())
                .select_from(Invoice)
                .where(
                    Invoice.company_id == company_a,
                    Invoice.currency == "USD",
                    posted.between(START, END),
                )
            )

            usd = await reconciliation_for_interval(
                db, interval("USD"),
                identity(company_a), cutoff=END,
            )

            t.assertEqual(usd["invoice_rows"], expected_usd)
            t.assertTrue(
                set(usd["currencies"]).issubset({"USD"})
            )

            print("PASS: Currency isolation")

            # 5. Use a real stored payment to verify history
            invoices = list((await db.scalars(
                select(Invoice)
                .where(
                    Invoice.currency == "INR",
                    Invoice.posting_date.between(START, END),
                )
                .limit(5001)
            )).all())

            t.assertLessEqual(len(invoices), 5000)

            invoice_ids = {invoice.id for invoice in invoices}

            payments = list((await db.scalars(
                select(Payment)
                .where(Payment.invoice_id.in_(invoice_ids))
                .limit(30001)
            )).all()) if invoice_ids else []

            t.assertLessEqual(len(payments), 30000)

            grouped = defaultdict(list)

            for payment in payments:
                grouped[payment.invoice_id].append(payment)

            found = False

            for invoice in invoices:
                events = grouped[invoice.id]

                successful = [
                    p for p in events
                    if status(p.status) == "SUCCESS"
                ]

                if (
                    not successful
                    or invoice.due_date is None
                    or status(invoice.status)
                    in ("CANCELLED", "WRITTEN_OFF")
                ):
                    continue

                if any(
                    p.currency != invoice.currency
                    or not isinstance(p.payment_date, datetime)
                    or p.payment_date.tzinfo is None
                    for p in successful
                ):
                    continue

                recorded_total = Decimal(
                    str(invoice.amount_paid)
                )

                event_total = sum(
                    (Decimal(str(p.amount)) for p in successful),
                    Decimal(0),
                )

                if recorded_total != event_total:
                    continue

                candidate_dates = sorted({
                    p.payment_date.astimezone(TZ).date()
                    for p in successful
                    if p.payment_date.astimezone(TZ).date()
                    > invoice.posting_date
                })

                if not candidate_dates:
                    continue

                payment_day = candidate_dates[0]

                before = build_ledger_evidence(
                    [invoice], events,
                    cutoff=payment_day - timedelta(days=1),
                    timezone="Asia/Kolkata",
                )

                after = build_ledger_evidence(
                    [invoice], events,
                    cutoff=payment_day,
                    timezone="Asia/Kolkata",
                )

                t.assertEqual(before["verified_invoices"], 1)
                t.assertEqual(after["verified_invoices"], 1)

                received_on_day = sum(
                    (
                        Decimal(str(p.amount))
                        for p in successful
                        if p.payment_date.astimezone(TZ).date()
                        == payment_day
                    ),
                    Decimal(0),
                )

                t.assertGreater(received_on_day, 0)

                t.assertEqual(
                    total(after, "cash_received_as_of")
                    - total(before, "cash_received_as_of"),
                    received_on_day,
                )

                t.assertEqual(
                    total(before, "outstanding_as_of")
                    - total(after, "outstanding_as_of"),
                    received_on_day,
                )

                found = True
                break

            if found:
                print(
                    "PASS: Historical payment cutoff "
                    "verified using a real PostgreSQL event"
                )
            else:
                print(
                    "SKIP: No suitable historical payment found; "
                    "existing unit tests still cover this rule"
                )

            print("PASS: Read-only PostgreSQL transaction")


class Phase6PostgresTests(unittest.TestCase):
    def test_company_isolation_and_financial_history(self):
        asyncio.run(checks(self))


if __name__ == "__main__":
    unittest.main(verbosity=2)
