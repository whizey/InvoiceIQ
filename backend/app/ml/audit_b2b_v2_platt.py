"""
RecoverIQ — Sequential Platt Calibration Audit

Fits calibrator only on PRIOR temporal folds.
Reserved test split is never read.
"""

from __future__ import annotations

import json

import numpy as np
import pandas as pd

from sklearn.linear_model import LogisticRegression

from app.ml.train_b2b_v2 import (
    ARTIFACT_ROOT,
    add_targets,
    build_temporal_folds,
    classifier_metrics,
    load_development_data,
)

from app.ml.calibrate_b2b_v2 import (
    classifier_oof,
)


OUTPUT = (
    ARTIFACT_ROOT
    / "calibration"
    / "platt_temporal_audit.json"
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


def fit_platt(probability, y):

    model = LogisticRegression(
        max_iter=2000,
        random_state=42,
    )

    model.fit(
        logit(
            probability
        ).reshape(-1, 1),
        np.asarray(y),
    )

    return model


def apply_platt(model, probability):

    return model.predict_proba(
        logit(
            probability
        ).reshape(-1, 1)
    )[:, 1]


def sequential_platt(oof):

    outputs = []

    folds = sorted(
        oof["fold"].unique()
    )

    for fold in folds[1:]:

        history = oof[
            oof["fold"] < fold
        ]

        current = oof[
            oof["fold"] == fold
        ].copy()

        positives = int(
            history["actual"].sum()
        )

        negatives = int(
            len(history) - positives
        )

        if (
            positives < 20
            or negatives < 20
        ):
            continue

        calibrator = fit_platt(
            history["raw_probability"],
            history["actual"],
        )

        current[
            "platt_probability"
        ] = apply_platt(
            calibrator,
            current["raw_probability"],
        )

        outputs.append(
            current
        )

    if not outputs:
        return pd.DataFrame()

    return pd.concat(
        outputs,
        ignore_index=True,
    )


def main():

    print("=" * 80)
    print("RECOVERIQ — TEMPORAL PLATT AUDIT")
    print("=" * 80)

    dev = add_targets(
        load_development_data()
    )

    folds = build_temporal_folds(
        dev
    )

    report = {}

    for head in [
        "late_payment",
        "delay_over_7d",
        "delay_over_15d",
    ]:

        print()
        print("=" * 80)
        print(head.upper())
        print("=" * 80)

        oof = classifier_oof(
            dev,
            folds,
            head,
        )

        calibrated = sequential_platt(
            oof
        )

        if calibrated.empty:
            print(
                "Insufficient support — EXPERIMENTAL"
            )

            report[head] = {
                "status":
                    "insufficient_support"
            }

            continue

        raw = classifier_metrics(
            calibrated["actual"],
            calibrated[
                "raw_probability"
            ],
        )

        platt = classifier_metrics(
            calibrated["actual"],
            calibrated[
                "platt_probability"
            ],
        )

        print(
            f"ROC-AUC  "
            f"{raw['roc_auc']:.4f}"
            f" → "
            f"{platt['roc_auc']:.4f}"
        )

        print(
            f"PR-AUC   "
            f"{raw['pr_auc']:.4f}"
            f" → "
            f"{platt['pr_auc']:.4f}"
        )

        print(
            f"Brier    "
            f"{raw['brier']:.4f}"
            f" → "
            f"{platt['brier']:.4f}"
        )

        print(
            f"ECE      "
            f"{raw['ece_10']:.4f}"
            f" → "
            f"{platt['ece_10']:.4f}"
        )

        auc_loss = (
            raw["roc_auc"]
            - platt["roc_auc"]
        )

        accepted = bool(
            platt["brier"]
            <= raw["brier"]
            and platt["ece_10"]
            < raw["ece_10"]
            and auc_loss <= 0.01
        )

        print()
        print(
            "VERDICT:",
            (
                "ACCEPT PLATT"
                if accepted
                else "KEEP RAW / REJECT CALIBRATION"
            )
        )

        report[head] = {
            "raw":
                raw,

            "platt":
                platt,

            "auc_loss":
                float(auc_loss),

            "accepted":
                accepted,

            "test_used":
                False,
        }

    OUTPUT.write_text(
        json.dumps(
            report,
            indent=2,
        )
    )

    print()
    print("=" * 80)
    print("PLATT AUDIT COMPLETE")
    print("=" * 80)
    print(OUTPUT)
    print()
    print("TEST SPLIT WAS NEVER READ.")


if __name__ == "__main__":
    main()
