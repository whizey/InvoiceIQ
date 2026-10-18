"""Conditional process-clearance probabilities for BPI 2019."""

import math
from collections.abc import Mapping, Sequence


def _validated_points(curve: Mapping):
    if not curve:
        raise ValueError("Empty survival curve")

    points = sorted(
        (float(day), float(prob))
        for day, prob in curve.items()
    )

    previous_day = 0.0
    previous_prob = 0.0

    for day, probability in points:
        if not math.isfinite(day) or day <= previous_day:
            raise ValueError("Invalid survival checkpoint")

        if not math.isfinite(probability):
            raise ValueError("Non-finite probability")

        if not 0 <= probability <= 1:
            raise ValueError("Probability outside [0, 1]")

        if probability < previous_prob - 1e-10:
            raise ValueError("Clearance curve is not monotonic")

        previous_day = day
        previous_prob = probability

    return [(0.0, 0.0), *points]


def _survival_at(points, day):
    if (
        not math.isfinite(day)
        or day < 0
        or day > points[-1][0]
    ):
        raise ValueError("Day outside modeled horizon")

    for (start, p0), (end, p1) in zip(
        points, points[1:]
    ):
        if day <= end:
            if day == start:
                return 1.0 - p0

            if day == end:
                return 1.0 - p1

            left = math.log(max(1.0 - p0, 1e-15))
            right = math.log(max(1.0 - p1, 1e-15))

            fraction = (day - start) / (end - start)

            return math.exp(
                left + fraction * (right - left)
            )

    return 1.0 - points[-1][1]


def conditional_clearance_probabilities(
    recovery_curve: Mapping,
    *,
    elapsed_days_since_receipt: float,
    horizons: Sequence[int] = (7, 30, 60, 90),
):
    """P(clear in next h days | uncleared at current age)."""

    points = _validated_points(recovery_curve)
    age = float(elapsed_days_since_receipt)

    if not math.isfinite(age) or age < 0:
        raise ValueError("Invalid invoice age")

    if not horizons or any(
        isinstance(h, bool)
        or not isinstance(h, int)
        or h <= 0
        for h in horizons
    ):
        raise ValueError("Invalid forecast horizons")

    if age + max(horizons) > points[-1][0]:
        raise ValueError("Not enough modeled horizon")

    survival_now = _survival_at(points, age)

    if survival_now <= 1e-8:
        raise ValueError(
            "Cannot condition on near-zero survival"
        )

    results = {}

    for horizon in horizons:
        future_survival = _survival_at(
            points, age + horizon
        )

        probability = (
            1.0 - future_survival / survival_now
        )

        results[
            f"clearance_probability_next_{horizon}d"
        ] = float(
            max(0.0, min(1.0, probability))
        )

    return results
