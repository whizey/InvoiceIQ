"""Builds a RiskFeatures vector from real payment history in Postgres.

This is the "load_customer_context" step feeding the risk model — the real-
data counterpart to app/ml/synthetic_data.py's training generator. Both
must agree on feature meaning and order (app/ml/features.FEATURE_NAMES);
only the data source differs.
"""


from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.time import utc_today
from app.ml.features import RiskFeatures
from app.models import Invoice, Payment, RecoveryCase
from app.models.enums import InvoiceStatus, RecoveryCaseStatus

# A company with no prior recovery-case history gets a neutral prior rather
# than 0.0 (which would unfairly read as "always fails to recover").
NEUTRAL_PRIOR_RECOVERY_SUCCESS_RATE = 0.5


async def build_risk_features(session: AsyncSession, invoice: Invoice) -> RiskFeatures:
    today = utc_today()
    days_overdue = max((today - invoice.due_date).days, 0)
    outstanding_balance = float(invoice.amount_total - invoice.amount_paid)

    paid_invoices_stmt = select(Invoice).where(
        Invoice.company_id == invoice.company_id,
        Invoice.id != invoice.id,
        Invoice.status == InvoiceStatus.PAID,
    )
    paid_invoices = list((await session.execute(paid_invoices_stmt)).scalars().all())

    delays: list[int] = []
    for paid_invoice in paid_invoices:
        payment_stmt = (
            select(Payment)
            .where(Payment.invoice_id == paid_invoice.id)
            .order_by(Payment.payment_date.desc())
        )
        payment = (await session.execute(payment_stmt)).scalars().first()
        if payment is not None:
            delays.append((payment.payment_date.date() - paid_invoice.due_date).days)

    avg_historical_payment_delay = sum(delays) / len(delays) if delays else 0.0
    num_prior_late_payments = sum(1 for d in delays if d > 0)
    num_prior_on_time_payments = sum(1 for d in delays if d <= 0)

    earliest_issue_result = await session.execute(
        select(func.min(Invoice.issue_date)).where(Invoice.company_id == invoice.company_id)
    )
    earliest_issue = earliest_issue_result.scalar_one()
    customer_tenure_days = (today - earliest_issue).days if earliest_issue else 0

    prior_cases_stmt = select(RecoveryCase).where(
        RecoveryCase.company_id == invoice.company_id,
        RecoveryCase.status.in_([RecoveryCaseStatus.CLOSED, RecoveryCaseStatus.CLOSED_UNRECOVERED]),
    )
    prior_cases = list((await session.execute(prior_cases_stmt)).scalars().all())
    if prior_cases:
        successes = sum(1 for c in prior_cases if c.recovered_amount and c.recovered_amount > 0)
        prior_recovery_success_rate = successes / len(prior_cases)
    else:
        prior_recovery_success_rate = NEUTRAL_PRIOR_RECOVERY_SUCCESS_RATE

    # Counts OVERDUE *and* PARTIALLY_PAID. Only OVERDUE was counted before,
    # which made this feature flicker: simulate_payment sets a late invoice
    # to PARTIALLY_PAID (correctly -- mark_overdue_invoices flips it back on
    # the next sweep), so between the payment and that sweep the invoice
    # vanished from this count and the model scored a company as having one
    # fewer outstanding bill than it does. A partly paid overdue invoice is
    # still an open invoice, and this now matches the set of statuses
    # mark_overdue_invoices itself treats as outstanding.
    open_invoices_result = await session.execute(
        select(func.count(Invoice.id)).where(
            Invoice.company_id == invoice.company_id,
            Invoice.id != invoice.id,
            Invoice.status.in_([InvoiceStatus.OVERDUE, InvoiceStatus.PARTIALLY_PAID]),
        )
    )
    num_open_invoices = open_invoices_result.scalar_one()

    return RiskFeatures(
        invoice_amount=float(invoice.amount_total),
        days_overdue=days_overdue,
        outstanding_balance=outstanding_balance,
        avg_historical_payment_delay=avg_historical_payment_delay,
        num_prior_late_payments=num_prior_late_payments,
        num_prior_on_time_payments=num_prior_on_time_payments,
        customer_tenure_days=customer_tenure_days,
        prior_recovery_success_rate=prior_recovery_success_rate,
        num_open_invoices=num_open_invoices,
    )
