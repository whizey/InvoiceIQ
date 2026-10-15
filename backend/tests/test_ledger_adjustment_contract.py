"""Dated financial adjustment and source provenance regressions."""
from datetime import date
from decimal import Decimal
import pytest
from app.services.ledger_adjustments import adjustment_totals

def row(**changes):
    entry = dict(id="adj-1", source_reference="ERP-1", company_id="co-1",
                 invoice_id="inv-1", currency="INR", kind="CREDIT_NOTE",
                 effective_date="2026-01-15", amount="25.50")
    entry.update(changes)
    return dict(id="inv-1", company_id="co-1", currency="INR", adjustments=[entry])

def run(record, cutoff=date(2026, 2, 1)):
    return adjustment_totals(record, cutoff, date(2026, 1, 1),
        money=lambda v, _: Decimal(str(v)),
        day=lambda v, _: date.fromisoformat(v))

@pytest.mark.parametrize("kind,key", [
    ("CREDIT_NOTE","credit_notes"),("DEBIT_ADJUSTMENT","debit_adjustments"),
    ("WRITE_OFF","writeoffs"),("CANCELLATION","cancellations"),
    ("DISPUTE","disputed"),
])
def test_valid_adjustment_is_dated_and_classified(kind,key):
    record=row(kind=kind)
    totals, refs=run(record)
    assert totals[key]==Decimal("25.50")
    assert sum(totals.values())==Decimal("25.50")
    assert refs==["ERP-1"]
    before, before_refs=run(record,date(2026,1,14))
    assert sum(before.values())==0 and before_refs==[]

@pytest.mark.parametrize("change", [
    {"id":""},{"source_reference":""},{"company_id":"other"},
    {"invoice_id":"other"},{"currency":"USD"},{"kind":"UNKNOWN"},
    {"effective_date":"2025-12-31"},
])
def test_invalid_adjustment_provenance_is_rejected(change):
    with pytest.raises(ValueError):
        run(row(**change))

def test_duplicate_adjustment_id_is_rejected():
    record=row()
    record["adjustments"].append(dict(record["adjustments"][0]))
    with pytest.raises(ValueError):
        run(record)

def test_empty_adjustments_have_zero_totals():
    record=row()
    record["adjustments"]=[]
    totals, refs=run(record)
    assert all(value==0 for value in totals.values())
    assert refs==[]
