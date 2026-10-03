from __future__ import annotations

import json
from dataclasses import dataclass

from app.ml.common import ARTIFACT_ROOT
from app.ml.payment_features import (
    FEATURES,
    TARGET_COLUMN,
)


FINAL_MODEL_FAMILIES = (
    "xgboost",
    "random_forest",
    "gradient_boosting",
)

PRIMARY_SELECTION_SCOPE = (
    "validation"
)

PRIMARY_SELECTION_METRIC = (
    "within_period_auc"
)

SECONDARY_REPORTING_METRICS = (
    "pooled_roc_auc",
    "pooled_pr_auc",
    "log_loss",
    "brier",
)


class ModelSelectionError(
    RuntimeError
):
    pass


@dataclass(
    frozen=True,
    slots=True,
)
class ModelSelectionResult:
    model_name: str
    primary_metric: str
    primary_value: float
    diagnostics: dict


def validate_report(
    model_name: str,
    report: dict,
) -> None:
    if (
        report.get("model")
        != model_name
    ):
        raise ModelSelectionError(
            f"{model_name}: report model "
            "identity mismatch"
        )

    if (
        report.get("target")
        != TARGET_COLUMN
    ):
        raise ModelSelectionError(
            f"{model_name}: target mismatch"
        )

    if report.get("test_used") is not False:
        raise ModelSelectionError(
            f"{model_name}: report indicates "
            "the reserved test split was used"
        )

    feature_block = report.get(
        "features",
        {},
    )

    if (
        feature_block.get("order")
        != FEATURES
    ):
        raise ModelSelectionError(
            f"{model_name}: feature contract "
            "mismatch"
        )

    validation = report.get(
        PRIMARY_SELECTION_SCOPE
    )

    if not isinstance(
        validation,
        dict,
    ):
        raise ModelSelectionError(
            f"{model_name}: missing "
            "validation metrics"
        )

    value = validation.get(
        PRIMARY_SELECTION_METRIC
    )

    if not isinstance(
        value,
        (int, float),
    ):
        raise ModelSelectionError(
            f"{model_name}: missing numeric "
            f"{PRIMARY_SELECTION_METRIC}"
        )


def select_final_model(
    reports: dict[str, dict],
) -> ModelSelectionResult:
    missing = [
        name
        for name
        in FINAL_MODEL_FAMILIES
        if name not in reports
    ]

    if missing:
        raise ModelSelectionError(
            "Missing final model reports: "
            + ", ".join(missing)
        )

    for model_name in (
        FINAL_MODEL_FAMILIES
    ):
        validate_report(
            model_name,
            reports[model_name],
        )

    selected_name = max(
        FINAL_MODEL_FAMILIES,
        key=lambda name: float(
            reports[name][
                PRIMARY_SELECTION_SCOPE
            ][
                PRIMARY_SELECTION_METRIC
            ]
        ),
    )

    report = reports[
        selected_name
    ]

    temporal = report.get(
        "temporal_folds",
        {},
    )

    validation = report[
        PRIMARY_SELECTION_SCOPE
    ]

    diagnostics = {
        "pooled_roc_auc": (
            validation.get(
                "pooled_roc_auc"
            )
        ),
        "pooled_pr_auc": (
            validation.get(
                "pooled_pr_auc"
            )
        ),
        "log_loss": (
            validation.get(
                "log_loss"
            )
        ),
        "brier": (
            validation.get(
                "brier"
            )
        ),
        "temporal_mean": (
            temporal.get("mean")
        ),
        "temporal_min": (
            temporal.get("min")
        ),
        "temporal_std": (
            temporal.get("std")
        ),
        "inverted_folds": (
            temporal.get(
                "inverted_folds",
                [],
            )
        ),
        "stable": (
            temporal.get("stable")
        ),
    }

    return ModelSelectionResult(
        model_name=selected_name,
        primary_metric=(
            PRIMARY_SELECTION_METRIC
        ),
        primary_value=float(
            validation[
                PRIMARY_SELECTION_METRIC
            ]
        ),
        diagnostics=diagnostics,
    )


def load_final_reports() -> dict:
    reports = {}

    for model_name in (
        FINAL_MODEL_FAMILIES
    ):
        path = (
            ARTIFACT_ROOT
            / model_name
            / "report.json"
        )

        if not path.exists():
            continue

        reports[model_name] = (
            json.loads(
                path.read_text()
            )
        )

    return reports


def main() -> None:
    reports = load_final_reports()

    result = select_final_model(
        reports
    )

    print(
        "FROZEN FINAL MODEL "
        "SELECTION RULE"
    )

    print(
        "scope:",
        PRIMARY_SELECTION_SCOPE,
    )

    print(
        "metric:",
        PRIMARY_SELECTION_METRIC,
    )

    print(
        "test used for selection: NO"
    )

    print(
        "selected model:",
        result.model_name,
    )

    print(
        "primary value:",
        f"{result.primary_value:.4f}",
    )

    print(
        "temporal diagnostics:",
        json.dumps(
            result.diagnostics,
            indent=2,
        ),
    )


if __name__ == "__main__":
    main()
