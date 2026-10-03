from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ml.payment_features import (
    COUNT_CAP,
    RATIO_CLIP,
    PaymentRiskFeatures,
)
from app.models import Invoice, Payment
from app.models.enums import PaymentStatus




class PaymentRiskInputsUnavailable(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class PriorInvoiceSnapshot:
    invoice_number: str
    amount: float
    posting_date: date
    due_date: date
    clearing_date: date | None


def _full_resolution_date(
    invoice: Invoice,
    payments: list[Payment],
) -> date | None:
    paid = Decimal("0")
    target = Decimal(invoice.amount_total)

    ordered = sorted(
        payments,
        key=lambda p: (
            p.payment_date,
            str(getattr(p, "id", "")),
        ),
    )

    for payment in ordered:
        paid += Decimal(payment.amount)

        if paid >= target:
            return payment.payment_date.date()

    return None


def compute_payment_risk_features(
    *,
    amount: float,
    posting_date: date,
    payment_term: int,
    history: list[PriorInvoiceSnapshot],
) -> PaymentRiskFeatures:
    prior = sorted(
        [
            row
            for row in history
            if row.posting_date < posting_date
        ],
        key=lambda row: (
            row.posting_date,
            row.invoice_number,
        ),
    )

    prior_invoice_count = len(prior)
    prior_amount_sum = sum(row.amount for row in prior)

    resolved = [
        row
        for row in prior
        if (
            row.clearing_date is not None
            and row.clearing_date < posting_date
        )
    ]

    outcomes = sorted(
        [
            (
                row.clearing_date,
                row.posting_date,
                row.invoice_number,
                (row.clearing_date - row.due_date).days,
            )
            for row in resolved
            if row.clearing_date is not None
        ],
        key=lambda item: (
            item[0],
            item[3],
        ),
    )

    resolved_count = len(outcomes)
    open_invoice_count = (
        prior_invoice_count - resolved_count
    )

    days_late_values = [
        float(item[3])
        for item in outcomes
    ]

    late_values = [
        1 if item[3] > 0 else 0
        for item in outcomes
    ]

    late_count = sum(late_values)

    if resolved_count:
        raw_historical_late_rate = (
            late_count / resolved_count
        )
        historical_avg_days_late = (
            sum(days_late_values) / resolved_count
        )
    else:
        raw_historical_late_rate = 0.5
        historical_avg_days_late = 0.0

    if prior_invoice_count:
        historical_avg_invoice_amount = (
            prior_amount_sum / prior_invoice_count
        )
        open_invoice_ratio = (
            open_invoice_count / prior_invoice_count
        )
    else:
        historical_avg_invoice_amount = 0.0
        open_invoice_ratio = 0.0

    if prior:
        first_posting_date = min(
            row.posting_date
            for row in prior
        )
        previous_posting_date = max(
            row.posting_date
            for row in prior
        )
    else:
        first_posting_date = posting_date
        previous_posting_date = None

    customer_tenure_days = max(
        (posting_date - first_posting_date).days,
        0,
    )

    if previous_posting_date is None:
        days_since_previous_invoice = 0
    else:
        days_since_previous_invoice = max(
            (
                posting_date
                - previous_posting_date
            ).days,
            0,
        )

    recent_10 = outcomes[-10:]
    recent_5 = recent_10[-5:]

    if recent_5:
        raw_recent_5_late_rate = (
            sum(
                1 if item[3] > 0 else 0
                for item in recent_5
            )
            / len(recent_5)
        )

        recent_5_avg_days_late = (
            sum(
                float(item[3])
                for item in recent_5
            )
            / len(recent_5)
        )
    else:
        raw_recent_5_late_rate = 0.5
        recent_5_avg_days_late = 0.0

    recent_late_streak = 0
    recent_on_time_streak = 0

    if recent_10:
        last_late = recent_10[-1][3] > 0
        streak = 0

        for item in reversed(recent_10):
            is_late = item[3] > 0

            if is_late != last_late:
                break

            streak += 1

        if last_late:
            recent_late_streak = streak
        else:
            recent_on_time_streak = streak

    recent_5_late_rate_delta = (
        raw_recent_5_late_rate
        - raw_historical_late_rate
    )

    recent_5_avg_days_late_delta = (
        recent_5_avg_days_late
        - historical_avg_days_late
    )

    if historical_avg_invoice_amount > 0:
        amount_ratio = (
            amount
            / historical_avg_invoice_amount
        )
    else:
        amount_ratio = 1.0

    amount_ratio = min(
        max(float(amount_ratio), 0.0),
        RATIO_CLIP,
    )

    has_resolved_history = (
        1.0 if resolved_count > 0 else 0.0
    )

    if has_resolved_history:
        historical_late_rate = float(
            raw_historical_late_rate
        )
        recent_5_late_rate = float(
            raw_recent_5_late_rate
        )
    else:
        historical_late_rate = float("nan")
        recent_5_late_rate = float("nan")

    return PaymentRiskFeatures(
        amount=float(amount),
        payment_term=float(payment_term),

        prior_resolved_invoice_count=float(
            min(resolved_count, COUNT_CAP)
        ),
        prior_open_invoice_count=float(
            min(open_invoice_count, COUNT_CAP)
        ),
        open_invoice_ratio=float(
            open_invoice_ratio
        ),
        has_resolved_history=has_resolved_history,

        historical_late_rate=historical_late_rate,
        historical_avg_days_late=float(
            historical_avg_days_late
        ),

        historical_avg_invoice_amount=float(
            historical_avg_invoice_amount
        ),
        amount_vs_historical_avg_ratio=float(
            amount_ratio
        ),

        customer_tenure_days=float(
            min(customer_tenure_days, COUNT_CAP)
        ),
        days_since_previous_invoice=float(
            min(
                days_since_previous_invoice,
                COUNT_CAP,
            )
        ),

        recent_5_late_rate=recent_5_late_rate,
        recent_5_avg_days_late=float(
            recent_5_avg_days_late
        ),

        recent_late_streak=float(
            recent_late_streak
        ),
        recent_on_time_streak=float(
            recent_on_time_streak
        ),

        recent_5_late_rate_delta=float(
            recent_5_late_rate_delta
        ),
        recent_5_avg_days_late_delta=float(
            recent_5_avg_days_late_delta
        ),

    )


async def build_payment_risk_features(
    session: AsyncSession,
    invoice: Invoice,
) -> PaymentRiskFeatures:
    if invoice.posting_date is None:
        raise PaymentRiskInputsUnavailable(
            "invoice.posting_date is required"
        )

    if invoice.payment_term is None:
        raise PaymentRiskInputsUnavailable(
            "invoice.payment_term is required"
        )

    if invoice.payment_term < 0:
        raise PaymentRiskInputsUnavailable(
            "invoice.payment_term cannot be negative"
        )

    prior_stmt = (
        select(Invoice)
        .where(
            Invoice.company_id
            == invoice.company_id,
            Invoice.id != invoice.id,
            Invoice.posting_date.is_not(None),
            Invoice.posting_date
            < invoice.posting_date,
        )
        .order_by(
            Invoice.posting_date,
            Invoice.invoice_number,
        )
    )

    prior_invoices = list(
        (
            await session.execute(prior_stmt)
        )
        .scalars()
        .all()
    )

    payments_by_invoice: dict = defaultdict(list)

    if prior_invoices:
        ids = [
            item.id
            for item in prior_invoices
        ]

        payment_stmt = (
            select(Payment)
            .where(
                Payment.invoice_id.in_(ids),
                Payment.status
                == PaymentStatus.SUCCESS,
            )
            .order_by(
                Payment.payment_date,
                Payment.id,
            )
        )

        successful_payments = list(
            (
                await session.execute(
                    payment_stmt
                )
            )
            .scalars()
            .all()
        )

        for payment in successful_payments:
            payments_by_invoice[
                payment.invoice_id
            ].append(payment)

    history: list[PriorInvoiceSnapshot] = []

    for prior_invoice in prior_invoices:
        clearing_date = _full_resolution_date(
            prior_invoice,
            payments_by_invoice[
                prior_invoice.id
            ],
        )

        history.append(
            PriorInvoiceSnapshot(
                invoice_number=(
                    prior_invoice.invoice_number
                ),
                amount=float(
                    prior_invoice.amount_total
                ),
                posting_date=(
                    prior_invoice.posting_date
                ),
                due_date=prior_invoice.due_date,
                clearing_date=clearing_date,
            )
        )

    return compute_payment_risk_features(
        amount=float(invoice.amount_total),
        posting_date=invoice.posting_date,
        payment_term=invoice.payment_term,
        history=history,
    )
