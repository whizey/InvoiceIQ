"""Preserved dashboard contract projected from an authenticated portal workspace.

This is a separate ledger. Customer/visitor identities can never read legacy
Invoice or Payment tables. Past balances use explicit dated payments only.
"""
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from statistics import median

from fastapi import HTTPException
from sqlalchemy import select

from app.models.portal import PortalInvoice, PortalPayment, PortalCaseEvent
from app.schemas.preserved_dashboard import PreservedDashboard


ZERO = Decimal(0)


def period_start(day: date, grain: str) -> date:
    if grain == "day":
        return day
    if grain == "week":
        return day - timedelta(days=day.weekday())
    if grain == "month":
        return day.replace(day=1)
    if grain == "quarter":
        return day.replace(month=1 + 3 * ((day.month - 1) // 3), day=1)
    return day.replace(month=1, day=1)


def period_end(day: date, grain: str) -> date:
    """Last calendar day in a chart bucket, including leap years."""
    if grain == "day":
        return day
    if grain == "week":
        return day + timedelta(days=6)
    if grain == "month":
        return (day.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)
    if grain == "quarter":
        month = day.month + 3
        first_after = date(day.year + (month > 12), (month - 1) % 12 + 1, 1)
        return first_after - timedelta(days=1)
    return date(day.year, 12, 31)


def invoice_state(invoice, payments, as_of):
    paid = sum((amount for when, amount in payments if when <= as_of), ZERO)
    if paid > invoice.amount_total:
        raise HTTPException(503, "Invoice payment ledger does not reconcile")
    balance = invoice.amount_total - paid
    age = max(0, (as_of - invoice.due_date).days)
    status = "PAID" if balance == 0 else "PARTIALLY_PAID" if paid else "OVERDUE" if age else "SENT"
    return paid, balance, age, status


def permitted(invoice, paid, balance, age, status, q, as_of, payment_dates, active_actions):
    axis = {"posting": invoice.posting_date, "due": invoice.due_date}
    if q.date_basis == "payment":
        hits = [when for when in payment_dates if when <= as_of
                and (q.start_date is None or when >= q.start_date)
                and (q.end_date is None or when <= q.end_date)]
        if not hits:
            return False
    else:
        at = axis[q.date_basis]
        if q.start_date and at < q.start_date or q.end_date and at > q.end_date:
            return False
    if q.company_id and q.company_id != invoice.workspace_id:
        return False
    if q.company_search and q.company_search.casefold() not in invoice.counterparty.casefold():
        return False
    if q.invoice_number and q.invoice_number.casefold() not in invoice.invoice_number.casefold():
        return False
    if q.search and q.search.casefold() not in (invoice.invoice_number + " " + invoice.counterparty).casefold():
        return False
    if q.posted_from and invoice.posting_date < q.posted_from or q.posted_to and invoice.posting_date > q.posted_to:
        return False
    if q.due_from and invoice.due_date < q.due_from or q.due_to and invoice.due_date > q.due_to:
        return False
    numeric = {"amount": invoice.amount_total, "paid": paid, "balance": balance, "days": age}
    for key, number in numeric.items():
        low, high = getattr(q, key + "_min", None), getattr(q, key + "_max", None)
        if low is not None and number < low or high is not None and number > high:
            return False
    if q.risk not in ("any", "unscored") and invoice.risk_probability is None:
        return False
    if invoice.risk_probability is not None:
        prob = invoice.risk_probability
        if q.risk == "unscored":
            return False
        if q.risk == "low" and prob >= Decimal(".3"):
            return False
        if q.risk == "medium" and (prob < Decimal(".3") or prob >= Decimal(".7")):
            return False
        if q.risk == "high" and prob < Decimal(".7"):
            return False
        if q.risk_min is not None and 100 * prob < q.risk_min or q.risk_max is not None and 100 * prob > q.risk_max:
            return False
    elif q.risk_min is not None or q.risk_max is not None:
        return False
    status_ok = {
        "all": True, "open": balance > 0, "outstanding": balance > 0,
        "overdue": balance > 0 and age > 0,
        "partial": balance > 0 and paid > 0, "settled": balance == 0,
        "cancelled": False, "written_off": False,
    }
    if not status_ok[q.status]:
        return False
    if q.case_presence == "yes" and invoice.id not in active_actions:
        return False
    if q.case_presence == "no" and invoice.id in active_actions:
        return False
    return True


def metric_of(rows, collection):
    total = sum((r["invoice"].amount_total for r in rows), ZERO)
    open_balance = sum((r["balance"] for r in rows), ZERO)
    overdue = sum((r["balance"] for r in rows if r["age"] > 0), ZERO)
    amount_values = [r["invoice"].amount_total for r in rows]
    return {
        "invoiced": total, "billed": total, "invoice_count": len(rows),
        "company_count": len({r["invoice"].counterparty for r in rows}),
        "open": open_balance,
        "open_count": sum(r["balance"] > 0 for r in rows),
        "overdue": overdue, "overdue_count": sum(r["balance"] > 0 and r["age"] > 0 for r in rows),
        "overdue_share": overdue * 100 / open_balance if open_balance else ZERO,
        "collected": collection,
        "scored_open": sum(r["balance"] > 0 and r["invoice"].risk_probability is not None for r in rows),
        "partial_count": sum(r["balance"] > 0 and r["paid"] > 0 for r in rows),
        "settled_count": sum(r["balance"] == 0 for r in rows),
        "cancelled_count": 0, "written_off_count": 0,
        "median_invoice": Decimal(str(median(amount_values))) if rows else None,
    }


def band(key, values):
    return {"key": key, "count": len(values), "amount": sum((r["balance"] for r in values), ZERO)}


async def build_portal_dashboard(db, q, subject, *, export: bool = False):
    session, workspace = subject
    today = date.today()
    as_of = q.as_of or today
    if as_of > today:
        raise HTTPException(422, "As-of cannot exceed the current business date")
    if q.compare_date and q.compare_date >= as_of:
        raise HTTPException(422, "Comparison cutoff must precede As of")
    if q.currency != "INR":
        raise HTTPException(422, "Portal ledger currently supports INR only")
    invoices = (await db.scalars(select(PortalInvoice).where(
        PortalInvoice.workspace_id == workspace.id).order_by(PortalInvoice.posting_date, PortalInvoice.id)
        .limit(20001))).all()
    if len(invoices) > 20000:
        raise HTTPException(422, "This portfolio exceeds the interactive analytics limit")
    payments = (await db.scalars(select(PortalPayment).where(
        PortalPayment.workspace_id == workspace.id).limit(50001))).all()
    if len(payments) > 50000:
        raise HTTPException(422, "This payment ledger exceeds the interactive analytics limit")
    by_id = {r.id: r for r in invoices}
    payment_map = defaultdict(list)
    for payment in payments:
        if payment.invoice_id not in by_id:
            raise HTTPException(503, "Invalid cross-workspace payment reference")
        if payment.paid_on <= today:
            payment_map[payment.invoice_id].append((payment.paid_on, payment.amount))
    for invoice in invoices:
        if sum((p[1] for p in payment_map[invoice.id]), ZERO) != invoice.amount_paid:
            raise HTTPException(503, "Dated payments do not reconcile with invoice")
    actor = str(session.user_id) if session.user_id else session.token_digest
    event_ids = set((await db.scalars(select(PortalCaseEvent.invoice_id).where(
        PortalCaseEvent.workspace_id == workspace.id,
        PortalCaseEvent.actor_scope == actor))).all())
    def make_rows(cutoff):
        rows = []
        for inv in invoices:
            if inv.posting_date > cutoff:
                continue
            p, balance, age, status = invoice_state(inv, payment_map[inv.id], cutoff)
            if permitted(inv, p, balance, age, status, q, cutoff,
                         [entry[0] for entry in payment_map[inv.id]], event_ids):
                rows.append({"invoice": inv, "paid": p, "balance": balance, "age": age, "status": status})
        return rows
    rows = make_rows(as_of)
    selected_ids = {r["invoice"].id for r in rows}
    def receipt_allowed(payment):
        return (payment.invoice_id in selected_ids and payment.paid_on <= as_of
                and (q.start_date is None or payment.paid_on >= q.start_date)
                and (q.end_date is None or payment.paid_on <= q.end_date))
    receipts = [p for p in payments if receipt_allowed(p)]
    collection = sum((p.amount for p in receipts), ZERO)
    metrics = metric_of(rows, collection)
    prior = None
    if q.compare_date:
        previous_rows = make_rows(q.compare_date)
        previous_ids = {r["invoice"].id for r in previous_rows}
        previous_receipts = sum((p.amount for p in payments
                                 if p.invoice_id in previous_ids and p.paid_on <= q.compare_date
                                 and (q.start_date is None or p.paid_on >= q.start_date)
                                 and (q.end_date is None or p.paid_on <= q.end_date)), ZERO)
        prior = metric_of(previous_rows, previous_receipts)

    aging_keys = ["not_due", "1-30", "31-60", "61-90", "90+"]
    aging = [band(k, [r for r in rows if r["balance"] > 0 and
             (k == "not_due" and r["age"] == 0 or k == "1-30" and 0 < r["age"] <= 30 or
              k == "31-60" and 30 < r["age"] <= 60 or k == "61-90" and 60 < r["age"] <= 90 or
              k == "90+" and r["age"] > 90)]) for k in aging_keys]
    risk_keys = ["0–20%", "20–40", "40–60", "60–80", "80–100", "Not scored"]
    def risk_bucket(r):
        p = r["invoice"].risk_probability
        if p is None:
            return "Not scored"
        return risk_keys[min(4, int(p * 5))]
    risk = [band(k, [r for r in rows if r["balance"] > 0 and risk_bucket(r) == k]) for k in risk_keys]
    due_groups = defaultdict(list)
    for r in rows:
        if r["balance"] > 0 and r["invoice"].due_date < as_of + timedelta(days=56):
            key = "Past due" if r["age"] else str(period_start(r["invoice"].due_date, "week"))
            due_groups[key].append(r)
    due = [band(key, values) for key, values in sorted(due_groups.items())]

    top_groups = defaultdict(list)
    for r in rows:
        if r["balance"] > 0:
            top_groups[r["invoice"].counterparty].append(r)
    top = []
    for name, values in top_groups.items():
        top.append({"id": str(workspace.id), "name": name, "count": len(values),
                    "open": sum((r["balance"] for r in values), ZERO),
                    "overdue": sum((r["balance"] for r in values if r["age"] > 0), ZERO),
                    "oldest": max(r["age"] for r in values), "risk": None})
    top.sort(key=lambda x: x["overdue"], reverse=True)
    top = top[:12]

    # The portal ledger contains every posting and dated payment for this
    # selected cohort, so its period-end open balance can be reconstructed.
    time_buckets = defaultdict(lambda: {"invoiced": ZERO, "collected": ZERO})
    for r in rows:
        at = period_start(r["invoice"].posting_date, q.grain)
        time_buckets[at]["invoiced"] += r["invoice"].amount_total
    for p in receipts:
        at = period_start(p.paid_on, q.grain)
        time_buckets[at]["collected"] += p.amount
    if len(time_buckets) > 400:
        raise HTTPException(422, "Choose a coarser Group by; too many chart buckets")
    series = []
    for at in sorted(time_buckets):
        point = time_buckets[at]
        cutoff = min(as_of, period_end(at, q.grain))
        open_at_cutoff = sum((
            r["invoice"].amount_total - sum((amount for when, amount in payment_map[r["invoice"].id]
                                              if when <= cutoff), ZERO)
            for r in rows if r["invoice"].posting_date <= cutoff
        ), ZERO)
        series.append({"period": at, "invoiced": point["invoiced"],
                       "collected": point["collected"], "open": open_at_cutoff})
    daily_amounts = defaultdict(lambda: ZERO)
    for p in receipts:
        if p.paid_on >= as_of - timedelta(days=181):
            daily_amounts[p.paid_on] += p.amount
    daily = [{"date": day, "amount": total} for day, total in sorted(daily_amounts.items())]
    sorters = {
        "dpd_desc": lambda r: -r["age"], "dpd_asc": lambda r: r["age"],
        "amount_desc": lambda r: -r["invoice"].amount_total, "amount_asc": lambda r: r["invoice"].amount_total,
        "posting_desc": lambda r: -r["invoice"].posting_date.toordinal(),
        "posting_asc": lambda r: r["invoice"].posting_date.toordinal(),
        "invoice_asc": lambda r: r["invoice"].invoice_number,
        "invoice_desc": lambda r: r["invoice"].invoice_number,
        "company_asc": lambda r: r["invoice"].counterparty,
        "company_desc": lambda r: r["invoice"].counterparty,
        "due_asc": lambda r: r["invoice"].due_date.toordinal(),
        "due_desc": lambda r: -r["invoice"].due_date.toordinal(),
        "paid_asc": lambda r: r["paid"], "paid_desc": lambda r: -r["paid"],
        "balance_asc": lambda r: r["balance"], "balance_desc": lambda r: -r["balance"],
        "risk_asc": lambda r: r["invoice"].risk_probability if r["invoice"].risk_probability is not None else Decimal(2),
        "risk_desc": lambda r: -(r["invoice"].risk_probability if r["invoice"].risk_probability is not None else Decimal(-1)),
    }
    rows.sort(key=lambda r: (sorters[q.sort](r), str(r["invoice"].id)))
    if q.sort in {"invoice_desc", "company_desc"}:
        rows.reverse()
    if export and len(rows) > 10000:
        raise HTTPException(422, "Export exceeds 10,000 invoices; narrow the filters")
    page_rows = rows if export else rows[(q.page - 1) * q.page_size:q.page * q.page_size]
    out_rows = [{
        "id": r["invoice"].id, "invoice_number": r["invoice"].invoice_number,
        "company_id": workspace.id, "company": r["invoice"].counterparty,
        "posting_date": r["invoice"].posting_date, "due_date": r["invoice"].due_date,
        "amount_total": r["invoice"].amount_total, "amount_paid": r["paid"],
        "balance": r["balance"], "days_past_due": r["age"],
        "status": r["status"], "probability": r["invoice"].risk_probability,
        "payment_term": (r["invoice"].due_date - r["invoice"].issue_date).days,
        "case_id": None,
    } for r in page_rows]
    output = {
        "generated_at": datetime.now(timezone.utc),
        "as_of": as_of, "start_date": q.start_date, "end_date": q.end_date,
        "timezone": "UTC", "workspace": workspace.name, "currency": "INR",
        "currency_verified": True,
        "currency_note": ("Isolated illustrative INR ledger, not live customer receivables."
                          if workspace.kind == "showcase" else "Customer-imported INR amounts, no currency conversion."),
        "historical_available": True,
        "historical_reason": "As-of balances reconstructed from dated portal payments.",
        "metrics": metrics, "comparison": prior,
        "series": series, "aging": aging, "risk": risk, "daily": daily, "due": due,
        "companies": [{"id": str(workspace.id), "name": workspace.name}],
        "top": top, "invoices": out_rows, "total": len(rows),
        "page": q.page, "page_size": q.page_size,
        "chart_notes": {
            "open_line": "Period-end open balance reconstructed from all selected postings and dated payments.",
            "bridge": "No dated write-off/cancellation ledger; no fabricated financial bridge.",
            "pipeline": "Visitor review decisions are not external collections executions.",
            "calendar": "Contractual due dates; banking holidays and grace periods not verified.",
            "settlement": "No verified settlement/grace-day policy for histogram.",
            "allocation": "No audited write-off or credit ledger; totals do not infer losses.",
            "limits": "Interactive cohort limited to 20,000 invoices and 50,000 payments.",
        },
    }
    return PreservedDashboard(**output)
