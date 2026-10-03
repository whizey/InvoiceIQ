"""Declarative company + historical-invoice data used by the seed script.

Amounts are in INR. Dates are expressed as offsets ("N days before the
invoice this history belongs to is issued") so the whole dataset shifts
forward in time automatically whenever the seed script is run — a
"5 days overdue" scenario stays 5 days overdue whether you seed today or
next month.
"""

from dataclasses import dataclass, field
from decimal import Decimal

from app.models.enums import CompanySegment


@dataclass
class ContactSpec:
    name: str
    email: str
    role: str
    is_primary: bool = False
    phone: str | None = None


@dataclass
class HistoricalInvoiceSpec:
    """A past invoice that was already paid, used to establish payment-history realism."""

    due_days_ago: int
    amount: Decimal
    paid_days_after_due: int  # negative = paid early, 0 = on due date, positive = late
    term_days: int = 30


@dataclass
class CompanySpec:
    key: str
    name: str
    industry: str
    segment: CompanySegment
    contacts: list[ContactSpec]
    history: list[HistoricalInvoiceSpec] = field(default_factory=list)


COMPANIES: list[CompanySpec] = [
    # Scenario A: low risk — reliable payer, invoice overdue only 5 days
    CompanySpec(
        key="northwind",
        name="Northwind Traders Pvt Ltd",
        industry="Consumer Goods Distribution",
        segment=CompanySegment.ENTERPRISE,
        contacts=[
            ContactSpec("Priya Sharma", "priya.sharma@northwindtraders.example", "AP Manager", is_primary=True),
            ContactSpec("Rohit Verma", "rohit.verma@northwindtraders.example", "Finance Controller"),
        ],
        history=[
            HistoricalInvoiceSpec(due_days_ago=145, amount=Decimal("380000.00"), paid_days_after_due=-2),
            HistoricalInvoiceSpec(due_days_ago=110, amount=Decimal("410000.00"), paid_days_after_due=0),
            HistoricalInvoiceSpec(due_days_ago=75, amount=Decimal("395000.00"), paid_days_after_due=0),
            HistoricalInvoiceSpec(due_days_ago=40, amount=Decimal("425000.00"), paid_days_after_due=-1),
        ],
    ),
    # Scenario B: medium risk — repeated late payer, invoice overdue 30 days
    CompanySpec(
        key="bluepeak",
        name="Bluepeak Logistics",
        industry="Logistics & Freight",
        segment=CompanySegment.MID_MARKET,
        contacts=[
            ContactSpec("Karan Mehta", "karan.mehta@bluepeaklogistics.example", "Accounts Payable Lead", is_primary=True),
        ],
        history=[
            HistoricalInvoiceSpec(due_days_ago=210, amount=Decimal("175000.00"), paid_days_after_due=15),
            HistoricalInvoiceSpec(due_days_ago=170, amount=Decimal("190000.00"), paid_days_after_due=-2),
            HistoricalInvoiceSpec(due_days_ago=130, amount=Decimal("200000.00"), paid_days_after_due=18),
            HistoricalInvoiceSpec(due_days_ago=95, amount=Decimal("215000.00"), paid_days_after_due=15),
        ],
    ),
    # Scenario C: high risk — large enterprise invoice, 60 days overdue, requires escalation
    CompanySpec(
        key="vertex",
        name="Vertex Infra Solutions",
        industry="Infrastructure & Construction",
        segment=CompanySegment.ENTERPRISE,
        contacts=[
            ContactSpec("Anjali Nair", "anjali.nair@vertexinfra.example", "CFO", is_primary=True),
            ContactSpec("Suresh Iyer", "suresh.iyer@vertexinfra.example", "AP Manager"),
        ],
        history=[
            HistoricalInvoiceSpec(due_days_ago=300, amount=Decimal("1550000.00"), paid_days_after_due=15),
            HistoricalInvoiceSpec(due_days_ago=240, amount=Decimal("1620000.00"), paid_days_after_due=-2),
            HistoricalInvoiceSpec(due_days_ago=180, amount=Decimal("1700000.00"), paid_days_after_due=22),
            HistoricalInvoiceSpec(due_days_ago=130, amount=Decimal("1680000.00"), paid_days_after_due=-5),
        ],
    ),
    # Scenario D: promise-to-pay — customer committed to a future payment date
    CompanySpec(
        key="sundial",
        name="Sundial Retail Group",
        industry="Retail Chain",
        segment=CompanySegment.MID_MARKET,
        contacts=[
            ContactSpec("Meera Joshi", "meera.joshi@sundialretail.example", "Finance Manager", is_primary=True),
        ],
        history=[
            HistoricalInvoiceSpec(due_days_ago=180, amount=Decimal("290000.00"), paid_days_after_due=-1),
            HistoricalInvoiceSpec(due_days_ago=135, amount=Decimal("300000.00"), paid_days_after_due=13),
            HistoricalInvoiceSpec(due_days_ago=90, amount=Decimal("310000.00"), paid_days_after_due=-1),
        ],
    ),
    # Scenario E: recovered — invoice was overdue but paid after a single reminder
    CompanySpec(
        key="aarav",
        name="Aarav Textiles Ltd",
        industry="Textiles Manufacturing",
        segment=CompanySegment.SMB,
        contacts=[
            ContactSpec("Vikram Desai", "vikram.desai@aaravtextiles.example", "Director", is_primary=True),
        ],
        history=[
            HistoricalInvoiceSpec(due_days_ago=150, amount=Decimal("85000.00"), paid_days_after_due=0),
            HistoricalInvoiceSpec(due_days_ago=105, amount=Decimal("90000.00"), paid_days_after_due=-1),
            HistoricalInvoiceSpec(due_days_ago=70, amount=Decimal("88000.00"), paid_days_after_due=0),
        ],
    ),
    # Healthy account: no overdue invoices, no recovery case — contrast data for the dashboard
    CompanySpec(
        key="orbit",
        name="Orbit Manufacturing Co",
        industry="Industrial Manufacturing",
        segment=CompanySegment.ENTERPRISE,
        contacts=[
            ContactSpec("Neha Kapoor", "neha.kapoor@orbitmanufacturing.example", "AP Manager", is_primary=True),
        ],
        history=[
            HistoricalInvoiceSpec(due_days_ago=400, amount=Decimal("620000.00"), paid_days_after_due=-3),
            HistoricalInvoiceSpec(due_days_ago=340, amount=Decimal("710000.00"), paid_days_after_due=0),
            HistoricalInvoiceSpec(due_days_ago=280, amount=Decimal("680000.00"), paid_days_after_due=-2),
            HistoricalInvoiceSpec(due_days_ago=220, amount=Decimal("750000.00"), paid_days_after_due=0),
            HistoricalInvoiceSpec(due_days_ago=160, amount=Decimal("695000.00"), paid_days_after_due=-1),
        ],
    ),
]
