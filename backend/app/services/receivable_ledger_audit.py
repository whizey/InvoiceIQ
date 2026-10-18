"""Read-only financial reconciliation for InvoiceIQ.

Historical balances are reconstructed from successful dated payments.
Unverifiable records are excluded instead of being assigned a false zero.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable, Mapping
from zoneinfo import ZoneInfo


BUCKETS = (
    "not_due",
    "days_1_30",
    "days_31_60",
    "days_61_90",
    "days_90_plus",
)

EXCLUDED = {"CANCELLED", "WRITTEN_OFF"}


def money(value: Any, label: str) -> Decimal:
    if isinstance(value, (bool, float)) or value is None:
        raise ValueError(f"{label}: invalid monetary value")

    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"{label}: invalid monetary value") from exc

    if (
        not amount.is_finite()
        or amount < 0
        or amount.as_tuple().exponent < -2
    ):
        raise ValueError(
            f"{label}: expected nonnegative amount to two decimal places"
        )

    return amount


def day(value: Any, label: str) -> date:
    if isinstance(value, datetime):
        raise ValueError(f"{label}: expected date, not timestamp")

    if isinstance(value, date):
        return value

    if isinstance(value, str):
        try:
            return date.fromisoformat(value)
        except ValueError:
            pass

    raise ValueError(f"{label}: expected ISO date")


def age_bucket(days_overdue: int) -> str:
    if days_overdue <= 0:
        return "not_due"
    if days_overdue <= 30:
        return "days_1_30"
    if days_overdue <= 60:
        return "days_31_60"
    if days_overdue <= 90:
        return "days_61_90"
    return "days_90_plus"


def audit_invoices(
    invoices: Iterable[Mapping[str, Any]],
    *,
    as_of: date | str,
    timezone: str = "Asia/Kolkata",
) -> dict[str, Any]:
    """Reconstruct balances from invoice and payment records.

    Requires invoice ID, currency, amount_total, amount_paid,
    posting_date, due_date, status and payments.

    Current amount_paid must reconcile to all recorded successful
    payments. Current CANCELLED/WRITTEN_OFF status is not backdated.

    No database queries or mutations occur in this function.
    """

    cutoff = day(as_of, "as_of")
    tz = ZoneInfo(timezone)

    invoices = list(invoices)
    # A source payment must never be allocated to two different invoice rows.
    owners = {}
    for record in invoices:
        for payment in record.get("payments", []):
            key = payment.get("id")
            if key in owners and owners[key] != record.get("id"):
                raise ValueError("Payment applied to multiple invoices")
            owners[key] = record.get("id")
    seen: set[str] = set()
    currencies: dict[str, dict[str, Any]] = {}

    result = {
        "as_of": cutoff.isoformat(),
        "timezone": timezone,
        "balance_basis": "dated_successful_payments",
        "aging_basis": "contract_due_date_without_holiday_adjustment",
        "invoice_rows": 0,
        "excluded_not_posted": 0,
        "unverifiable_count": 0,
        "unverifiable_reasons": {},
        "currencies": currencies,
    }

    def unavailable(reason: str) -> None:
        result["unverifiable_count"] += 1
        reasons = result["unverifiable_reasons"]
        reasons[reason] = reasons.get(reason, 0) + 1

    for row in invoices:
        result["invoice_rows"] += 1

        invoice_id = str(row.get("id") or "").strip()

        if not invoice_id or invoice_id in seen:
            raise ValueError("Missing or repeated invoice ID")

        seen.add(invoice_id)

        currency = row.get("currency")

        if (
            not isinstance(currency, str)
            or len(currency) != 3
            or not currency.isupper()
            or not currency.isalpha()
        ):
            raise ValueError(f"Invoice {invoice_id}: invalid currency")

        total = money(row.get("amount_total"), "amount_total")
        current_paid = money(row.get("amount_paid"), "amount_paid")

        if current_paid > total:
            unavailable("paid_amount_exceeds_face_value")
            continue

        posting_date = row.get("posting_date")

        if posting_date is None:
            unavailable("posting_date_missing")
            continue

        posted = day(posting_date, "posting_date")

        if posted > cutoff:
            result["excluded_not_posted"] += 1
            continue

        status = str(row.get("status") or "").upper()

        adjustments = row.get("adjustments", [])
        required_kind = {"CANCELLED": "CANCELLATION", "WRITTEN_OFF": "WRITE_OFF"}.get(status)
        if status in EXCLUDED and not any(a.get("kind") == required_kind for a in adjustments):
            unavailable("undated_cancellation_or_writeoff")
            continue

        if row.get("due_date") is None:
            unavailable("due_date_missing")
            continue

        due = day(row["due_date"], "due_date")

        successful_total = Decimal("0.00")
        successful_as_of = Decimal("0.00")

        payment_ids: set[str] = set()
        invalid = False

        for payment in row.get("payments", []):
            payment_id = str(payment.get("id") or "").strip()

            if not payment_id or payment_id in payment_ids:
                invalid = True
                break

            payment_ids.add(payment_id)

            if (payment.get("currency") != currency
                    or ("invoice_id" in payment and str(payment["invoice_id"]) != invoice_id)
                    or ("company_id" in payment and payment["company_id"] != row.get("company_id"))):
                invalid = True
                break

            amount = money(payment.get("amount"), "payment.amount")

            if str(payment.get("status") or "").upper() != "SUCCESS":
                continue

            timestamp = payment.get("payment_date")

            if (
                not isinstance(timestamp, datetime)
                or timestamp.tzinfo is None
                or timestamp.utcoffset() is None
            ):
                invalid = True
                break

            payment_day = timestamp.astimezone(tz).date()

            if payment_day < posted:
                invalid = True
                break

            successful_total += amount

            if payment_day <= cutoff:
                successful_as_of += amount

        if invalid:
            unavailable("invalid_payment_event")
            continue

        if successful_total != current_paid:
            unavailable("payment_history_does_not_reconcile")
            continue

        if successful_as_of > total:
            unavailable("historical_overpayment")
            continue

        from app.services.ledger_adjustments import adjustment_totals
        try:
            adjustments, references = adjustment_totals(row, cutoff, posted, money=money, day=day)
        except ValueError:
            unavailable("invalid_adjustment_event")
            continue
        net_adjustment = (adjustments["debit_adjustments"] - adjustments["credit_notes"]
                          - adjustments["writeoffs"] - adjustments["cancellations"])
        outstanding = total - successful_as_of + net_adjustment
        if outstanding < 0 or adjustments["disputed"] > outstanding:
            unavailable("adjustments_exceed_outstanding")
            continue

        group = currencies.setdefault(
            currency,
            {
                "verified_invoices": 0,
                "face_amount": Decimal("0.00"),
                "net_adjustments_as_of": Decimal("0.00"),
                "adjustments": {key: Decimal("0.00") for key in adjustments},
                "source_references": [],
                "cash_received_as_of": Decimal("0.00"),
                "outstanding_as_of": Decimal("0.00"),
                "overdue_as_of": Decimal("0.00"),
                "aging": {
                    bucket: Decimal("0.00")
                    for bucket in BUCKETS
                },
            },
        )

        group["net_adjustments_as_of"] += net_adjustment
        for key, amount in adjustments.items():
            group["adjustments"][key] += amount
        group["source_references"].extend(references)
        group["verified_invoices"] += 1
        group["face_amount"] += total
        group["cash_received_as_of"] += successful_as_of
        group["outstanding_as_of"] += outstanding

        if outstanding > 0:
            overdue_days = (cutoff - due).days
            bucket = age_bucket(overdue_days)

            group["aging"][bucket] += outstanding

            if bucket != "not_due":
                group["overdue_as_of"] += outstanding

    for group in currencies.values():
        aging_total = sum(
            group["aging"].values(),
            Decimal("0.00"),
        )

        assert aging_total == group["outstanding_as_of"]

        assert (
            group["face_amount"] - group["cash_received_as_of"] + group["net_adjustments_as_of"]
            == group["outstanding_as_of"]
        )

        group["aging"] = {
            key: str(value)
            for key, value in group["aging"].items()
        }

        group["adjustments"] = {key: str(value) for key, value in group["adjustments"].items()}
        for key in (
            "net_adjustments_as_of",
            "face_amount",
            "cash_received_as_of",
            "outstanding_as_of",
            "overdue_as_of",
        ):
            group[key] = str(group[key])

    result["status"] = (
        "complete_for_supplied_rows"
        if result["unverifiable_count"] == 0
        else "partial_unverifiable_excluded"
    )

    return result
