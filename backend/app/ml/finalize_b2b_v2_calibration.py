"""
RecoverIQ — Freeze B2B v2 calibration policy.

TEST SPLIT IS NEVER READ.

Final serving policy:
- late_payment: raw score
- delay_over_7d: Platt calibrated probability
- delay_over_15d: raw experimental score
"""

from __future__ import annotations

import json

import joblib
import numpy as np
from sklearn.linear_model import LogisticRegression

from app.ml.train_b2b_v2 import (
    ARTIFACT_ROOT,
    MODEL_ROOT,
    add_targets,
    build_temporal_folds,
    load_development_data,
)

from app.ml.calibrate_b2b_v2 import (
    classifier_oof,
)


POLICY_PATH = (
    ARTIFACT_ROOT
    / "calibration"
    / "serving_calibration_policy.json"
)


def logit(p):
    p = np.clip(
        np.asarray(p, dtype=float),
        1e-6,
        1 - 1e-6,
    )

    return np.log(
        p / (1 - p)
    )


def main():

    print("=" * 80)
    print("RECOVERIQ — FREEZE B2B V2 CALIBRATION POLICY")
    print("=" * 80)

    dev = add_targets(
        load_development_data()
    )

    folds = build_temporal_folds(
        dev
    )

    # --------------------------------------------------------
    # Train final Platt calibrator for >7d
    #
    # OOF predictions are used so calibration isn't trained
    # directly on in-sample predictions.
    # --------------------------------------------------------

    oof = classifier_oof(
        dev,
        folds,
        "delay_over_7d",
    )

    X = logit(
        oof["raw_probability"]
    ).reshape(-1, 1)

    y = (
        oof["actual"]
        .astype(int)
        .to_numpy()
    )

    calibrator = LogisticRegression(
        max_iter=2000,
        random_state=42,
    )

    calibrator.fit(
        X,
        y,
    )

    output = (
        MODEL_ROOT
        / "delay_over_7d"
        / "calibrator_platt.joblib"
    )

    joblib.dump(
        {
            "kind": "platt",
            "model": calibrator,
        },
        output,
    )

    policy = {
        "late_payment": {
            "serving_mode":
                "raw",

            "status":
                "SUPPORTED_UNCALIBRATED",

            "display":
                "late_risk_score",

            "reason":
                (
                    "Sequential Platt and isotonic calibration "
                    "did not improve temporal calibration "
                    "reliably."
                ),
        },

        "delay_over_7d": {
            "serving_mode":
                "platt",

            "status":
                "SUPPORTED_CALIBRATED",

            "display":
                "probability",

            "calibrator":
                str(output),

            "reason":
                (
                    "Sequential Platt materially improved "
                    "Brier and ECE without harming temporal "
                    "ranking."
                ),
        },

        "delay_over_15d": {
            "serving_mode":
                "raw",

            "status":
                "EXPERIMENTAL",

            "display":
                "experimental_risk_score",

            "reason":
                (
                    "Insufficient temporal positives for "
                    "honest sequential calibration."
                ),
        },
    }

    POLICY_PATH.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    POLICY_PATH.write_text(
        json.dumps(
            policy,
            indent=2,
        )
    )

    print()
    print("Saved Platt calibrator:")
    print(" ", output)

    print()
    print("Saved serving policy:")
    print(" ", POLICY_PATH)

    print()
    print("late_payment      → RAW")
    print("delay_over_7d     → PLATT")
    print("delay_over_15d    → RAW / EXPERIMENTAL")

    print()
    print("TEST SPLIT WAS NEVER READ.")


if __name__ == "__main__":
    main()
