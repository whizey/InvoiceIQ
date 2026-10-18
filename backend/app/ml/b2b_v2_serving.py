"""
RecoverIQ — B2B v2 Serving / Decision Layer

Produces all supported B2B business answers from one invoice.

Important:
- ML produces probabilities / duration estimates.
- Deterministic policy decides actions.
- Unsupported questions are explicitly identified.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

import joblib
import numpy as np
import pandas as pd

from app.ml.named_inference import inference_input


BASE = (
    Path(__file__).resolve().parent
    / "artifacts"
    / "b2b_v2"
)

MODEL_ROOT = (
    BASE
    / "models"
)

QUANTILE_CALIBRATION = (
    BASE
    / "calibration"
    / "quantile_calibration.json"
)

AGENT_CONTRACT = (
    BASE
    / "agent_answer_contract.json"
)

CALIBRATION_POLICY = (
    BASE
    / "calibration"
    / "serving_calibration_policy.json"
)


@lru_cache(maxsize=None)
def load_json(path: str):
    return json.loads(
        Path(path).read_text()
    )


@lru_cache(maxsize=None)
def load_model(name: str):
    return joblib.load(
        MODEL_ROOT
        / name
        / "model.joblib"
    )


@lru_cache(maxsize=None)
def load_metadata(name: str):
    return json.loads(
        (
            MODEL_ROOT
            / name
            / "metadata.json"
        ).read_text()
    )


@lru_cache(maxsize=None)
def load_calibrator(name: str):
    return joblib.load(
        MODEL_ROOT
        / name
        / "calibrator.joblib"
    )


def logit_probability(p):
    p = np.clip(
        np.asarray(p),
        1e-6,
        1 - 1e-6,
    )

    return np.log(
        p / (1 - p)
    )


def calibrate(
    calibrator,
    probability,
):
    probability = np.asarray(
        probability,
        dtype=float,
    )

    if (
        calibrator["kind"]
        == "isotonic"
    ):
        output = calibrator[
            "model"
        ].predict(
            probability
        )

    else:
        output = calibrator[
            "model"
        ].predict_proba(
            logit_probability(
                probability
            ).reshape(-1, 1)
        )[:, 1]

    return np.clip(
        output,
        0,
        1,
    )


def make_frame(
    feature_values: dict,
    features: list[str],
):
    return pd.DataFrame(
        [
            {
                feature:
                    feature_values.get(
                        feature,
                        np.nan,
                    )
                for feature
                in features
            }
        ]
    )


def classifier_probability(
    name: str,
    feature_values: dict,
):
    metadata = load_metadata(
        name
    )

    features = metadata[
        "features"
    ]

    frame = make_frame(
        feature_values,
        features,
    )

    estimator, prepared = inference_input(load_model(name), frame)
    raw = float(estimator.predict_proba(prepared)[0, 1])

    policy = load_json(
        str(
            CALIBRATION_POLICY
        )
    )[name]

    mode = policy[
        "serving_mode"
    ]

    if mode == "platt":

        calibrator = joblib.load(
            MODEL_ROOT
            / name
            / "calibrator_platt.joblib"
        )

        served = float(
            calibrate(
                calibrator,
                [raw],
            )[0]
        )

    else:
        served = raw

    return {
        "raw":
            raw,

        # Kept for compatibility with score_invoice().
        "calibrated":
            served,

        "served":
            served,

        "calibration_mode":
            mode,

        "calibration_status":
            policy[
                "status"
            ],

        "display":
            policy[
                "display"
            ],

        "metadata":
            metadata,
    }


def regression_prediction(
    name: str,
    feature_values: dict,
):
    metadata = load_metadata(
        name
    )

    frame = make_frame(
        feature_values,
        metadata[
            "features"
        ],
    )

    estimator, prepared = inference_input(load_model(name), frame)
    value = float(estimator.predict(prepared)[0])

    return max(
        value,
        0.0,
    )


def model_status(
    metadata: dict,
):
    temporal = metadata.get(
        "temporal_backtest",
        {}
    )

    worst_auc = temporal.get(
        "worst_roc_auc"
    )

    if worst_auc is None:
        return "SUPPORTED"

    if worst_auc < 0.55:
        return "EXPERIMENTAL"

    return "SUPPORTED"


def risk_tier(
    late_probability: float,
):
    if late_probability >= 0.75:
        return "HIGH"

    if late_probability >= 0.50:
        return "MEDIUM"

    return "LOW"


def intervention_policy(
    *,
    late_probability,
    p7,
    p15,
    days_until_due,
    expected_exposure=None,
    high_value_threshold=None,
):
    reasons = []

    if days_until_due is None:
        if late_probability >= 0.75:
            return (
                "REVIEW",
                [
                    "High late-payment probability."
                ],
            )

        return (
            "MONITOR",
            [
                "Due-date urgency unavailable."
            ],
        )

    overdue = (
        days_until_due < 0
    )

    if overdue:
        reasons.append(
            "Invoice is already overdue."
        )

        if (
            p7 >= 0.50
            or p15 >= 0.25
        ):
            reasons.append(
                "Material delay risk remains elevated."
            )

            return (
                "ESCALATE",
                reasons,
            )

        return (
            "CONTACT",
            reasons,
        )

    if (
        days_until_due <= 7
        and late_probability >= 0.65
    ):
        reasons.extend(
            [
                (
                    f"Due in only "
                    f"{days_until_due} day(s)."
                ),
                (
                    "High predicted probability "
                    "of missing due date."
                ),
            ]
        )

        if (
            high_value_threshold
            is not None
            and expected_exposure
            is not None
            and expected_exposure
            >= high_value_threshold
        ):
            reasons.append(
                "Expected overdue exposure "
                "exceeds high-value threshold."
            )

        return (
            "CONTACT_BEFORE_DUE",
            reasons,
        )

    if (
        days_until_due <= 14
        and late_probability >= 0.55
    ):
        return (
            "EARLY_REMINDER",
            [
                (
                    f"Due in "
                    f"{days_until_due} day(s)."
                ),
                (
                    "Elevated late-payment risk."
                ),
            ],
        )

    if late_probability >= 0.75:
        return (
            "REVIEW",
            [
                "High late-payment probability."
            ],
        )

    return (
        "MONITOR",
        [
            "No intervention threshold triggered."
        ],
    )


def add_days(
    date_value,
    days,
):
    if date_value is None:
        return None

    date = pd.Timestamp(
        date_value
    )

    return (
        date
        + pd.to_timedelta(
            float(days),
            unit="D",
        )
    ).isoformat()


def score_invoice(
    feature_values: dict,
    *,
    due_date=None,
    as_of_date=None,
    amount=None,
    high_value_threshold=None,
):
    # --------------------------------------------------------
    # Classifier heads
    # --------------------------------------------------------

    late = classifier_probability(
        "late_payment",
        feature_values,
    )

    delay7 = classifier_probability(
        "delay_over_7d",
        feature_values,
    )

    delay15 = classifier_probability(
        "delay_over_15d",
        feature_values,
    )

    p_late = late[
        "calibrated"
    ]

    # Logical consistency:
    # P(late) >= P(>7d) >= P(>15d)
    p7 = min(
        delay7[
            "calibrated"
        ],
        p_late,
    )

    # >15d does not have enough temporal support for
    # reliable probability calibration.
    #
    # Keep only its raw experimental ranking score.
    p15_experimental = float(
        delay15[
            "raw"
        ]
    )

    # Do not allow this experimental head to influence
    # production intervention policy.
    p15 = 0.0

    # --------------------------------------------------------
    # Conditional lateness
    # --------------------------------------------------------

    mean_late = regression_prediction(
        "days_late_if_late_mean",
        feature_values,
    )

    p80_raw = regression_prediction(
        "days_late_p80",
        feature_values,
    )

    qcal = load_json(
        str(
            QUANTILE_CALIBRATION
        )
    )

    # P80 temporal calibration was reasonably close to
    # the requested 80% coverage. Keep it as a conservative
    # clearance-window estimate rather than exposing quantile
    # jargon in the product response.
    conservative_clearance_window_days = max(
        0.0,
        p80_raw
        + float(
            qcal[
                "p80"
            ][
                "adjustment_days"
            ]
        ),
    )

    # --------------------------------------------------------
    # Derived business measures
    # --------------------------------------------------------

    expected_days_late = (
        p_late
        * mean_late
    )

    if amount is None:
        amount = feature_values.get(
            "amount"
        )

    try:
        numeric_amount = float(
            amount
        )
    except (
        TypeError,
        ValueError,
    ):
        numeric_amount = None

    expected_exposure = (
        numeric_amount
        * p_late
        if numeric_amount
        is not None
        else None
    )

    # --------------------------------------------------------
    # Due-date calculations
    # --------------------------------------------------------

    days_until_due = None

    if (
        due_date is not None
        and as_of_date is not None
    ):
        due = pd.Timestamp(
            due_date
        )

        now = pd.Timestamp(
            as_of_date
        )

        days_until_due = int(
            np.ceil(
                (
                    due - now
                ).total_seconds()
                / 86400
            )
        )

    action, reasons = (
        intervention_policy(
            late_probability=p_late,
            p7=p7,
            p15=p15,
            days_until_due=days_until_due,
            expected_exposure=expected_exposure,
            high_value_threshold=high_value_threshold,
        )
    )

    # --------------------------------------------------------
    # Output
    # --------------------------------------------------------

    result = {
        "risk": {
            "late_probability":
                p_late,

            "clear_by_due_probability":
                1 - p_late,

            "delay_over_7d_probability":
                p7,

            "clear_by_due_plus_7d_probability":
                1 - p7,

            "delay_over_15d_probability":
                None,

            "delay_over_15d_experimental_score":
                p15_experimental,

            "clear_by_due_plus_15d_probability":
                None,
        },

        "lateness": {
            "conditional_expected_days_late":
                mean_late,

            "conservative_clearance_window_days":
                conservative_clearance_window_days,

            "probability_weighted_expected_days_late":
                expected_days_late,
        },

        "dates": {
            "days_until_due":
                days_until_due,

            "conditional_expected_clearance_date":
                add_days(
                    due_date,
                    mean_late,
                ),

            "conservative_clearance_date":
                add_days(
                    due_date,
                    conservative_clearance_window_days,
                ),
        },

        "exposure": {
            "invoice_amount":
                numeric_amount,

            "expected_overdue_exposure":
                expected_exposure,
        },

        "decision": {
            "risk_tier":
                risk_tier(
                    p_late
                ),

            "recommended_action":
                action,

            "policy_reasons":
                reasons,
        },

        "model_status": {
            "late_payment":
                "SUPPORTED_UNCALIBRATED",

            "delay_over_7d":
                "SUPPORTED_CALIBRATED",

            "delay_over_15d":
                "EXPERIMENTAL_UNCALIBRATED",

            "lateness_mean":
                "SUPPORTED",

            "conservative_clearance_window":
                "SUPPORTED_PROVISIONAL",
        },

        "agent_answers": {
            "will_it_be_late":
                p_late,

            "will_it_clear_by_due_date":
                1 - p_late,

            "will_it_be_more_than_7_days_late":
                p7,

            "will_it_clear_by_due_plus_7_days":
                1 - p7,

            "will_it_be_more_than_15_days_late_probability":
                None,

            "delay_over_15d_experimental_score":
                p15_experimental,

            "will_it_clear_by_due_plus_15_days_probability":
                None,

            "if_late_how_many_days_expected":
                mean_late,

            "if_late_conservative_clearance_window_days":
                conservative_clearance_window_days,

            "expected_days_late_across_all_outcomes":
                expected_days_late,

            "cash_at_risk":
                expected_exposure,

            "do_we_need_to_intervene":
                action,

            "how_urgent":
                risk_tier(
                    p_late
                ),
        },

        "unsupported_questions": {
            "will_it_ever_be_recovered":
                (
                    "Not supported by B2B v2. "
                    "Requires recovery/censoring model."
                ),

            "when_will_it_clear_from_today":
                (
                    "Not supported by B2B v2. "
                    "Use BPI time-to-clear survival model."
                ),

            "probability_clear_within_next_7_days":
                (
                    "Use BPI survival model."
                ),

            "did_intervention_cause_recovery":
                (
                    "Requires intervention/control data."
                ),
        },
    }

    return result


def supported_question_contract():
    return load_json(
        str(
            AGENT_CONTRACT
        )
    )
