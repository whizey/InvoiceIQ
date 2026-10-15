import unittest

from datetime import date, datetime, timezone
from decimal import Decimal
from types import SimpleNamespace

from app.services.analytics_reconciliation import (
    build_ledger_evidence,
)


def invoice(**changes):
    data = {
        "id": "invoice-1",
        "currency": "INR",
        "amount_total": Decimal("50000.00"),
        "amount_paid": Decimal("0.00"),
        "posting_date": date(2026, 9, 4),
        "due_date": date(2026, 10, 4),
        "status": "SENT",
    }
    data.update(changes)
    return SimpleNamespace(**data)


class AnalyticsReconciliationTests(unittest.TestCase):

    def report(self, invoices, payments=None):
        return build_ledger_evidence(
            invoices,
            payments or [],
            cutoff=date(2026, 9, 4),
            timezone="Asia/Kolkata",
        )

    def test_verified_outstanding(self):
        result = self.report([invoice()])

        self.assertEqual(result["verified_invoices"], 1)
        self.assertEqual(
            result["currencies"]["INR"]["outstanding_as_of"],
            "50000.00",
        )

    def test_missing_posting_date_is_not_inferred(self):
        result = self.report([
            invoice(posting_date=None)
        ])

        self.assertEqual(result["verified_invoices"], 0)
        self.assertEqual(
            result["unverifiable_reasons"],
            {"posting_date_missing": 1},
        )

    def test_paid_without_payment_event(self):
        result = self.report([
            invoice(
                amount_paid=Decimal("50000.00"),
                status="PAID",
            )
        ])

        self.assertEqual(
            result["unverifiable_reasons"],
            {"payment_history_does_not_reconcile": 1},
        )

    def test_partial_payment(self):
        payment = SimpleNamespace(
            id="payment-1",
            invoice_id="invoice-1",
            currency="INR",
            amount=Decimal("10000.00"),
            status="SUCCESS",
            payment_date=datetime(
                2026, 9, 4, 7,
                tzinfo=timezone.utc,
            ),
        )

        result = self.report(
            [
                invoice(
                    amount_paid=Decimal("10000.00"),
                    status="PARTIALLY_PAID",
                )
            ],
            [payment],
        )

        self.assertEqual(
            result["currencies"]["INR"]["outstanding_as_of"],
            "40000.00",
        )


if __name__ == "__main__":
    unittest.main()
