from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping, Sequence

from app.ml.payment_features import (
    CATEGORICAL_FEATURES,
    FEATURES,
    NUMERIC_FEATURES,
    TARGET_COLUMN,
)


PREPROCESSING_VERSION = "numeric_only_v1"
TRAINING_CONTRACT_SCHEMA_VERSION = 1


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()

    with path.open("rb") as handle:
        for chunk in iter(
            lambda: handle.read(1024 * 1024),
            b"",
        ):
            digest.update(chunk)

    return digest.hexdigest()


def save_training_contract(
    *,
    model_name: str,
    directory: Path,
    model_path: Path,
    train_split_path: Path,
    category_levels: Mapping[
        str,
        Sequence[str],
    ],
    median_path: Path | None = None,
) -> Path:
    if not model_path.exists():
        raise FileNotFoundError(model_path)

    if not train_split_path.exists():
        raise FileNotFoundError(
            train_split_path
        )

    normalized_levels = {
        column: [
            str(value)
            for value in category_levels[column]
        ]
        for column in CATEGORICAL_FEATURES
    }

    model_sha256 = sha256_file(model_path)

    artifact = {
        "model_file": model_path.name,
        "model_sha256": model_sha256,
    }

    if median_path is not None:
        if not median_path.exists():
            raise FileNotFoundError(
                median_path
            )

        artifact["median_file"] = (
            median_path.name
        )
        artifact["median_sha256"] = (
            sha256_file(median_path)
        )

    contract = {
        "schema_version": (
            TRAINING_CONTRACT_SCHEMA_VERSION
        ),
        "model": model_name,
        "model_version": (
            f"{model_name}:"
            f"{model_sha256[:12]}"
        ),
        "target": TARGET_COLUMN,
        "features": FEATURES,
        "numeric": NUMERIC_FEATURES,
        "categorical": (
            CATEGORICAL_FEATURES
        ),
        "preprocessing": (
            PREPROCESSING_VERSION
        ),
        "category_levels": (
            normalized_levels
        ),
        "train_split": {
            "file": train_split_path.name,
            "sha256": sha256_file(
                train_split_path
            ),
        },
        "artifact": artifact,
        "trained_at": (
            datetime.now(timezone.utc)
            .isoformat()
        ),
    }

    path = (
        directory
        / "training_contract.json"
    )

    path.write_text(
        json.dumps(
            contract,
            indent=2,
        )
        + "\n"
    )

    return path
