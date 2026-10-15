# BPI 2019 Survival Model Comparison

## Dataset

- Raw events: **1,595,923**
- Raw cases: **251,734**
- Eligible invoice cases: **199,985**
- Observed clears: **172,385**
- Right-censored invoices: **27,600**
- Prediction point: **Record Invoice Receipt**

## Final comparison

| Model | Training | AUC | Worst AUC | Brier | Status |
|---|---|---:|---:|---:|---|
| LightGBM Discrete-Time Hazard | Full temporal folds | 0.9157 | 0.8799 | 0.0748 | PRODUCTION |
| XGBoost AFT | Full temporal folds | 0.9006 | 0.8504 | 0.1151 | STRONG CHALLENGER |
| CoxNet Elastic-Net | Full temporal folds | 0.8571 | 0.7401 | 0.0977 | CHALLENGER |
| Cox Proportional Hazards | Full temporal folds | — | — | — | CLASSICAL BASELINE |
| Gradient Boosting Survival | Resource-controlled temporal folds | 0.7989 | 0.6633 | 0.1096 | RESOURCE-CONTROLLED CHALLENGER |
| Logistic Discrete-Time Hazard | Full temporal folds | 0.7348 | 0.6129 | 0.2091 | SIMPLE BASELINE |
| Random Survival Forest | Full fold attempted | — | — | — | RESOURCE CONSTRAINED |

## Production selection

**Winner: LightGBM Discrete-Time Hazard**

The LightGBM hazard model remains the production champion because it achieved the best combination of discrimination, worst-case temporal stability and probability calibration.

XGBoost AFT was the strongest challenger and approached the production model in ranking performance, but its Brier score and several horizon-level calibration errors were worse.

## Temporal validation

All full-data models use expanding temporal backtests rather than random train/test splits.

| Fold | Train cutoff | Evaluation window | Train invoices | Evaluation invoices |
|---|---|---|---:|---:|
| fold_0 | 2018-07-01 | 2018-07-01 → 2018-09-01 | 87,253 | 36,905 |
| fold_1 | 2018-09-01 | 2018-09-01 → 2018-11-01 | 124,158 | 34,985 |
| fold_2 | 2018-11-01 | 2018-11-01 → 2018-12-01 | 159,143 | 15,588 |
| fold_3 | 2018-12-01 | 2018-12-01 → 2019-01-18 | 174,731 | 25,253 |

## Experiment history

| Model | Experiment | AUC | Worst AUC | Brier | Purpose |
|---|---|---:|---:|---:|---|
| CoxNet Elastic-Net | 50k-per-fold | 0.8585 | 0.7430 | 0.0973 | Laptop-friendly benchmark before the full-data run. |
| CoxNet Elastic-Net | Full temporal data | 0.8571 | 0.7401 | 0.0977 | Final CoxNet comparison. |
| XGBoost AFT | 10k / 20 rounds | 0.5580 | 0.2664 | 0.3862 | Engineering smoke test only; deliberately undertrained. |
| XGBoost AFT | 10k / 200 rounds | 0.7696 | 0.5100 | 0.2437 | Development run confirming that the AFT model needed substantially more boosting rounds. |
| XGBoost AFT | Full temporal data / 200 rounds | 0.9006 | 0.8504 | 0.1151 | Final AFT challenger benchmark. |

## Methodology

- Historical training invoices are re-censored at each temporal cutoff so future clearances are not revealed to the model.
- Right-censored unresolved invoices are retained rather than discarded.
- AUC measures discrimination/ranking at each clearance horizon.
- Brier score measures probability error at the fixed evaluation horizons.
- Current Brier metrics are horizon-specific known-outcome metrics; they are not claimed as a formal IPCW integrated Brier score.
- Gradient Boosting Survival used a 25k-per-fold resource cap and therefore is not presented as a strict full-data algorithmic comparison.
- Random Survival Forest is listed without a score because the full benchmark did not complete under the available laptop memory.

## Current production outputs

- `clearance_probability_7d`
- `clearance_probability_30d`
- `clearance_probability_60d`
- `clearance_probability_90d`
- `likely_clearance_window_days`
- advanced `recovery_curve`
