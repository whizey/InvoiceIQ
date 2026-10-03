from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from app.ml.create_customer_holdout_manifest import (
    write_or_verify,
)
from app.ml.payment_features import (
    CATEGORICAL_FEATURES,
    FEATURES,
    NUMERIC_FEATURES,
    TARGET_COLUMN,
)
from app.ml.selection_policy import (
    FINAL_MODEL_FAMILIES,
    PRIMARY_SELECTION_METRIC,
    PRIMARY_SELECTION_SCOPE,
)


PROJECT_ROOT = (
    Path(__file__)
    .resolve()
    .parents[2]
)


EXPECTED_FEATURES = [
    "amount",
    "payment_term",
    "prior_resolved_invoice_count",
    "prior_open_invoice_count",
    "open_invoice_ratio",
    "has_resolved_history",
    "historical_late_rate",
    "historical_avg_days_late",
    "historical_avg_invoice_amount",
    "amount_vs_historical_avg_ratio",
    "customer_tenure_days",
    "days_since_previous_invoice",
    "recent_5_late_rate",
    "recent_5_avg_days_late",
    "recent_late_streak",
    "recent_on_time_streak",
    "recent_5_late_rate_delta",
    "recent_5_avg_days_late_delta",
]


def run(
    *args: str,
    env: dict | None = None,
) -> None:
    command = [
        sys.executable,
        *args,
    ]

    print()
    print(
        "$",
        " ".join(command),
        flush=True,
    )

    subprocess.run(
        command,
        cwd=PROJECT_ROOT,
        env=env,
        check=True,
    )


def check_contract() -> None:
    assert (
        TARGET_COLUMN
        == "late_payment_weekend_adjusted"
    )

    assert (
        FEATURES
        == EXPECTED_FEATURES
    )

    assert (
        NUMERIC_FEATURES
        == EXPECTED_FEATURES
    )

    assert (
        CATEGORICAL_FEATURES
        == []
    )

    print(
        "Feature contract: PASS "
        "(18 numeric / 0 categorical)"
    )


def check_documentation() -> None:
    required = [
        "EVALUATION_PROTOCOL.md",
        "docs/FEATURE_PROVENANCE.md",
        "docs/DATASET_CARD.md",
        (
            "docs/"
            "CUSTOMER_HOLDOUT_PROTOCOL.md"
        ),
    ]

    for relative in required:
        path = (
            PROJECT_ROOT
            / relative
        )

        if not path.exists():
            raise RuntimeError(
                f"Missing {relative}"
            )

    print(
        "Methodology documentation: PASS"
    )


def check_selection_rule() -> None:
    assert (
        PRIMARY_SELECTION_SCOPE
        == "validation"
    )

    assert (
        PRIMARY_SELECTION_METRIC
        == "within_period_auc"
    )

    assert FINAL_MODEL_FAMILIES == (
        "xgboost",
        "random_forest",
        "gradient_boosting",
    )

    print(
        "Frozen model-selection rule: PASS"
    )


def main() -> None:
    print(
        "=" * 72
    )

    print(
        "RECOVERIQ PRE-TRAINING "
        "VERIFICATION"
    )

    print(
        "=" * 72
    )

    print(
        "Reserved test performance "
        "is NOT evaluated here."
    )

    check_contract()
    check_documentation()
    check_selection_rule()

    write_or_verify(
        check_only=True
    )

    env = os.environ.copy()

    env.pop(
        "PAYMENT_RISK_MODEL",
        None,
    )

    run(
        "-m",
        "py_compile",
        "app/ml/payment_features.py",
        (
            "app/ml/"
            "create_customer_holdout_manifest.py"
        ),
        "app/ml/selection_policy.py",
        "app/ml/payment_risk_model.py",
        (
            "app/ml/"
            "package_payment_risk_artifacts.py"
        ),
        "tests/test_selection_policy.py",
        (
            "tests/"
            "test_payment_risk_parity.py"
        ),
        (
            "tests/"
            "test_payment_risk_randomized_parity.py"
        ),
        (
            "tests/"
            "test_payment_risk_runtime_artifacts.py"
        ),
        env=env,
    )

    run(
        "-m",
        "pytest",
        "tests/test_selection_policy.py",
        (
            "tests/"
            "test_payment_risk_context.py"
        ),
        (
            "tests/"
            "test_payment_risk_parity.py"
        ),
        (
            "tests/"
            "test_payment_risk_randomized_parity.py"
        ),
        (
            "tests/"
            "test_payment_risk_runtime_artifacts.py"
        ),
        "-q",
        env=env,
    )

    run(
        "-m",
        "pytest",
        "-q",
        env=env,
    )

    print()
    print(
        "=" * 72
    )

    print(
        "PRE-TRAINING VERIFICATION: PASS"
    )

    print(
        "=" * 72
    )

    print(
        "No model was trained."
    )

    print(
        "No reserved test performance "
        "was evaluated."
    )


if __name__ == "__main__":
    main()
