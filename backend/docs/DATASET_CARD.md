# RecoverIQ B2B Invoice Dataset Card

Snapshot documented: 2026-10-03

## Intended use

This dataset is used for:

**posting-time prediction of whether a B2B invoice will be paid late**

Canonical target:

`late_payment_weekend_adjusted`

It is not used as evidence of general creditworthiness or default risk.

## Source

The project uses a public B2B invoice-payment dataset commonly found in
HighRadius-style internship/project repositories.

It is not represented as:

- official HighRadius production data
- confidential enterprise data
- a proprietary RecoverIQ dataset

## Raw dataset

- Raw rows: 45,839
- Raw columns: 29
- Customers: 69
- All rows contain eventual clearing information

Because every invoice eventually clears, this dataset is suitable for
payment-timing / late-payment modelling.

It is not a valid dataset for true non-payment/default prediction.

## Observation horizon

A 45-day observation horizon is applied so that invoices near the end of the
available timeline are not labelled using outcomes for which sufficient
observation time was unavailable.

Final data cutoff:

`2016-03-17`

Rows:

`45,839 -> 41,804`

Retention:

approximately `91.2%`

## Target construction

Two concepts are kept separate.

### Strict lateness

An invoice is strictly late when:

`clearing_date > due_date`

### Weekend-adjusted target

The canonical prediction target is:

`late_payment_weekend_adjusted`

After the final observation-horizon build:

- strict late rate: approximately 54.32%
- weekend-adjusted late rate: approximately 36.18%
- 7,581 labels differ because of the weekend adjustment

## Final feature contract

- 18 numeric model features
- 0 categorical model features

See:

`docs/FEATURE_PROVENANCE.md`

## Chronological split

### Train

- Rows: 29,219
- Dates: 2011-11-23 through 2015-10-22

### Validation

- Rows: 6,404
- Dates: 2015-10-23 through 2015-12-07

### Test

- Rows: 6,181
- Dates: 2015-12-08 through 2016-01-27
- Reserved from tuning/model selection

The test target is deliberately not summarized here.

## Important limitations

### Small customer population

The raw data contains only 69 customers and is strongly concentrated.

Results should therefore not be presented as evidence of broad B2B-population
generalization.

### Validation is mostly known-customer behavior

The chronological validation split does not provide a clean unseen-customer
benchmark.

A separate frozen customer-holdout diagnostic is maintained for that question.

### Temporal regime instability

Late-payment prevalence changes materially across time.

Rolling temporal folds must therefore be reported alongside aggregate
validation metrics.

A good validation AUC must not be described as expected future performance
when temporal folds are unstable.

### Geography acts as a customer proxy

Region, city and zipcode were investigated and removed from the final feature
set because they were strongly tied to individual customers.

### Payment-method provenance is uncertain

`payment_method_description` was predictive, but the public dataset did not
establish clearly enough that the field was known at posting time.

It was removed from the model.

### Tenure has a temporal-proxy caveat

`customer_tenure_days` improved validation and temporal diagnostics in
ablation, so it remains in the final contract.

However, it is correlated with calendar progression and must not be presented
as pure credit-risk signal.

## Appropriate claims

Supported:

- late-payment ranking within this dataset
- leakage-aware posting-time feature engineering
- temporal evaluation
- production/offline feature parity
- model artifact/runtime engineering

Not supported:

- probability of permanent default
- universal company creditworthiness
- performance on arbitrary unseen companies
- expected future production performance without further external validation
