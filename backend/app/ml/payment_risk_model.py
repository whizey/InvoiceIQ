from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import joblib
import numpy as np
import pandas as pd
import xgboost as xgb

from app.ml.artifact_contract import sha256_file
from app.ml.payment_features import (
    CATEGORICAL_FEATURES,
    FEATURES,
    NUMERIC_FEATURES,
    TARGET_COLUMN,
    PaymentRiskFeatures,
)


ARTIFACT_ROOT = (
    Path(__file__).parent
    / "artifacts"
    / "models"
)

SUPPORTED_MODELS = {
    "xgboost",
    "random_forest",
    "gradient_boosting",
}

PREPROCESSING_VERSION = "numeric_only_v1"


class PaymentRiskArtifactError(RuntimeError):
    pass


@dataclass(slots=True)
class PaymentRiskContribution:
    feature: str
    label: str
    value: float | str | None
    shap_value: float
    direction: str


@dataclass(slots=True)
class PaymentRiskPrediction:
    late_payment_probability: float
    model_name: str
    contributions: list[
        PaymentRiskContribution
    ]


def _label(name: str) -> str:
    return name.replace("_", " ").title()


@lru_cache(maxsize=3)
def _load_contract(
    model_name: str,
) -> dict:
    if model_name not in SUPPORTED_MODELS:
        raise PaymentRiskArtifactError(
            f"Unsupported model: {model_name}"
        )

    path = (
        ARTIFACT_ROOT
        / model_name
        / "runtime_contract.json"
    )

    if not path.exists():
        raise PaymentRiskArtifactError(
            f"{model_name} is not runtime-ready. "
            "Retrain it, then package the "
            "runtime contract."
        )

    contract = json.loads(
        path.read_text()
    )

    if contract.get("target") != TARGET_COLUMN:
        raise PaymentRiskArtifactError(
            "Artifact target does not match "
            "the current target."
        )

    if contract.get("features") != FEATURES:
        raise PaymentRiskArtifactError(
            "Artifact feature order does not "
            "match the current 18-feature "
            "contract."
        )

    if (
        contract.get("numeric")
        != NUMERIC_FEATURES
    ):
        raise PaymentRiskArtifactError(
            "Numeric feature contract mismatch."
        )

    if (
        contract.get("categorical")
        != CATEGORICAL_FEATURES
    ):
        raise PaymentRiskArtifactError(
            "Categorical feature contract mismatch."
        )

    if (
        contract.get("preprocessing")
        != PREPROCESSING_VERSION
    ):
        raise PaymentRiskArtifactError(
            "Preprocessing version mismatch."
        )

    levels = contract.get(
        "category_levels",
        {},
    )

    for column in CATEGORICAL_FEATURES:
        if column not in levels:
            raise PaymentRiskArtifactError(
                f"Missing category levels for "
                f"{column}."
            )

    directory = (
        ARTIFACT_ROOT / model_name
    )

    model_file = contract.get(
        "model_file"
    )

    model_sha256 = contract.get(
        "model_sha256"
    )

    if (
        not isinstance(model_file, str)
        or not model_file
        or not isinstance(
            model_sha256,
            str,
        )
        or not model_sha256
    ):
        raise PaymentRiskArtifactError(
            f"{model_name}: incomplete "
            "runtime model fingerprint."
        )

    model_path = (
        directory / model_file
    )

    if not model_path.exists():
        raise PaymentRiskArtifactError(
            f"Missing {model_path}"
        )

    if (
        sha256_file(model_path)
        != model_sha256
    ):
        raise PaymentRiskArtifactError(
            f"{model_name}: model artifact "
            "hash mismatch."
        )

    if model_name != "xgboost":
        median_file = contract.get(
            "median_file"
        )

        median_sha256 = contract.get(
            "median_sha256"
        )

        if (
            not isinstance(
                median_file,
                str,
            )
            or not median_file
            or not isinstance(
                median_sha256,
                str,
            )
            or not median_sha256
        ):
            raise PaymentRiskArtifactError(
                f"{model_name}: incomplete "
                "runtime median fingerprint."
            )

        median_path = (
            directory / median_file
        )

        if not median_path.exists():
            raise PaymentRiskArtifactError(
                f"Missing {median_path}"
            )

        if (
            sha256_file(median_path)
            != median_sha256
        ):
            raise PaymentRiskArtifactError(
                f"{model_name}: median artifact "
                "hash mismatch."
            )

    return contract


@lru_cache(maxsize=3)
def _load_model(model_name: str):
    contract = _load_contract(
        model_name
    )

    directory = (
        ARTIFACT_ROOT / model_name
    )

    path = (
        directory
        / contract["model_file"]
    )

    if model_name == "xgboost":
        model = xgb.XGBClassifier()
        model.load_model(str(path))
        return model

    return joblib.load(path)


def _prepare_frames(
    model_name: str,
    features: PaymentRiskFeatures,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    contract = _load_contract(model_name)

    raw = pd.DataFrame(
        [features.to_row()],
        columns=FEATURES,
    )

    prepared = raw.copy()

    levels = contract[
        "category_levels"
    ]

    for column in CATEGORICAL_FEATURES:
        prepared[column] = pd.Categorical(
            prepared[column].astype("string"),
            categories=levels[column],
        )

    if model_name == "xgboost":
        return raw, prepared

    for column in CATEGORICAL_FEATURES:
        prepared[column] = (
            prepared[column]
            .cat.codes
            .astype(float)
        )

    median_path = (
        ARTIFACT_ROOT
        / model_name
        / contract["median_file"]
    )

    if not median_path.exists():
        raise PaymentRiskArtifactError(
            f"Missing {median_path}"
        )

    medians = json.loads(
        median_path.read_text()
    )

    for column in NUMERIC_FEATURES:
        prepared[column] = pd.to_numeric(
            prepared[column],
            errors="coerce",
        )

        if column not in medians:
            raise PaymentRiskArtifactError(
                f"Missing training median for "
                f"{column}."
            )

        prepared[column] = (
            prepared[column]
            .fillna(float(medians[column]))
        )

    return raw, prepared


@lru_cache(maxsize=3)
def _load_shap_explainer(
    model_name: str,
):
    import shap

    model = _load_model(model_name)

    return shap.TreeExplainer(
        model
    )


def _positive_class_shap(
    model_name: str,
    frame: pd.DataFrame,
) -> np.ndarray:
    explainer = (
        _load_shap_explainer(
            model_name
        )
    )

    values = explainer.shap_values(
        frame
    )

    if isinstance(values, list):
        selected = (
            values[1]
            if len(values) > 1
            else values[0]
        )
        return np.asarray(selected)[0]

    array = np.asarray(values)

    if array.ndim == 1:
        return array

    if array.ndim == 2:
        return array[0]

    if array.ndim == 3:
        if (
            array.shape[0] == 1
            and array.shape[2] >= 2
        ):
            return array[0, :, 1]

        if (
            array.shape[0] >= 2
            and array.shape[1] == 1
        ):
            return array[1, 0, :]

    raise PaymentRiskArtifactError(
        "Unsupported SHAP output shape: "
        f"{array.shape}"
    )


def predict_payment_risk(
    features: PaymentRiskFeatures,
    model_name: str,
) -> PaymentRiskPrediction:
    model_name = model_name.strip().lower()

    raw, prepared = _prepare_frames(
        model_name,
        features,
    )

    model = _load_model(model_name)

    probability = float(
        model.predict_proba(prepared)[0][1]
    )

    if model_name == "xgboost":
        matrix = xgb.DMatrix(
            prepared,
            enable_categorical=True,
        )

        shap_values = (
            model
            .get_booster()
            .predict(
                matrix,
                pred_contribs=True,
            )[0][:-1]
        )
    else:
        shap_values = _positive_class_shap(
            model_name,
            prepared,
        )

    contributions = []

    for feature_name, shap_value in zip(
        FEATURES,
        shap_values,
        strict=True,
    ):
        raw_value = raw.iloc[0][
            feature_name
        ]

        if pd.isna(raw_value):
            value = None
        elif feature_name in CATEGORICAL_FEATURES:
            value = str(raw_value)
        else:
            value = float(raw_value)

        shap_float = float(shap_value)

        if shap_float > 0:
            direction = (
                "increases_late_payment_probability"
            )
        elif shap_float < 0:
            direction = (
                "decreases_late_payment_probability"
            )
        else:
            direction = "neutral"

        contributions.append(
            PaymentRiskContribution(
                feature=feature_name,
                label=_label(feature_name),
                value=value,
                shap_value=round(
                    shap_float,
                    6,
                ),
                direction=direction,
            )
        )

    contributions.sort(
        key=lambda item: abs(
            item.shap_value
        ),
        reverse=True,
    )

    return PaymentRiskPrediction(
        late_payment_probability=round(
            probability,
            6,
        ),
        model_name=model_name,
        contributions=contributions,
    )
