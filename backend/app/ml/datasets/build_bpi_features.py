"""
RecoverIQ — BPI Challenge 2019 Feature Pipeline
================================================

Purpose
-------
Transform the real BPI Challenge 2019 SAP purchase-to-pay event log into
a leakage-safe invoice-clearing ML dataset.

Prediction point
----------------
Record Invoice Receipt

Targets
-------
clearing_days
    Time from invoice receipt to Clear Invoice.

slow_clear_45d
    1 if clearing takes more than 45 days.

Important
---------
BPI 2019 does NOT contain a contractual invoice due date.

Therefore:
    slow_clear_45d != late_payment

It measures operational clearing latency, not contractual lateness.

Data-quality policy
-------------------
1. Keep only cases containing exactly:
       1 Record Invoice Receipt
       1 Clear Invoice

2. Remove cases containing:
       Cancel Invoice Receipt

3. Remove impossible target rows:
       clearing_date < invoice_receipt_date

4. Restrict pre-invoice process features to a 730-day lookback window.

   This prevents legacy timestamp anomalies such as events from
   1948/1993/2001 from creating absurd process-age features.

5. Historical vendor outcomes become available only after their actual
   clearing timestamp is earlier than the current invoice receipt.

This prevents target leakage.
"""

from __future__ import annotations

from collections import deque
import heapq
import json
from pathlib import Path

import numpy as np
import pandas as pd
import pm4py


# ============================================================
# PATHS
# ============================================================

PROJECT_ROOT = Path(__file__).resolve().parents[4]

RAW_DIR = (
    PROJECT_ROOT
    / "data"
    / "raw"
    / "bpi_2019"
)

OUTPUT_DIR = (
    PROJECT_ROOT
    / "data"
    / "processed"
    / "bpi_2019"
)

OUTPUT_PATH = (
    OUTPUT_DIR
    / "bpi_invoice_features.csv.gz"
)

MANIFEST_PATH = (
    OUTPUT_DIR
    / "bpi_invoice_features_manifest.json"
)


# ============================================================
# BPI SCHEMA
# ============================================================

CASE = "case:concept:name"
ACTIVITY = "concept:name"
TIME = "time:timestamp"
AMOUNT = "Cumulative net worth (EUR)"

INVOICE_ACTIVITY = "Record Invoice Receipt"
CLEAR_ACTIVITY = "Clear Invoice"
CANCEL_ACTIVITY = "Cancel Invoice Receipt"


# ============================================================
# MODEL DEFINITION
# ============================================================

TARGET_COLUMN = "slow_clear_45d"

TARGET_DAYS = 45

PRE_INVOICE_LOOKBACK_DAYS = 730


# ============================================================
# STATIC CASE ATTRIBUTES
# ============================================================

STATIC_COLUMNS = [
    "case:Vendor",
    "case:Company",
    "case:Document Type",
    "case:Purchasing Document",
    "case:Purch. Doc. Category name",
    "case:Item Type",
    "case:Item Category",
    "case:Spend classification text",
    "case:Spend area text",
    "case:Sub spend area text",
    "case:Source",
    "case:GR-Based Inv. Verif.",
    "case:Goods Receipt",
    "case:Item",
]


# ============================================================
# PRE-INVOICE PROCESS FEATURES
# ============================================================

PRE_INVOICE_ACTIVITIES = {
    "Record Goods Receipt":
        "goods_receipt_count_before_invoice",

    "Record Service Entry Sheet":
        "service_entry_count_before_invoice",

    "Create Purchase Requisition Item":
        "purchase_requisition_count_before_invoice",

    "Receive Order Confirmation":
        "order_confirmation_count_before_invoice",

    "Change Quantity":
        "quantity_change_count_before_invoice",

    "Change Price":
        "price_change_count_before_invoice",

    "Change Approval for Purchase Order":
        "approval_change_count_before_invoice",

    "Set Payment Block":
        "set_payment_block_count_before_invoice",

    "Remove Payment Block":
        "remove_payment_block_count_before_invoice",

    "Vendor creates invoice":
        "vendor_invoice_created_count_before_receipt",
}


# ============================================================
# UTILITIES
# ============================================================

def banner(title: str) -> None:
    print()
    print("=" * 72)
    print(title)
    print("=" * 72)


def locate_raw_file() -> Path:

    candidates = (
        list(RAW_DIR.glob("*.xes"))
        + list(RAW_DIR.glob("*.xes.gz"))
    )

    if not candidates:
        raise FileNotFoundError(
            f"No XES/XES.GZ dataset found in {RAW_DIR}"
        )

    return max(
        candidates,
        key=lambda p: p.stat().st_size,
    )


def normalize_column_name(value: str) -> str:

    return (
        value
        .replace("case:", "")
        .lower()
        .replace(" ", "_")
        .replace(".", "")
        .replace("-", "_")
    )


# ============================================================
# LOAD
# ============================================================

def load_events() -> pd.DataFrame:

    path = locate_raw_file()

    banner("1. LOADING BPI 2019")

    print("Source:", path)
    print(
        "File size:",
        round(path.stat().st_size / 1024 / 1024, 1),
        "MB",
    )

    df = pm4py.read_xes(
        str(path)
    )

    required = {
        CASE,
        ACTIVITY,
        TIME,
        AMOUNT,
        "case:Vendor",
        "case:Company",
        "case:Item Category",
    }

    missing = required - set(df.columns)

    if missing:
        raise ValueError(
            "Missing required columns: "
            f"{sorted(missing)}"
        )

    df[TIME] = pd.to_datetime(
        df[TIME],
        utc=True,
        errors="coerce",
    )

    invalid_times = int(
        df[TIME].isna().sum()
    )

    if invalid_times:
        raise ValueError(
            f"{invalid_times} invalid timestamps found"
        )

    print(f"Events: {len(df):,}")
    print(
        "Cases:",
        f"{df[CASE].nunique():,}",
    )
    print(
        "Vendors:",
        f"{df['case:Vendor'].nunique():,}",
    )

    return df


# ============================================================
# CASE POPULATION
# ============================================================

def build_activity_counts(
    df: pd.DataFrame,
) -> pd.DataFrame:

    return (
        df.groupby(
            [CASE, ACTIVITY]
        )
        .size()
        .unstack(fill_value=0)
    )


def activity_count(
    counts: pd.DataFrame,
    name: str,
) -> pd.Series:

    if name in counts.columns:
        return counts[name]

    return pd.Series(
        0,
        index=counts.index,
        dtype=int,
    )


def select_candidate_cases(
    counts: pd.DataFrame,
) -> tuple[pd.Index, dict]:

    invoice_count = activity_count(
        counts,
        INVOICE_ACTIVITY,
    )

    clear_count = activity_count(
        counts,
        CLEAR_ACTIVITY,
    )

    cancel_count = activity_count(
        counts,
        CANCEL_ACTIVITY,
    )

    mask = (
        (invoice_count == 1)
        & (clear_count == 1)
        & (cancel_count == 0)
    )

    audit = {
        "all_cases": int(
            len(counts)
        ),

        "cases_with_invoice": int(
            (invoice_count > 0).sum()
        ),

        "cases_with_clear": int(
            (clear_count > 0).sum()
        ),

        "cases_with_multiple_invoices": int(
            (invoice_count > 1).sum()
        ),

        "cases_with_multiple_clears": int(
            (clear_count > 1).sum()
        ),

        "cases_with_invoice_cancellation": int(
            (cancel_count > 0).sum()
        ),

        "candidate_cases": int(
            mask.sum()
        ),
    }

    return counts.index[mask], audit


# ============================================================
# TARGET TABLE
# ============================================================

def build_case_table(
    events: pd.DataFrame,
    candidate_ids: pd.Index,
) -> tuple[pd.DataFrame, dict]:

    df = events[
        events[CASE].isin(candidate_ids)
    ].copy()

    invoice_events = (
        df[
            df[ACTIVITY]
            == INVOICE_ACTIVITY
        ]
        .set_index(CASE)
    )

    clear_events = (
        df[
            df[ACTIVITY]
            == CLEAR_ACTIVITY
        ]
        .set_index(CASE)
    )

    case_first = (
        df.sort_values(TIME)
        .drop_duplicates(CASE)
        .set_index(CASE)
    )

    result = pd.DataFrame(
        index=candidate_ids
    )

    result["invoice_receipt_date"] = (
        invoice_events[TIME]
    )

    result["clearing_date"] = (
        clear_events[TIME]
    )

    result["clearing_days"] = (
        (
            result["clearing_date"]
            - result["invoice_receipt_date"]
        )
        .dt.total_seconds()
        / 86400
    )

    impossible = (
        result["clearing_days"] < 0
    )

    impossible_count = int(
        impossible.sum()
    )

    result = (
        result.loc[~impossible]
        .copy()
    )

    result[TARGET_COLUMN] = (
        result["clearing_days"]
        > TARGET_DAYS
    ).astype(int)

    for column in STATIC_COLUMNS:

        if column not in case_first.columns:
            continue

        clean_name = normalize_column_name(
            column
        )

        result[clean_name] = (
            case_first[column]
            .reindex(result.index)
        )

    # Amount known at invoice receipt.
    result["amount"] = (
        invoice_events[AMOUNT]
        .reindex(result.index)
        .astype(float)
    )

    audit = {
        "candidate_cases": int(
            len(candidate_ids)
        ),

        "impossible_negative_clearing_rows_removed":
            impossible_count,

        "valid_cases": int(
            len(result)
        ),
    }

    return result, audit


# ============================================================
# PRE-INVOICE PROCESS FEATURES
# ============================================================

def build_pre_invoice_features(
    events: pd.DataFrame,
    cases: pd.DataFrame,
) -> pd.DataFrame:

    result = cases.copy()

    subset = events[
        events[CASE].isin(
            result.index
        )
    ].copy()

    subset["_invoice_time"] = (
        subset[CASE]
        .map(
            result[
                "invoice_receipt_date"
            ]
        )
    )

    lower_bound = (
        subset["_invoice_time"]
        - pd.Timedelta(
            days=PRE_INVOICE_LOOKBACK_DAYS
        )
    )

    pre = subset[
        (
            subset[TIME]
            <= subset["_invoice_time"]
        )
        &
        (
            subset[TIME]
            >= lower_bound
        )
    ].copy()

    result["events_before_invoice"] = (
        pre.groupby(CASE)
        .size()
        .reindex(result.index)
        .fillna(0)
        .astype(int)
    )

    result[
        "unique_resources_before_invoice"
    ] = (
        pre.groupby(CASE)["org:resource"]
        .nunique(dropna=True)
        .reindex(result.index)
        .fillna(0)
        .astype(int)
    )

    table = (
        pre.groupby(
            [CASE, ACTIVITY]
        )
        .size()
        .unstack(fill_value=0)
    )

    for (
        activity,
        feature_name,
    ) in PRE_INVOICE_ACTIVITIES.items():

        if activity in table.columns:

            values = table[
                activity
            ]

        else:

            values = pd.Series(
                0,
                index=table.index,
            )

        result[feature_name] = (
            values
            .reindex(result.index)
            .fillna(0)
            .astype(int)
        )

    first_valid_event = (
        pre.groupby(CASE)[TIME]
        .min()
    )

    result[
        "case_age_days_at_invoice"
    ] = (
        (
            result[
                "invoice_receipt_date"
            ]
            - first_valid_event.reindex(
                result.index
            )
        )
        .dt.total_seconds()
        / 86400
    )

    goods = pre[
        pre[ACTIVITY]
        == "Record Goods Receipt"
    ]

    first_goods = (
        goods.groupby(CASE)[TIME]
        .min()
    )

    result[
        "has_goods_receipt_before_invoice"
    ] = (
        first_goods
        .reindex(result.index)
        .notna()
        .astype(int)
    )

    result[
        "days_gr_to_invoice"
    ] = (
        (
            result[
                "invoice_receipt_date"
            ]
            - first_goods.reindex(
                result.index
            )
        )
        .dt.total_seconds()
        / 86400
    )

    result[
        "days_gr_to_invoice"
    ] = (
        result[
            "days_gr_to_invoice"
        ]
        .where(
            result[
                "days_gr_to_invoice"
            ] >= 0
        )
    )

    result[
        "pre_invoice_rework_count"
    ] = (
        result[
            [
                "quantity_change_count_before_invoice",
                "price_change_count_before_invoice",
                "approval_change_count_before_invoice",
            ]
        ]
        .sum(axis=1)
    )

    return result


# ============================================================
# LEAKAGE-SAFE VENDOR HISTORY
# ============================================================

def add_vendor_history(
    frame: pd.DataFrame,
) -> pd.DataFrame:

    result = frame.copy()

    columns = [
        "vendor_prior_invoice_count",
        "vendor_prior_resolved_count",
        "vendor_prior_open_count",
        "vendor_open_invoice_ratio",

        "vendor_has_resolved_history",

        "vendor_historical_slow_rate_45d",
        "vendor_historical_avg_clearing_days",

        "vendor_recent_5_slow_rate_45d",
        "vendor_recent_5_avg_clearing_days",

        "vendor_tenure_days",
        "vendor_days_since_previous_invoice",

        "vendor_historical_avg_amount",
        "amount_vs_vendor_historical_avg",
    ]

    for column in columns:
        result[column] = np.nan

    result["_row_id"] = np.arange(
        len(result)
    )

    for _, group in result.groupby(
        "vendor",
        sort=False,
        dropna=False,
    ):

        group = group.sort_values(
            [
                "invoice_receipt_date",
                "_row_id",
            ],
            kind="stable",
        )

        first_invoice_date = (
            group[
                "invoice_receipt_date"
            ]
            .min()
        )

        previous_invoice_date = None

        prior_invoice_count = 0
        prior_amount_sum = 0.0

        resolved_count = 0
        slow_count = 0
        clearing_days_sum = 0.0

        recent = deque(
            maxlen=5
        )

        unresolved = []

        for (
            invoice_time,
            batch,
        ) in group.groupby(
            "invoice_receipt_date",
            sort=True,
        ):

            # Only expose an old invoice outcome once the old invoice
            # has actually cleared BEFORE this new invoice appears.
            while (
                unresolved
                and unresolved[0][0]
                < invoice_time
            ):

                (
                    clear_time,
                    clearing_days,
                    slow,
                ) = heapq.heappop(
                    unresolved
                )

                resolved_count += 1
                slow_count += slow
                clearing_days_sum += (
                    clearing_days
                )

                recent.append(
                    {
                        "slow": slow,
                        "days": clearing_days,
                    }
                )

            open_count = len(
                unresolved
            )

            if resolved_count:

                historical_slow_rate = (
                    slow_count
                    / resolved_count
                )

                historical_avg_days = (
                    clearing_days_sum
                    / resolved_count
                )

                recent_slow_rate = (
                    np.mean(
                        [
                            x["slow"]
                            for x in recent
                        ]
                    )
                    if recent
                    else np.nan
                )

                recent_avg_days = (
                    np.mean(
                        [
                            x["days"]
                            for x in recent
                        ]
                    )
                    if recent
                    else np.nan
                )

                has_history = 1

            else:

                historical_slow_rate = np.nan
                historical_avg_days = np.nan
                recent_slow_rate = np.nan
                recent_avg_days = np.nan
                has_history = 0

            if prior_invoice_count:

                historical_avg_amount = (
                    prior_amount_sum
                    / prior_invoice_count
                )

                open_ratio = (
                    open_count
                    / prior_invoice_count
                )

            else:

                historical_avg_amount = np.nan
                open_ratio = 0.0

            tenure_days = (
                invoice_time
                - first_invoice_date
            ).total_seconds() / 86400

            if previous_invoice_date is None:

                days_since_previous = np.nan

            else:

                days_since_previous = (
                    invoice_time
                    - previous_invoice_date
                ).total_seconds() / 86400

            idx = batch.index

            result.loc[
                idx,
                "vendor_prior_invoice_count",
            ] = prior_invoice_count

            result.loc[
                idx,
                "vendor_prior_resolved_count",
            ] = resolved_count

            result.loc[
                idx,
                "vendor_prior_open_count",
            ] = open_count

            result.loc[
                idx,
                "vendor_open_invoice_ratio",
            ] = open_ratio

            result.loc[
                idx,
                "vendor_has_resolved_history",
            ] = has_history

            result.loc[
                idx,
                "vendor_historical_slow_rate_45d",
            ] = historical_slow_rate

            result.loc[
                idx,
                "vendor_historical_avg_clearing_days",
            ] = historical_avg_days

            result.loc[
                idx,
                "vendor_recent_5_slow_rate_45d",
            ] = recent_slow_rate

            result.loc[
                idx,
                "vendor_recent_5_avg_clearing_days",
            ] = recent_avg_days

            result.loc[
                idx,
                "vendor_tenure_days",
            ] = tenure_days

            result.loc[
                idx,
                "vendor_days_since_previous_invoice",
            ] = days_since_previous

            result.loc[
                idx,
                "vendor_historical_avg_amount",
            ] = historical_avg_amount

            if (
                pd.notna(
                    historical_avg_amount
                )
                and historical_avg_amount != 0
            ):

                result.loc[
                    idx,
                    "amount_vs_vendor_historical_avg",
                ] = (
                    batch["amount"]
                    / historical_avg_amount
                )

            for row in batch.itertuples():

                prior_invoice_count += 1

                if pd.notna(
                    row.amount
                ):
                    prior_amount_sum += float(
                        row.amount
                    )

                heapq.heappush(
                    unresolved,
                    (
                        row.clearing_date,
                        float(
                            row.clearing_days
                        ),
                        int(
                            getattr(
                                row,
                                TARGET_COLUMN,
                            )
                        ),
                    ),
                )

            previous_invoice_date = (
                invoice_time
            )

    return result.drop(
        columns=["_row_id"]
    )


# ============================================================
# DATA QUALITY REPORT
# ============================================================

def quality_report(
    features: pd.DataFrame,
) -> dict:

    clearing = features[
        "clearing_days"
    ]

    return {
        "rows": int(
            len(features)
        ),

        "vendors": int(
            features[
                "vendor"
            ].nunique(
                dropna=True
            )
        ),

        "date_min": str(
            features[
                "invoice_receipt_date"
            ].min()
        ),

        "date_max": str(
            features[
                "invoice_receipt_date"
            ].max()
        ),

        "target_positive_count": int(
            features[
                TARGET_COLUMN
            ].sum()
        ),

        "target_negative_count": int(
            (
                features[
                    TARGET_COLUMN
                ] == 0
            ).sum()
        ),

        "target_positive_rate": float(
            features[
                TARGET_COLUMN
            ].mean()
        ),

        "clearing_days": {
            "mean": float(
                clearing.mean()
            ),

            "median": float(
                clearing.median()
            ),

            "p90": float(
                clearing.quantile(.90)
            ),

            "p95": float(
                clearing.quantile(.95)
            ),

            "p99": float(
                clearing.quantile(.99)
            ),

            "max": float(
                clearing.max()
            ),
        },
    }


# ============================================================
# MAIN
# ============================================================

def main() -> None:

    OUTPUT_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    events = load_events()

    banner(
        "2. SELECTING VALID INVOICE-CLEARING CASES"
    )

    counts = build_activity_counts(
        events
    )

    (
        candidate_ids,
        population_audit,
    ) = select_candidate_cases(
        counts
    )

    for key, value in (
        population_audit.items()
    ):
        print(
            f"{key:<40} {value:,}"
        )

    banner(
        "3. BUILDING TARGET"
    )

    (
        cases,
        target_audit,
    ) = build_case_table(
        events,
        candidate_ids,
    )

    for key, value in (
        target_audit.items()
    ):
        print(
            f"{key:<45} {value:,}"
        )

    print()
    print(
        "Slow-clear >45d rate:",
        f"{cases[TARGET_COLUMN].mean():.2%}",
    )

    banner(
        "4. BUILDING PRE-INVOICE PROCESS FEATURES"
    )

    features = (
        build_pre_invoice_features(
            events,
            cases,
        )
    )

    print(
        "Pre-invoice lookback:",
        PRE_INVOICE_LOOKBACK_DAYS,
        "days",
    )

    banner(
        "5. BUILDING LEAKAGE-SAFE VENDOR HISTORY"
    )

    features = (
        add_vendor_history(
            features
        )
    )

    features = (
        features
        .reset_index()
        .rename(
            columns={
                CASE: "bpi_case_id"
            }
        )
    )

    features = (
        features
        .sort_values(
            [
                "invoice_receipt_date",
                "bpi_case_id",
            ],
            kind="stable",
        )
        .reset_index(
            drop=True
        )
    )

    banner(
        "6. FINAL DATA QUALITY"
    )

    report = quality_report(
        features
    )

    print(
        "Rows:",
        f"{report['rows']:,}",
    )

    print(
        "Vendors:",
        f"{report['vendors']:,}",
    )

    print(
        "Date range:",
        report["date_min"],
        "->",
        report["date_max"],
    )

    print(
        "Slow-clear >45d:",
        f"{report['target_positive_rate']:.2%}",
    )

    print()
    print("Clearing-days distribution:")

    for key, value in (
        report["clearing_days"]
        .items()
    ):
        print(
            f"  {key:<10}"
            f"{value:>10.2f}"
        )

    banner(
        "7. SAVING ARTIFACTS"
    )

    features.to_csv(
        OUTPUT_PATH,
        index=False,
        compression="gzip",
    )

    manifest = {
        "pipeline_version": 1,

        "dataset": (
            "BPI Challenge 2019"
        ),

        "source_file": (
            locate_raw_file().name
        ),

        "prediction_point": (
            INVOICE_ACTIVITY
        ),

        "target": {
            "regression": (
                "clearing_days"
            ),

            "classification": (
                TARGET_COLUMN
            ),

            "threshold_days":
                TARGET_DAYS,

            "important_note": (
                "BPI does not provide contractual due dates. "
                "This target measures operational clearing "
                "latency and must not be described as contractual "
                "late payment."
            ),
        },

        "filters": {
            "exact_invoice_receipts": 1,
            "exact_clear_events": 1,
            "invoice_cancellations": 0,
            "require_non_negative_clearing_days": True,
            "pre_invoice_lookback_days":
                PRE_INVOICE_LOOKBACK_DAYS,
        },

        "population_audit":
            population_audit,

        "target_audit":
            target_audit,

        "final_dataset":
            report,

        "leakage_policy": (
            "Historical vendor payment outcomes are available "
            "only when the historical clearing timestamp is "
            "strictly earlier than the current invoice receipt."
        ),
    }

    MANIFEST_PATH.write_text(
        json.dumps(
            manifest,
            indent=2,
        )
    )

    print(
        "Feature dataset:"
    )
    print(
        " ",
        OUTPUT_PATH,
    )

    print(
        "Audit manifest:"
    )
    print(
        " ",
        MANIFEST_PATH,
    )

    print()
    print(
        "Final rows:",
        f"{len(features):,}",
    )

    print(
        "Final columns:",
        len(
            features.columns
        ),
    )

    banner(
        "BPI FEATURE PIPELINE COMPLETE"
    )


if __name__ == "__main__":
    main()
