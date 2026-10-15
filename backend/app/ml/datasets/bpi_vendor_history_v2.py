"""Experimental, order-invariant BPI vendor-history features.

This module is deliberately separate from the persisted BPI survival feature
builder and model contract. It uses receipt-time cohorts: invoices sharing a
vendor and prediction timestamp see the same historical snapshot. An outcome
enters history only when its clear timestamp is strictly earlier than the
prediction timestamp.
"""

from __future__ import annotations

import heapq
import math
from collections import deque
from typing import Any

import numpy as np
import pandas as pd


OUTPUT_COLUMNS = (
    "vendor_prior_invoice_count",
    "vendor_prior_resolved_count",
    "vendor_prior_open_count",
    "vendor_historical_clear_rate",
    "vendor_historical_avg_clear_days",
    "vendor_recent5_avg_clear_days",
    "vendor_days_since_previous_invoice",
    "vendor_historical_avg_invoice_amount",
    "amount_vs_vendor_historical_avg",
    "vendor_has_resolved_history",
)


def add_vendor_history_v2(
    cases: pd.DataFrame,
    *,
    identity: str = "case_id",
    vendor: str = "vendor",
    prediction_time: str = "invoice_timestamp",
    clear_time: str = "clear_timestamp",
    event_observed: str = "event_observed",
    amount: str = "invoice_amount_eur",
    currency: str | None = None,
) -> pd.DataFrame:
    """Return experimental vendor-history columns without changing input.

    A vendor/currency partition is used for amount aggregates when ``currency``
    exists, or amounts are treated as the single currency supplied by the
    canonical BPI EUR dataset. Non-amount payment history stays vendor scoped.
    Rows with missing vendor or invalid/missing prediction timestamps receive
    missing history and do not contribute to another invoice's history.
    """
    required = {identity, vendor, prediction_time, clear_time, event_observed, amount}
    missing = required.difference(cases.columns)
    if missing:
        raise ValueError(f"Missing BPI vendor-history columns: {sorted(missing)}")
    if cases[identity].isna().any() or cases[identity].duplicated().any():
        raise ValueError(f"{identity} must be present and unique")
    if currency is not None and currency not in cases.columns:
        raise ValueError(f"Currency column {currency!r} does not exist")

    result = cases.copy()
    for column in OUTPUT_COLUMNS:
        result[column] = np.nan

    prediction = pd.to_datetime(cases[prediction_time], utc=True, errors="coerce")
    clearing = pd.to_datetime(cases[clear_time], utc=True, errors="coerce")
    observed = pd.to_numeric(cases[event_observed], errors="coerce").eq(1)
    numeric_amount = pd.to_numeric(cases[amount], errors="coerce")
    valid = prediction.notna() & cases[vendor].notna()
    result["vendor_has_resolved_history"] = pd.Series(pd.NA, index=cases.index, dtype="Int64")

    # Grouping on the original index is unsafe when callers have duplicate index
    # labels. Work with positional row numbers while retaining source identity.
    work = pd.DataFrame({
        "_pos": np.arange(len(cases)),
        "_identity": cases[identity].to_numpy(),
        "_vendor": cases[vendor].to_numpy(),
        "_prediction": prediction.to_numpy(),
        "_clear": clearing.to_numpy(),
        "_observed": observed.to_numpy(),
        "_amount": numeric_amount.to_numpy(),
    })
    if currency is not None:
        work["_currency"] = cases[currency].astype("string").fillna("__MISSING_CURRENCY__").to_numpy()
    else:
        work["_currency"] = "EUR"
    work = work.loc[valid.to_numpy()]

    values: dict[str, np.ndarray] = {
        column: np.full(len(cases), np.nan, dtype=float) for column in OUTPUT_COLUMNS
    }
    for _, vendor_rows in work.groupby("_vendor", sort=False, dropna=True):
        vendor_rows = vendor_rows.sort_values(["_prediction", "_identity"], kind="stable")
        amount_state: dict[Any, tuple[float, int]] = {}
        prior_count = 0
        resolved_count = 0
        resolved_duration_sum = 0.0
        pending: list[tuple[int, float, str]] = []
        resolved_cohorts: deque[tuple[float, int]] = deque()
        previous_time: pd.Timestamp | None = None

        for timestamp, batch in vendor_rows.groupby("_prediction", sort=True):
            ts_ns = pd.Timestamp(timestamp).value
            while pending and pending[0][0] < ts_ns:
                clear_ns = pending[0][0]
                cohort: list[float] = []
                while pending and pending[0][0] == clear_ns:
                    _, duration, _ = heapq.heappop(pending)
                    cohort.append(duration)
                # Equal-time clears have no defensible within-cohort ordering.
                # Their mean is one deterministic clearance-time observation.
                cohort_total = math.fsum(cohort)
                cohort_mean = cohort_total / len(cohort)
                resolved_count += len(cohort)
                resolved_duration_sum = math.fsum((resolved_duration_sum, cohort_total))
                resolved_cohorts.append((cohort_mean, len(cohort)))

            open_count = max(0, prior_count - resolved_count)
            historical_avg = amount_state
            resolved_avg = (
                resolved_duration_sum / resolved_count if resolved_count else np.nan
            )
            recent_values: list[tuple[float, int]] = []
            remaining = 5
            for cohort_mean, cohort_size in reversed(resolved_cohorts):
                take = min(remaining, cohort_size)
                recent_values.append((cohort_mean, take))
                remaining -= take
                if remaining == 0:
                    break
            recent_avg = (
                float(sum(mean * count for mean, count in recent_values) /
                      sum(count for _, count in recent_values))
                if recent_values else np.nan
            )
            since_previous = (
                (timestamp - previous_time).total_seconds() / 86400.0
                if previous_time is not None else np.nan
            )

            amount_batch: dict[Any, list[float]] = {}
            for row in batch.itertuples(index=False, name=None):
                pos = int(row[0])
                currency_key = row[7]
                amount_value = row[6]
                avg_amount, amount_n = historical_avg.get(currency_key, (0.0, 0))
                values["vendor_prior_invoice_count"][pos] = prior_count
                values["vendor_prior_resolved_count"][pos] = resolved_count
                values["vendor_prior_open_count"][pos] = open_count
                values["vendor_historical_clear_rate"][pos] = (
                    resolved_count / prior_count if prior_count else np.nan
                )
                values["vendor_historical_avg_clear_days"][pos] = resolved_avg
                values["vendor_recent5_avg_clear_days"][pos] = recent_avg
                values["vendor_days_since_previous_invoice"][pos] = since_previous
                values["vendor_historical_avg_invoice_amount"][pos] = (
                    avg_amount / amount_n if amount_n else np.nan
                )
                values["amount_vs_vendor_historical_avg"][pos] = (
                    float(amount_value) / (avg_amount / amount_n)
                    if pd.notna(amount_value) and amount_n and avg_amount > 0 else np.nan
                )
                values["vendor_has_resolved_history"][pos] = int(resolved_count > 0)

            # Add known invoice amounts and future outcomes only after the
            # shared snapshot is emitted for the whole timestamp cohort.
            for row in batch.itertuples(index=False, name=None):
                pos = int(row[0])
                currency_key = row[7]
                amount_value = row[6]
                if pd.notna(amount_value):
                    amount_batch.setdefault(currency_key, []).append(float(amount_value))
                clear_value = row[4]
                if bool(row[5]) and not pd.isna(clear_value):
                    clear_stamp = pd.Timestamp(clear_value)
                    if clear_stamp > timestamp:
                        duration = (clear_stamp - timestamp).total_seconds() / 86400.0
                        heapq.heappush(pending, (clear_stamp.value, duration, str(row[1])))
                prior_count += 1
            for currency_key, amounts in amount_batch.items():
                old_sum, old_n = amount_state.get(currency_key, (0.0, 0))
                amount_state[currency_key] = (
                    math.fsum((old_sum, math.fsum(amounts))), old_n + len(amounts)
                )
            previous_time = pd.Timestamp(timestamp)

    for column, array in values.items():
        result[column] = array
    result["vendor_has_resolved_history"] = pd.Series(
        pd.array(values["vendor_has_resolved_history"], dtype="Int64"), index=cases.index
    )
    return result
