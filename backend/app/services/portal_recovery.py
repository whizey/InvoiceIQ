"""Recovery-case and approval aggregates derived only from stored workspace records."""
from collections import Counter
from datetime import date, timedelta
from decimal import Decimal

STATUSES = ("APPROVE", "REJECT", "ESCALATE")
WEEKS = 8


def recovery_summary(invoices, events, today: date | None = None) -> dict:
    """Latest decision per invoice defines its status; RESET returns it to unreviewed."""
    today = today or date.today()
    latest = {}
    for event in sorted(events, key=lambda e: e.created_at):
        latest[event.invoice_id] = event.decision
    by_id = {row.id: row for row in invoices}
    counts, exposure = Counter(), {status: Decimal(0) for status in STATUSES}
    for invoice_id, decision in latest.items():
        row = by_id.get(invoice_id)
        if row is None or decision not in STATUSES:
            continue
        counts[decision] += 1
        exposure[decision] += row.amount_total - row.amount_paid
    awaiting = [r for r in invoices
                if r.due_date < today and r.amount_total > r.amount_paid
                and latest.get(r.id) not in STATUSES]
    reviewed = sum(counts.values())
    week_start = today - timedelta(days=today.weekday())
    weeks = [week_start - timedelta(weeks=n) for n in range(WEEKS - 1, -1, -1)]
    series = {w: Counter() for w in weeks}
    for event in events:
        if event.decision not in STATUSES:
            continue
        day = event.created_at.date()
        start = day - timedelta(days=day.weekday())
        if start in series:
            series[start][event.decision] += 1
    return {
        "kpis": {
            "awaiting_review": len(awaiting),
            "awaiting_exposure": str(sum((r.amount_total - r.amount_paid for r in awaiting), Decimal(0))),
            "approved": counts["APPROVE"], "rejected": counts["REJECT"], "escalated": counts["ESCALATE"],
            "reviewed": reviewed,
            "escalated_exposure": str(exposure["ESCALATE"]),
            "case_exposure": str(sum(exposure.values(), Decimal(0))),
        },
        "status_breakdown": [
            {"status": "Awaiting review", "count": len(awaiting),
             "amount": str(sum((r.amount_total - r.amount_paid for r in awaiting), Decimal(0)))},
            {"status": "Approved", "count": counts["APPROVE"], "amount": str(exposure["APPROVE"])},
            {"status": "Escalated", "count": counts["ESCALATE"], "amount": str(exposure["ESCALATE"])},
            {"status": "Rejected", "count": counts["REJECT"], "amount": str(exposure["REJECT"])},
        ],
        "weekly_decisions": [
            {"week": w.isoformat(), "approved": series[w]["APPROVE"],
             "escalated": series[w]["ESCALATE"], "rejected": series[w]["REJECT"]}
            for w in weeks
        ],
    }
