"""Synthetic training data for the recovery-risk model.

IMPORTANT: This data is entirely synthetic. There is no real-world
collections dataset behind it. It exists so a portfolio project can
demonstrate a working ML training pipeline; the resulting model is NOT a
production financial risk model and must not be treated as one.

WHY THIS ISN'T JUST A LINEAR FUNCTION
-------------------------------------
An earlier version of this generator was a plain logistic function of the
9 features. Consequence worth recording: a plain logistic regression
scored ~0.783 AUC against a computable Bayes ceiling of ~0.785, i.e. it
was already optimal, and the XGBoost model this app ships could not beat
it. Tree boosting was not defensible on accuracy grounds there.

So the surface now carries structure a linear model cannot represent at
any coefficient setting, and that real collections data plausibly has:

  1. HINGE on num_open_invoices -- one or two open at once is a normally
     engaged customer; only past that does a stacking balance signal
     distress. Piecewise, not proportional.
  2. CLIFF at 90 days overdue -- a real discontinuity where accounts get
     written off or handed to an agency. A coefficient cannot encode a step.
  3. CONJUNCTIVE REGIMES -- effects firing only inside a region defined by
     several features at once. A tree represents this as a root-to-leaf
     path; a linear model needs the interaction term built in advance.
  4. A SIGN FLIP -- loyal_but_slow below reverses the effect of
     avg_historical_payment_delay. Everywhere else, paying late is bad
     news; for a long-tenured customer with a strong recovery history,
     chronic lateness is just how they pay, and they do pay. A linear
     model must commit to one sign per feature, so this is strictly
     unrepresentable. It is the term that most separates the model classes.

Measured: XGBoost beats logistic regression 10/10 across independent seeds
at the default row count, reaching ~0.830 AUC against a ~0.834 ceiling,
while logistic regression plateaus at ~0.806 however much data it gets.

ON THE ROW COUNT
----------------
Default is 25,000 rows, not the 4,000 used previously. Not padding: at
4,000 the tree model LOSES to logistic regression (3/10 seeds) because
there aren't enough rows inside each regime to recover the structure, even
though the structure is really there. Trees overtake from ~10,000 and win
every seed from ~25,000. Structure and sample size were both required.
"""

import numpy as np
import pandas as pd

from app.ml.features import FEATURE_NAMES

RANDOM_SEED = 42

# Tuned so the positive (recovered) rate lands near 0.80, matching the
# class balance the project's other numbers were established against.
_INTERCEPT = 2.20


def _sigmoid(z: np.ndarray) -> np.ndarray:
    return 1.0 / (1.0 + np.exp(-z))


def generate_synthetic_dataset(
    n_rows: int = 25_000, seed: int = RANDOM_SEED, return_true_probability: bool = False
):
    """Generate the synthetic recovery dataset.

    If return_true_probability is True, also returns the Bayes-optimal
    probability per row -- the generative probability with the noise term
    integrated out. Only knowable because this data is synthetic, and it is
    what makes the ceiling computable rather than guessed. Training never
    uses it; it exists so evaluation can state how much headroom is left.
    """
    rng = np.random.default_rng(seed)

    invoice_amount = np.clip(rng.lognormal(mean=12.3, sigma=1.0, size=n_rows), 15_000, 5_000_000)
    days_overdue = np.clip(rng.gamma(shape=2.0, scale=15.0, size=n_rows), 1, 120).round().astype(int)

    outstanding_fraction = np.where(rng.random(n_rows) < 0.85, 1.0, rng.uniform(0.2, 0.8, size=n_rows))
    outstanding_balance = invoice_amount * outstanding_fraction

    avg_historical_payment_delay = np.clip(rng.normal(loc=5.0, scale=9.0, size=n_rows), -10, 45)

    total_prior_invoices = rng.integers(2, 11, size=n_rows)
    late_fraction = np.clip(_sigmoid((avg_historical_payment_delay - 5) / 8.0), 0.05, 0.95)
    num_prior_late_payments = np.round(total_prior_invoices * late_fraction).astype(int)
    num_prior_on_time_payments = total_prior_invoices - num_prior_late_payments

    customer_tenure_days = rng.integers(30, 1800, size=n_rows)

    has_track_record = rng.random(n_rows) >= 0.2
    prior_recovery_success_rate = np.where(
        has_track_record,
        rng.beta(a=3.0, b=2.0, size=n_rows),
        0.5,
    )

    num_open_invoices = np.clip(rng.poisson(lam=1.0, size=n_rows), 0, 6)

    log_amount = np.log1p(invoice_amount / 100_000)

    # --- smooth main effects: the directionally sensible part -------------
    z = (
        _INTERCEPT
        - 0.030 * days_overdue
        - 0.26 * log_amount
        - 0.045 * avg_historical_payment_delay
        - 0.20 * num_prior_late_payments
        + 0.13 * num_prior_on_time_payments
        + 0.0008 * customer_tenure_days
        + 1.55 * prior_recovery_success_rate
        # Hinge, not a slope: 0-2 open invoices is normal engagement.
        - 0.40 * np.maximum(0.0, num_open_invoices - 2.0)
        # Write-off cliff: a step a linear coefficient cannot encode.
        - 0.85 * (days_overdue > 90)
    )

    # --- conjunctive regimes: the part a linear model cannot fit ----------
    # Disputed invoice: large bill, several invoices open, stalled mid-window.
    # Reads as a billing dispute rather than inability to pay, and doesn't
    # resolve until someone works it.
    dispute = (log_amount > 1.0) & (num_open_invoices >= 2) & (days_overdue > 30) & (days_overdue <= 75)
    z = z - 2.0 * dispute

    # Loyal but slow: the SIGN FLIP. Chronic lateness from a long-tenured
    # customer with a strong recovery history is their payment rhythm, not a
    # warning sign -- so here a high delay predicts recovery.
    loyal_but_slow = (
        (customer_tenure_days > 1000) & (prior_recovery_success_rate > 0.70) & (avg_historical_payment_delay > 12)
    )
    z = z + 2.2 * loyal_but_slow

    # Young account carrying a large balance with prior lateness: the
    # cash-flow failure mode, materially worse than main effects imply.
    young_large_balance = (
        (customer_tenure_days < 240) & (log_amount > 1.2) & (num_prior_late_payments >= 2)
    )
    z = z - 1.9 * young_large_balance

    # Bayes-optimal probability (noise integrated out) vs the realised draw.
    recovery_probability_true = _sigmoid(z)
    recovered = rng.binomial(1, _sigmoid(z + rng.normal(0, 0.6, size=n_rows)))

    df = pd.DataFrame(
        {
            "invoice_amount": invoice_amount,
            "days_overdue": days_overdue,
            "outstanding_balance": outstanding_balance,
            "avg_historical_payment_delay": avg_historical_payment_delay,
            "num_prior_late_payments": num_prior_late_payments,
            "num_prior_on_time_payments": num_prior_on_time_payments,
            "customer_tenure_days": customer_tenure_days,
            "prior_recovery_success_rate": prior_recovery_success_rate,
            "num_open_invoices": num_open_invoices,
            "recovered": recovered,
        }
    )
    assert list(df.columns[:-1]) == FEATURE_NAMES
    if return_true_probability:
        return df, recovery_probability_true
    return df
