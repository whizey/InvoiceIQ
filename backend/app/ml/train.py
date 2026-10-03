"""Trains the recovery-risk XGBoost model on synthetic data and saves artifacts.

Run with:  python -m app.ml.train

Outputs (both committed to the repo so it runs out of the box):
  app/ml/artifacts/recovery_risk_model.json  — the trained booster
  app/ml/artifacts/metrics.json              — evaluation metrics + metadata

Reminder: the training data is synthetic (see app/ml/synthetic_data.py).
This model demonstrates the ML pipeline; it is not a production financial
risk model.
"""

import json
from pathlib import Path

import xgboost as xgb
from sklearn.metrics import accuracy_score, log_loss, roc_auc_score
from sklearn.model_selection import train_test_split

from app.ml.features import FEATURE_NAMES
from app.ml.synthetic_data import RANDOM_SEED, generate_synthetic_dataset

ARTIFACT_DIR = Path(__file__).parent / "artifacts"
MODEL_PATH = ARTIFACT_DIR / "recovery_risk_model.json"
METRICS_PATH = ARTIFACT_DIR / "metrics.json"

# Single source of truth for the booster config -- app/ml/evaluate.py imports
# this rather than keeping its own copy, so the cross-validated report can
# never silently describe a different model than the one actually shipped.
#
# Tuned by sweep, not guessed: 48 configs, selected on repeated CV over a
# selection pool, then confirmed on a held-out split selection never touched.
# Depth 4 wins because synthetic_data.py's surface carries conjunctive
# regimes and a sign flip needing interaction depth -- on the earlier
# purely-linear generator the same sweep picked depth 2.
#
# Headroom: the generative process is known, so the Bayes-optimal AUC is
# computable (~0.834). This reaches ~0.830 held-out, within ~0.005 of the
# hard ceiling. A plain logistic regression reaches only ~0.806 and does not
# improve with more data, because the sign flip is unrepresentable in a
# linear model at any coefficient setting. That gap is why tree boosting is
# defensible here rather than only convenient for SHAP.
MODEL_PARAMS = dict(
    n_estimators=500,
    max_depth=4,
    learning_rate=0.03,
    min_child_weight=20,
    subsample=0.85,
    colsample_bytree=0.85,
    reg_lambda=5.0,
    eval_metric="logloss",
    random_state=RANDOM_SEED,
)


def train() -> dict:
    # 25,000 rows, not 4,000: below ~10k there aren't enough rows inside each
    # of the DGP's conjunctive regimes for the trees to recover them, and the
    # model loses to logistic regression. See synthetic_data.py.
    df = generate_synthetic_dataset(seed=RANDOM_SEED)
    X = df[FEATURE_NAMES]
    y = df["recovered"]

    X_train, X_test, y_train, y_test = train_test_split(
        X, y, test_size=0.2, random_state=RANDOM_SEED, stratify=y
    )

    model = xgb.XGBClassifier(**MODEL_PARAMS)
    model.fit(X_train, y_train)

    y_pred_proba = model.predict_proba(X_test)[:, 1]
    y_pred = model.predict(X_test)

    metrics = {
        "auc": round(float(roc_auc_score(y_test, y_pred_proba)), 4),
        "accuracy": round(float(accuracy_score(y_test, y_pred)), 4),
        "log_loss": round(float(log_loss(y_test, y_pred_proba)), 4),
        "n_train": int(len(X_train)),
        "n_test": int(len(X_test)),
        "positive_rate": round(float(y.mean()), 4),
        "feature_names": FEATURE_NAMES,
        "random_seed": RANDOM_SEED,
        "note": "Trained on synthetic data (app/ml/synthetic_data.py) — not a production financial model.",
    }

    ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
    model.save_model(str(MODEL_PATH))
    METRICS_PATH.write_text(json.dumps(metrics, indent=2) + "\n")

    return metrics


if __name__ == "__main__":
    result = train()
    print(json.dumps(result, indent=2))
