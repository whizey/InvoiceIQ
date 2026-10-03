"""Create RecoverIQ V2 temporal splits.

The split itself is independent of the target definition.

Every output file retains BOTH targets:

1. late_payment_strict
   Calendar lateness:
   clearing_date > raw net_due_date

2. late_payment_weekend_adjusted
   Weekend-adjusted operational lateness:
   Saturday/Sunday due dates roll to Monday.

Why one shared split?
---------------------
We want target-definition experiments to use exactly the same invoices.

TRAIN:
    model development

VALIDATION:
    model comparison / tuning

TEST:
    reserved for final evaluation only

All posting dates remain intact:
the same posting date can never appear in two different splits.
"""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

from app.ml.datasets.build_primary_features import (
    STRICT_TARGET_COLUMN,
    TARGET_COLUMN,
)


PROJECT_ROOT = Path(__file__).resolve().parents[4]

INPUT_PATH = (
    PROJECT_ROOT
    / "data"
    / "processed"
    / "primary"
    / "b2b_primary_features.csv.gz"
)

OUTPUT_DIR = (
    PROJECT_ROOT
    / "data"
    / "processed"
    / "primary"
    / "splits"
)

TRAIN_PATH = (
    OUTPUT_DIR
    / "train.csv.gz"
)

VALIDATION_PATH = (
    OUTPUT_DIR
    / "validation.csv.gz"
)

TEST_PATH = (
    OUTPUT_DIR
    / "test.csv.gz"
)

MANIFEST_PATH = (
    OUTPUT_DIR
    / "split_manifest.json"
)


TRAIN_FRACTION = 0.70
VALIDATION_FRACTION = 0.15
TEST_FRACTION = 0.15


def choose_cutoff_index(
    daily_counts: pd.DataFrame,
    target_cumulative_rows: float,
    minimum_index: int = 0,
) -> int:
    """Choose whole-date cutoff closest to requested row count."""

    candidates = daily_counts.loc[
        daily_counts.index >= minimum_index
    ].copy()

    difference = (
        candidates["cumulative_rows"]
        - target_cumulative_rows
    ).abs()

    return int(
        difference.idxmin()
    )


def target_summary(
    frame: pd.DataFrame,
    target: str,
) -> dict:
    counts = (
        frame[target]
        .value_counts()
        .sort_index()
    )

    return {
        "negative_count": int(
            counts.get(0, 0)
        ),

        "positive_count": int(
            counts.get(1, 0)
        ),

        "positive_rate": float(
            frame[target].mean()
        ),
    }


def describe_split(
    frame: pd.DataFrame,
) -> dict:
    return {
        "rows": int(
            len(frame)
        ),

        "customers": int(
            frame[
                "cust_num"
            ].nunique()
        ),

        "documents": int(
            frame[
                "document_no"
            ].nunique()
        ),

        "posting_date_min": str(
            frame[
                "posting_date"
            ].min().date()
        ),

        "posting_date_max": str(
            frame[
                "posting_date"
            ].max().date()
        ),

        "calendar_target": (
            target_summary(
                frame,
                STRICT_TARGET_COLUMN,
            )
        ),

        "weekend_adjusted_target": (
            target_summary(
                frame,
                TARGET_COLUMN,
            )
        ),
    }


def print_split_summary(
    name: str,
    frame: pd.DataFrame,
) -> None:
    print()
    print(name)
    print("-" * 60)

    print(
        "Rows:",
        len(frame),
    )

    print(
        "Customers:",
        frame[
            "cust_num"
        ].nunique(),
    )

    print(
        "Documents:",
        frame[
            "document_no"
        ].nunique(),
    )

    print(
        "Dates:",
        frame[
            "posting_date"
        ].min().date(),
        "->",
        frame[
            "posting_date"
        ].max().date(),
    )

    print()
    print(
        "Calendar late %:",
        round(
            float(
                frame[
                    STRICT_TARGET_COLUMN
                ].mean()
                * 100
            ),
            2,
        ),
    )

    print(
        "Weekend-adjusted late %:",
        round(
            float(
                frame[
                    TARGET_COLUMN
                ].mean()
                * 100
            ),
            2,
        ),
    )

    changed = int(
        (
            frame[
                STRICT_TARGET_COLUMN
            ]
            != frame[
                TARGET_COLUMN
            ]
        ).sum()
    )

    print(
        "Labels differing between targets:",
        changed,
    )

    print(
        "Different-label %:",
        round(
            changed
            / len(frame)
            * 100,
            2,
        ),
    )


def split() -> None:
    if not INPUT_PATH.exists():
        raise FileNotFoundError(
            "V2 feature dataset not found: "
            f"{INPUT_PATH}"
        )

    print(
        "Loading:",
        INPUT_PATH,
    )

    df = pd.read_csv(
        INPUT_PATH,
        parse_dates=[
            "doc_date",
            "posting_date",
            "net_due_date",
        ],
        low_memory=False,
    )

    required_columns = {
        "document_no",
        "cust_num",
        "posting_date",
        STRICT_TARGET_COLUMN,
        TARGET_COLUMN,
    }

    missing = (
        required_columns
        - set(df.columns)
    )

    if missing:
        raise ValueError(
            "Input dataset missing columns: "
            f"{sorted(missing)}"
        )

    df = (
        df.sort_values(
            [
                "posting_date",
                "document_no",
            ]
        )
        .reset_index(
            drop=True
        )
    )

    total_rows = len(df)

    print(
        "Total rows:",
        total_rows,
    )

    # ---------------------------------------------------------
    # Count rows for every posting date.
    #
    # Splitting by complete posting dates prevents the same
    # business date from appearing in two different partitions.
    # ---------------------------------------------------------

    daily_counts = (
        df.groupby(
            "posting_date",
            sort=True,
        )
        .size()
        .rename("rows")
        .reset_index()
    )

    daily_counts[
        "cumulative_rows"
    ] = (
        daily_counts[
            "rows"
        ].cumsum()
    )

    train_target_rows = (
        total_rows
        * TRAIN_FRACTION
    )

    validation_end_target_rows = (
        total_rows
        * (
            TRAIN_FRACTION
            + VALIDATION_FRACTION
        )
    )

    train_cutoff_index = (
        choose_cutoff_index(
            daily_counts,
            train_target_rows,
        )
    )

    validation_cutoff_index = (
        choose_cutoff_index(
            daily_counts,
            validation_end_target_rows,
            minimum_index=(
                train_cutoff_index
                + 1
            ),
        )
    )

    train_cutoff = (
        pd.Timestamp(
            daily_counts.loc[
                train_cutoff_index,
                "posting_date",
            ]
        )
    )

    validation_cutoff = (
        pd.Timestamp(
            daily_counts.loc[
                validation_cutoff_index,
                "posting_date",
            ]
        )
    )

    # ---------------------------------------------------------
    # Temporal split
    # ---------------------------------------------------------

    train = df[
        df["posting_date"]
        <= train_cutoff
    ].copy()

    validation = df[
        (
            df["posting_date"]
            > train_cutoff
        )
        & (
            df["posting_date"]
            <= validation_cutoff
        )
    ].copy()

    test = df[
        df["posting_date"]
        > validation_cutoff
    ].copy()

    # ---------------------------------------------------------
    # Integrity checks
    # ---------------------------------------------------------

    if (
        len(train)
        + len(validation)
        + len(test)
        != total_rows
    ):
        raise AssertionError(
            "Split row counts do not "
            "preserve all input rows."
        )

    if not (
        train[
            "posting_date"
        ].max()
        <
        validation[
            "posting_date"
        ].min()
    ):
        raise AssertionError(
            "Train/validation chronology violated."
        )

    if not (
        validation[
            "posting_date"
        ].max()
        <
        test[
            "posting_date"
        ].min()
    ):
        raise AssertionError(
            "Validation/test chronology violated."
        )

    train_documents = set(
        train["document_no"]
    )

    validation_documents = set(
        validation[
            "document_no"
        ]
    )

    test_documents = set(
        test["document_no"]
    )

    if (
        train_documents
        & validation_documents
    ):
        raise AssertionError(
            "Document overlap between "
            "train and validation."
        )

    if (
        train_documents
        & test_documents
    ):
        raise AssertionError(
            "Document overlap between "
            "train and test."
        )

    if (
        validation_documents
        & test_documents
    ):
        raise AssertionError(
            "Document overlap between "
            "validation and test."
        )

    # Posting-date overlap checks.
    train_dates = set(
        train["posting_date"]
    )

    validation_dates = set(
        validation["posting_date"]
    )

    test_dates = set(
        test["posting_date"]
    )

    if (
        train_dates
        & validation_dates
    ):
        raise AssertionError(
            "Posting-date overlap between "
            "train and validation."
        )

    if (
        train_dates
        & test_dates
    ):
        raise AssertionError(
            "Posting-date overlap between "
            "train and test."
        )

    if (
        validation_dates
        & test_dates
    ):
        raise AssertionError(
            "Posting-date overlap between "
            "validation and test."
        )

    # ---------------------------------------------------------
    # Save
    # ---------------------------------------------------------

    OUTPUT_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    train.to_csv(
        TRAIN_PATH,
        index=False,
        compression="gzip",
    )

    validation.to_csv(
        VALIDATION_PATH,
        index=False,
        compression="gzip",
    )

    test.to_csv(
        TEST_PATH,
        index=False,
        compression="gzip",
    )

    manifest = {
        "version": 2,

        "strategy": (
            "chronological whole-posting-date "
            "70/15/15"
        ),

        "fractions": {
            "train": (
                TRAIN_FRACTION
            ),
            "validation": (
                VALIDATION_FRACTION
            ),
            "test": (
                TEST_FRACTION
            ),
        },

        "train_cutoff": str(
            train_cutoff.date()
        ),

        "validation_cutoff": str(
            validation_cutoff.date()
        ),

        "targets": {
            "calendar": {
                "column": (
                    STRICT_TARGET_COLUMN
                ),

                "definition": (
                    "1 when clearing_date "
                    "> raw net_due_date"
                ),
            },

            "weekend_adjusted": {
                "column": (
                    TARGET_COLUMN
                ),

                "definition": (
                    "1 when clearing_date "
                    "> weekend-adjusted due date; "
                    "Saturday/Sunday due dates "
                    "roll forward to Monday"
                ),
            },
        },

        "train": (
            describe_split(
                train
            )
        ),

        "validation": (
            describe_split(
                validation
            )
        ),

        "test": (
            describe_split(
                test
            )
        ),

        "integrity_checks": {
            "all_rows_preserved": True,
            "strict_chronology": True,
            "no_document_overlap": True,
            "no_posting_date_overlap": True,
        },

        "test_reserved": True,

        "notes": [
            (
                "Both target definitions use "
                "the exact same invoice splits."
            ),

            (
                "Target definition does not "
                "influence split membership."
            ),

            (
                "The test partition is created "
                "but must remain unused during "
                "model development and tuning."
            ),
        ],
    }

    MANIFEST_PATH.write_text(
        json.dumps(
            manifest,
            indent=2,
        )
        + "\n"
    )

    # ---------------------------------------------------------
    # Report
    # ---------------------------------------------------------

    print()
    print("=" * 80)
    print(
        "RECOVERIQ V2 DUAL-TARGET "
        "TEMPORAL SPLIT COMPLETE"
    )
    print("=" * 80)

    print()
    print(
        "Train cutoff:",
        train_cutoff.date(),
    )

    print(
        "Validation cutoff:",
        validation_cutoff.date(),
    )

    print_split_summary(
        "TRAIN",
        train,
    )

    print_split_summary(
        "VALIDATION",
        validation,
    )

    print_split_summary(
        "TEST",
        test,
    )

    print()
    print("=" * 80)
    print("INTEGRITY CHECKS")
    print("=" * 80)

    print(
        "Chronology: PASSED"
    )

    print(
        "Posting-date isolation: PASSED"
    )

    print(
        "Document overlap: PASSED"
    )

    print(
        "All rows preserved: PASSED"
    )

    print()
    print(
        "Train:",
        TRAIN_PATH,
    )

    print(
        "Validation:",
        VALIDATION_PATH,
    )

    print(
        "Test:",
        TEST_PATH,
    )

    print(
        "Manifest:",
        MANIFEST_PATH,
    )

    print()
    print(
        "TEST IS RESERVED AND SHOULD "
        "NOT BE USED DURING TUNING."
    )


if __name__ == "__main__":
    split()
