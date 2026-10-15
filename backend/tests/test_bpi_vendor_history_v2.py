"""Regression tests for the isolated experimental BPI feature contract."""

from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from app.ml.datasets.bpi_vendor_history_v2 import OUTPUT_COLUMNS, add_vendor_history_v2


def frame(rows):
    return pd.DataFrame(rows, columns=[
        "case_id", "vendor", "invoice_timestamp", "clear_timestamp",
        "event_observed", "invoice_amount_eur",
    ])


def two_same_time():
    return frame([
        ("a", "v", "2020-01-01T09:00:00Z", "2020-01-03T09:00:00Z", 1, 10),
        ("b", "v", "2020-01-01T09:00:00Z", "2020-01-04T09:00:00Z", 1, 20),
        ("c", "v", "2020-01-02T09:00:00Z", None, 0, 30),
    ])


def output(data):
    return add_vendor_history_v2(data).set_index("case_id")[list(OUTPUT_COLUMNS)].sort_index()


def test_two_same_vendor_timestamp_rows_share_one_snapshot():
    got = output(two_same_time())
    assert got.loc["a", "vendor_prior_invoice_count"] == got.loc["b", "vendor_prior_invoice_count"] == 0
    assert got.loc["a", "vendor_historical_avg_invoice_amount"] == got.loc["b", "vendor_historical_avg_invoice_amount"] or (
        pd.isna(got.loc["a", "vendor_historical_avg_invoice_amount"])
        and pd.isna(got.loc["b", "vendor_historical_avg_invoice_amount"])
    )


def test_ten_same_vendor_timestamp_rows_share_snapshot():
    data = frame([(f"c{i}", "v", "2020-01-01T09:00:00Z", None, 0, i + 1) for i in range(10)])
    got = output(data)
    assert got.vendor_prior_invoice_count.nunique() == 1
    assert got.vendor_prior_invoice_count.iloc[0] == 0


def test_identical_times_do_not_cross_vendor_boundaries():
    data = frame([
        ("a", "v1", "2020-01-01T00:00:00Z", None, 0, 10),
        ("b", "v2", "2020-01-02T00:00:00Z", None, 0, 20),
    ])
    got = output(data)
    assert got.vendor_prior_invoice_count.tolist() == [0, 0]


def test_input_row_shuffle_does_not_change_features():
    data = pd.concat([two_same_time(), frame([
        ("d", "v", "2020-01-05T00:00:00Z", "2020-01-06T00:00:00Z", 1, 40),
    ])], ignore_index=True)
    expected = output(data)
    for seed in (3, 17, 2026, 99173):
        pd.testing.assert_frame_equal(expected, output(data.sample(frac=1, random_state=seed)))


def test_future_clear_does_not_affect_earlier_invoice():
    got = output(two_same_time())
    assert got.loc["c", "vendor_prior_resolved_count"] == 0


def test_clear_strictly_before_cutoff_is_visible():
    data = frame([
        ("old", "v", "2020-01-01T00:00:00Z", "2020-01-02T00:00:00Z", 1, 10),
        ("new", "v", "2020-01-03T00:00:00Z", None, 0, 20),
    ])
    got = output(data)
    assert got.loc["new", "vendor_prior_resolved_count"] == 1
    assert got.loc["new", "vendor_historical_avg_clear_days"] == pytest.approx(1)


def test_clear_at_exact_prediction_timestamp_is_not_yet_observable():
    data = frame([
        ("old", "v", "2020-01-01T00:00:00Z", "2020-01-03T00:00:00Z", 1, 10),
        ("new", "v", "2020-01-03T00:00:00Z", None, 0, 20),
    ])
    got = output(data)
    assert got.loc["new", "vendor_prior_resolved_count"] == 0
    assert got.loc["new", "vendor_prior_open_count"] == 1


def test_clear_after_cutoff_remains_open():
    data = frame([
        ("old", "v", "2020-01-01T00:00:00Z", "2020-01-04T00:00:00Z", 1, 10),
        ("new", "v", "2020-01-03T00:00:00Z", None, 0, 20),
    ])
    got = output(data)
    assert got.loc["new", "vendor_prior_resolved_count"] == 0
    assert got.loc["new", "vendor_prior_open_count"] == 1


def test_current_invoice_own_future_outcome_is_excluded():
    got = output(two_same_time())
    assert got.loc["a", "vendor_prior_resolved_count"] == 0
    assert got.loc["a", "vendor_prior_invoice_count"] == 0


def test_first_vendor_invoice_has_explicit_empty_history():
    got = output(two_same_time())
    assert got.loc["a", "vendor_prior_invoice_count"] == 0
    assert got.loc["a", "vendor_has_resolved_history"] == 0
    assert pd.isna(got.loc["a", "vendor_historical_avg_clear_days"])


def test_missing_or_malformed_prediction_time_gets_missing_history():
    data = frame([
        ("a", "v", None, "2020-01-02T00:00:00Z", 1, 10),
        ("b", "v", "not-a-date", None, 0, 20),
    ])
    got = output(data)
    assert got.loc["a", "vendor_prior_invoice_count"] is np.nan or pd.isna(got.loc["a", "vendor_prior_invoice_count"])
    assert pd.isna(got.loc["b", "vendor_prior_invoice_count"])


def test_missing_vendor_has_explicit_unknown_history_and_no_shared_bucket():
    data = frame([
        ("missing-vendor", None, "2020-01-01T00:00:00Z", None, 0, 10),
        ("known-vendor", "v", "2020-01-02T00:00:00Z", None, 0, 20),
    ])
    got = output(data)
    assert pd.isna(got.loc["missing-vendor", "vendor_prior_invoice_count"])
    assert pd.isna(got.loc["missing-vendor", "vendor_has_resolved_history"])
    assert got.loc["known-vendor", "vendor_prior_invoice_count"] == 0


def test_duplicate_case_identity_is_rejected():
    data = two_same_time()
    data.loc[1, "case_id"] = "a"
    with pytest.raises(ValueError, match="unique"):
        add_vendor_history_v2(data)


def test_amounts_are_aggregated_only_within_currency():
    data = frame([
        ("a", "v", "2020-01-01T00:00:00Z", None, 0, 100),
        ("b", "v", "2020-01-02T00:00:00Z", None, 0, 10),
    ])
    data["currency"] = ["EUR", "USD"]
    got = add_vendor_history_v2(data, currency="currency").set_index("case_id")
    assert pd.isna(got.loc["b", "vendor_historical_avg_invoice_amount"])


def test_full_timestamp_precision_separates_events():
    data = frame([
        ("a", "v", "2020-01-01T09:00:00Z", None, 0, 10),
        ("b", "v", "2020-01-01T09:00:01Z", None, 0, 20),
    ])
    got = output(data)
    assert got.loc["b", "vendor_prior_invoice_count"] == 1


def test_day_precision_tie_does_not_invent_order():
    data = frame([
        ("a", "v", "2020-01-01", None, 0, 10),
        ("b", "v", "2020-01-01", None, 0, 20),
    ])
    assert output(data).vendor_prior_invoice_count.tolist() == [0, 0]


def test_boundary_rows_only_see_strictly_earlier_receipts():
    data = frame([
        ("train", "v", "2020-01-01T00:00:00Z", None, 0, 10),
        ("boundary", "v", "2020-02-01T00:00:00Z", None, 0, 20),
        ("validation", "v", "2020-02-01T00:00:00Z", None, 0, 30),
    ])
    got = output(data)
    assert got.loc["boundary", "vendor_prior_invoice_count"] == 1
    assert got.loc["validation", "vendor_prior_invoice_count"] == 1


def test_later_validation_invoice_cannot_change_training_snapshot():
    train_only = frame([
        ("train", "v", "2020-01-01T00:00:00Z", None, 0, 10),
    ])
    with_validation = pd.concat([train_only, frame([
        ("validation", "v", "2020-02-01T00:00:00Z", None, 0, 1000),
    ])], ignore_index=True)
    first = output(train_only).loc["train"]
    second = output(with_validation).loc["train"]
    pd.testing.assert_series_equal(first, second)


def test_current_target_fields_do_not_change_its_own_snapshot():
    data = frame([
        ("a", "v", "2020-01-01T00:00:00Z", "2020-01-10T00:00:00Z", 1, 10),
        ("later", "v", "2020-01-05T00:00:00Z", None, 0, 20),
    ])
    changed = data.copy()
    changed.loc[0, "clear_timestamp"] = "2020-01-03T00:00:00Z"
    changed.loc[0, "event_observed"] = 0
    pd.testing.assert_series_equal(output(data).loc["a"], output(changed).loc["a"])


def test_vendor_group_holdout_does_not_import_other_vendor_history():
    data = frame([
        ("seen", "v1", "2020-01-01T00:00:00Z", "2020-01-02T00:00:00Z", 1, 10),
        ("held", "v2", "2020-01-03T00:00:00Z", None, 0, 20),
    ])
    got = output(data)
    assert got.loc["held", "vendor_prior_invoice_count"] == 0
    assert got.loc["held", "vendor_prior_resolved_count"] == 0


def test_censored_invoice_does_not_create_clearance_outcome():
    data = frame([
        ("censored", "v", "2020-01-01T00:00:00Z", "2020-01-02T00:00:00Z", 0, 10),
        ("later", "v", "2020-01-03T00:00:00Z", None, 0, 20),
    ])
    got = output(data)
    assert got.loc["later", "vendor_prior_resolved_count"] == 0


def test_clearance_tie_at_recent_five_boundary_uses_cohort_mean():
    data = frame([
        ("old", "v", "2020-01-01T00:00:00Z", "2020-01-03T00:00:00Z", 1, 1),
        ("t1", "v", "2020-01-01T00:00:00Z", "2020-01-15T00:00:00Z", 1, 1),
        ("t2", "v", "2019-12-26T00:00:00Z", "2020-01-15T00:00:00Z", 1, 1),
        ("n1", "v", "2020-01-01T00:00:00Z", "2020-01-16T00:00:00Z", 1, 1),
        ("n2", "v", "2019-12-28T00:00:00Z", "2020-01-16T00:00:00Z", 1, 1),
        ("n3", "v", "2019-12-27T00:00:00Z", "2020-01-16T00:00:00Z", 1, 1),
        ("later", "v", "2020-01-20T00:00:00Z", None, 0, 1),
    ])
    got = output(data)
    # Latest three are 15, 19, 20 days. The next clearance-time cohort has
    # durations 14 and 20 days, represented by its mean for the two remaining slots.
    assert got.loc["later", "vendor_recent5_avg_clear_days"] == pytest.approx(17.6)


def test_malformed_clear_timestamp_never_becomes_resolved_history():
    data = frame([
        ("bad-clear", "v", "2020-01-01T00:00:00Z", "broken", 1, 1),
        ("later", "v", "2020-01-03T00:00:00Z", None, 0, 1),
    ])
    got = output(data)
    assert got.loc["later", "vendor_prior_resolved_count"] == 0


def test_repeat_runs_are_reproducible():
    data = two_same_time()
    pd.testing.assert_frame_equal(output(data), output(data.copy(deep=True)))


def test_real_bpi_sample_is_row_order_invariant():
    path = Path(__file__).parents[2] / "data/processed/bpi_2019/bpi_survival_features.csv.gz"
    if path.is_file():
        sample = pd.read_csv(path, nrows=5000)
    else:
        # CI does not distribute the 700 MB BPI data product. Exercise the
        # same input contract and large-cohort ordering invariant locally.
        n = 5000
        received = pd.date_range("2020-01-01", periods=250, freq="12h", tz="UTC")
        receipt = received.take(np.arange(n) % len(received))
        durations = np.arange(n) % 35 + 1
        clear = (receipt + pd.to_timedelta(durations, unit="D")).astype("object")
        clear = pd.Series(clear)
        clear.loc[np.arange(n) % 7 == 0] = pd.NaT
        sample = pd.DataFrame({
            "case_id": [f"fixture-{i}" for i in range(n)],
            "vendor": [f"vendor-{i % 43}" for i in range(n)],
            "invoice_timestamp": receipt,
            "clear_timestamp": clear,
            "event_observed": (np.arange(n) % 7 != 0).astype(int),
            "invoice_amount_eur": 100 + (np.arange(n) * 37) % 10000,
        })
    expected = add_vendor_history_v2(sample)
    cols = list(OUTPUT_COLUMNS)
    left = expected.set_index("case_id")[cols].sort_index()
    for seed in (17, 41, 2026, 99173):
        shuffled = add_vendor_history_v2(sample.sample(frac=1, random_state=seed))
        right = shuffled.set_index("case_id")[cols].sort_index()
        pd.testing.assert_frame_equal(left, right)


def test_existing_b2b_feature_builder_is_separate():
    from app.ml.datasets import build_primary_features
    assert hasattr(build_primary_features, "build")
    assert add_vendor_history_v2.__module__.endswith("bpi_vendor_history_v2")
