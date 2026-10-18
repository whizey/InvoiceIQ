import unittest

from datetime import date, datetime, timezone
from decimal import Decimal

from app.services.receivable_ledger_audit import audit_invoices


def invoice(**changes):
    row = {
        "id": "i1",
        "currency": "INR",
        "amount_total": Decimal("100.00"),
        "amount_paid": Decimal("40.00"),
        "posting_date": date(2026, 1, 1),
        "due_date": date(2026, 1, 31),
        "status": "PARTIALLY_PAID",
        "payments": [
            {
                "id": "p1",
                "amount": Decimal("40.00"),
                "currency": "INR",
                "status": "SUCCESS",
                "payment_date": datetime(
                    2026, 2, 4, tzinfo=timezone.utc
                ),
            }
        ],
    }

    row.update(changes)
    return row


class LedgerAuditTests(unittest.TestCase):

    def test_partial_payment(self):
        result = audit_invoices(
            [invoice()], as_of="2026-02-05"
        )

        amounts = result["currencies"]["INR"]

        self.assertEqual(
            amounts["outstanding_as_of"], "60.00"
        )
        self.assertEqual(
            amounts["overdue_as_of"], "60.00"
        )

    def test_future_payment_excluded(self):
        result = audit_invoices(
            [invoice()], as_of="2026-02-01"
        )

        self.assertEqual(
            result["currencies"]["INR"]["outstanding_as_of"],
            "100.00",
        )

    def test_failed_payment_excluded(self):
        row = invoice(amount_paid=Decimal("0.00"))
        row["payments"][0]["status"] = "FAILED"

        result = audit_invoices(
            [row], as_of="2026-02-05"
        )

        self.assertEqual(
            result["currencies"]["INR"]["outstanding_as_of"],
            "100.00",
        )

    def test_incomplete_payment_history(self):
        result = audit_invoices(
            [invoice(payments=[])],
            as_of="2026-02-05",
        )

        self.assertEqual(
            result["unverifiable_count"], 1
        )

    def test_cancelled_invoice(self):
        result = audit_invoices(
            [invoice(status="CANCELLED")],
            as_of="2026-02-05",
        )

        self.assertEqual(
            result["unverifiable_count"], 1
        )

    def test_writeoff_invoice(self):
        result = audit_invoices(
            [invoice(status="WRITTEN_OFF")],
            as_of="2026-02-05",
        )

        self.assertEqual(
            result["unverifiable_count"], 1
        )

    def test_future_posting(self):
        result = audit_invoices(
            [invoice()], as_of="2025-12-31"
        )

        self.assertEqual(
            result["excluded_not_posted"], 1
        )

    def test_separate_currencies(self):
        other = invoice(
            id="i2",
            currency="USD",
            amount_paid="0.00",
            payments=[],
        )

        result = audit_invoices(
            [invoice(), other],
            as_of="2026-02-05",
        )

        self.assertEqual(
            set(result["currencies"]),
            {"INR", "USD"},
        )

    def test_duplicate_invoice(self):
        with self.assertRaises(ValueError):
            audit_invoices(
                [invoice(), invoice()],
                as_of="2026-02-05",
            )

    def test_overpayment(self):
        result = audit_invoices(
            [invoice(amount_paid="101.00")],
            as_of="2026-02-05",
        )

        self.assertEqual(
            result["unverifiable_count"], 1
        )

    def test_float_rejected(self):
        with self.assertRaises(ValueError):
            audit_invoices(
                [invoice(amount_total=100.0)],
                as_of="2026-02-05",
            )

    def test_business_timezone(self):
        row = invoice(
            payments=[
                {
                    "id": "p1",
                    "currency": "INR",
                    "amount": "40.00",
                    "status": "SUCCESS",
                    "payment_date": datetime(
                        2026, 2, 4, 23, 30,
                        tzinfo=timezone.utc,
                    ),
                }
            ]
        )

        result = audit_invoices(
            [row], as_of="2026-02-04"
        )

        self.assertEqual(
            result["currencies"]["INR"]["cash_received_as_of"],
            "0.00",
        )

    def test_aging_boundaries(self):
        row = invoice(
            amount_paid="0.00",
            payments=[],
            due_date=date(2026, 1, 31),
        )

        first = audit_invoices(
            [row], as_of="2026-03-02"
        )
        second = audit_invoices(
            [row], as_of="2026-03-03"
        )

        self.assertEqual(
            first["currencies"]["INR"]["aging"]["days_1_30"],
            "100.00",
        )

        self.assertEqual(
            second["currencies"]["INR"]["aging"]["days_31_60"],
            "100.00",
        )


    def test_paid_invoice_without_payment_events(self):
        row = invoice(
            amount_total="50000.00",
            amount_paid="50000.00",
            status="PAID",
            payments=[],
        )
        result = audit_invoices(
            [row], as_of="2026-09-04"
        )

        self.assertEqual(result["unverifiable_count"], 1)
        self.assertEqual(
            result["unverifiable_reasons"],
            {"payment_history_does_not_reconcile": 1},
        )
        self.assertEqual(result["currencies"], {})


if __name__ == "__main__":
    unittest.main()
