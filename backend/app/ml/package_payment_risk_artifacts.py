from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

import joblib
import xgboost as xgb

from app.ml.artifact_contract import (
    PREPROCESSING_VERSION,
    sha256_file,
)
from app.ml.common import ARTIFACT_ROOT
from app.ml.payment_features import (
    CATEGORICAL_FEATURES,
    FEATURES,
    NUMERIC_FEATURES,
    TARGET_COLUMN,
)


MODELS = [
    "xgboost",
    "random_forest",
    "gradient_boosting",
]


def validate_feature_count(
    model_name: str,
    directory: Path,
) -> None:
    if model_name == "xgboost":
        model_path = (
            directory / "model.json"
        )

        model = xgb.XGBClassifier()
        model.load_model(
            str(model_path)
        )

        count = (
            model.get_booster()
            .num_features()
        )
    else:
        model_path = (
            directory / "model.joblib"
        )

        model = joblib.load(
            model_path
        )

        count = int(
            model.n_features_in_
        )

    if count != len(FEATURES):
        raise RuntimeError(
            f"{model_name}: model has "
            f"{count} features; expected "
            f"{len(FEATURES)}."
        )


def load_and_verify_training_contract(
    model_name: str,
    directory: Path,
) -> dict:
    path = (
        directory
        / "training_contract.json"
    )

    if not path.exists():
        raise RuntimeError(
            f"{model_name}: missing "
            "training_contract.json. "
            "Retrain this model."
        )

    contract = json.loads(
        path.read_text()
    )

    checks = {
        "model": model_name,
        "target": TARGET_COLUMN,
        "features": FEATURES,
        "numeric": NUMERIC_FEATURES,
        "categorical": (
            CATEGORICAL_FEATURES
        ),
        "preprocessing": (
            PREPROCESSING_VERSION
        ),
    }

    for key, expected in checks.items():
        actual = contract.get(key)

        if actual != expected:
            raise RuntimeError(
                f"{model_name}: "
                f"{key} mismatch. "
                f"expected={expected!r}, "
                f"actual={actual!r}"
            )

    category_levels = contract.get(
        "category_levels"
    )

    if not isinstance(
        category_levels,
        dict,
    ):
        raise RuntimeError(
            f"{model_name}: missing "
            "category_levels."
        )

    for column in CATEGORICAL_FEATURES:
        levels = category_levels.get(
            column
        )

        if not isinstance(levels, list):
            raise RuntimeError(
                f"{model_name}: invalid "
                f"category levels for "
                f"{column}."
            )

    artifact = contract.get(
        "artifact"
    )

    if not isinstance(artifact, dict):
        raise RuntimeError(
            f"{model_name}: missing "
            "artifact metadata."
        )

    model_file = artifact.get(
        "model_file"
    )
    model_sha256 = artifact.get(
        "model_sha256"
    )

    if not model_file or not model_sha256:
        raise RuntimeError(
            f"{model_name}: incomplete "
            "model fingerprint."
        )

    model_path = (
        directory / model_file
    )

    if not model_path.exists():
        raise RuntimeError(
            f"{model_name}: missing "
            f"{model_file}."
        )

    actual_model_hash = (
        sha256_file(model_path)
    )

    if actual_model_hash != model_sha256:
        raise RuntimeError(
            f"{model_name}: model hash "
            "does not match its training "
            "contract."
        )

    if model_name != "xgboost":
        median_file = artifact.get(
            "median_file"
        )
        median_sha256 = artifact.get(
            "median_sha256"
        )

        if (
            not median_file
            or not median_sha256
        ):
            raise RuntimeError(
                f"{model_name}: incomplete "
                "median fingerprint."
            )

        median_path = (
            directory / median_file
        )

        if not median_path.exists():
            raise RuntimeError(
                f"{model_name}: missing "
                f"{median_file}."
            )

        if (
            sha256_file(median_path)
            != median_sha256
        ):
            raise RuntimeError(
                f"{model_name}: median "
                "file hash does not match "
                "its training contract."
            )

    return contract


def main() -> None:
    parser = argparse.ArgumentParser()

    parser.add_argument(
        "--confirm-retrained",
        action="store_true",
    )

    args = parser.parse_args()

    if not args.confirm_retrained:
        raise SystemExit(
            "Refusing to package stale "
            "artifacts. Retrain all three "
            "models first, then rerun with "
            "--confirm-retrained."
        )

    for model_name in MODELS:
        directory = (
            ARTIFACT_ROOT
            / model_name
        )

        validate_feature_count(
            model_name,
            directory,
        )

        training = (
            load_and_verify_training_contract(
                model_name,
                directory,
            )
        )

        artifact = training[
            "artifact"
        ]

        runtime_contract = {
            "schema_version": 2,
            "model": model_name,
            "model_version": training[
                "model_version"
            ],
            "target": training[
                "target"
            ],
            "features": training[
                "features"
            ],
            "numeric": training[
                "numeric"
            ],
            "categorical": training[
                "categorical"
            ],
            "preprocessing": training[
                "preprocessing"
            ],
            "category_levels": training[
                "category_levels"
            ],
            "model_file": artifact[
                "model_file"
            ],
            "model_sha256": artifact[
                "model_sha256"
            ],
            "train_split_sha256": (
                training[
                    "train_split"
                ]["sha256"]
            ),
            "trained_at": training[
                "trained_at"
            ],
            "packaged_at": (
                datetime.now(
                    timezone.utc
                ).isoformat()
            ),
        }

        if model_name != "xgboost":
            runtime_contract[
                "median_file"
            ] = artifact[
                "median_file"
            ]

            runtime_contract[
                "median_sha256"
            ] = artifact[
                "median_sha256"
            ]

        path = (
            directory
            / "runtime_contract.json"
        )

        path.write_text(
            json.dumps(
                runtime_contract,
                indent=2,
            )
            + "\n"
        )

        print(
            model_name,
            "->",
            path,
            "version=",
            runtime_contract[
                "model_version"
            ],
        )


if __name__ == "__main__":
    main()
