import csv

import pytest

from app.ml.drift_monitor import compare_feature, population_stability_index


def test_identical_samples_have_zero_psi():
    sample = list(range(200))
    assert population_stability_index(sample, sample) == 0
    assert population_stability_index(sample, [n + 500 for n in sample]) > 0.2


def test_drift_monitor_reports_missingness_without_exposing_rows(tmp_path):
    baseline = tmp_path / "baseline.csv"
    current = tmp_path / "recent.csv"
    for target, values in ((baseline, [1, 2, 3, 4, 5]), (current, [2, 4, "", 9, 12])):
        with target.open("w", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=["payment_term"])
            writer.writeheader()
            writer.writerows({"payment_term": item} for item in values)
    result = compare_feature(baseline, current, "payment_term")
    assert result["current_rows"] == 5
    assert result["current_missing_rate"] == pytest.approx(0.2)
    assert "rows" not in result


def test_unknown_column_is_not_assumed(tmp_path):
    path = tmp_path / "a.csv"
    path.write_text("amount\n10\n")
    with pytest.raises(ValueError, match="Missing numeric feature"):
        compare_feature(path, path, "customer_secret")
