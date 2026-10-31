"""Preserved dashboard portal math, without production DB or credentials."""
from datetime import date, timedelta
from decimal import Decimal
from types import SimpleNamespace

from app.services.portal_dashboard import invoice_state, metric_of, period_start, permitted


def test_portal_source_metadata_survives_response_serialization():
    from app.schemas.preserved_dashboard import DashboardSelection, PreservedDashboard
    from app.services.portal_dashboard import compute_portal_dashboard

    day = date(2026, 10, 30)
    workspace = SimpleNamespace(id="workspace", name="Sample", kind="showcase", as_of=day)
    output = compute_portal_dashboard([], [], set(), DashboardSelection(), workspace, today=day)
    response = PreservedDashboard(**output).model_dump(mode="json")
    assert response["workspace_kind"] == "showcase"
    assert response["fixed_as_of"] is True


def invoice(*, amount="1000", posted=None, due=None):
    day = date(2026, 10, 15)
    return SimpleNamespace(id="inv", workspace_id="tenant", invoice_number="INV-1",
                           counterparty="Client One", amount_total=Decimal(amount),
                           risk_probability=None, posting_date=posted or day - timedelta(days=40),
                           due_date=due or day - timedelta(days=10))


def selection(**overrides):
    values = dict(start_date=None, end_date=None, date_basis="posting", company_id=None,
                  company_search="", invoice_number="", search="", posted_from=None, posted_to=None,
                  due_from=None, due_to=None, amount_min=None, amount_max=None, paid_min=None,
                  paid_max=None, balance_min=None, balance_max=None, days_min=None, days_max=None,
                  risk_min=None, risk_max=None, risk="any", status="all", case_presence="any")
    values.update(overrides)
    return SimpleNamespace(**values)


def test_dated_payments_not_counted_before_occurrence():
    row = invoice()
    today = date(2026, 10, 15)
    entries = [(date(2026, 10, 10), Decimal("250")), (date(2026, 10, 20), Decimal("750"))]
    paid, balance, age, status = invoice_state(row, entries, today)
    assert (paid, balance, age, status) == (Decimal("250"), Decimal("750"), 10, "PARTIALLY_PAID")
    later = invoice_state(row, entries, date(2026, 10, 21))
    assert later[0] == Decimal("1000")
    assert later[1] == 0


def test_date_basis_and_workspace_deny_unrelated_rows():
    row = invoice()
    today = date(2026, 10, 15)
    args = (row, Decimal("250"), Decimal("750"), 10, "PARTIALLY_PAID")
    assert permitted(*args, selection(status="overdue"), today, [date(2026, 10, 10)], set())
    assert not permitted(*args, selection(company_id="another"), today, [], set())
    assert not permitted(*args, selection(date_basis="payment", start_date=today), today,
                         [date(2026, 10, 10)], set())
    assert not permitted(*args, selection(case_presence="yes"), today, [], set())
    assert permitted(*args, selection(case_presence="yes"), today, [], {"inv"})


def test_six_existing_financial_metrics_reconcile():
    row = invoice()
    r = {"invoice": row, "paid": Decimal("250"), "balance": Decimal("750"), "age": 10}
    m = metric_of([r], Decimal("250"))
    assert m["invoiced"] == Decimal("1000")
    assert m["open"] == Decimal("750")
    assert m["overdue"] == Decimal("750")
    assert m["overdue_share"] == Decimal("100")
    assert m["collected"] == Decimal("250")
    assert m["scored_open"] == 0
    assert period_start(date(2026, 10, 15), "quarter") == date(2026, 10, 1)


def test_risk_grouping_and_all_filter_types_are_enforced():
    row = invoice()
    row.risk_probability = Decimal(".82")
    day = date(2026, 10, 15)
    args = (row, Decimal("250"), Decimal("750"), 10, "PARTIALLY_PAID")
    assert permitted(*args, selection(risk="high"), day, [], set())
    assert not permitted(*args, selection(risk="medium"), day, [], set())
    assert not permitted(*args, selection(risk="low"), day, [], set())
    assert not permitted(*args, selection(risk="unscored"), day, [], set())
    assert permitted(*args, selection(risk_min=Decimal("80")), day, [], set())
    assert not permitted(*args, selection(risk_max=Decimal("50")), day, [], set())
    assert permitted(*args, selection(company_search="client"), day, [], set())
    assert not permitted(*args, selection(company_search="other"), day, [], set())
    assert permitted(*args, selection(invoice_number="INV"), day, [], set())
    assert not permitted(*args, selection(invoice_number="OTHER"), day, [], set())
    assert not permitted(*args, selection(search="missing"), day, [], set())
    assert not permitted(*args, selection(posted_from=day), day, [], set())
    assert not permitted(*args, selection(due_to=day - timedelta(days=11)), day, [], set())
    assert not permitted(*args, selection(amount_min=Decimal("1200")), day, [], set())
    assert not permitted(*args, selection(paid_max=Decimal("200")), day, [], set())
    assert not permitted(*args, selection(balance_min=Decimal("800")), day, [], set())
    assert not permitted(*args, selection(days_max=9), day, [], set())
    assert not permitted(*args, selection(status="settled"), day, [], set())
    assert permitted(*args, selection(status="partial"), day, [], set())
    assert not permitted(*args, selection(status="cancelled"), day, [], set())
    assert not permitted(*args, selection(status="written_off"), day, [], set())
    assert permitted(*args, selection(case_presence="no"), day, [], set())
    assert not permitted(*args, selection(case_presence="no"), day, [], {"inv"})
    assert period_start(day, "day") == day
    assert period_start(day, "week") == date(2026, 10, 12)
    assert period_start(day, "month") == date(2026, 10, 1)
    assert period_start(day, "year") == date(2026, 1, 1)
