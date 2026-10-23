"""Read receivables exports from Tally and Zoho Books (CSV or Excel), map their columns and validate every row.

Nothing is written here: `prepare` returns the rows ready to import plus a plain report of what was skipped and
why ("12 rows skipped: missing due date"), so the person can fix the file or adjust the column mapping first.
"""
from __future__ import annotations

import csv
import io
import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation

MAX_ROWS = 2000  # the scoring limit per workspace
MAX_BYTES = 2_500_000
HEADER_SCAN = 15  # Tally puts the company name and report title above the real header

# Accepted column names per field, compared after lower-casing and dropping punctuation.
SYNONYMS = {
    "invoice_number": ["invoice number", "invoice no", "invoice", "ref no", "reference no",
                       "voucher no", "voucher number", "bill no", "bill number", "document no"],
    "customer": ["customer name", "customer", "partys name", "party name", "party", "ledger name", "ledger",
                 "particulars", "client", "client name", "company"],
    "invoice_date": ["invoice date", "date", "bill date", "voucher date", "document date", "posting date"],
    "due_date": ["due date", "due on", "due", "payment due date"],
    "total": ["total", "invoice amount", "amount", "bill amount", "opening amount", "grand total", "invoice total",
              "total amount"],
    "balance": ["balance", "balance due", "pending amount", "outstanding", "amount due", "outstanding amount",
                "closing balance"],
    "paid": ["amount paid", "paid amount", "payment made", "paid", "received"],
    "payment_date": ["last payment date", "payment date", "paid on", "date of payment", "last paid date"],
    "terms_days": ["credit days", "payment terms days", "credit period", "terms"],
    "status": ["invoice status", "status"],
}
REQUIRED = ("invoice_number", "customer", "invoice_date", "total")
FIELD_LABELS = {"invoice_number": "invoice number", "customer": "customer", "invoice_date": "invoice date",
                "due_date": "due date", "total": "invoice amount", "balance": "balance",
                "paid": "amount paid", "payment_date": "payment date", "terms_days": "credit days",
                "status": "status"}
SKIP_STATUSES = {"void", "draft", "cancelled", "canceled"}
DATE_FORMATS = ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%d.%m.%Y", "%d-%b-%Y", "%d-%b-%y", "%d %b %Y", "%d %b %y",
                "%d/%m/%y", "%d-%m-%y", "%b %d, %Y", "%d %B %Y", "%Y/%m/%d")


@dataclass
class Prepared:
    source: str
    headers: list[str]
    mapping: dict[str, int | None]
    rows: list[dict] = field(default_factory=list)
    skipped: dict[str, list[int]] = field(default_factory=lambda: defaultdict(list))
    warnings: list[str] = field(default_factory=list)
    total_rows: int = 0

    def report(self) -> dict:
        return {"source": self.source, "headers": self.headers,
                "mapping": {k: (self.headers[v] if v is not None else None) for k, v in self.mapping.items()},
                "total_rows": self.total_rows, "ready": len(self.rows),
                "skipped": [{"reason": reason, "count": len(lines), "rows": lines[:8]}
                            for reason, lines in sorted(self.skipped.items(), key=lambda kv: -len(kv[1]))],
                "warnings": self.warnings,
                "missing": [FIELD_LABELS[f] for f in REQUIRED if self.mapping.get(f) is None],
                "preview": [_jsonable(r) for r in self.rows[:5]]}


def _key(text) -> str:
    return re.sub(r"[^a-z0-9 ]+", "", str(text or "").lower().replace("#", " no").replace("_", " ")).strip()


def _jsonable(row: dict) -> dict:
    return {k: (str(v) if isinstance(v, (date, Decimal)) else v) for k, v in row.items()}


def read_table(filename: str, content: bytes) -> list[list]:
    if len(content) > MAX_BYTES:
        raise ValueError("The file is larger than 2.5 MB; export a shorter period")
    if filename.lower().endswith((".xlsx", ".xlsm")):
        from openpyxl import load_workbook

        sheet = load_workbook(io.BytesIO(content), read_only=True, data_only=True).active
        rows = [list(r) for r in sheet.iter_rows(values_only=True)]
    else:
        text = content.decode("utf-8-sig", errors="replace")
        dialect = csv.Sniffer().sniff(text[:4096], delimiters=",;\t") if text.strip() else csv.excel
        rows = list(csv.reader(io.StringIO(text), dialect))
    rows = [r for r in rows if any(str(c or "").strip() for c in r)]
    if len(rows) > MAX_ROWS + HEADER_SCAN:
        raise ValueError(f"At most {MAX_ROWS:,} invoices per import")
    return rows


def _match(header: str) -> str | None:
    key = _key(header)
    for name, names in SYNONYMS.items():
        if key in names:
            return name
    return None


def find_header(rows: list[list]) -> int:
    """Index of the first row that names at least three known columns."""
    for index, row in enumerate(rows[:HEADER_SCAN]):
        if sum(_match(cell) is not None for cell in row) >= 3:
            return index
    raise ValueError("Could not find the column headings; the file needs at least invoice number, customer, "
                     "date and amount columns")


def detect_source(headers: list[str]) -> str:
    keys = {_key(h) for h in headers}
    if {"partys name", "pending amount"} & keys or {"ref no", "due on"} <= keys:
        return "Tally"
    if {"invoice status", "balance"} <= keys or "customer name" in keys and "invoice number" in keys:
        return "Zoho Books"
    return "Spreadsheet"


def map_columns(headers: list[str], overrides: dict[str, str | None] | None = None) -> dict[str, int | None]:
    mapping: dict[str, int | None] = {name: None for name in SYNONYMS}
    for index, header in enumerate(headers):
        name = _match(header)
        if name and mapping[name] is None:
            mapping[name] = index
    for name, header in (overrides or {}).items():
        if name in mapping:
            mapping[name] = headers.index(header) if header in headers else None
    return mapping


def parse_date(value) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, (int, float)) and 20000 < value < 80000:  # an Excel serial date
        return date(1899, 12, 30) + timedelta(days=int(value))
    text = str(value or "").strip()
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def parse_amount(value) -> Decimal | None:
    """₹1,20,000.00 / 120000 Dr / (500) / -500 → Decimal; a Cr suffix or brackets mean negative."""
    if isinstance(value, (int, float, Decimal)) and not isinstance(value, bool):
        return Decimal(str(value)).quantize(Decimal("0.01"))
    text = str(value or "").strip()
    if not text:
        return None
    negative = text.startswith("(") and text.endswith(")") or text.lower().endswith("cr") or text.startswith("-")
    digits = re.sub(r"[^0-9.]", "", text)
    try:
        amount = Decimal(digits).quantize(Decimal("0.01"))
    except InvalidOperation:
        return None
    return -amount if negative else amount


def prepare(filename: str, content: bytes, overrides: dict | None = None, *, existing: set[str] = frozenset(),
            today: date | None = None) -> Prepared:
    today = today or date.today()
    table = read_table(filename, content)
    start = find_header(table)
    headers = [str(h or "").strip() for h in table[start]]
    mapping = map_columns(headers, overrides)
    result = Prepared(detect_source(headers), headers, mapping, total_rows=len(table) - start - 1)
    if any(mapping[f] is None for f in REQUIRED):
        return result
    if mapping["due_date"] is None and mapping["terms_days"] is None:
        result.warnings.append("No due date or credit-days column: every invoice is treated as due 30 days after "
                               "its date.")
    if mapping["balance"] is None and mapping["paid"] is None:
        result.warnings.append("No balance or amount-paid column: every invoice is treated as unpaid.")
    seen: set[str] = set()
    undated_payments = 0

    def cell(row, name):
        index = mapping[name]
        return row[index] if index is not None and index < len(row) else None

    for offset, row in enumerate(table[start + 1:], start=start + 2):  # spreadsheet row numbers
        number = str(cell(row, "invoice_number") or "").strip()
        customer = str(cell(row, "customer") or "").strip()
        if not number and _key(customer) in {"total", "grand total", ""}:
            continue  # Tally's closing totals line
        status = _key(cell(row, "status"))
        if status in SKIP_STATUSES:
            result.skipped["draft or void invoice"].append(offset)
            continue
        if not number:
            result.skipped["missing invoice number"].append(offset)
            continue
        if not customer:
            result.skipped["missing customer"].append(offset)
            continue
        posted = parse_date(cell(row, "invoice_date"))
        if posted is None:
            result.skipped["missing or unreadable invoice date"].append(offset)
            continue
        due = parse_date(cell(row, "due_date"))
        if due is None and mapping["due_date"] is not None and str(cell(row, "due_date") or "").strip():
            result.skipped["unreadable due date"].append(offset)
            continue
        if due is None:
            terms = parse_amount(cell(row, "terms_days"))
            if mapping["due_date"] is not None and terms is None:
                result.skipped["missing due date"].append(offset)
                continue
            due = posted + timedelta(days=int(terms if terms is not None else 30))
        total = parse_amount(cell(row, "total"))
        if total is None or total <= 0:
            result.skipped["missing or zero amount"].append(offset)
            continue
        balance = parse_amount(cell(row, "balance"))
        paid = parse_amount(cell(row, "paid"))
        if balance is not None:
            paid = total - balance
        paid = paid or Decimal(0)
        if paid < 0 or paid > total:
            result.skipped["balance larger than the invoice amount"].append(offset)
            continue
        if due < posted or posted > today:
            result.skipped["due date before invoice date, or invoice dated in the future"].append(offset)
            continue
        if number.casefold() in seen:
            result.skipped["duplicate invoice number in the file"].append(offset)
            continue
        if number.casefold() in existing:
            result.skipped["already imported"].append(offset)
            continue
        seen.add(number.casefold())
        paid_on = parse_date(cell(row, "payment_date")) if paid > 0 else None
        if paid > 0 and (paid_on is None or paid_on < posted or paid_on > today):
            paid_on = min(due, today)
            undated_payments += 1
        result.rows.append({"invoice_number": number[:64], "counterparty": customer[:160], "issue_date": posted,
                            "posting_date": posted, "due_date": due, "amount_total": total, "amount_paid": paid,
                            "payment_date": paid_on})
    if undated_payments:
        result.warnings.append(f"{undated_payments} paid invoices have no payment date, so their payment is dated "
                               "on the due date. Add a payment-date column for accurate on-time history.")
    return result
