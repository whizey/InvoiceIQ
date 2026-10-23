"""Cash forecast: survival curve, conditioning on age, as-of balances and the track-record range."""
import uuid
from datetime import date, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.services import cash_forecast as cf

TODAY = date(2026, 10, 23)


def test_kaplan_meier_counts_unpaid_invoices_as_still_waiting():
    curve = cf.Curve([(0, True), (10, True), (5, False), (20, False)])
    assert curve(-1) == 1.0
    assert curve(0) == pytest.approx(0.75)  # 1 of 4 paid on the due date
    assert curve(10) == pytest.approx(0.75 * 0.5)  # the invoice censored at day 5 left the risk set


def test_an_old_unpaid_invoice_is_conditioned_on_having_waited():
    invoices, settled, payments = [], {}, []
    for i in range(40):  # history: half pay in week one after due, half in week three
        row = SimpleNamespace(id=uuid.uuid4(), posting_date=TODAY - timedelta(days=200 + i),
                              due_date=TODAY - timedelta(days=170 + i), amount_total=Decimal(1000),
                              amount_paid=Decimal(1000))
        when = row.due_date + timedelta(days=3 if i % 2 else 17)
        settled[row.id] = when
        payments.append(SimpleNamespace(invoice_id=row.id, paid_on=when, amount=Decimal(1000)))
        invoices.append(row)
    late = SimpleNamespace(id=uuid.uuid4(), posting_date=TODAY - timedelta(days=40),
                           due_date=TODAY - timedelta(days=10), amount_total=Decimal(5000), amount_paid=Decimal(0))
    invoices.append(late)
    chance = {r.id: 0.5 for r in invoices}
    result = cf._forecast(invoices, settled, payments, chance, TODAY, weeks=2)
    # Ten days past due it can no longer be an early payer, so all of it is expected 17 days after due,
    # which is a week from today: nothing this week, everything next week.
    assert result["weeks"][0]["expected"] == pytest.approx(0)
    assert result["weeks"][1]["expected"] == pytest.approx(5000)


def test_balance_on_an_earlier_date_ignores_later_payments():
    row = SimpleNamespace(id=uuid.uuid4(), posting_date=TODAY - timedelta(days=60), due_date=TODAY - timedelta(days=30),
                          amount_total=Decimal(1000), amount_paid=Decimal(600))
    payments = [SimpleNamespace(invoice_id=row.id, paid_on=TODAY - timedelta(days=40), amount=Decimal(200)),
                SimpleNamespace(invoice_id=row.id, paid_on=TODAY - timedelta(days=5), amount=Decimal(400))]
    assert cf._open_on([row], {}, payments, TODAY - timedelta(days=20)) == [(row, 800.0)]
    assert cf._open_on([row], {}, payments, TODAY) == [(row, 400.0)]


def test_track_record_widens_a_range_that_was_too_narrow():
    row = {"week_start": "2026-10-23", "expected": 100.0, "low": 95.0, "high": 105.0}
    assert cf._widen(row, [0.8, 1.0, 1.1, 1.3]) == {**row, "low": 80.0, "high": 130.0}
    assert cf._widen(row, [0.8, 1.3]) == row  # too little history to judge
