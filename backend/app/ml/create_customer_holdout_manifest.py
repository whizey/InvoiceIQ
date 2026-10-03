from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path

import pandas as pd

from app.ml.artifact_contract import sha256_file
from app.ml.common import SPLIT_DIR


FROZEN_ON = "2026-10-03"
SALT = "recoveriq-new-customer-diagnostic-v1"
HOLDOUT_FRACTION = 0.15

MANIFEST_PATH = (
    SPLIT_DIR.parent
    / "customer_holdout_manifest.json"
)


def customer_hash(
    customer_id: str,
) -> str:
    normalized = str(
        customer_id
    ).strip()

    payload = (
        f"{SALT}|{normalized}"
    )

    return hashlib.sha256(
        payload.encode("utf-8")
    ).hexdigest()


def load_pretest_customers() -> list[str]:
    customers: set[str] = set()

    for split_name in (
        "train",
        "validation",
    ):
        path = (
            SPLIT_DIR
            / f"{split_name}.csv.gz"
        )

        if not path.exists():
            raise FileNotFoundError(
                path
            )

        frame = pd.read_csv(
            path,
            usecols=["cust_num"],
            dtype={
                "cust_num": "string",
            },
        )

        customers.update(
            frame["cust_num"]
            .dropna()
            .astype(str)
            .str.strip()
            .tolist()
        )

    return sorted(customers)


def select_holdout_hashes(
    customers: list[str],
) -> list[str]:
    unique = sorted(
        {
            str(value).strip()
            for value in customers
            if str(value).strip()
        }
    )

    if not unique:
        raise ValueError(
            "No customers supplied"
        )

    ranked = sorted(
        customer_hash(value)
        for value in unique
    )

    count = max(
        1,
        math.ceil(
            len(ranked)
            * HOLDOUT_FRACTION
        ),
    )

    return ranked[:count]


def build_manifest() -> dict:
    customers = (
        load_pretest_customers()
    )

    selected = (
        select_holdout_hashes(
            customers
        )
    )

    train_path = (
        SPLIT_DIR
        / "train.csv.gz"
    )

    validation_path = (
        SPLIT_DIR
        / "validation.csv.gz"
    )

    return {
        "schema_version": 1,
        "frozen_on": FROZEN_ON,
        "purpose": (
            "New-customer diagnostic only; "
            "never model selection or tuning."
        ),
        "selection_rule": (
            "Hash each train/validation customer "
            "with SHA-256 using the frozen salt, "
            "sort hashes ascending, and reserve "
            "the first ceil(15%)."
        ),
        "salt": SALT,
        "holdout_fraction": (
            HOLDOUT_FRACTION
        ),
        "source_population": (
            "unique customers appearing in "
            "train or validation only"
        ),
        "test_split_read": False,
        "target_columns_read": [],
        "available_customer_count": (
            len(customers)
        ),
        "holdout_customer_count": (
            len(selected)
        ),
        "customer_sha256": selected,
        "source_files": {
            "train.csv.gz": (
                sha256_file(
                    train_path
                )
            ),
            "validation.csv.gz": (
                sha256_file(
                    validation_path
                )
            ),
        },
    }


def write_or_verify(
    *,
    check_only: bool,
) -> None:
    expected = build_manifest()

    if check_only:
        if not MANIFEST_PATH.exists():
            raise SystemExit(
                "Customer holdout manifest "
                "does not exist."
            )

        actual = json.loads(
            MANIFEST_PATH.read_text()
        )

        if actual != expected:
            raise SystemExit(
                "Customer holdout manifest "
                "does not match frozen rule "
                "or current train/validation files."
            )

        print(
            "Customer holdout manifest: "
            "VERIFIED"
        )

        print(
            "Customers:",
            actual[
                "available_customer_count"
            ],
        )

        print(
            "Held out:",
            actual[
                "holdout_customer_count"
            ],
        )

        print(
            "Test split read:",
            actual["test_split_read"],
        )

        return

    if MANIFEST_PATH.exists():
        actual = json.loads(
            MANIFEST_PATH.read_text()
        )

        if actual != expected:
            raise SystemExit(
                "A different customer holdout "
                "manifest already exists. "
                "Refusing to replace a frozen "
                "diagnostic after the fact."
            )

        print(
            "Customer holdout already frozen "
            "and unchanged."
        )

        return

    MANIFEST_PATH.write_text(
        json.dumps(
            expected,
            indent=2,
        )
        + "\n"
    )

    print(
        "Frozen new-customer diagnostic:"
    )

    print(
        MANIFEST_PATH
    )

    print(
        "Available customers:",
        expected[
            "available_customer_count"
        ],
    )

    print(
        "Held-out customers:",
        expected[
            "holdout_customer_count"
        ],
    )

    print(
        "Targets read: NONE"
    )

    print(
        "Test split read: NO"
    )


def main() -> None:
    parser = argparse.ArgumentParser()

    parser.add_argument(
        "--check",
        action="store_true",
    )

    args = parser.parse_args()

    write_or_verify(
        check_only=args.check
    )


if __name__ == "__main__":
    main()
