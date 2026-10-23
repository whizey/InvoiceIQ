"""The real 2015 ledger: a fixed customer subset, balances as they stood on the as-of date, a fixed business date."""
from datetime import date

import pandas as pd

from app.models.portal import PortalWorkspace
from scripts import provision_public_ledger as public


def frame(rows):
    df = pd.DataFrame(rows, columns=["Cust_Num", "Customer_Name", "Document_No", "Amount", "Doc_Date", "Posting_Date",
                                     "Net_Due_Date", "Clearing_date"])
    for column in ("Doc_Date", "Posting_Date", "Net_Due_Date", "Clearing_date"):
        df[column] = pd.to_datetime(df[column])
    return df


def test_balances_are_as_they_stood_on_the_as_of_date():
    rows = frame([
        (1001, "AA10", 1, 5000, "2015-10-01", "2015-10-01", "2015-10-31", "2015-10-20"),  # paid before as-of
        (1001, "AA10", 2, 7000, "2015-11-01", "2015-11-01", "2015-11-15", "2015-12-20"),  # paid after: open, overdue
        (2002, "AB20", 3, 3000, "2015-11-20", "2015-11-20", "2015-12-20", "2015-12-01"),  # open, not yet due
        (2002, "AB20", 4, 9000, "2014-06-01", "2014-06-01", "2014-07-01", "2014-07-10"),  # older than a year: left out
        (3003, "AC30", 5, 1000, "2015-12-05", "2015-12-05", "2016-01-05", "2016-01-10"),  # after as-of: left out
    ])
    invoices, payments = public.ledger(public.select_rows(rows, date(2015, 11, 30)), date(2015, 11, 30))
    by_number = {i["number"]: i for i in invoices}
    assert set(by_number) == {"1", "2", "3"}
    assert by_number["1"]["paid"] == by_number["1"]["total"] and by_number["2"]["paid"] == 0
    assert [p["number"] for p in payments] == ["1"]
    assert by_number["2"]["customer"] == "Customer AA10-1001"


def test_subset_stays_within_the_scoring_limit_and_skips_giant_accounts(monkeypatch):
    monkeypatch.setattr(public, "LIMIT", 5)
    monkeypatch.setattr(public, "MAX_PER_CUSTOMER", 3)
    rows = frame([(c, f"N{c}", c * 10 + i, 100, "2015-11-01", "2015-11-01", "2015-11-30", "2015-11-10")
                  for c, n in ((1, 2), (2, 3), (3, 4), (4, 1)) for i in range(n)])
    chosen = public.select_rows(rows, date(2015, 11, 30))
    assert len(chosen) <= 5 and 3 not in set(chosen.Cust_Num)  # customer 3 has 4 invoices, over the cap


def test_a_historical_workspace_lives_on_its_as_of_date():
    ledger = PortalWorkspace(name="L", kind="showcase", as_of=date(2015, 11, 30))
    assert ledger.today() == date(2015, 11, 30) and ledger.now().date() == date(2015, 11, 30)
    assert PortalWorkspace(name="C", kind="customer").today() == date.today()
