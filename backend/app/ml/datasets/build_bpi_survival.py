"""
RecoverIQ — BPI 2019 Survival Dataset Builder

Goal
----
Build a leakage-safe, censoring-aware invoice time-to-clear dataset.

Target:
    duration_days =
        clear_timestamp - invoice_timestamp
        OR
        observation_cutoff - invoice_timestamp

    event_observed =
        1 -> Clear Invoice observed
        0 -> still unresolved at observation cutoff

Prediction timestamp:
    Record Invoice Receipt

Important:
- unresolved invoices are RETAINED
- cancelled invoices are excluded
- ambiguous multiple-invoice / multiple-clear cases are excluded
- pre-invoice process features only use information available
  at or before invoice receipt
- vendor history only uses outcomes that had already resolved
  before the current invoice arrived

This is NOT the old slow_clear_45d classifier.
"""

from __future__ import annotations

import heapq
import json
from collections import deque
from pathlib import Path

import numpy as np
import pandas as pd
import pm4py


# ============================================================
# PATHS
# ============================================================

RAW_XES = Path(
    "../data/raw/bpi_2019/BPI_Challenge_2019.xes"
)

OUTPUT_DIR = Path(
    "../data/processed/bpi_2019"
)

OUTPUT_CSV = (
    OUTPUT_DIR
    / "bpi_survival_features.csv.gz"
)

MANIFEST_PATH = (
    OUTPUT_DIR
    / "bpi_survival_manifest.json"
)


# ============================================================
# BPI COLUMN NAMES
# ============================================================

CASE = "case:concept:name"
ACTIVITY = "concept:name"
TIMESTAMP = "time:timestamp"

INVOICE_ACTIVITY = "Record Invoice Receipt"
CLEAR_ACTIVITY = "Clear Invoice"
CANCEL_ACTIVITY = "Cancel Invoice Receipt"

AMOUNT_COLUMN = "Cumulative net worth (EUR)"


CASE_ATTRIBUTES = [
    "case:Vendor",
    "case:Company",
    "case:Document Type",
    "case:Item Category",
    "case:GR-Based Inv. Verif.",
    "case:Goods Receipt",
]


# ============================================================
# PRE-INVOICE PROCESS FEATURES
# ============================================================

PROCESS_ACTIVITIES = {
    "Record Goods Receipt":
        "pre_goods_receipt_count",

    "Record Service Entry Sheet":
        "pre_service_entry_count",

    "Create Purchase Requisition Item":
        "pre_purchase_requisition_count",

    "Receive Order Confirmation":
        "pre_order_confirmation_count",

    "Change Quantity":
        "pre_quantity_change_count",

    "Change Price":
        "pre_price_change_count",

    "Change Approval":
        "pre_approval_change_count",

    "Delete Purchase Order Item":
        "pre_delete_po_item_count",

    "Set Payment Block":
        "pre_set_payment_block_count",

    "Remove Payment Block":
        "pre_remove_payment_block_count",
}


HORIZONS = [
    7,
    15,
    30,
    45,
    60,
    90,
]


# Ignore ancient/bogus process timestamps farther than this
# before invoice receipt.
LOOKBACK_DAYS = 730


# ============================================================
# UTILS
# ============================================================

def banner(title: str):

    print()
    print("=" * 92)
    print(title)
    print("=" * 92)


def safe_category(series):

    return (
        series
        .fillna("__MISSING__")
        .astype(str)
        .replace(
            {
                "nan": "__MISSING__",
                "None": "__MISSING__",
            }
        )
    )


# ============================================================
# LOAD XES
# ============================================================

def load_events():

    banner(
        "BPI 2019 — LOADING XES"
    )

    if not RAW_XES.exists():
        raise FileNotFoundError(
            RAW_XES.resolve()
        )

    loaded = pm4py.read_xes(
        str(RAW_XES)
    )

    if isinstance(
        loaded,
        pd.DataFrame,
    ):
        events = loaded

    else:
        events = (
            pm4py.convert_to_dataframe(
                loaded
            )
        )

    required = [
        CASE,
        ACTIVITY,
        TIMESTAMP,
    ]

    missing = [
        c
        for c in required
        if c not in events.columns
    ]

    if missing:
        raise RuntimeError(
            f"Missing required XES columns: "
            f"{missing}"
        )

    events[TIMESTAMP] = pd.to_datetime(
        events[TIMESTAMP],
        utc=True,
        errors="coerce",
    )

    events = events.dropna(
        subset=[
            CASE,
            ACTIVITY,
            TIMESTAMP,
        ]
    )

    print(
        f"Events: {len(events):,}"
    )

    print(
        f"Cases: "
        f"{events[CASE].nunique():,}"
    )

    print(
        f"Date range: "
        f"{events[TIMESTAMP].min()} "
        f"→ "
        f"{events[TIMESTAMP].max()}"
    )

    return events


# ============================================================
# OBSERVATION CUTOFF
# ============================================================

def determine_observation_cutoff(
    events,
):

    # We derive the operational cutoff only from the
    # invoice/clear process and ignore obviously anomalous
    # post-2019 timestamps.
    relevant = events[
        events[ACTIVITY].isin(
            [
                INVOICE_ACTIVITY,
                CLEAR_ACTIVITY,
            ]
        )
        &
        (
            events[
                TIMESTAMP
            ].dt.year <= 2019
        )
    ]

    cutoff = relevant[
        TIMESTAMP
    ].max()

    if pd.isna(cutoff):
        raise RuntimeError(
            "Could not determine observation cutoff."
        )

    return cutoff


# ============================================================
# CLEAN COHORT
# ============================================================

def build_case_cohort(
    events,
    observation_cutoff,
):

    banner(
        "BUILDING CLEAN INVOICE COHORT"
    )

    relevant = events[
        events[ACTIVITY].isin(
            [
                INVOICE_ACTIVITY,
                CLEAR_ACTIVITY,
                CANCEL_ACTIVITY,
            ]
        )
    ]

    counts = (
        relevant
        .groupby(
            [
                CASE,
                ACTIVITY,
            ]
        )
        .size()
        .unstack(
            fill_value=0
        )
    )

    for activity in [
        INVOICE_ACTIVITY,
        CLEAR_ACTIVITY,
        CANCEL_ACTIVITY,
    ]:
        if activity not in counts:
            counts[activity] = 0

    eligible = counts[
        (
            counts[
                INVOICE_ACTIVITY
            ] == 1
        )
        &
        (
            counts[
                CLEAR_ACTIVITY
            ] <= 1
        )
        &
        (
            counts[
                CANCEL_ACTIVITY
            ] == 0
        )
    ].index

    invoice_rows = (
        events[
            (
                events[CASE].isin(
                    eligible
                )
            )
            &
            (
                events[ACTIVITY]
                == INVOICE_ACTIVITY
            )
        ]
        .sort_values(
            [
                CASE,
                TIMESTAMP,
            ]
        )
        .drop_duplicates(
            CASE,
            keep="first",
        )
        .set_index(
            CASE
        )
    )

    clear_rows = (
        events[
            (
                events[CASE].isin(
                    eligible
                )
            )
            &
            (
                events[ACTIVITY]
                == CLEAR_ACTIVITY
            )
        ]
        .sort_values(
            [
                CASE,
                TIMESTAMP,
            ]
        )
        .drop_duplicates(
            CASE,
            keep="first",
        )
        .set_index(
            CASE
        )
    )

    cases = pd.DataFrame(
        index=invoice_rows.index
    )

    cases[
        "invoice_timestamp"
    ] = invoice_rows[
        TIMESTAMP
    ]

    cases[
        "clear_timestamp"
    ] = clear_rows[
        TIMESTAMP
    ].reindex(
        cases.index
    )

    # Invoice must occur within observation period.
    cases = cases[
        cases[
            "invoice_timestamp"
        ]
        <= observation_cutoff
    ].copy()

    # Remove impossible clear-before-invoice cases.
    negative = (
        cases[
            "clear_timestamp"
        ].notna()
        &
        (
            cases[
                "clear_timestamp"
            ]
            <
            cases[
                "invoice_timestamp"
            ]
        )
    )

    negative_count = int(
        negative.sum()
    )

    cases = cases[
        ~negative
    ].copy()

    # Case attributes available at invoice receipt.
    for column in CASE_ATTRIBUTES:

        if column in invoice_rows.columns:

            clean_name = (
                column
                .replace(
                    "case:",
                    ""
                )
                .lower()
                .replace(
                    " ",
                    "_"
                )
                .replace(
                    ".",
                    ""
                )
        )

            cases[
                clean_name
            ] = safe_category(
                invoice_rows[
                    column
                ].reindex(
                    cases.index
                )
            )

    cases[
        "vendor"
    ] = (
        cases.get(
            "vendor",
            "__MISSING__",
        )
    )

    # Outcome.
    observed = (
        cases[
            "clear_timestamp"
        ].notna()
        &
        (
            cases[
                "clear_timestamp"
            ]
            <= observation_cutoff
        )
    )

    cases[
        "event_observed"
    ] = (
        observed
        .astype(int)
    )

    endpoint = (
        cases[
            "clear_timestamp"
        ].where(
            observed,
            observation_cutoff,
        )
    )

    cases[
        "duration_days"
    ] = (
        (
            endpoint
            -
            cases[
                "invoice_timestamp"
            ]
        )
        .dt.total_seconds()
        /
        86400.0
    )

    cases = cases[
        cases[
            "duration_days"
        ]
        >= 0
    ].copy()

    print(
        f"Eligible cases: "
        f"{len(cases):,}"
    )

    print(
        f"Observed clears: "
        f"{cases['event_observed'].sum():,}"
    )

    print(
        f"Censored unresolved: "
        f"{(cases['event_observed'] == 0).sum():,}"
    )

    print(
        f"Removed clear-before-invoice: "
        f"{negative_count:,}"
    )

    return (
        cases,
        invoice_rows,
    )


# ============================================================
# PRE-INVOICE FEATURES
# ============================================================

def add_pre_invoice_features(
    events,
    cases,
):

    banner(
        "BUILDING PRE-INVOICE PROCESS FEATURES"
    )

    invoice_map = (
        cases[
            "invoice_timestamp"
        ]
    )

    cohort_events = events[
        events[CASE].isin(
            cases.index
        )
    ].copy()

    cohort_events[
        "_invoice_timestamp"
    ] = cohort_events[
        CASE
    ].map(
        invoice_map
    )

    lookback_start = (
        cohort_events[
            "_invoice_timestamp"
        ]
        -
        pd.Timedelta(
            days=LOOKBACK_DAYS
        )
    )

    pre = cohort_events[
        (
            cohort_events[
                TIMESTAMP
            ]
            <=
            cohort_events[
                "_invoice_timestamp"
            ]
        )
        &
        (
            cohort_events[
                TIMESTAMP
            ]
            >= lookback_start
        )
    ].copy()

    # Total number of events known by invoice receipt.
    total = (
        pre.groupby(
            CASE
        )
        .size()
        .rename(
            "pre_invoice_event_count"
        )
    )

    cases[
        "pre_invoice_event_count"
    ] = (
        total.reindex(
            cases.index,
            fill_value=0,
        )
        .astype(int)
    )

    selected = pre[
        pre[ACTIVITY].isin(
            PROCESS_ACTIVITIES
        )
    ]

    process_counts = (
        selected
        .groupby(
            [
                CASE,
                ACTIVITY,
            ]
        )
        .size()
        .unstack(
            fill_value=0
        )
    )

    for (
        activity,
        output_column,
    ) in PROCESS_ACTIVITIES.items():

        if activity in process_counts:

            values = (
                process_counts[
                    activity
                ]
            )

        else:
            values = pd.Series(
                dtype=float
            )

        cases[
            output_column
        ] = (
            values.reindex(
                cases.index,
                fill_value=0,
            )
            .astype(int)
        )

    # Process age from earliest sane event in the
    # 730-day lookback to invoice receipt.
    first_event = (
        pre.groupby(
            CASE
        )[TIMESTAMP]
        .min()
    )

    cases[
        "process_age_at_invoice_days"
    ] = (
        (
            cases[
                "invoice_timestamp"
            ]
            -
            first_event.reindex(
                cases.index
            )
        )
        .dt.total_seconds()
        /
        86400.0
    )

    # Rework / friction count.
    rework_columns = [
        "pre_quantity_change_count",
        "pre_price_change_count",
        "pre_approval_change_count",
        "pre_delete_po_item_count",
        "pre_set_payment_block_count",
        "pre_remove_payment_block_count",
    ]

    cases[
        "pre_rework_count"
    ] = (
        cases[
            rework_columns
        ]
        .sum(
            axis=1
        )
    )

    cases[
        "has_goods_receipt_before_invoice"
    ] = (
        cases[
            "pre_goods_receipt_count"
        ]
        > 0
    ).astype(int)

    cases[
        "has_service_entry_before_invoice"
    ] = (
        cases[
            "pre_service_entry_count"
        ]
        > 0
    ).astype(int)

    cases[
        "had_payment_block_activity_before_invoice"
    ] = (
        (
            cases[
                "pre_set_payment_block_count"
            ]
            +
            cases[
                "pre_remove_payment_block_count"
            ]
        )
        > 0
    ).astype(int)

    # Amount available by invoice time.
    if AMOUNT_COLUMN in pre.columns:

        amount_source = (
            pre[
                [
                    CASE,
                    TIMESTAMP,
                    AMOUNT_COLUMN,
                ]
            ]
            .copy()
        )

        amount_source[
            AMOUNT_COLUMN
        ] = pd.to_numeric(
            amount_source[
                AMOUNT_COLUMN
            ],
            errors="coerce",
        )

        amount_source = (
            amount_source
            .dropna(
                subset=[
                    AMOUNT_COLUMN
                ]
            )
            .sort_values(
                [
                    CASE,
                    TIMESTAMP,
                ]
            )
        )

        amount = (
            amount_source
            .groupby(
                CASE
            )[
                AMOUNT_COLUMN
            ]
            .last()
        )

        cases[
            "invoice_amount_eur"
        ] = amount.reindex(
            cases.index
        )

    else:

        cases[
            "invoice_amount_eur"
        ] = np.nan

    print(
        "Pre-invoice feature construction complete."
    )

    return cases


# ============================================================
# LEAKAGE-SAFE VENDOR HISTORY
# ============================================================

def add_vendor_history(
    cases,
):

    banner(
        "BUILDING LEAKAGE-SAFE VENDOR HISTORY"
    )

    df = cases.copy()

    output_columns = [
        "vendor_prior_invoice_count",
        "vendor_prior_resolved_count",
        "vendor_prior_open_count",
        "vendor_historical_clear_rate",
        "vendor_historical_avg_clear_days",
        "vendor_recent5_avg_clear_days",
        "vendor_days_since_previous_invoice",
        "vendor_historical_avg_invoice_amount",
        "amount_vs_vendor_historical_avg",
    ]

    for column in output_columns:
        df[column] = np.nan

    for vendor, index in df.groupby(
        "vendor",
        dropna=False,
    ).groups.items():

        group = (
            df.loc[index]
            .sort_values(
                [
                    "invoice_timestamp",
                ]
            )
        )

        pending = []

        resolved_count = 0
        resolved_duration_sum = 0.0

        recent_resolved = deque(
            maxlen=5
        )

        prior_invoice_count = 0

        prior_amount_sum = 0.0
        prior_amount_count = 0

        previous_invoice = None

        for idx, row in group.iterrows():

            invoice_ts = row[
                "invoice_timestamp"
            ]

            # Reveal earlier outcomes only if they had
            # already cleared strictly before this invoice.
            while (
                pending
                and pending[0][0]
                <
                invoice_ts.value
            ):

                (
                    _,
                    duration,
                ) = heapq.heappop(
                    pending
                )

                resolved_count += 1

                resolved_duration_sum += (
                    duration
                )

                recent_resolved.append(
                    duration
                )

            open_count = (
                prior_invoice_count
                -
                resolved_count
            )

            df.at[
                idx,
                "vendor_prior_invoice_count",
            ] = prior_invoice_count

            df.at[
                idx,
                "vendor_prior_resolved_count",
            ] = resolved_count

            df.at[
                idx,
                "vendor_prior_open_count",
            ] = max(
                0,
                open_count,
            )

            if prior_invoice_count > 0:

                df.at[
                    idx,
                    "vendor_historical_clear_rate",
                ] = (
                    resolved_count
                    /
                    prior_invoice_count
                )

            if resolved_count > 0:

                df.at[
                    idx,
                    "vendor_historical_avg_clear_days",
                ] = (
                    resolved_duration_sum
                    /
                    resolved_count
                )

            if recent_resolved:

                df.at[
                    idx,
                    "vendor_recent5_avg_clear_days",
                ] = float(
                    np.mean(
                        recent_resolved
                    )
                )

            if previous_invoice is not None:

                df.at[
                    idx,
                    "vendor_days_since_previous_invoice",
                ] = (
                    invoice_ts
                    -
                    previous_invoice
                ).total_seconds() / 86400.0

            if prior_amount_count > 0:

                historical_amount = (
                    prior_amount_sum
                    /
                    prior_amount_count
                )

                df.at[
                    idx,
                    "vendor_historical_avg_invoice_amount",
                ] = historical_amount

                amount = row[
                    "invoice_amount_eur"
                ]

                if (
                    pd.notna(amount)
                    and historical_amount > 0
                ):

                    df.at[
                        idx,
                        "amount_vs_vendor_historical_avg",
                    ] = (
                        amount
                        /
                        historical_amount
                    )

            # Only after features are captured does the
            # current invoice become part of history.
            amount = row[
                "invoice_amount_eur"
            ]

            if pd.notna(amount):

                prior_amount_sum += float(
                    amount
                )

                prior_amount_count += 1

            if (
                int(
                    row[
                        "event_observed"
                    ]
                )
                == 1
                and pd.notna(
                    row[
                        "clear_timestamp"
                    ]
                )
            ):

                heapq.heappush(
                    pending,
                    (
                        row[
                            "clear_timestamp"
                        ].value,

                        float(
                            row[
                                "duration_days"
                            ]
                        ),
                    ),
                )

            prior_invoice_count += 1
            previous_invoice = invoice_ts

    df[
        "vendor_has_resolved_history"
    ] = (
        df[
            "vendor_prior_resolved_count"
        ]
        > 0
    ).astype(int)

    return df


# ============================================================
# HORIZON AUDIT LABELS
# ============================================================

def add_horizon_labels(
    cases,
):

    for horizon in HORIZONS:

        column = (
            f"clear_within_{horizon}d"
        )

        # 1:
        # clear observed within horizon.
        positive = (
            (
                cases[
                    "event_observed"
                ] == 1
            )
            &
            (
                cases[
                    "duration_days"
                ] <= horizon
            )
        )

        # 0:
        # we observed the case for at least this long
        # and it had not cleared by the horizon.
        known_negative = (
            cases[
                "duration_days"
            ]
            >= horizon
        )

        cases[column] = np.nan

        cases.loc[
            positive,
            column,
        ] = 1.0

        cases.loc[
            (
                ~positive
                &
                known_negative
            ),
            column,
        ] = 0.0

    return cases


# ============================================================
# MANIFEST
# ============================================================

def make_manifest(
    cases,
    cutoff,
):

    manifest = {
        "dataset":
            "BPI Challenge 2019",

        "task":
            "right-censored invoice time-to-clear",

        "prediction_timestamp":
            INVOICE_ACTIVITY,

        "observation_cutoff":
            str(cutoff),

        "rows":
            int(len(cases)),

        "observed_clears":
            int(
                cases[
                    "event_observed"
                ].sum()
            ),

        "censored_rows":
            int(
                (
                    cases[
                        "event_observed"
                    ]
                    == 0
                ).sum()
            ),

        "event_rate":
            float(
                cases[
                    "event_observed"
                ].mean()
            ),

        "invoice_date_min":
            str(
                cases[
                    "invoice_timestamp"
                ].min()
            ),

        "invoice_date_max":
            str(
                cases[
                    "invoice_timestamp"
                ].max()
            ),

        "duration": {
            "median_days":
                float(
                    cases[
                        "duration_days"
                    ].median()
                ),

            "p90_days":
                float(
                    cases[
                        "duration_days"
                    ].quantile(
                        0.90
                    )
                ),

            "max_days":
                float(
                    cases[
                        "duration_days"
                    ].max()
                ),
        },

        "horizons": {},

        "leakage_policy": {
            "direct_vendor_id_is_model_feature":
                False,

            "clear_timestamp_is_model_feature":
                False,

            "event_observed_is_model_feature":
                False,

            "duration_days_is_model_feature":
                False,

            "process_feature_lookback_days":
                LOOKBACK_DAYS,

            "vendor_history_requires_resolution_before_current_invoice":
                True,
        },
    }

    for horizon in HORIZONS:

        column = (
            f"clear_within_{horizon}d"
        )

        known = (
            cases[column]
            .notna()
        )

        manifest[
            "horizons"
        ][
            str(horizon)
        ] = {
            "known_rows":
                int(
                    known.sum()
                ),

            "unknown_censored_rows":
                int(
                    (~known).sum()
                ),

            "clear_rate":
                float(
                    cases.loc[
                        known,
                        column,
                    ].mean()
                )
                if known.any()
                else None,
        }

    return manifest


# ============================================================
# MAIN
# ============================================================

def main():

    OUTPUT_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    events = load_events()

    cutoff = (
        determine_observation_cutoff(
            events
        )
    )

    banner(
        "OBSERVATION CUTOFF"
    )

    print(
        cutoff
    )

    cases, _ = (
        build_case_cohort(
            events,
            cutoff,
        )
    )

    cases = (
        add_pre_invoice_features(
            events,
            cases,
        )
    )

    # Free the huge XES dataframe before vendor history.
    del events

    cases = (
        add_vendor_history(
            cases
        )
    )

    cases = (
        add_horizon_labels(
            cases
        )
    )

    # Convert case id from index to normal column.
    cases = (
        cases
        .reset_index()
        .rename(
            columns={
                CASE:
                    "case_id"
            }
        )
    )

    cases = cases.sort_values(
        [
            "invoice_timestamp",
            "case_id",
        ]
    ).reset_index(
        drop=True
    )

    manifest = make_manifest(
        cases,
        cutoff,
    )

    cases.to_csv(
        OUTPUT_CSV,
        index=False,
        compression="gzip",
    )

    MANIFEST_PATH.write_text(
        json.dumps(
            manifest,
            indent=2,
        )
    )

    banner(
        "BPI SURVIVAL DATASET COMPLETE"
    )

    print(
        f"Rows: "
        f"{len(cases):,}"
    )

    print(
        f"Observed clears: "
        f"{cases['event_observed'].sum():,}"
    )

    print(
        f"Censored: "
        f"{(cases['event_observed'] == 0).sum():,}"
    )

    print()
    print(
        "Duration distribution:"
    )

    print(
        cases[
            "duration_days"
        ]
        .describe(
            percentiles=[
                0.25,
                0.50,
                0.75,
                0.90,
                0.95,
                0.99,
            ]
        )
        .to_string()
    )

    print()
    print(
        "Horizon support:"
    )

    for horizon in HORIZONS:

        c = (
            f"clear_within_{horizon}d"
        )

        known = (
            cases[c]
            .notna()
        )

        print(
            f"  {horizon:>2}d  "
            f"known={known.sum():>7,}  "
            f"unknown_censored={(~known).sum():>7,}  "
            f"clear_rate="
            f"{cases.loc[known, c].mean():.2%}"
        )

    print()
    print(
        "Output:"
    )

    print(
        " ",
        OUTPUT_CSV.resolve()
    )

    print(
        " ",
        MANIFEST_PATH.resolve()
    )


if __name__ == "__main__":
    main()
