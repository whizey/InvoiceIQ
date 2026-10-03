from __future__ import annotations

import math
from datetime import date

import pandas as pd
import pytest

from app.ml.datasets.build_primary_features import (
    STRICT_TARGET_COLUMN,
    build_customer_history,
    repair_feature_scales,
)
from app.ml.payment_features import (
    NUMERIC_FEATURES,
)
from app.services.payment_risk_context import (
    PriorInvoiceSnapshot,
    compute_payment_risk_features,
)


def _strict_late(
    clearing_date: date,
    due_date: date,
) -> tuple[int, int]:
    days_late = (clearing_date - due_date).days
    return days_late, int(days_late > 0)


def test_offline_online_all_18_features_match_with_same_day_clearings():
    customer = "CUSTOMER-1"
    clearing = date(2026, 1, 20)

    prior_specs = [
        {
            "document_no": 900,
            "invoice_number": "Z-900",
            "posting_date": date(2025, 12, 1),
            "due_date": date(2026, 1, 25),
            "amount": 100.0,
        },
        {
            "document_no": 100,
            "invoice_number": "A-100",
            "posting_date": date(2025, 12, 2),
            "due_date": date(2026, 1, 22),
            "amount": 200.0,
        },
        {
            "document_no": 800,
            "invoice_number": "Y-800",
            "posting_date": date(2025, 12, 3),
            "due_date": date(2026, 1, 20),
            "amount": 300.0,
        },
        {
            "document_no": 200,
            "invoice_number": "B-200",
            "posting_date": date(2025, 12, 4),
            "due_date": date(2026, 1, 19),
            "amount": 400.0,
        },
        {
            "document_no": 700,
            "invoice_number": "X-700",
            "posting_date": date(2025, 12, 5),
            "due_date": date(2026, 1, 18),
            "amount": 500.0,
        },
        {
            "document_no": 300,
            "invoice_number": "C-300",
            "posting_date": date(2025, 12, 6),
            "due_date": date(2026, 1, 17),
            "amount": 600.0,
        },
    ]

    rows = []
    online_history = []

    for spec in prior_specs:
        days_late, late = _strict_late(
            clearing,
            spec["due_date"],
        )

        rows.append(
            {
                "cust_num": customer,
                "document_no": spec["document_no"],
                "posting_date": pd.Timestamp(
                    spec["posting_date"]
                ),
                "clearing_date": pd.Timestamp(clearing),
                "amount": spec["amount"],
                "payment_term": 30,
                "days_late_strict": days_late,
                STRICT_TARGET_COLUMN: late,
                "payment_method_description": "BANK",
                "region": "R1",
                "city": "C1",
                "zipcode": "Z1",
            }
        )

        online_history.append(
            PriorInvoiceSnapshot(
                invoice_number=spec["invoice_number"],
                amount=spec["amount"],
                posting_date=spec["posting_date"],
                due_date=spec["due_date"],
                clearing_date=clearing,
            )
        )

    current_posting = date(2026, 2, 1)
    current_due = date(2026, 3, 3)
    current_clearing = date(2026, 3, 10)

    current_days_late, current_late = _strict_late(
        current_clearing,
        current_due,
    )

    rows.append(
        {
            "cust_num": customer,
            "document_no": 999999,
            "posting_date": pd.Timestamp(current_posting),
            "clearing_date": pd.Timestamp(current_clearing),
            "amount": 750.0,
            "payment_term": 30,
            "days_late_strict": current_days_late,
            STRICT_TARGET_COLUMN: current_late,
            "payment_method_description": "BANK",
            "region": "R1",
            "city": "C1",
            "zipcode": "Z1",
        }
    )

    frame = pd.DataFrame(rows)

    offline = build_customer_history(frame)
    offline = repair_feature_scales(offline)

    offline_current = offline.loc[
        offline["document_no"] == 999999
    ].iloc[0]

    online = compute_payment_risk_features(
        amount=750.0,
        posting_date=current_posting,
        payment_term=30,
        history=online_history,
    ).to_row()

    for feature in NUMERIC_FEATURES:
        expected = float(offline_current[feature])
        actual = float(online[feature])

        if math.isnan(expected):
            assert math.isnan(actual), feature
        else:
            assert actual == pytest.approx(
                expected,
                rel=1e-12,
                abs=1e-12,
            ), feature

