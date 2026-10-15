"""
RecoverIQ — LightGBM Champion Calibration Audit

Tests the proposed LightGBM winners:
- late_payment / behaviour_heavy
- delay_over_7d / behaviour_heavy

Calibration is fitted ONLY on prior temporal folds.

TEST SPLIT IS NEVER READ.
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

from lightgbm import LGBMClassifier
from sklearn.linear_model import LogisticRegression

from app.ml.train_b2b_v2 import (
    ARTIFACT_ROOT,
    FEATURE_SETS,
    CLASSIFICATION_TARGETS,
    POSTING_DATE,
    add_targets,
    build_temporal_folds,
    classifier_metrics,
    load_development_data,
    make_pipeline,
)


OUTPUT = (
    ARTIFACT_ROOT
    / "lightgbm_challenge"
    / "calibration_audit.json"
)


def lgbm():
    return LGBMClassifier(
        objective="binary",
        n_estimators=700,
        learning_rate=0.025,
        num_leaves=31,
        max_depth=-1,
        min_child_samples=40,
        subsample=0.85,
        subsample_freq=1,
        colsample_bytree=0.80,
        reg_alpha=1.0,
        reg_lambda=10.0,
        random_state=42,
        n_jobs=-1,
        verbosity=-1,
    )


def utc(x):
    ts = pd.Timestamp(x)

    if ts.tzinfo is None:
        return ts.tz_localize("UTC")

    return ts.tz_convert("UTC")


def logit(p):
    p = np.clip(
        np.asarray(p, dtype=float),
        1e-6,
        1 - 1e-6,
    )

    return np.log(
        p / (1 - p)
    )


def build_oof(
    dev,
    folds,
    head_name,
):
    head = CLASSIFICATION_TARGETS[
        head_name
    ]

    features = FEATURE_SETS[
        "behaviour_heavy"
    ]

    outputs = []

    for fold in folds:

        eval_start = utc(
            fold["eval_start"]
        )

        eval_end = (
            utc(
                fold["eval_end"]
            )
            + pd.Timedelta(days=1)
        )

        train = dev[
            (
                dev[POSTING_DATE]
                < eval_start
            )
            &
            (
                dev[
                    head["availability"]
                ]
                < eval_start
            )
        ].copy()

        evaluation = dev[
            (
                dev[POSTING_DATE]
                >= eval_start
            )
            &
            (
                dev[POSTING_DATE]
                < eval_end
            )
        ].copy()

        y_train = train[
            head["target"]
        ].astype(int)

        y_eval = evaluation[
            head["target"]
        ].astype(int)

        if (
            len(train) < 1000
            or len(evaluation) < 100
            or y_train.nunique() < 2
            or y_eval.nunique() < 2
        ):
            continue

        model = make_pipeline(
            lgbm()
        )

        model.fit(
            train[features],
            y_train,
        )

        probability = (
            model.predict_proba(
                evaluation[
                    features
                ]
            )[:, 1]
        )

        outputs.append(
            pd.DataFrame(
                {
                    "fold":
                        int(
                            fold["fold"]
                        ),

                    "actual":
                        y_eval.values,

                    "raw_probability":
                        probability,
                }
            )
        )

    return pd.concat(
        outputs,
        ignore_index=True,
    )


def temporal_platt(oof):

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
            len(history)
            - positives
        )

        if (
            positives < 20
            or negatives < 20
        ):
            continue

        model = LogisticRegression(
            max_iter=2000,
            random_state=42,
        )

        model.fit(
            logit(
                history[
                    "raw_probability"
                ]
            ).reshape(-1, 1),

            history[
                "actual"
            ].astype(int),
        )

        current[
            "platt_probability"
        ] = model.predict_proba(
            logit(
                current[
                    "raw_probability"
                ]
            ).reshape(-1, 1)
        )[:, 1]

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

    print("=" * 84)
    print("RECOVERIQ — LIGHTGBM TEMPORAL CALIBRATION")
    print("=" * 84)

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
    ]:

        print()
        print("=" * 84)
        print(head.upper())
        print("=" * 84)

        oof = build_oof(
            dev,
            folds,
            head,
        )

        sequential = temporal_platt(
            oof
        )

        if sequential.empty:
            print(
                "Insufficient calibration support."
            )
            continue

        raw = classifier_metrics(
            sequential["actual"],
            sequential[
                "raw_probability"
            ],
        )

        platt = classifier_metrics(
            sequential["actual"],
            sequential[
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
            and
            platt["ece_10"]
            < raw["ece_10"]
            and
            auc_loss <= 0.01
        )

        print()
        print(
            "CALIBRATION:",
            (
                "ACCEPT PLATT"
                if accepted
                else "KEEP RAW"
            )
        )

        report[head] = {
            "raw":
                raw,

            "platt":
                platt,

            "accepted":
                accepted,

            "test_used":
                False,
        }

    OUTPUT.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    OUTPUT.write_text(
        json.dumps(
            report,
            indent=2,
        )
    )

    print()
    print("=" * 84)
    print("LIGHTGBM CALIBRATION AUDIT COMPLETE")
    print("=" * 84)

    print(OUTPUT)

    print()
    print(
        "TEST SPLIT WAS NEVER READ."
    )


if __name__ == "__main__":
    main()
