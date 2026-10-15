"""Company-scoped, read-only ledger evidence for analytics.

The reconciliation report supplements existing metrics. It never
promotes missing posting dates or undocumented payments into
verified financial balances.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date
from typing import Any

from sqlalchemy import func, select

from app.core.config import settings
from app.models import Invoice, Payment
from app.services.receivable_ledger_audit import audit_invoices


MAX_INVOICES = 5000
MAX_PAYMENTS = 30000


def enum_value(value: Any) -> str:
    return str(getattr(value, "value", value)).upper()


def build_ledger_evidence(
    invoices: list[Any],
    payments: list[Any],
    *,
    cutoff: date,
    timezone: str,
) -> dict[str, Any]:
    payments_by_invoice = defaultdict(list)

    for payment in payments:
        payments_by_invoice[payment.invoice_id].append({
            "id": str(payment.id),
            "invoice_id": str(payment.invoice_id),
            "currency": payment.currency,
            "amount": payment.amount,
            "status": enum_value(payment.status),
            "payment_date": payment.payment_date,
        })

    rows = []

    for invoice in invoices:
        rows.append({
            "id": str(invoice.id),
            "company_id": str(invoice.company_id) if getattr(invoice, "company_id", None) else None,
            "currency": invoice.currency,
            "amount_total": invoice.amount_total,
            "amount_paid": invoice.amount_paid,
            "posting_date": invoice.posting_date,
            "due_date": invoice.due_date,
            "status": enum_value(invoice.status),
            "payments": payments_by_invoice[invoice.id],
        })

    try:
        report = audit_invoices(
            rows,
            as_of=cutoff,
            timezone=timezone,
        )
    except (ValueError, TypeError, OverflowError):
        return {
            "status": "unavailable",
            "reason": "invalid_ledger_record_requires_investigation",
            "cutoff": cutoff.isoformat(),
            "invoice_rows": len(invoices),
            "verified_balances": None,
        }

    verified = sum(
        group["verified_invoices"]
        for group in report["currencies"].values()
    )

    report.update({
        "verified_invoices": verified,
        "verified_coverage_percent": (
            round(100 * verified / len(invoices), 2)
            if invoices else None
        ),
        "date_selection": (
            "posting_date_else_issue_date_for_cohort_only"
        ),
        "accounting_date_rule": (
            "actual_posting_date_required_for_verified_balances"
        ),
        "provisional_issue_date_fallback": False,
        "financial_reporting_note": (
            "Balances describe verified invoices only. "
            "Unverifiable records are excluded, never assigned "
            "a false zero. Collections do not establish causal "
            "recovery effectiveness."
        ),
    })

    return report


async def reconciliation_for_interval(
    db,
    interval,
    identity,
    *,
    cutoff: date,
) -> dict[str, Any]:
    """Generate scoped financial evidence without mutating records."""

    timezone = (
        interval.source_timezone
        or settings.business_timezone
    )

    cohort_date = func.coalesce(
        Invoice.posting_date,
        Invoice.issue_date,
    )

    stmt = (
        select(Invoice)
        .where(
            Invoice.company_id.in_(identity.company_ids),
            Invoice.currency == interval.currency,
            cohort_date >= interval.start_date,
            cohort_date <= cutoff,
        )
        .order_by(Invoice.id)
        .limit(MAX_INVOICES + 1)
    )

    invoices = list((await db.scalars(stmt)).all())

    if len(invoices) > MAX_INVOICES:
        return {
            "status": "unavailable",
            "reason": "cohort_too_large_for_interactive_reconciliation",
            "limit": MAX_INVOICES,
            "cutoff": cutoff.isoformat(),
        }

    payments = []
    ids = [invoice.id for invoice in invoices]

    if ids:
        payment_stmt = (
            select(Payment)
            .where(Payment.invoice_id.in_(ids))
            .order_by(Payment.id)
            .limit(MAX_PAYMENTS + 1)
        )

        payments = list(
            (await db.scalars(payment_stmt)).all()
        )

        if len(payments) > MAX_PAYMENTS:
            return {
                "status": "unavailable",
                "reason": "payment_history_too_large",
                "limit": MAX_PAYMENTS,
                "cutoff": cutoff.isoformat(),
            }

    return build_ledger_evidence(
        invoices,
        payments,
        cutoff=cutoff,
        timezone=timezone,
    )
