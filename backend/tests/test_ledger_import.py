"""Importing Tally and Zoho Books exports: detection, mapping, Indian formats and a plain skip report."""
import io
from datetime import date, datetime
from decimal import Decimal

import pytest
from openpyxl import Workbook

from app.seed import showcase_seed
from app.services import ledger_import as li

TODAY = date(2026, 10, 23)


def test_zoho_export_round_trips_the_whole_ledger_exactly():
    ledger = showcase_seed.build(TODAY)
    prepared = li.prepare("zoho.csv", showcase_seed.zoho_export(ledger, TODAY).encode(), today=TODAY)
    assert prepared.source == "Zoho Books" and not prepared.skipped and not prepared.warnings
    by_number = {i["number"]: i for i in ledger.invoices}
    assert len(prepared.rows) == len(ledger.invoices)
    for row in prepared.rows:
        source = by_number[row["invoice_number"]]
        assert (row["amount_total"], row["amount_paid"], row["due_date"]) == (source["total"], source["paid"],
                                                                               source["due"])


def test_tally_report_with_title_lines_dr_amounts_and_a_totals_line():
    ledger = showcase_seed.build(TODAY)
    prepared = li.prepare("tally.csv", showcase_seed.tally_export(ledger, TODAY).encode(), today=TODAY)
    open_bills = [i for i in ledger.invoices if i["paid"] < i["total"]]
    assert prepared.source == "Tally" and len(prepared.rows) == len(open_bills) and not prepared.skipped
    assert sum(r["amount_total"] - r["amount_paid"] for r in prepared.rows) == \
        sum(i["total"] - i["paid"] for i in open_bills)


def test_skip_report_names_each_problem_with_its_spreadsheet_rows():
    csv_text = "\n".join([
        "Invoice Number,Customer Name,Invoice Date,Due Date,Total,Balance,Invoice Status",
        "A-1,Acme,01/09/2026,01/10/2026,\"₹1,20,000.00\",120000,Sent",
        "A-2,Acme,02/09/2026,,5000,5000,Sent",
        "A-3,,02/09/2026,02/10/2026,5000,0,Paid",
        "A-4,Beta,31/02/2026,02/10/2026,5000,0,Paid",
        "A-5,Beta,03/09/2026,03/10/2026,0,0,Sent",
        "A-6,Beta,03/09/2026,03/10/2026,5000,0,Void",
        "A-1,Beta,03/09/2026,03/10/2026,5000,0,Paid",
        "A-7,Gamma,03/09/2026,03/10/2026,5000,9000,Sent",
    ])
    prepared = li.prepare("export.csv", csv_text.encode(), today=TODAY)
    report = prepared.report()
    assert report["ready"] == 1 and prepared.rows[0]["amount_total"] == Decimal("120000.00")
    assert {s["reason"]: s["rows"] for s in report["skipped"]} == {
        "missing due date": [3], "missing customer": [4], "missing or unreadable invoice date": [5],
        "missing or zero amount": [6], "draft or void invoice": [7], "duplicate invoice number in the file": [8],
        "balance larger than the invoice amount": [9]}


def test_already_imported_numbers_are_skipped_not_duplicated():
    text = "Invoice Number,Customer Name,Invoice Date,Due Date,Total\nX-1,Acme,2026-09-01,2026-10-01,100\n"
    prepared = li.prepare("x.csv", text.encode(), existing={"x-1"}, today=TODAY)
    assert prepared.report()["skipped"] == [{"reason": "already imported", "count": 1, "rows": [2]}]


def test_excel_files_dates_and_manual_column_mapping():
    book = Workbook()
    sheet = book.active
    sheet.append(["Bill", "Client", "Billed on", "Due", "Value", "Outstanding"])
    sheet.append(["B-9", "Delta Traders", datetime(2026, 8, 1), datetime(2026, 9, 15), 45000, 15000])
    buffer = io.BytesIO()
    book.save(buffer)
    prepared = li.prepare("ledger.xlsx", buffer.getvalue(),
                          {"invoice_number": "Bill", "invoice_date": "Billed on", "total": "Value"}, today=TODAY)
    assert prepared.report()["missing"] == []
    row = prepared.rows[0]
    assert (row["invoice_number"], row["due_date"], row["amount_paid"]) == ("B-9", date(2026, 9, 15), Decimal("30000.00"))
    assert "no payment date" in prepared.warnings[0]


def test_missing_required_columns_are_reported_before_any_row():
    prepared = li.prepare("x.csv", b"Invoice Number,Customer Name,Invoice Date,Due Date\nA,B,2026-01-01,2026-02-01\n",
                          today=TODAY)
    assert prepared.report()["missing"] == ["invoice amount"] and prepared.rows == []


@pytest.mark.parametrize("text,expected", [("1-Apr-24", date(2024, 4, 1)), ("15/08/2026", date(2026, 8, 15)),
                                           ("2026-08-15", date(2026, 8, 15)), (45658, date(2025, 1, 1)),
                                           ("not a date", None)])
def test_dates(text, expected):
    assert li.parse_date(text) == expected


@pytest.mark.parametrize("text,expected", [("₹1,20,000.50", Decimal("120000.50")), ("8,100.00 Dr", Decimal("8100.00")),
                                           ("500.00 Cr", Decimal("-500.00")), ("(250)", Decimal("-250.00")), ("", None)])
def test_amounts(text, expected):
    assert li.parse_amount(text) == expected


@pytest.mark.asyncio
async def test_preview_then_import_a_zoho_export_into_a_company_workspace(monkeypatch):
    import base64
    import uuid

    from httpx import ASGITransport, AsyncClient
    from sqlalchemy import delete, func, select

    from app.core.db import async_session_factory, engine
    from app.main import app
    from app.models.portal import PortalInvoice, PortalUser, PortalWorkspace

    monkeypatch.setenv("PUBLIC_READ_ONLY", "false")
    monkeypatch.setenv("INVOICEIQ_PORTAL_ENABLED", "true")
    await engine.dispose()
    today = date.today()
    ledger = showcase_seed.build(today)
    body = {"filename": "zoho.csv",
            "content": base64.b64encode(showcase_seed.zoho_export(ledger, today).encode()).decode()}
    workspace_id = user_id = None
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
            reg = await client.post("/portal/register", json={
                "email": f"import-{uuid.uuid4().hex}@example.invalid", "full_name": "Import Owner",
                "company": "Import Co", "password": "A-strong-unique-long-password-37"})
            headers = {"Authorization": f"Bearer {reg.json()['token']}"}
            me = (await client.get("/portal/me", headers=headers)).json()
            workspace_id, user_id = uuid.UUID(me["workspace_id"]), uuid.UUID(me["user_id"])
            preview = await client.post("/portal/import-preview", json=body, headers=headers)
            assert preview.status_code == 200, preview.text
            assert preview.json()["source"] == "Zoho Books" and preview.json()["ready"] == len(ledger.invoices)
            done = await client.post("/portal/import-file", json=body, headers=headers)
            assert done.status_code == 201, done.text
            assert done.json()["imported"] == done.json()["scored"] == len(ledger.invoices)
            again = (await client.post("/portal/import-preview", json=body, headers=headers)).json()
            assert again["ready"] == 0 and again["skipped"][0]["reason"] == "already imported"
            follow = (await client.get("/portal/follow-ups", headers=headers)).json()
            assert follow["chase"] and follow["scorer"]["evaluated"] > 100
            example = (await client.get("/portal/example-export?format=tally", headers=headers)).json()
            assert example["filename"].endswith(".csv") and "Bills Receivable" in example["content"]
        async with async_session_factory() as db:
            assert await db.scalar(select(func.count()).select_from(PortalInvoice).where(
                PortalInvoice.workspace_id == workspace_id)) == len(ledger.invoices)
    finally:
        async with async_session_factory() as db:
            if workspace_id:
                await db.execute(delete(PortalWorkspace).where(PortalWorkspace.id == workspace_id))
            if user_id:
                await db.execute(delete(PortalUser).where(PortalUser.id == user_id))
            await db.commit()
        await engine.dispose()
