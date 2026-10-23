"""Load real invoices from the public B2B dataset as a second public workspace, viewed as of 30 Nov 2015.

Every figure in this workspace is an observation: real (anonymised) customers, amounts, due dates and payment
dates, nothing generated. It is shown as of 30 Nov 2015, so invoices paid after that date are open. The source only
contains invoices that were eventually paid (by March 2016), so customers who never paid are missing; the as-of
date sits well before the end of the data to keep that effect small.

To fit the model's scoring limit it holds every invoice from the year before the as-of date for a fixed,
hash-ordered set of customers (about 2,000 invoices). Idempotent: does nothing when the workspace is complete.
"""
from __future__ import annotations

import asyncio
import hashlib
import os
import tempfile
import uuid
from collections import Counter, defaultdict
from datetime import date
from decimal import Decimal
from pathlib import Path

import httpx
import pandas as pd
from sqlalchemy import delete, func, select

from app.api.portal import PUBLIC_LEDGER_ID
from app.core.db import async_session_factory, engine
from app.models.portal import PortalInvoice, PortalPayment, PortalWorkspace
from app.services.portal_risk import score_workspace
from scripts.provision_portal_showcase import validate_target

AS_OF = date(2015, 11, 30)
NAME = "Public B2B ledger (real, anonymised), as of 30 Nov 2015"
SOURCE_URL = ("https://raw.githubusercontent.com/suryakiransk302-ops/b2b-invoice-payment-predictor/"
              "27bcd4292db856ecef935e267a2685228f983ecd/data/Dataset%20b2b_invoice_payment.csv")
SOURCE_SHA256 = "2a7f1dfc4e52231d4a4cb60ee019d6cef3730708afffb1c12786b4d3061bb32d"
LOCAL = Path(__file__).resolve().parents[2] / "data/raw/b2b_primary/b2b_invoice_payment_45839.csv"
LIMIT = 2000  # the scoring limit per workspace
MAX_PER_CUSTOMER = 250  # leaves room for many customers rather than one giant account


def source_csv() -> Path:
    """The verified dataset: a local copy if present, otherwise downloaded from the pinned commit."""
    path = Path(os.getenv("REAL_DATASET_PATH", str(LOCAL)))
    if not path.is_file():
        path = Path(tempfile.gettempdir()) / "invoiceiq-b2b-public.csv"
        if not path.is_file():
            with httpx.Client(timeout=120, follow_redirects=True) as client:
                path.write_bytes(client.get(SOURCE_URL).raise_for_status().content)
    if hashlib.sha256(path.read_bytes()).hexdigest() != SOURCE_SHA256:
        raise RuntimeError("Public dataset checksum mismatch; refusing to load it")
    return path


def select_rows(frame: pd.DataFrame, as_of: date = AS_OF) -> pd.DataFrame:
    """Every invoice posted in the year to `as_of` for a fixed set of customers, up to LIMIT invoices."""
    end = pd.Timestamp(as_of)
    year = frame[(frame.Posting_Date <= end) & (frame.Posting_Date > end - pd.Timedelta(days=365))]
    counts = year.groupby("Cust_Num").size()
    order = sorted(counts[counts <= MAX_PER_CUSTOMER].index, key=lambda c: hashlib.sha256(str(c).encode()).hexdigest())
    chosen, total = [], 0
    for customer in order:
        if total + counts[customer] <= LIMIT:
            chosen.append(customer)
            total += counts[customer]
    return year[year.Cust_Num.isin(chosen)].sort_values(["Posting_Date", "Document_No"])


def customer_labels(rows: pd.DataFrame) -> dict:
    """The dataset's own anonymised code for each customer, with the end of its number to keep them distinct."""
    names = defaultdict(Counter)
    for customer, name in zip(rows.Cust_Num, rows.Customer_Name):
        names[customer][name] += 1
    return {c: f"Customer {n.most_common(1)[0][0]}-{str(c)[-4:]}" for c, n in names.items()}


def ledger(rows: pd.DataFrame, as_of: date = AS_OF) -> tuple[list[dict], list[dict]]:
    """Invoices and payments as they stood on `as_of`: settled on or before it is paid, anything later is open."""
    labels = customer_labels(rows)
    invoices, payments = [], []
    for r in rows.itertuples(index=False):
        number = str(r.Document_No)
        settled = r.Clearing_date.date()
        amount = Decimal(int(r.Amount))
        paid = settled <= as_of
        posted = r.Posting_Date.date()
        invoices.append({"number": number, "customer": labels[r.Cust_Num], "issued": min(r.Doc_Date.date(), posted),
                         "posted": posted, "due": max(r.Net_Due_Date.date(), posted), "total": amount,
                         "paid": amount if paid else Decimal(0)})
        if paid:
            payments.append({"number": number, "paid_on": settled, "amount": amount})
    return invoices, payments


def _id(kind: str, key: str) -> uuid.UUID:
    return uuid.uuid5(PUBLIC_LEDGER_ID, f"public-2016:{kind}:{key}")


async def provision() -> str:
    frame = pd.read_csv(source_csv(), parse_dates=["Posting_Date", "Net_Due_Date", "Clearing_date", "Doc_Date"])
    invoices, payments = ledger(select_rows(frame))
    async with async_session_factory() as db:
        workspace = await db.get(PortalWorkspace, PUBLIC_LEDGER_ID)
        if workspace is None:
            workspace = PortalWorkspace(id=PUBLIC_LEDGER_ID, name=NAME, kind="showcase", as_of=AS_OF)
            db.add(workspace)
        elif workspace.kind != "showcase":
            raise RuntimeError("Refusing to load the public ledger into a customer workspace")
        workspace.name, workspace.as_of = NAME, AS_OF
        count = await db.scalar(select(func.count()).select_from(PortalInvoice).where(
            PortalInvoice.workspace_id == PUBLIC_LEDGER_ID))
        if count == len(invoices):
            await db.commit()
            return f"Public ledger current: {count} invoices as of {AS_OF}"
        for model in (PortalPayment, PortalInvoice):
            await db.execute(delete(model).where(model.workspace_id == PUBLIC_LEDGER_ID))
        await db.flush()
        for row in invoices:
            db.add(PortalInvoice(id=_id("invoice", row["number"]), workspace_id=PUBLIC_LEDGER_ID,
                                 invoice_number=row["number"], counterparty=row["customer"], issue_date=row["issued"],
                                 posting_date=row["posted"], due_date=row["due"], amount_total=row["total"],
                                 amount_paid=row["paid"]))
        await db.flush()
        for row in payments:
            db.add(PortalPayment(id=_id("payment", row["number"]), workspace_id=PUBLIC_LEDGER_ID,
                                 invoice_id=_id("invoice", row["number"]), paid_on=row["paid_on"], amount=row["amount"]))
        await db.flush()
        scored = await score_workspace(db, PUBLIC_LEDGER_ID)
        await db.commit()
    open_rows = [i for i in invoices if i["paid"] < i["total"]]
    overdue = [i for i in open_rows if i["due"] < AS_OF]
    return (f"Public ledger loaded: {len(invoices)} invoices for {len({i['customer'] for i in invoices})} customers, "
            f"{len(open_rows)} open and {len(overdue)} overdue as of {AS_OF}, {scored['scored']} scored")


async def main() -> None:
    validate_target()  # only databases approved for public workspaces
    try:
        print(await provision(), flush=True)
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
