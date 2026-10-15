"""Read-only local development receivables audit."""

import argparse
import asyncio
import json
import sys

from collections import defaultdict
from pathlib import Path
from uuid import UUID

sys.path.insert(
    0, str(Path(__file__).resolve().parents[1])
)

from sqlalchemy import select, text
from sqlalchemy.engine.url import make_url

from app.core.config import settings
from app.core.db import async_session_factory
from app.models import Invoice, Payment

from app.services.receivable_ledger_audit import (
    audit_invoices,
)


async def main():
    parser = argparse.ArgumentParser()

    parser.add_argument("--as-of", required=True)

    scope = parser.add_mutually_exclusive_group(
        required=True
    )

    scope.add_argument("--company-id", type=UUID)
    scope.add_argument("--all-companies", action="store_true")

    parser.add_argument(
        "--limit", type=int, default=5000
    )

    args = parser.parse_args()

    url = make_url(settings.database_url)

    if (
        settings.env.lower() != "local"
        or url.host not in {"localhost", "127.0.0.1"}
        or url.port != 5435
        or url.database != "recovery_db"
    ):
        raise SystemExit(
            "STOP: Audit is restricted to the local "
            "development recovery_db on port 5435."
        )

    if not 1 <= args.limit <= 20000:
        raise SystemExit("Invalid invoice limit")

    async with async_session_factory() as session:
        async with session.begin():

            await session.execute(
                text(
                    "SET TRANSACTION ISOLATION LEVEL "
                    "REPEATABLE READ, READ ONLY"
                )
            )

            query = (
                select(Invoice)
                .order_by(Invoice.id)
                .limit(args.limit + 1)
            )

            if args.company_id is not None:
                query = query.where(
                    Invoice.company_id == args.company_id
                )

            invoices = list(
                (
                    await session.execute(query)
                ).scalars().all()
            )

            if len(invoices) > args.limit:
                raise SystemExit(
                    "STOP: Invoice limit exceeded. "
                    "No partial report generated."
                )

            ids = [row.id for row in invoices]

            if ids:
                payment_query = select(Payment).where(
                    Payment.invoice_id.in_(ids)
                )

                payments = list(
                    (
                        await session.execute(payment_query)
                    ).scalars().all()
                )
            else:
                payments = []

    by_invoice = defaultdict(list)

    for payment in payments:
        by_invoice[payment.invoice_id].append({
            "id": str(payment.id),
            "amount": payment.amount,
            "currency": payment.currency,
            "status": getattr(
                payment.status,
                "value",
                str(payment.status),
            ),
            "payment_date": payment.payment_date,
        })

    rows = []

    for invoice in invoices:
        rows.append({
            "id": str(invoice.id),
            "currency": invoice.currency,
            "amount_total": invoice.amount_total,
            "amount_paid": invoice.amount_paid,
            "posting_date": invoice.posting_date,
            "due_date": invoice.due_date,
            "status": getattr(
                invoice.status,
                "value",
                str(invoice.status),
            ),
            "payments": by_invoice[invoice.id],
        })

    report = audit_invoices(
        rows,
        as_of=args.as_of,
    )

    report["source"] = "local_read_only_postgresql"
    report["scope"] = (
        "all_local_companies"
        if args.all_companies
        else "single_company"
    )

    print(json.dumps(report, indent=2, sort_keys=True))


if __name__ == "__main__":
    asyncio.run(main())
