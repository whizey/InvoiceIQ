# RecoverIQ Late-Payment Model Evaluation Protocol

Frozen: 2026-10-03

## Scope

This protocol applies to the RecoverIQ posting-time B2B late-payment model.

Target:

`late_payment_weekend_adjusted`

Final feature contract:

- 18 numeric features
- 0 categorical features

The following are not model inputs:

- customer identifier
- document identifier
- region
- city
- zipcode
- payment_method_description
- clearing information
- post-outcome leakage fields

The late-payment model is separate from RecoverIQ's overdue recovery-risk model.

---

## Freeze point

This protocol is being committed after the final feature contract and data splits were frozen and after XGBoost and Random Forest were trained.

It is frozen before:

- Gradient Boosting final training
- final three-model comparison
- runtime packaging
- model activation
- first evaluation on the reserved test split

No hyperparameter tuning or feature changes will be performed after the final three-model comparison unless a correctness bug is discovered.

If a correctness bug changes features, labels, preprocessing, or inference semantics, affected models must be retrained and the reason documented.

---

## Data split policy

Splits are chronological.

Training:

- used to fit models
- used to fit preprocessing statistics

Validation:

- used for candidate selection
- used for comparison of the predefined model families

Test:

- reserved from model selection
- not used for hyperparameter tuning
- read once after the final model and runtime contract are frozen

The test result will be reported even if it is poor.

---

## Models

Only these three final model families are compared:

1. XGBoost
2. Random Forest
3. Gradient Boosting

No additional model families will be added before the final comparison.

No additional hyperparameter search will be performed after this protocol is frozen.

---

## Primary validation metric

The primary model-selection metric is:

`validation within-period ROC-AUC`

This is the same metric used by the existing candidate-selection code.

The selected model is the predefined model family with the highest validation within-period ROC-AUC.

---

## Secondary validation metrics

The following must also be reported:

- pooled ROC-AUC
- PR-AUC
- log loss
- Brier score
- validation base rate
- customer-history baseline
- constant/base-rate baseline

These metrics provide context but do not replace the predefined primary selection metric.

---

## Temporal stability

For every final model, report:

- every temporal-fold AUC
- minimum temporal-fold AUC
- mean temporal-fold AUC
- standard deviation across folds
- number of folds below 0.50

A fold below 0.50 is reported as an inverted ranking period.

Temporal instability must not be hidden behind a strong aggregate validation score.

A model may still be selected by the predefined validation metric, but if temporal stability is poor it must not be described as production-ready or as having reliable future performance.

---

## Cold-start limitation

Validation currently does not provide a clean unseen-customer evaluation.

Therefore the project must not claim that validation performance demonstrates generalization to completely new customers.

Cold-start behavior may be evaluated separately as a diagnostic, but it is not used for final model selection.

---

## Feature-semantics caveat

Historical behavior features use strict lateness:

`clearing_date > due_date`

The prediction target uses weekend-adjusted lateness.

This difference is intentional and consistent between offline and online feature generation.

It must be documented rather than presented as identical label semantics.

---

## Tenure caveat

`customer_tenure_days` is retained because ablation reduced model performance.

However, it is strongly correlated with calendar position.

Therefore model performance must not be described as pure customer-level credit-risk skill.

---

## Validation-selection caveat

Several candidate configurations were evaluated on the same validation split.

Therefore validation metrics are optimistic relative to a completely untouched evaluation.

The reserved test split is the only final untouched performance estimate.

---

## Baseline policy

The required baselines are:

- constant/base-rate prediction
- customer-history baseline

A logistic regression baseline is optional and will not be added if doing so changes the frozen comparison process.

---

## Final model-selection procedure

After Gradient Boosting training:

1. Run the existing three-model comparison.
2. Do not tune any model afterward.
3. Select the model with the highest validation within-period ROC-AUC.
4. Record all temporal-fold results beside the validation result.
5. Complete runtime integrity and inference tests.
6. Package the selected runtime artifacts.
7. Freeze model version and artifact hashes.
8. Evaluate the reserved test split once.

---

## Test-set evaluation

The test split will be read only after:

- model family is selected
- hyperparameters are frozen
- feature contract is frozen
- preprocessing is frozen
- runtime artifact contract is frozen

The following test metrics will be reported:

- within-period ROC-AUC
- pooled ROC-AUC
- PR-AUC
- log loss
- Brier score

No changes will be made to improve the reported test result.

If test performance is poor, that result will remain part of the project documentation.

---

## Production-readiness rule

Good validation or test performance alone does not make the model production-ready.

Activation also requires:

- offline/online feature parity
- randomized parity tests
- model loader tests
- SHAP compatibility tests
- artifact hash verification
- model-version persistence
- non-blocking scoring failure handling
- authenticated approval/rejection endpoints
- valid invoice-term/date consistency
- deterministic policy enforcement
- human review for protected actions

---

## Reporting rule

The project will distinguish between:

- validation performance
- temporal stability
- reserved test performance
- runtime correctness
- production readiness

These are not interchangeable.

A high AUC will not be used to hide instability, leakage risk, poor unseen-customer evidence, or runtime weaknesses.
