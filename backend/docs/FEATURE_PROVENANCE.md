# RecoverIQ Late-Payment Feature Provenance

Frozen feature contract: 2026-10-03

## Prediction point

Every feature represents information that is available when an invoice is posted.

Historical payment outcomes may enter the feature state only when:

`historical clearing_date < current posting_date`

An invoice clearing on the same date as the invoice being scored is therefore
not treated as known history.

Invoices posted on the same date share the same pre-day historical snapshot.

## Final model contract

- Target: `late_payment_weekend_adjusted`
- Numeric features: 18
- Categorical features: 0
- Preprocessing contract: `numeric_only_v1`

| # | Feature | Source at scoring time | Definition | Outcome-dependent? | Main risk / safeguard |
|---|---|---|---|---|---|
| 1 | `amount` | Current invoice | Current invoice total amount | No | Known at posting |
| 2 | `payment_term` | Current invoice | Contractual payment term in days | No | Must remain consistent with posting/due dates |
| 3 | `prior_resolved_invoice_count` | Prior invoices + payments | Prior posted invoices whose clearing outcome became known before current posting | Yes | Uses strict `< posting_date` cutoff |
| 4 | `prior_open_invoice_count` | Prior invoices + payments | Prior posted invoices without a known full resolution before current posting | No future outcome used | Same causal cutoff |
| 5 | `open_invoice_ratio` | Historical state | Prior open invoices / prior posted invoices | No future outcome used | Zero when no prior invoices |
| 6 | `has_resolved_history` | Historical state | 1 when at least one resolved prior outcome exists | Yes | Explicit cold-start flag |
| 7 | `historical_late_rate` | Resolved history | Fraction of resolved prior invoices with strict days-late > 0 | Yes | NaN at cold start rather than fake behavioral evidence |
| 8 | `historical_avg_days_late` | Resolved history | Mean `(clearing_date - due_date).days` across known outcomes | Yes | Only outcomes known before posting |
| 9 | `historical_avg_invoice_amount` | Prior invoices | Mean amount across prior posted invoices | No | Uses prior invoices, not current/future invoices |
| 10 | `amount_vs_historical_avg_ratio` | Current + prior invoices | Current amount / historical average amount | No | Neutral 1.0 when no historical average; clipped to 0–20 |
| 11 | `customer_tenure_days` | Invoice history | Days from first observed posting to current posting | No | Capped at 500; documented temporal-proxy caveat |
| 12 | `days_since_previous_invoice` | Invoice history | Days since most recent prior posting | No | Capped at 500 |
| 13 | `recent_5_late_rate` | Resolved history | Strict late rate over the five most recently known resolved outcomes | Yes | Canonical resolution ordering; parity-tested |
| 14 | `recent_5_avg_days_late` | Resolved history | Mean strict days-late over five most recently known outcomes | Yes | Same causal ordering |
| 15 | `recent_late_streak` | Last 10 resolved outcomes | Consecutive late outcomes at end of recent history | Yes | Maximum recent history window = 10 |
| 16 | `recent_on_time_streak` | Last 10 resolved outcomes | Consecutive on-time/early outcomes at end of recent history | Yes | Mutually exclusive with late streak |
| 17 | `recent_5_late_rate_delta` | Resolved history | Recent-5 late rate minus historical late rate | Yes | Measures behavioral change rather than absolute level only |
| 18 | `recent_5_avg_days_late_delta` | Resolved history | Recent-5 average delay minus historical average delay | Yes | Same causal history restriction |

## Historical-label semantics

Historical behavior uses strict lateness:

`clearing_date > due_date`

The model target is weekend-adjusted lateness.

This is deliberate. The historical features describe observed payment behavior;
the prediction label applies the project's business-calendar adjustment.

They must not be described as identical semantics.

## Cold-start handling

When there is no resolved customer history:

- `has_resolved_history = 0`
- rate features that require resolved behavior are represented as missing
- non-rate historical aggregates use explicitly defined neutral/zero semantics
- the model therefore receives an explicit distinction between "no evidence"
  and "observed 0% / 50% behavior"

## Scale controls

- cumulative/time-like counts are capped at `COUNT_CAP = 500`
- `amount_vs_historical_avg_ratio` is clipped to `RATIO_CLIP = 20`

## Features deliberately excluded from the model

### `cust_num`

Retained only for grouping, splitting and diagnostics. It is not a predictor.

### Geography: `region`, `city`, `zipcode`

Removed because diagnostics showed that these values were strongly associated
with customer identity and therefore behaved mainly as customer proxies.

They remain ordinary application metadata.

### `payment_method_description`

Removed because its exact availability at invoice-posting time could not be
established confidently from the public dataset.

### Clearing / post-outcome fields

Clearing dates, delay labels and other outcome-derived columns are forbidden
because they would reveal information unavailable when an invoice is posted.

## Offline / online invariant

The offline training builder and the production/Postgres feature path must
produce the same 18 values for the same posting-time state.

This invariant is protected by:

- deterministic parity tests
- randomized parity over 150 histories
- cold-start cases
- open invoices
- histories longer than 10 outcomes
- same-day posting batches
- same-day clearing ties
- randomized incoming row order

Feature changes are not considered complete unless this parity remains green.
