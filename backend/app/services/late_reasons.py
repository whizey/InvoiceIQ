"""Why an overdue invoice is still unpaid, in the sentence a credit controller would write next to it.

The reason comes from what the workspace already knows, strongest evidence first: a dispute note, a part
payment, something the customer said or promised, the customer's usual payment pattern, then the latest
follow-up note. Notes flagged as instructions to the system are never quoted.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date
from decimal import Decimal
from statistics import median

from app.agents.guardrails import is_untrusted_instruction

MIN_HISTORY = 3  # paid invoices needed before a customer's habit counts as a pattern
HABIT_DAYS = 10  # paying this many days late, typically, is a habit worth naming


def customer_lateness(invoices, last_payment: dict) -> dict[str, int]:
    """Typical days late per customer, over its fully paid invoices; customers without a pattern are left out."""
    late = defaultdict(list)
    for row in invoices:
        paid_on = last_payment.get(row.id)
        if paid_on is not None and row.amount_paid >= row.amount_total:
            late[row.counterparty].append((paid_on - row.due_date).days)
    return {name: round(median(days)) for name, days in late.items() if len(days) >= MIN_HISTORY}


def _rupees(value) -> str:
    return "₹" + format(Decimal(value).quantize(Decimal(1)), ",")


def late_reason(row, notes, habit_days: int | None, today: date) -> dict | None:
    """`notes` are the invoice's notes, newest first. None when nothing is overdue."""
    balance = row.amount_total - row.amount_paid
    if balance <= 0 or row.due_date >= today:
        return None
    days = (today - row.due_date).days
    usable = [n for n in notes if not is_untrusted_instruction(n.body)]

    def latest(*kinds):
        return next((n for n in usable if n.kind in kinds), None)

    if dispute := latest("dispute"):
        return {"days_late": days, "kind": "dispute", "text": f"Disputed: {dispute.body}"}
    said = latest("customer_message", "promise")
    if row.amount_paid > 0:
        text = f"Part paid: {_rupees(row.amount_paid)} of {_rupees(row.amount_total)} received."
        return {"days_late": days, "kind": "part_paid", "text": text + (f" {said.body}" if said else "")}
    if said:
        prefix = "Promised" if said.kind == "promise" else "Customer says"
        text = said.body if said.body.lower().startswith(prefix.lower()) else f"{prefix}: {said.body}"
        return {"days_late": days, "kind": said.kind, "text": text}
    if habit_days is not None and habit_days >= HABIT_DAYS:
        return {"days_late": days, "kind": "habit",
                "text": f"Usually pays about {habit_days} days after the due date."}
    if follow_up := latest("internal"):
        return {"days_late": days, "kind": "internal", "text": follow_up.body}
    return {"days_late": days, "kind": "unknown", "text": "No reason recorded yet."}
