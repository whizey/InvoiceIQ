"""Offline numeric feature drift for InvoiceIQ model monitoring.

Does not read customer data into logs, retrain a model or take recovery actions.
PSI is an alerting diagnostic, not proof of performance degradation.
Run against a comparable *current* feature extract and an approved baseline.
"""

import argparse
import bisect
import csv
import gzip
import json
import math
from pathlib import Path


def _open_csv(path: Path):
    return gzip.open(path, "rt", newline="", encoding="utf-8-sig") if path.suffix == ".gz" else path.open(
        "r", newline="", encoding="utf-8-sig"
    )


def numeric_column(path: Path, column: str) -> tuple[list[float], int, int]:
    values: list[float] = []
    missing = 0
    count = 0
    with _open_csv(path) as handle:
        reader = csv.DictReader(handle)
        if not reader.fieldnames or column not in reader.fieldnames:
            raise ValueError(f"Missing numeric feature {column!r} in {path.name}")
        for row in reader:
            count += 1
            value = (row.get(column) or "").strip()
            try:
                number = float(value)
                if not math.isfinite(number):
                    raise ValueError
            except ValueError:
                missing += 1
                continue
            values.append(number)
    if not count:
        raise ValueError(f"No records in {path.name}")
    return values, missing, count


def population_stability_index(reference: list[float], current: list[float], bins: int = 10) -> float:
    if not reference or not current:
        raise ValueError("PSI requires numeric observations in both periods")
    sorted_ref = sorted(reference)
    edges = sorted({sorted_ref[int(i * (len(sorted_ref) - 1) / bins)] for i in range(1, bins)})
    n_bins = len(edges) + 1
    ref_count = [0] * n_bins
    current_count = [0] * n_bins
    for value in reference:
        ref_count[bisect.bisect_right(edges, value)] += 1
    for value in current:
        current_count[bisect.bisect_right(edges, value)] += 1
    epsilon = 1e-6
    psi = 0.0
    for r, c in zip(ref_count, current_count):
        p = max(r / len(reference), epsilon)
        q = max(c / len(current), epsilon)
        psi += (p - q) * math.log(p / q)
    return round(psi, 6)


def compare_feature(reference_file: Path, current_file: Path, name: str) -> dict:
    baseline, baseline_missing, baseline_rows = numeric_column(reference_file, name)
    latest, latest_missing, latest_rows = numeric_column(current_file, name)
    psi = population_stability_index(baseline, latest)
    return {
        "feature": name,
        "reference_rows": baseline_rows,
        "current_rows": latest_rows,
        "reference_missing_rate": round(baseline_missing / baseline_rows, 5),
        "current_missing_rate": round(latest_missing / latest_rows, 5),
        "psi": psi,
        "drift_warning": psi > 0.2,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--reference", type=Path, required=True)
    parser.add_argument("--current", type=Path, required=True)
    parser.add_argument("--features", nargs="+", required=True)
    parser.add_argument("--output", type=Path)
    options = parser.parse_args()
    report = {
        "reference_file": options.reference.name,
        "current_file": options.current.name,
        "features": [compare_feature(options.reference, options.current, feature) for feature in options.features],
        "note": "PSI is a diagnostic; investigate data, calibration, and actual outcomes before model promotion.",
    }
    document = json.dumps(report, indent=2) + "\n"
    if options.output:
        options.output.parent.mkdir(parents=True, exist_ok=True)
        options.output.write_text(document, encoding="utf-8")
        print(f"Drift report saved to {options.output}")
    else:
        print(document, end="")


if __name__ == "__main__":
    main()
