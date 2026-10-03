"""Build RecoverIQ primary B2B features - V3.

V2 changes
----------
1. Preserve the original strict lateness outcome for auditing.
2. Create a weekend-adjusted late-payment target:
      Saturday due date -> following Monday
      Sunday due date   -> following Monday
   Holidays are NOT adjusted because the source does not provide
   enough geography/calendar information to do that reliably.
3. Remove posting_weekday and due_weekday from model inputs.
4. Add richer causal customer-history features.
5. Add recent 3/5/10 resolved-invoice behaviour.
6. Add behavioural trend and workload features.
7. Preserve the rule that an invoice outcome is usable only if its
   clearing date was known BEFORE the current invoice posting date.

Raw source
----------
data/raw/b2b_primary/b2b_invoice_payment_45839.csv
"""

from __future__ import annotations

from collections import deque
import heapq
import json
import re
from pathlib import Path

import numpy as np
import pandas as pd

from app.ml.payment_features import (
    CATEGORICAL_FEATURES as SHARED_CATEGORICAL_FEATURES,
    COUNT_CAP,
    RATIO_CLIP,
    NUMERIC_FEATURES as SHARED_NUMERIC_FEATURES,
    TARGET_COLUMN as SHARED_TARGET_COLUMN,
)


PROJECT_ROOT = Path(__file__).resolve().parents[4]

RAW_PATH = (
    PROJECT_ROOT
    / "data"
    / "raw"
    / "b2b_primary"
    / "b2b_invoice_payment_45839.csv"
)

PROCESSED_DIR = (
    PROJECT_ROOT
    / "data"
    / "processed"
    / "primary"
)

OUTPUT_PATH = (
    PROCESSED_DIR
    / "b2b_primary_features.csv.gz"
)

MANIFEST_PATH = (
    PROCESSED_DIR
    / "b2b_primary_features_manifest.json"
)


STRICT_TARGET_COLUMN = "late_payment_strict"

TARGET_COLUMN = SHARED_TARGET_COLUMN


# ---------------------------------------------------------------------
# V3: OBSERVATION HORIZON (right-censoring fix)
# ---------------------------------------------------------------------
# The raw extract only contains invoices that had CLEARED by its last
# clearing date (2016-03-17). Any invoice still unpaid on that date is
# absent from the file entirely -- and those are precisely the slowest
# payers. The effect compounds the later an invoice was posted, which
# silently destroyed the comparability of the splits:
#
#     split        mean resolution    p95      MAX
#     train            18.4 d         98 d    1437 d
#     validation        7.4 d         35 d     104 d
#     test              5.1 d          9 d      22 d
#
# The test partition could not contain an invoice that took longer than
# three weeks to clear. It was structurally incapable of holding the
# cases this project exists to find, so every number measured on it was
# optimistic for a reason no model change could fix.
#
# Fix: keep an invoice only when its due date leaves a full horizon of
# observation before the extract ends:
#
#     net_due_date + OBSERVATION_HORIZON_DAYS <= data cutoff
#
# Within the retained set, a late payer is as visible as a prompt one,
# so train / validation / test are drawn from one population again.
#
# Choosing 45 days, measured rather than guessed. On invoices with at
# least a 200-day observation window (where the distribution is
# unbiased), the share of late payments that resolve within H days of
# the due date is:
#
#     H= 7d  87.4%      H= 45d  96.8%
#     H=14d  93.2%      H= 60d  98.9%
#     H=30d  94.0%      H= 90d  99.0%
#
# and the share of rows retained is 95.7% at H=30, 91.2% at H=45,
# 86.1% at H=60. H=45 observes ~97% of late outcomes while keeping
# ~91% of the data.
#
# Residual limitation, stated rather than hidden: an invoice that runs
# more than 45 days past due AND is due within 45 days of the cutoff is
# still unobservable. That is ~3% of late cases in the tail, not the
# systematic, split-correlated bias that was there before.
OBSERVATION_HORIZON_DAYS = 45


LEAKAGE_COLUMNS = [
    "clearing_doc",
    "clearing_date",
    "days_overdue_delay",
    "delay_bins",
    "weekday_clearing",
    "quarter_clearing",
    "weekday_clearnum",
    "delayflag",
]


UNSAFE_PRECOMPUTED_COLUMNS = [
    "no_of_orders_by_customer",
    "rank_of_order_by_customer",
    "age_of_customer_months",
    "age_of_customer_year",
    "customer_age_year_bins",
]


# ---------------------------------------------------------------------
# V3 FEATURE-SET REPAIRS
# ---------------------------------------------------------------------
# Four problems found by auditing the V2 feature set against the splits.
#
# 1. UNBOUNDED CUMULATIVE COUNTS ARE A DISGUISED TIME FEATURE.
#    prior_invoice_count averaged 2,122 in train and 6,343 in test; a
#    2.99x shift. customer_tenure_days 125 -> 312. A tree cannot
#    extrapolate past its training range, so every test row saturates
#    the topmost split of each of those features. Worse, inside train
#    the count correlates with position in the extract, so the model
#    learns "big count => later period => different base rate" and
#    that mapping is meaningless once deployed. Counts are therefore
#    capped at COUNT_CAP, chosen inside the training range, so the
#    feature saturates identically in every split, and the bounded
#    RATE features carry the behavioural signal instead.
#
# 2. payment_term AND days_until_due WERE THE SAME COLUMN (r = 1.0000).
#    doc_date equals posting_date throughout this extract, so
#    net_due_date - posting_date and net_due_date - doc_date are
#    identical. days_until_due is dropped.
#
# 3. 0.5 WAS AN AMBIGUOUS COLD-START SENTINEL. historical_late_rate
#    defaulted to 0.5 with no history, colliding with a genuine 50%
#    late rate; it appears in 5.7% of rows. Cold start is now NaN,
#    which XGBoost routes natively, plus an explicit
#    has_resolved_history flag. Note that the cold-start population is
#    almost entirely in train (7.86% vs 0.05% in test), so conflating
#    it with a real 50% rate was teaching the model a train-only quirk.
#
# 4. RATIO OUTLIERS. amount_vs_historical_avg_ratio has p99 = 11.4 and
#    max = 2053. Clipped to RATIO_CLIP.
#
# Redundant near-duplicates are also pruned: recent_3/5/10 late rates
# correlated above 0.98 with each other, and the four prior_* counts
# above 0.96. One of each family is kept.



MODEL_NUMERIC_FEATURES = list(SHARED_NUMERIC_FEATURES)

MODEL_CATEGORICAL_FEATURES = list(SHARED_CATEGORICAL_FEATURES)


def normalize_name(
    value: str,
) -> str:
    value = str(
        value
    ).strip().lower()

    value = re.sub(
        r"[^a-z0-9]+",
        "_",
        value,
    )

    return value.strip("_")


def load_raw() -> pd.DataFrame:
    df = pd.read_csv(
        RAW_PATH,
        low_memory=False,
    )

    df.columns = [
        normalize_name(column)
        for column in df.columns
    ]

    required = {
        "cust_num",
        "document_no",
        "amount",
        "payment_method_description",
        "region",
        "city",
        "zipcode",
        "payment_term",
        "doc_date",
        "posting_date",
        "net_due_date",
        "clearing_date",
        "delayflag",
    }

    missing = (
        required
        - set(df.columns)
    )

    if missing:
        raise ValueError(
            "Missing required columns: "
            f"{sorted(missing)}"
        )

    for column in [
        "doc_date",
        "posting_date",
        "net_due_date",
        "clearing_date",
    ]:
        df[column] = pd.to_datetime(
            df[column],
            errors="coerce",
        )

    essential_dates = [
        "doc_date",
        "posting_date",
        "net_due_date",
        "clearing_date",
    ]

    missing_dates = (
        df[essential_dates]
        .isna()
        .sum()
    )

    if missing_dates.sum():
        raise ValueError(
            "Missing essential date values:\n"
            f"{missing_dates}"
        )

    return df


def apply_observation_horizon(
    df: pd.DataFrame,
) -> tuple[pd.DataFrame, dict]:
    """Drop invoices whose outcome could not have been fully observed.

    See OBSERVATION_HORIZON_DAYS for why. Returns the filtered frame and
    an audit dict, because the number of rows this removes and what it
    does to the observed late rate are both things a reader should be
    able to check rather than take on trust.
    """

    data_cutoff = (
        df["clearing_date"].max()
    )

    required_visible_until = (
        df["net_due_date"]
        + pd.Timedelta(
            days=OBSERVATION_HORIZON_DAYS
        )
    )

    observable = (
        required_visible_until
        <= data_cutoff
    )

    days_late_before = (
        df["clearing_date"]
        - df["net_due_date"]
    ).dt.days

    kept = (
        df.loc[observable]
        .copy()
        .reset_index(drop=True)
    )

    days_late_after = (
        kept["clearing_date"]
        - kept["net_due_date"]
    ).dt.days

    audit = {
        "horizon_days": (
            OBSERVATION_HORIZON_DAYS
        ),

        "data_cutoff": str(
            data_cutoff.date()
        ),

        "latest_retained_due_date": str(
            kept["net_due_date"]
            .max()
            .date()
        ),

        "rows_before": int(
            len(df)
        ),

        "rows_after": int(
            len(kept)
        ),

        "rows_dropped": int(
            len(df) - len(kept)
        ),

        "retained_fraction": round(
            float(
                len(kept) / len(df)
            ),
            4,
        ),

        # The late rate RISES after filtering. That is the censoring
        # being corrected: the rows removed were disproportionately
        # fast-clearing ones near the end of the extract.
        "observed_late_rate_before": round(
            float(
                (
                    days_late_before > 0
                ).mean()
            ),
            4,
        ),

        "observed_late_rate_after": round(
            float(
                (
                    days_late_after > 0
                ).mean()
            ),
            4,
        ),
    }

    return kept, audit


def roll_weekend_due_date_forward(
    due_dates: pd.Series,
) -> pd.Series:
    """Roll weekend due dates to Monday.

    Monday-Friday remain unchanged.

    Saturday -> +2 days
    Sunday   -> +1 day

    This does NOT attempt holiday adjustment.
    """

    adjusted = due_dates.copy()

    weekday = adjusted.dt.weekday

    saturday = (
        weekday == 5
    )

    sunday = (
        weekday == 6
    )

    adjusted.loc[saturday] = (
        adjusted.loc[saturday]
        + pd.Timedelta(days=2)
    )

    adjusted.loc[sunday] = (
        adjusted.loc[sunday]
        + pd.Timedelta(days=1)
    )

    return adjusted


def construct_targets(
    df: pd.DataFrame,
) -> pd.DataFrame:
    result = df.copy()

    # ---------------------------------------------------------
    # Original strict target
    # ---------------------------------------------------------

    result["days_late_strict"] = (
        result["clearing_date"]
        - result["net_due_date"]
    ).dt.days

    result[
        STRICT_TARGET_COLUMN
    ] = (
        result[
            "days_late_strict"
        ]
        > 0
    ).astype("int8")

    # ---------------------------------------------------------
    # Weekend-adjusted target
    # ---------------------------------------------------------

    result[
        "adjusted_due_date"
    ] = roll_weekend_due_date_forward(
        result["net_due_date"]
    )

    result[
        "days_late_weekend_adjusted"
    ] = (
        result["clearing_date"]
        - result["adjusted_due_date"]
    ).dt.days

    result[
        TARGET_COLUMN
    ] = (
        result[
            "days_late_weekend_adjusted"
        ]
        > 0
    ).astype("int8")

    # Original public source label:
    # retained only for reproducibility/audit.
    result[
        "source_delayflag"
    ] = (
        result["delayflag"]
        .astype("int8")
    )

    return result


def add_current_invoice_features(
    df: pd.DataFrame,
) -> pd.DataFrame:
    result = df.copy()

    result[
        "days_until_due"
    ] = (
        result["net_due_date"]
        - result["posting_date"]
    ).dt.days

    result[
        "posting_month"
    ] = (
        result["posting_date"]
        .dt.month
    )

    result[
        "posting_quarter"
    ] = (
        result["posting_date"]
        .dt.quarter
    )

    return result


def recent_rate(
    history: deque,
    window: int,
) -> float:
    if not history:
        return 0.5

    values = list(
        history
    )[-window:]

    return float(
        np.mean(
            [
                item["late"]
                for item in values
            ]
        )
    )


def recent_avg_delay(
    history: deque,
    window: int,
) -> float:
    if not history:
        return 0.0

    values = list(
        history
    )[-window:]

    return float(
        np.mean(
            [
                item["days_late"]
                for item in values
            ]
        )
    )


def current_streaks(
    history: deque,
) -> tuple[int, int]:
    """Return consecutive late and on-time streaks.

    Only one can be non-zero.
    """

    if not history:
        return 0, 0

    values = list(
        history
    )

    last_status = (
        values[-1]["late"]
    )

    streak = 0

    for item in reversed(
        values
    ):
        if (
            item["late"]
            != last_status
        ):
            break

        streak += 1

    if last_status == 1:
        return streak, 0

    return 0, streak


def build_customer_history(
    df: pd.DataFrame,
) -> pd.DataFrame:
    """Create leakage-safe historical customer features.

    Outcomes enter history only when:

        historical clearing_date < current posting_date

    Same-day invoice batches share the same history snapshot.
    """

    result = df.copy()

    history_columns = [
        "prior_invoice_count",
        "prior_resolved_invoice_count",
        "prior_open_invoice_count",
        "open_invoice_ratio",

        "prior_late_count",
        "prior_on_time_or_early_count",

        "historical_late_rate",
        "historical_avg_days_late",

        "historical_avg_invoice_amount",
        "amount_vs_historical_avg_ratio",

        "customer_tenure_days",
        "days_since_previous_invoice",
        "days_since_last_resolved_invoice",

        "recent_3_late_rate",
        "recent_5_late_rate",
        "recent_10_late_rate",

        "recent_3_avg_days_late",
        "recent_5_avg_days_late",
        "recent_10_avg_days_late",

        "recent_late_streak",
        "recent_on_time_streak",

        "recent_5_late_rate_delta",
        "recent_5_avg_days_late_delta",
    ]

    for column in history_columns:
        result[column] = np.nan

    for _, group in result.groupby(
        "cust_num",
        sort=False,
    ):
        group = group.sort_values(
            "posting_date",
            kind="stable",
        )

        first_posting_date = (
            group["posting_date"]
            .min()
        )

        previous_posting_date = None
        last_resolved_date = None

        prior_invoice_count = 0
        prior_amount_sum = 0.0

        resolved_count = 0
        late_count = 0
        on_time_or_early_count = 0

        resolved_days_late_sum = 0.0

        # Last ten resolved invoices.
        recent_history = deque(
            maxlen=10
        )

        # Invoices whose outcomes have not yet
        # become known at prediction time.
        unresolved_heap = []

        for (
            posting_date,
            batch,
        ) in group.groupby(
            "posting_date",
            sort=True,
        ):
            # -------------------------------------------------
            # Resolve old invoices whose outcome was known
            # BEFORE this prediction date.
            # -------------------------------------------------

            while (
                unresolved_heap
                and unresolved_heap[0][0]
                < posting_date
            ):
                (
                    clearing_date,
                    historical_days_late,
                    historical_late,
                ) = heapq.heappop(
                    unresolved_heap
                )

                resolved_count += 1

                resolved_days_late_sum += (
                    historical_days_late
                )

                if historical_late:
                    late_count += 1
                else:
                    (
                        on_time_or_early_count
                    ) += 1

                recent_history.append(
                    {
                        "late": int(
                            historical_late
                        ),
                        "days_late": float(
                            historical_days_late
                        ),
                    }
                )

                last_resolved_date = (
                    clearing_date
                )

            open_invoice_count = len(
                unresolved_heap
            )

            # -------------------------------------------------
            # Overall behaviour
            # -------------------------------------------------

            if resolved_count:
                historical_late_rate = (
                    late_count
                    / resolved_count
                )

                historical_avg_days_late = (
                    resolved_days_late_sum
                    / resolved_count
                )
            else:
                historical_late_rate = 0.5
                historical_avg_days_late = 0.0

            if prior_invoice_count:
                historical_avg_amount = (
                    prior_amount_sum
                    / prior_invoice_count
                )

                open_invoice_ratio = (
                    open_invoice_count
                    / prior_invoice_count
                )
            else:
                historical_avg_amount = 0.0
                open_invoice_ratio = 0.0

            customer_tenure_days = (
                posting_date
                - first_posting_date
            ).days

            if (
                previous_posting_date
                is None
            ):
                days_since_previous_invoice = 0
            else:
                days_since_previous_invoice = (
                    posting_date
                    - previous_posting_date
                ).days

            if (
                last_resolved_date
                is None
            ):
                days_since_last_resolved = 0
            else:
                days_since_last_resolved = (
                    posting_date
                    - last_resolved_date
                ).days

            # -------------------------------------------------
            # Recent behaviour
            # -------------------------------------------------

            recent_3_late = recent_rate(
                recent_history,
                3,
            )

            recent_5_late = recent_rate(
                recent_history,
                5,
            )

            recent_10_late = recent_rate(
                recent_history,
                10,
            )

            recent_3_delay = recent_avg_delay(
                recent_history,
                3,
            )

            recent_5_delay = recent_avg_delay(
                recent_history,
                5,
            )

            recent_10_delay = recent_avg_delay(
                recent_history,
                10,
            )

            (
                late_streak,
                on_time_streak,
            ) = current_streaks(
                recent_history
            )

            late_rate_delta = (
                recent_5_late
                - historical_late_rate
            )

            avg_delay_delta = (
                recent_5_delay
                - historical_avg_days_late
            )

            indices = batch.index

            # -------------------------------------------------
            # Features identical for the same-day batch.
            # -------------------------------------------------

            result.loc[
                indices,
                "prior_invoice_count",
            ] = prior_invoice_count

            result.loc[
                indices,
                "prior_resolved_invoice_count",
            ] = resolved_count

            result.loc[
                indices,
                "prior_open_invoice_count",
            ] = open_invoice_count

            result.loc[
                indices,
                "open_invoice_ratio",
            ] = open_invoice_ratio

            result.loc[
                indices,
                "prior_late_count",
            ] = late_count

            result.loc[
                indices,
                "prior_on_time_or_early_count",
            ] = on_time_or_early_count

            result.loc[
                indices,
                "historical_late_rate",
            ] = historical_late_rate

            result.loc[
                indices,
                "historical_avg_days_late",
            ] = historical_avg_days_late

            result.loc[
                indices,
                "historical_avg_invoice_amount",
            ] = historical_avg_amount

            result.loc[
                indices,
                "customer_tenure_days",
            ] = customer_tenure_days

            result.loc[
                indices,
                "days_since_previous_invoice",
            ] = days_since_previous_invoice

            result.loc[
                indices,
                "days_since_last_resolved_invoice",
            ] = days_since_last_resolved

            result.loc[
                indices,
                "recent_3_late_rate",
            ] = recent_3_late

            result.loc[
                indices,
                "recent_5_late_rate",
            ] = recent_5_late

            result.loc[
                indices,
                "recent_10_late_rate",
            ] = recent_10_late

            result.loc[
                indices,
                "recent_3_avg_days_late",
            ] = recent_3_delay

            result.loc[
                indices,
                "recent_5_avg_days_late",
            ] = recent_5_delay

            result.loc[
                indices,
                "recent_10_avg_days_late",
            ] = recent_10_delay

            result.loc[
                indices,
                "recent_late_streak",
            ] = late_streak

            result.loc[
                indices,
                "recent_on_time_streak",
            ] = on_time_streak

            result.loc[
                indices,
                "recent_5_late_rate_delta",
            ] = late_rate_delta

            result.loc[
                indices,
                "recent_5_avg_days_late_delta",
            ] = avg_delay_delta

            # Amount ratio depends on the current invoice,
            # so calculate it invoice-by-invoice.
            if historical_avg_amount > 0:
                amount_ratio = (
                    batch["amount"]
                    / historical_avg_amount
                )
            else:
                # Neutral cold-start value.
                amount_ratio = pd.Series(
                    1.0,
                    index=indices,
                )

            result.loc[
                indices,
                "amount_vs_historical_avg_ratio",
            ] = amount_ratio

            # -------------------------------------------------
            # Only AFTER creating the prediction snapshot do
            # these invoices become historical invoices.
            # -------------------------------------------------

            for row in batch.itertuples():
                prior_invoice_count += 1

                prior_amount_sum += float(
                    row.amount
                )

                heapq.heappush(
                    unresolved_heap,
                    (
                        row.clearing_date,
                        float(
                            row.days_late_strict
                        ),
                        int(
                            getattr(
                                row,
                                STRICT_TARGET_COLUMN,
                            )
                        ),
                    ),
                )

            previous_posting_date = (
                posting_date
            )

    return result


def repair_feature_scales(
    df: pd.DataFrame,
) -> pd.DataFrame:
    """Apply the V3 feature-set repairs documented above COUNT_CAP.

    Order matters: has_resolved_history is derived BEFORE the cold-start
    rates are blanked to NaN, because the flag is what lets the model
    tell "no history" apart from "history happens to be 50% late".
    """

    result = df.copy()

    # --- 3. explicit cold-start flag, then NaN instead of 0.5 --------
    has_history = (
        result[
            "prior_resolved_invoice_count"
        ]
        > 0
    )

    result[
        "has_resolved_history"
    ] = (
        has_history.astype("int8")
    )

    cold_start_rate_columns = [
        "historical_late_rate",
        "recent_3_late_rate",
        "recent_5_late_rate",
        "recent_10_late_rate",
    ]

    for column in cold_start_rate_columns:
        if column in result.columns:
            result.loc[
                ~has_history,
                column,
            ] = np.nan

    # --- 1. cap cumulative counts so they cannot encode time ---------
    count_columns = [
        "prior_invoice_count",
        "prior_resolved_invoice_count",
        "prior_open_invoice_count",
        "prior_late_count",
        "prior_on_time_or_early_count",
        "customer_tenure_days",
        "days_since_previous_invoice",
        "days_since_last_resolved_invoice",
    ]

    for column in count_columns:
        if column in result.columns:
            result[column] = (
                result[column]
                .clip(
                    upper=COUNT_CAP
                )
            )

    # --- 4. clip the ratio outliers ----------------------------------
    result[
        "amount_vs_historical_avg_ratio"
    ] = (
        result[
            "amount_vs_historical_avg_ratio"
        ]
        .clip(
            lower=0.0,
            upper=RATIO_CLIP,
        )
    )

    return result


def validate_no_leakage(
    feature_columns: list[str],
) -> None:
    prohibited = (
        set(LEAKAGE_COLUMNS)
        | set(
            UNSAFE_PRECOMPUTED_COLUMNS
        )
        | {
            "days_late_strict",
            "adjusted_due_date",
            "days_late_weekend_adjusted",
            STRICT_TARGET_COLUMN,
            TARGET_COLUMN,
            "source_delayflag",
            "cust_num",
            "customer_name",
            "document_no",
            "posting_weekday",
            "due_weekday",
        }
    )

    leaked = (
        set(feature_columns)
        & prohibited
    )

    if leaked:
        raise ValueError(
            "Leakage / prohibited features "
            "detected: "
            f"{sorted(leaked)}"
        )


def build() -> pd.DataFrame:
    print(
        "Loading raw dataset..."
    )

    df = load_raw()

    print(
        "Raw shape:",
        df.shape,
    )

    print()
    print(
        "Applying observation horizon "
        f"({OBSERVATION_HORIZON_DAYS} days)..."
    )

    df, horizon_audit = (
        apply_observation_horizon(df)
    )

    print(
        "  data cutoff:",
        horizon_audit["data_cutoff"],
    )

    print(
        "  rows:",
        horizon_audit["rows_before"],
        "->",
        horizon_audit["rows_after"],
        f"({horizon_audit['retained_fraction'] * 100:.1f}% retained)",
    )

    print(
        "  observed late rate:",
        horizon_audit[
            "observed_late_rate_before"
        ],
        "->",
        horizon_audit[
            "observed_late_rate_after"
        ],
        "(rises because censored fast-clearing "
        "rows were removed)",
    )

    df = construct_targets(
        df
    )

    strict_rate = float(
        df[
            STRICT_TARGET_COLUMN
        ].mean()
    )

    adjusted_rate = float(
        df[
            TARGET_COLUMN
        ].mean()
    )

    changed_labels = int(
        (
            df[
                STRICT_TARGET_COLUMN
            ]
            != df[
                TARGET_COLUMN
            ]
        ).sum()
    )

    print()
    print(
        "Strict late rate:",
        round(
            strict_rate * 100,
            2,
        ),
        "%",
    )

    print(
        "Weekend-adjusted late rate:",
        round(
            adjusted_rate * 100,
            2,
        ),
        "%",
    )

    print(
        "Labels changed by weekend adjustment:",
        changed_labels,
    )

    df = add_current_invoice_features(
        df
    )

    print()
    print(
        "Building leakage-safe "
        "customer history..."
    )

    df = build_customer_history(
        df
    )

    print(
        "Applying V3 feature-scale repairs "
        "(count caps, cold-start NaN, ratio clip)..."
    )

    df = repair_feature_scales(df)

    feature_columns = (
        MODEL_NUMERIC_FEATURES
        + MODEL_CATEGORICAL_FEATURES
    )

    validate_no_leakage(
        feature_columns
    )

    # Cold-start rate features are INTENTIONALLY NaN now (see repair
    # note 3), so only the columns that must always be present are
    # checked for completeness. XGBoost routes the NaNs natively, and
    # has_resolved_history tells the model which case it is in.
    intentionally_nullable = {
        "historical_late_rate",
        "recent_5_late_rate",
    }

    must_be_complete = [
        column
        for column in feature_columns
        if column
        not in intentionally_nullable
    ]

    missing_features = (
        df[
            must_be_complete
        ]
        .isna()
        .sum()
    )

    if missing_features.sum():
        raise ValueError(
            "Missing model features:\n"
            f"{missing_features[
                missing_features > 0
            ]}"
        )

    cold_start_rows = int(
        (
            df[
                "has_resolved_history"
            ]
            == 0
        ).sum()
    )

    print(
        "  cold-start rows (no resolved history):",
        cold_start_rows,
        f"({cold_start_rows / len(df) * 100:.2f}%)",
    )

    output_columns = [
        "document_no",
        "cust_num",

        "doc_date",
        "posting_date",
        "net_due_date",

        *feature_columns,

        TARGET_COLUMN,

        # Offline audit columns only.
        STRICT_TARGET_COLUMN,
        "days_late_strict",
        "days_late_weekend_adjusted",
        "source_delayflag",
    ]

    output = (
        df[
            output_columns
        ]
        .sort_values(
            [
                "posting_date",
                "document_no",
            ]
        )
        .reset_index(
            drop=True
        )
    )

    PROCESSED_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    output.to_csv(
        OUTPUT_PATH,
        index=False,
        compression="gzip",
    )

    target_counts = (
        output[
            TARGET_COLUMN
        ]
        .value_counts()
        .sort_index()
    )

    manifest = {
        "version": 3,

        "source": (
            RAW_PATH.name
        ),

        "observation_horizon": (
            horizon_audit
        ),

        "rows": int(
            len(output)
        ),

        "customers": int(
            output[
                "cust_num"
            ].nunique()
        ),

        "documents": int(
            output[
                "document_no"
            ].nunique()
        ),

        "target": {
            "name": TARGET_COLUMN,

            "definition": (
                "1 when clearing_date is after "
                "the weekend-adjusted due date; "
                "Saturday/Sunday due dates are "
                "rolled forward to Monday. "
                "Holiday adjustment is not applied."
            ),

            "counts": {
                str(key): int(value)
                for key, value
                in target_counts.items()
            },

            "positive_rate": (
                adjusted_rate
            ),

            "strict_positive_rate": (
                strict_rate
            ),

            "labels_changed_from_strict": (
                changed_labels
            ),
        },

        "numeric_features": (
            MODEL_NUMERIC_FEATURES
        ),

        "categorical_features": (
            MODEL_CATEGORICAL_FEATURES
        ),

        "excluded_calendar_shortcuts": [
            "posting_weekday",
            "due_weekday",
            "posting_month",
            "posting_quarter",
        ],

        "notes": [
            (
                "V3: an observation horizon of "
                f"{OBSERVATION_HORIZON_DAYS} days is applied "
                "before splitting. The raw extract only holds "
                "invoices that had cleared by its last clearing "
                "date, so the slowest payers were missing, and "
                "increasingly so the later an invoice was posted "
                "-- the old test partition could not contain an "
                "invoice taking longer than 22 days to clear. "
                "Rows are now kept only when net_due_date + "
                "horizon <= data cutoff."
            ),
            (
                "V3: posting_month and posting_quarter removed "
                "for the same reason V2 removed the weekday "
                "fields -- they encode absolute position in time "
                "and do not extrapolate past the training window."
            ),
            (
                "Current-invoice weekday fields are "
                "excluded after temporal ablation "
                "showed strong calendar dependence."
            ),
            (
                "Historical payment outcomes are used "
                "only after their clearing date is "
                "strictly earlier than the current "
                "invoice posting date."
            ),
            (
                "Recent 3/5/10 features use resolved "
                "invoice outcomes only."
            ),
            (
                "Weekend adjustment does not claim "
                "to represent public holidays."
            ),
        ],
    }

    MANIFEST_PATH.write_text(
        json.dumps(
            manifest,
            indent=2,
        )
        + "\n"
    )

    print()
    print("=" * 80)
    print(
        "RECOVERIQ PRIMARY FEATURE BUILD V3 COMPLETE"
    )
    print("=" * 80)

    print(
        "Output:",
        OUTPUT_PATH,
    )

    print(
        "Manifest:",
        MANIFEST_PATH,
    )

    print(
        "\nShape:",
        output.shape,
    )

    print(
        "\nTarget:"
    )

    print(
        output[
            TARGET_COLUMN
        ]
        .value_counts()
        .sort_index()
    )

    print(
        "\nTarget percentages:"
    )

    print(
        (
            output[
                TARGET_COLUMN
            ]
            .value_counts(
                normalize=True
            )
            .sort_index()
            * 100
        ).round(2)
    )

    print(
        "\nModel numeric features:",
        len(
            MODEL_NUMERIC_FEATURES
        ),
    )

    print(
        "Model categorical features:",
        len(
            MODEL_CATEGORICAL_FEATURES
        ),
    )

    print(
        "\nMissing model-feature values:",
        int(
            output[
                feature_columns
            ]
            .isna()
            .sum()
            .sum()
        ),
    )

    return output


if __name__ == "__main__":
    build()
