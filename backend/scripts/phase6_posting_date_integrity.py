"""Read-only inspection of invoices with missing posting dates."""

import asyncio
import json
import sys
from collections import Counter, defaultdict
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

from sqlalchemy import func, select, text
from sqlalchemy.engine.url import make_url

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.config import settings
from app.core.db import async_session_factory
from app.models import Invoice


DATE_CANDIDATES = (
    "invoice_date",
    "issue_date",
    "document_date",
    "billing_date",
    "receipt_date",
    "received_date",
    "source_posted_at",
    "imported_at",
    "created_at",
    "updated_at",
)

# These fields are not automatically valid posting-date substitutes.
OPERATIONAL_FIELDS = {"created_at", "updated_at", "imported_at"}


def describe_date(value):
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return str(value) if value is not None else None


async def main():
    url = make_url(settings.database_url)

    if (
        settings.env.lower() != "local"
        or url.host not in {"127.0.0.1", "localhost"}
        or url.port != 5435
        or url.database != "recovery_db"
    ):
        raise SystemExit(
            "STOP: Only local recovery_db on port 5435 "
            "is permitted."
        )

    async with async_session_factory() as session:
        async with session.begin():
            await session.execute(text(
                "SET TRANSACTION ISOLATION LEVEL "
                "REPEATABLE READ, READ ONLY"
            ))

            total = await session.scalar(
                select(func.count()).select_from(Invoice)
            )

            missing_count = await session.scalar(
                select(func.count())
                .select_from(Invoice)
                .where(Invoice.posting_date.is_(None))
            )

            if missing_count > 5000:
                raise SystemExit(
                    "STOP: Too many records for this local audit."
                )

            result = await session.execute(
                select(Invoice)
                .where(Invoice.posting_date.is_(None))
                .limit(5001)
            )

            missing = list(result.scalars().all())

    columns = set(Invoice.__table__.columns.keys())
    candidates = [x for x in DATE_CANDIDATES if x in columns]

    statuses = Counter()
    currencies = Counter()
    face_values = defaultdict(lambda: Decimal("0"))
    candidate_values = defaultdict(list)
    missing_amounts = Counter()

    for invoice in missing:
        status = getattr(invoice, "status", None)
        status = getattr(status, "value", status)

        statuses[str(status)] += 1

        currency = str(getattr(invoice, "currency", "UNKNOWN"))
        currencies[currency] += 1

        raw_amount = getattr(invoice, "amount_total", None)

        if raw_amount is None:
            missing_amounts[currency] += 1
        else:
            try:
                value = Decimal(str(raw_amount))
                if value.is_finite() and value >= 0:
                    face_values[currency] += value
                else:
                    missing_amounts[currency] += 1
            except Exception:
                missing_amounts[currency] += 1

        for field in candidates:
            value = getattr(invoice, field, None)
            if value is not None:
                candidate_values[field].append(describe_date(value))

    date_report = {}

    for field in candidates:
        values = sorted(candidate_values[field])

        date_report[field] = {
            "present": len(values),
            "missing": len(missing) - len(values),
            "earliest": values[0] if values else None,
            "latest": values[-1] if values else None,
            "classification": (
                "operational_timestamp_not_posting_evidence"
                if field in OPERATIONAL_FIELDS
                else "candidate_requires_source_verification"
            ),
        }

    report = {
        "status": "read_only_audit",
        "database": "local_recovery_db",
        "total_invoices": total,
        "missing_posting_date": missing_count,
        "records_inspected": len(missing),
        "missing_by_status": dict(statuses),
        "missing_by_currency": dict(currencies),
        "excluded_raw_face_value_by_currency": {
            key: str(value)
            for key, value in face_values.items()
        },
        "unavailable_face_amount_counts": dict(missing_amounts),
        "available_date_fields": date_report,
        "warning": (
            "Raw excluded face amounts are not verified outstanding "
            "balances. No dates were inferred or written."
        ),
    }

    print(json.dumps(report, indent=2, sort_keys=True))


if __name__ == "__main__":
    asyncio.run(main())
