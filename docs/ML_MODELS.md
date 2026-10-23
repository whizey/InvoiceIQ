# Machine-learning models

Everything here is reported with its limitations. Reports live in `backend/app/ml/artifacts`, the protocol in `backend/EVALUATION_PROTOCOL.md`, and a summary in `docs/evaluations/model-risk-v1.json`.

## Models at a glance

| Model | Purpose | Family | Where it is used |
|---|---|---|---|
| **B2B late-payment champion** (`b2b_v2`) | Probability that an invoice misses its adjusted due date | LightGBM | Invoice intelligence, portfolio scoring, the assistant's facts |
| **BPI survival models** | Probability a purchase-to-pay item clears within horizons (a process target, not customer payment) | Cox-style survival, XGBoost AFT challenger | `/ml/process-survival/*` |
| **Recovery risk model** | Early scoring of recovery cases, trained on synthetic data | XGBoost | Legacy recovery workflow; labelled as not production evidence |

## The B2B late-payment champion

- **Question it answers:** "Will this invoice miss its adjusted due date?"
- **Data:** a public B2B invoice-payment dataset of 45,839 rows. Its SHA-256 checksum is verified in CI before the tests run.
- **Posting-time features only.** Only information available before an invoice's posting date can enter its features, which prevents future payments leaking into a score.
- **14 numeric features** in the served schema: `amount`, `payment_term`, `prior_open_invoice_count`, `has_resolved_history`, `historical_late_rate`, `historical_avg_days_late`, `amount_vs_historical_avg_ratio`, `days_since_previous_invoice`, `recent_5_late_rate`, `recent_5_avg_days_late`, `recent_late_streak`, `recent_on_time_streak`, `recent_5_late_rate_delta`, `recent_5_avg_days_late_delta`. Identifiers, region, city, zipcode and payment-method text are excluded on purpose.
- **Training set:** 34,437 rows (late-payment rate about 35%).
- **Model version:** `b2b_v2:5a8417263da99809`.

### Reported performance (development validation)

| Measure | Validation (13,355 rows, ~40% late) | Chronological test (6,181 rows, ~46% late) |
|---|---|---|
| ROC-AUC | 0.714 | 0.706 |
| PR-AUC | 0.610 | 0.636 |
| Brier score (lower is better) | 0.241 | 0.281 |

Temporal backtest: 4 folds, none below random, median ROC-AUC 0.666.

**Calibration:** a Platt calibration was evaluated and **not accepted**, so the model serves its raw probabilities. Treat them as ranking scores, not exact probabilities.

**Evidence status:** the model was selected on the development split and a temporal backtest. The frozen model was then scored once on the chronological test split, with no refit or threshold tuning. That split had been examined during earlier development, so the test figures are retrospective held-out evidence rather than a blind test (see `docs/evaluations/model-risk-v1.json`). Explainability cohort coverage and prospective production performance are the next validation steps, tracked in the evaluation JSON.

### Explanations
Each score can include native **TreeSHAP** contributions, so the product can show which features pushed a particular invoice up or down.

### Safety and integrity
- Artifacts (model, calibrator, manifest, contracts) are versioned and fingerprinted. A runtime-integrity check verifies hashes before serving; `/health/ready` fails if the primary model is unavailable.
- Stored scores carry the model version and an artifact fingerprint, and a score whose provenance no longer matches the current model is not treated as valid.
- Model files are stored in Git LFS (21 files).

## Survival models (purchase-to-pay)

These estimate a **process-clearance** target from the public BPI Challenge 2019 purchase-to-pay event log (about 730 MB; used offline, not downloaded or checksummed in CI). They are benchmarked (Coxnet, XGBoost AFT and others), backtested over time, and compared in `bpi_survival/MODEL_COMPARISON.md`. A recovery-policy backtest evaluates how a policy would have performed. **Their probabilities are not customer payment or collection estimates** and must not be presented as such.

## Offline tooling

| Area | Examples (all in `backend/app/ml/`) |
|---|---|
| Training | `train_b2b_v2.py`, `train_bpi_survival.py`, plus gradient boosting, random forest and XGBoost baselines |
| Evaluation | `evaluate_collections.py`, `rolling_backtest.py`, `stage3_validation.py`, `benchmark_*` |
| Calibration | `calibrate_b2b_v2.py`, `audit_b2b_v2_platt.py`, `audit_lightgbm_calibration.py` |
| Selection and promotion | `selection_policy.py`, `promote_b2b_v2_champions.py`, `model_risk.py` |
| Serving contracts | `champion_serving.py`, `b2b_v2_serving.py`, `artifact_contract.py` |
| Monitoring | `drift_monitor.py` computes population stability (PSI) for numeric features; it is an alerting diagnostic, not proof of degradation |
| Verification | `verify_pretraining.py`, customer-holdout manifests |

Documentation for the data and features: `backend/docs/DATASET_CARD.md`, `FEATURE_PROVENANCE.md` and `CUSTOMER_HOLDOUT_PROTOCOL.md`.

## One-line summary

"A LightGBM model scores each invoice's risk of paying late, using only information available when the invoice was posted, with per-invoice TreeSHAP explanations. Development-validation ROC-AUC is about 0.71, with a stable four-fold temporal backtest."
