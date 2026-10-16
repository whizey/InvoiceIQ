from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace as NS
import uuid

from app.services.portal_recovery import recovery_summary

TODAY = date(2026, 10, 16)


def invoice(due, total, paid):
    return NS(id=uuid.uuid4(), due_date=due, amount_total=Decimal(total), amount_paid=Decimal(paid))


def event(inv, decision, days_ago=0):
    return NS(invoice_id=inv.id, decision=decision,
              created_at=datetime(2026, 10, 16, 12, tzinfo=timezone.utc) - timedelta(days=days_ago))


def test_latest_decision_defines_status_and_reset_returns_to_queue():
    a, b, c, paid = (invoice(date(2026, 9, 1), 100, 0), invoice(date(2026, 9, 1), 200, 50),
                     invoice(date(2026, 9, 1), 300, 0), invoice(date(2026, 9, 1), 400, 400))
    events = [event(a, "APPROVE", 3), event(a, "ESCALATE", 1), event(b, "REJECT", 2),
              event(c, "APPROVE", 2), event(c, "RESET", 1)]
    result = recovery_summary([a, b, c, paid], events, TODAY)
    kpis = result["kpis"]
    assert (kpis["approved"], kpis["escalated"], kpis["rejected"]) == (0, 1, 1)
    assert kpis["awaiting_review"] == 1 and kpis["awaiting_exposure"] == "300"
    assert kpis["escalated_exposure"] == "100"
    assert sum(x["count"] for x in result["status_breakdown"]) == 3


def test_weekly_series_counts_each_decision_event_in_its_week():
    a = invoice(date(2026, 9, 1), 100, 0)
    result = recovery_summary([a], [event(a, "APPROVE"), event(a, "APPROVE", 9)], TODAY)
    weeks = result["weekly_decisions"]
    assert len(weeks) == 8 and weeks[-1]["approved"] == 1 and weeks[-2]["approved"] == 1
    assert weeks[-3]["approved"] == 0


def test_empty_workspace_has_zero_kpis():
    kpis = recovery_summary([], [], TODAY)["kpis"]
    assert kpis["reviewed"] == 0 and kpis["awaiting_review"] == 0
