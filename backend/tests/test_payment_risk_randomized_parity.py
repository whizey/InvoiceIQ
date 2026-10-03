from __future__ import annotations

import math
import random
from datetime import date, timedelta

import pandas as pd
import pytest

from app.ml.datasets.build_primary_features import (
    STRICT_TARGET_COLUMN,
    build_customer_history,
    repair_feature_scales,
)
from app.ml.payment_features import NUMERIC_FEATURES
from app.services.payment_risk_context import (
    PriorInvoiceSnapshot,
    compute_payment_risk_features,
)


SEED = 20261003
CASES = 150


def _strict_outcome(
    clearing_date: date,
    due_date: date,
) -> tuple[int, int]:
    days_late = (
        clearing_date - due_date
    ).days

    return (
        days_late,
        int(days_late > 0),
    )


def _assert_numeric_feature_parity(
    *,
    offline_row: pd.Series,
    online_row: dict[str, float],
    case_id: int,
) -> None:
    for feature in NUMERIC_FEATURES:
        expected = float(
            offline_row[feature]
        )

        actual = float(
            online_row[feature]
        )

        message = (
            f"case={case_id}, "
            f"feature={feature}, "
            f"offline={expected!r}, "
            f"online={actual!r}"
        )

        if math.isnan(expected):
            assert math.isnan(actual), message
        else:
            assert actual == pytest.approx(
                expected,
                rel=1e-10,
                abs=1e-10,
            ), message


def _random_amount(
    rng: random.Random,
) -> float:
    # Occasionally create extreme ratios so the
    # amount-ratio clipping path is exercised.
    roll = rng.random()

    if roll < 0.05:
        return 1.0

    if roll < 0.10:
        return 10_000_000.0

    return round(
        rng.uniform(
            100.0,
            500_000.0,
        ),
        2,
    )


def _build_case(
    *,
    rng: random.Random,
    case_id: int,
) -> tuple[
    pd.DataFrame,
    list[PriorInvoiceSnapshot],
    int,
    date,
    float,
    int,
]:
    customer = f"CUSTOMER-{case_id}"

    current_posting = (
        date(2026, 1, 1)
        + timedelta(
            days=rng.randint(
                120,
                700,
            )
        )
    )

    current_term = rng.choice(
        [7, 15, 30, 45, 60, 90]
    )

    current_amount = _random_amount(
        rng
    )

    # Guarantee a useful spread:
    #
    #   case % 5 == 0 -> true cold start
    #   case % 5 == 1 -> many resolved outcomes (>10)
    #   remaining     -> mixed open/resolved history
    if case_id % 5 == 0:
        prior_count = rng.randint(
            0,
            8,
        )
        force_cold_start = True

    elif case_id % 5 == 1:
        prior_count = rng.randint(
            15,
            35,
        )
        force_cold_start = False

    else:
        prior_count = rng.randint(
            1,
            30,
        )
        force_cold_start = False

    rows: list[dict] = []
    history: list[
        PriorInvoiceSnapshot
    ] = []

    prior_specs: list[dict] = []

    for i in range(prior_count):
        # Duplicate posting dates are intentional.
        posting_days_ago = rng.randint(
            1,
            420,
        )

        posting_date = (
            current_posting
            - timedelta(
                days=posting_days_ago
            )
        )

        term = rng.choice(
            [7, 15, 30, 45, 60, 90]
        )

        due_date = (
            posting_date
            + timedelta(days=term)
        )

        amount = _random_amount(rng)

        if force_cold_start:
            # Prior invoices exist, but none of their
            # outcomes are knowable at scoring time.
            clearing_date = (
                current_posting
                + timedelta(
                    days=rng.randint(
                        0,
                        90,
                    )
                )
            )

        else:
            outcome_roll = rng.random()

            if outcome_roll < 0.25:
                # Open/unresolved at current posting.
                # clearing == current day is intentionally
                # still unavailable because the rule is <.
                clearing_date = (
                    current_posting
                    + timedelta(
                        days=rng.randint(
                            0,
                            120,
                        )
                    )
                )
            else:
                delay = rng.randint(
                    -30,
                    75,
                )

                candidate = (
                    due_date
                    + timedelta(days=delay)
                )

                # A clearing event cannot happen before
                # the invoice itself was posted.
                earliest_clearing = posting_date

                # This branch represents an outcome known
                # before the invoice currently being scored.
                latest_clearing = (
                    current_posting
                    - timedelta(days=1)
                )

                clearing_date = min(
                    max(
                        candidate,
                        earliest_clearing,
                    ),
                    latest_clearing,
                )

        prior_specs.append(
            {
                "posting_date": posting_date,
                "due_date": due_date,
                "clearing_date": clearing_date,
                "amount": amount,
                "invoice_number": (
                    f"INV-{case_id:03d}-"
                    f"{9999 - i:04d}"
                ),
            }
        )

    # Force same-day clearing ties regularly.
    #
    # The invoice identifiers deliberately run in a
    # different order from the lateness values. This is
    # specifically designed to catch the old
    # document_no/invoice_number tie-order bug.
    if (
        not force_cold_start
        and prior_count >= 3
        and case_id % 3 == 0
    ):
        # Deliberately manufacture three VALID invoices
        # that all clear on the same date but have very
        # different strict lateness values.
        #
        # Keeping their posting dates well before the
        # clearing date ensures the randomized fixture
        # itself obeys real invoice chronology.
        shared_clearing = (
            current_posting
            - timedelta(days=20)
        )

        tie_indices = rng.sample(
            range(prior_count),
            3,
        )

        tie_days_late = [
            -4,
            0,
            11,
        ]

        for offset, (
            idx,
            days_late,
        ) in enumerate(
            zip(
                tie_indices,
                tie_days_late,
            )
        ):
            spec = prior_specs[idx]

            spec["posting_date"] = (
                current_posting
                - timedelta(
                    days=90 + offset
                )
            )

            spec["clearing_date"] = (
                shared_clearing
            )

            spec["due_date"] = (
                shared_clearing
                - timedelta(
                    days=days_late
                )
            )

    # Shuffle before building the two representations.
    # Feature semantics must not depend on incoming row
    # order or identifier ordering.
    rng.shuffle(prior_specs)

    for i, spec in enumerate(
        prior_specs,
        start=1,
    ):
        assert (
            spec["due_date"]
            >= spec["posting_date"]
        ), (
            "Generated invoice has due date "
            "before posting date"
        )

        assert (
            spec["clearing_date"]
            >= spec["posting_date"]
        ), (
            "Generated invoice has clearing date "
            "before posting date"
        )

        days_late, late = (
            _strict_outcome(
                spec["clearing_date"],
                spec["due_date"],
            )
        )

        # Numeric document_no is intentionally unrelated
        # to invoice_number lexical order.
        document_no = (
            case_id * 100_000
            + rng.randint(
                1,
                99_999,
            )
        )

        rows.append(
            {
                "cust_num": customer,
                "document_no": (
                    document_no
                ),
                "posting_date": pd.Timestamp(
                    spec["posting_date"]
                ),
                "clearing_date": pd.Timestamp(
                    spec["clearing_date"]
                ),
                "amount": spec["amount"],
                "payment_term": (
                    spec["due_date"]
                    - spec["posting_date"]
                ).days,
                "days_late_strict": (
                    days_late
                ),
                STRICT_TARGET_COLUMN: late,
            }
        )

        history.append(
            PriorInvoiceSnapshot(
                invoice_number=(
                    spec[
                        "invoice_number"
                    ]
                ),
                amount=spec["amount"],
                posting_date=(
                    spec["posting_date"]
                ),
                due_date=(
                    spec["due_date"]
                ),
                clearing_date=(
                    spec[
                        "clearing_date"
                    ]
                ),
            )
        )

    # Add 0-3 OTHER invoices posted on the exact same
    # date as the invoice being scored.
    #
    # They must not appear in its history.
    same_day_companions = (
        rng.randint(
            0,
            3,
        )
    )

    for companion in range(
        same_day_companions
    ):
        amount = _random_amount(rng)

        due_date = (
            current_posting
            + timedelta(
                days=rng.choice(
                    [15, 30, 45]
                )
            )
        )

        clearing_date = (
            due_date
            + timedelta(
                days=rng.randint(
                    -5,
                    30,
                )
            )
        )

        days_late, late = (
            _strict_outcome(
                clearing_date,
                due_date,
            )
        )

        document_no = (
            case_id * 1_000_000
            + 800_000
            + companion
        )

        rows.append(
            {
                "cust_num": customer,
                "document_no": (
                    document_no
                ),
                "posting_date": pd.Timestamp(
                    current_posting
                ),
                "clearing_date": pd.Timestamp(
                    clearing_date
                ),
                "amount": amount,
                "payment_term": (
                    due_date
                    - current_posting
                ).days,
                "days_late_strict": (
                    days_late
                ),
                STRICT_TARGET_COLUMN: late,
            }
        )

        history.append(
            PriorInvoiceSnapshot(
                invoice_number=(
                    f"SAME-DAY-"
                    f"{case_id}-"
                    f"{companion}"
                ),
                amount=amount,
                posting_date=(
                    current_posting
                ),
                due_date=due_date,
                clearing_date=(
                    clearing_date
                ),
            )
        )

    current_document_no = (
        case_id * 1_000_000
        + 999_999
    )

    current_due = (
        current_posting
        + timedelta(
            days=current_term
        )
    )

    # The current invoice outcome must not affect its
    # own posting-time features.
    current_clearing = max(
        current_posting,
        (
            current_due
            + timedelta(
                days=rng.randint(
                    -10,
                    45,
                )
            )
        ),
    )

    current_days_late, current_late = (
        _strict_outcome(
            current_clearing,
            current_due,
        )
    )

    rows.append(
        {
            "cust_num": customer,
            "document_no": (
                current_document_no
            ),
            "posting_date": pd.Timestamp(
                current_posting
            ),
            "clearing_date": pd.Timestamp(
                current_clearing
            ),
            "amount": current_amount,
            "payment_term": current_term,
            "days_late_strict": (
                current_days_late
            ),
            STRICT_TARGET_COLUMN: (
                current_late
            ),
        }
    )

    # Randomize raw DataFrame order too.
    rng.shuffle(rows)

    return (
        pd.DataFrame(rows),
        history,
        current_document_no,
        current_posting,
        current_amount,
        current_term,
    )


def test_randomized_offline_online_feature_parity():
    rng = random.Random(SEED)

    assert len(NUMERIC_FEATURES) == 18

    saw_cold_start = False
    saw_more_than_10_resolved = False
    saw_open_invoice = False
    saw_same_day_clearing = False
    saw_same_day_invoice = False

    for case_id in range(CASES):
        (
            frame,
            history,
            current_document_no,
            current_posting,
            current_amount,
            current_term,
        ) = _build_case(
            rng=rng,
            case_id=case_id,
        )

        offline = (
            build_customer_history(
                frame
            )
        )

        offline = (
            repair_feature_scales(
                offline
            )
        )

        matches = offline.loc[
            offline["document_no"]
            == current_document_no
        ]

        assert len(matches) == 1

        offline_current = (
            matches.iloc[0]
        )

        online = (
            compute_payment_risk_features(
                amount=current_amount,
                posting_date=(
                    current_posting
                ),
                payment_term=(
                    current_term
                ),
                history=history,
            )
            .to_row()
        )

        assert set(online) == set(
            NUMERIC_FEATURES
        )

        _assert_numeric_feature_parity(
            offline_row=offline_current,
            online_row=online,
            case_id=case_id,
        )

        resolved_before = [
            item
            for item in history
            if (
                item.posting_date
                < current_posting
                and item.clearing_date
                is not None
                and item.clearing_date
                < current_posting
            )
        ]

        prior_before = [
            item
            for item in history
            if (
                item.posting_date
                < current_posting
            )
        ]

        if not resolved_before:
            saw_cold_start = True

        if len(resolved_before) > 10:
            saw_more_than_10_resolved = (
                True
            )

        if (
            len(prior_before)
            > len(resolved_before)
        ):
            saw_open_invoice = True

        clearing_dates = [
            item.clearing_date
            for item in resolved_before
        ]

        if (
            len(clearing_dates)
            != len(set(clearing_dates))
        ):
            saw_same_day_clearing = True

        if any(
            item.posting_date
            == current_posting
            for item in history
        ):
            saw_same_day_invoice = True

    assert saw_cold_start
    assert saw_more_than_10_resolved
    assert saw_open_invoice
    assert saw_same_day_clearing
    assert saw_same_day_invoice
