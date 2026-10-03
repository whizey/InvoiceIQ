from datetime import date
from math import isnan

from app.services.payment_risk_context import (
    PriorInvoiceSnapshot,
    compute_payment_risk_features,
)


def test_payment_risk_cold_start():
    result = compute_payment_risk_features(
        amount=1000.0,
        posting_date=date(2026, 1, 10),
        payment_term=30,
        history=[],
    )

    assert result.prior_resolved_invoice_count == 0
    assert result.prior_open_invoice_count == 0
    assert result.has_resolved_history == 0
    assert isnan(result.historical_late_rate)
    assert isnan(result.recent_5_late_rate)
    assert result.recent_5_late_rate_delta == 0
    assert result.amount_vs_historical_avg_ratio == 1


def test_resolved_and_open_history():
    history = [
        PriorInvoiceSnapshot(
            invoice_number="A",
            amount=1000.0,
            posting_date=date(2025, 10, 1),
            due_date=date(2025, 10, 31),
            clearing_date=date(2025, 11, 5),
        ),
        PriorInvoiceSnapshot(
            invoice_number="B",
            amount=2000.0,
            posting_date=date(2025, 11, 10),
            due_date=date(2025, 12, 10),
            clearing_date=None,
        ),
    ]

    result = compute_payment_risk_features(
        amount=1500.0,
        posting_date=date(2026, 1, 1),
        payment_term=30,
        history=history,
    )

    assert result.prior_resolved_invoice_count == 1
    assert result.prior_open_invoice_count == 1
    assert result.open_invoice_ratio == 0.5
    assert result.has_resolved_history == 1
    assert result.historical_late_rate == 1.0
    assert result.historical_avg_days_late == 5.0
    assert result.recent_5_late_rate == 1.0
    assert result.recent_late_streak == 1.0
    assert result.recent_on_time_streak == 0.0
    assert result.historical_avg_invoice_amount == 1500.0
    assert result.amount_vs_historical_avg_ratio == 1.0


def test_same_day_invoice_is_not_prior_history():
    history = [
        PriorInvoiceSnapshot(
            invoice_number="SAME-DAY",
            amount=900.0,
            posting_date=date(2026, 1, 10),
            due_date=date(2026, 2, 10),
            clearing_date=date(2026, 1, 20),
        )
    ]

    result = compute_payment_risk_features(
        amount=1000.0,
        posting_date=date(2026, 1, 10),
        payment_term=30,
        history=history,
    )

    assert result.prior_resolved_invoice_count == 0
    assert result.prior_open_invoice_count == 0


def test_same_day_resolution_is_still_open():
    history = [
        PriorInvoiceSnapshot(
            invoice_number="A",
            amount=1000.0,
            posting_date=date(2025, 12, 1),
            due_date=date(2025, 12, 20),
            clearing_date=date(2026, 1, 10),
        )
    ]

    result = compute_payment_risk_features(
        amount=1000.0,
        posting_date=date(2026, 1, 10),
        payment_term=30,
        history=history,
    )

    assert result.prior_resolved_invoice_count == 0
    assert result.prior_open_invoice_count == 1


def test_v3_caps_are_applied():
    history = [
        PriorInvoiceSnapshot(
            invoice_number="OLD",
            amount=1.0,
            posting_date=date(2020, 1, 1),
            due_date=date(2020, 1, 2),
            clearing_date=date(2020, 1, 2),
        )
    ]

    result = compute_payment_risk_features(
        amount=1000.0,
        posting_date=date(2026, 1, 10),
        payment_term=30,
        history=history,
    )

    assert result.customer_tenure_days == 500.0
    assert result.days_since_previous_invoice == 500.0
    assert result.amount_vs_historical_avg_ratio == 20.0
