"""Expected collections per week, with a range, from how long this workspace's invoices take to get paid.

A Kaplan-Meier curve of time to settlement, measured from the due date, is fitted on settled invoices (unpaid
ones count as still waiting, not as never paying). Curves are kept per late-payment risk band when a band has
enough history. An open invoice already some days past due is conditioned on having waited that long, so the
chance it is paid in week k is (S(a + 7(k-1)) - S(a + 7k)) / S(a).

Treating invoices as independent gives too narrow a range (payments move together), so the range is widened to
cover how far off the same forecast was over the last few four-week periods, each made with only what was known
at the time. That track record is reported alongside.
"""
from __future__ import annotations

import math
from bisect import bisect_right
from datetime import date, timedelta
from decimal import Decimal

WEEKS = 8
BANDS = ((0.0, 0.4, "low"), (0.4, 0.7, "medium"), (0.7, 1.01, "high"))
MIN_BAND = 30  # settled invoices a risk band needs for its own curve
Z80 = 1.2816  # two-sided 80% interval
BACKTEST_DAYS = 28
TRACK_WINDOWS = 6  # past four-week periods the track record covers
MIN_TRACK = 4  # periods needed before the track record widens the range
EARLIEST = -120  # relative days before the due date the curve starts


class Curve:
    """Kaplan-Meier survival S(t): the chance an invoice is still unpaid t days after its due date."""

    def __init__(self, observations: list[tuple[int, bool]]):
        events: dict[int, int] = {}
        exits: dict[int, int] = {}
        for t, paid in observations:
            t = max(t, EARLIEST)
            exits[t] = exits.get(t, 0) + 1
            if paid:
                events[t] = events.get(t, 0) + 1
        at_risk = len(observations)
        survival = 1.0
        self.times, self.values = [], []
        for t in sorted(exits):
            if events.get(t):
                survival *= 1 - events[t] / at_risk
                self.times.append(t)
                self.values.append(survival)
            at_risk -= exits[t]
        self.size = len(observations)

    def __call__(self, t: int) -> float:
        index = bisect_right(self.times, t)
        return 1.0 if index == 0 else self.values[index - 1]


def _band(chance: float) -> str:
    return next(name for low, high, name in BANDS if low <= chance < high)


def fit(invoices, settled: dict, chance: dict, as_of: date) -> dict[str, Curve]:
    """Curves from what was known on `as_of`: settled by then is an event, still open is censored."""
    by_band: dict[str, list] = {"all": []}
    for row in invoices:
        if row.posting_date >= as_of:
            continue
        when = settled.get(row.id)
        observation = ((when - row.due_date).days, True) if when is not None and when < as_of else \
            ((as_of - row.due_date).days, False)
        by_band["all"].append(observation)
        by_band.setdefault(_band(chance[row.id]), []).append(observation)
    return {name: Curve(obs) for name, obs in by_band.items() if name == "all" or len(obs) >= MIN_BAND}


def _open_on(invoices, settled: dict, payments, as_of: date) -> list[tuple]:
    """(invoice, balance owed on `as_of`) for every invoice posted and not yet settled by then."""
    paid_before: dict = {}
    for p in payments:
        if p.paid_on < as_of:
            paid_before[p.invoice_id] = paid_before.get(p.invoice_id, Decimal(0)) + p.amount
    result = []
    for row in invoices:
        when = settled.get(row.id)
        if row.posting_date >= as_of or (when is not None and when < as_of):
            continue
        balance = row.amount_total - paid_before.get(row.id, Decimal(0))
        if balance > 0:
            result.append((row, float(balance)))
    return result


def _collect(open_rows, curves, chance: dict, as_of: date, first_day: int, last_day: int) -> tuple[float, float]:
    """Expected amount paid between `first_day` and `last_day` days after `as_of`, and its variance."""
    expected = variance = 0.0
    for row, balance in open_rows:
        curve = curves.get(_band(chance[row.id]), curves["all"])
        age = (as_of - row.due_date).days
        still_open = curve(age - 1)
        if still_open <= 0:
            continue
        p = (curve(age + first_day - 1) - curve(age + last_day)) / still_open
        expected += balance * p
        variance += balance * balance * p * (1 - p)
    return expected, variance


def _row(label_day: date, expected: float, variance: float) -> dict:
    spread = Z80 * math.sqrt(variance)
    return {"week_start": str(label_day), "expected": round(expected, 2),
            "low": round(max(0.0, expected - spread), 2), "high": round(expected + spread, 2)}


def _widen(row: dict, ratios: list[float]) -> dict:
    if len(ratios) < MIN_TRACK:
        return row
    low, high = row["expected"] * min(ratios), row["expected"] * max(ratios)
    return {**row, "low": round(min(row["low"], low), 2), "high": round(max(row["high"], high), 2)}


def _forecast(invoices, settled: dict, payments, chance: dict, as_of: date, weeks: int) -> dict:
    curves = fit(invoices, settled, chance, as_of)
    open_rows = _open_on(invoices, settled, payments, as_of)
    by_week = [_row(as_of + timedelta(days=7 * k), *_collect(open_rows, curves, chance, as_of, 7 * k, 7 * k + 6))
               for k in range(weeks)]
    month = _row(as_of, *_collect(open_rows, curves, chance, as_of, 0, BACKTEST_DAYS - 1))
    return {"weeks": by_week, "next_4_weeks": {k: month[k] for k in ("expected", "low", "high")},
            "open_balance": round(sum(b for _, b in open_rows), 2),
            "curves": {name: c.size for name, c in curves.items()}}


def track_record(invoices, settled: dict, payments, chance: dict, today: date) -> list[dict]:
    """The four-week forecast as it would have been made at the start of each recent four-week period, with
    only what was known then, against the money that actually came in from the invoices open at that time."""
    periods = []
    for back in range(1, TRACK_WINDOWS + 1):
        as_of = today - timedelta(days=BACKTEST_DAYS * back)
        end = as_of + timedelta(days=BACKTEST_DAYS)
        made = _forecast(invoices, settled, payments, chance, as_of, weeks=0)["next_4_weeks"]
        open_then = {row.id for row, _ in _open_on(invoices, settled, payments, as_of)}
        actual = sum((p.amount for p in payments if p.invoice_id in open_then and as_of <= p.paid_on < end),
                     Decimal(0))
        if made["expected"] > 0:
            periods.append({"from": str(as_of), "to": str(end - timedelta(days=1)), "expected": made["expected"],
                            "actual": float(actual)})
    return periods


def forecast(invoices, settled: dict, payments, chance: dict, today: date, weeks: int = WEEKS) -> dict:
    """Expected collections per week and over the next four weeks, ranges widened by the track record."""
    result = _forecast(invoices, settled, payments, chance, today, weeks)
    history = track_record(invoices, settled, payments, chance, today)
    ratios = [p["actual"] / p["expected"] for p in history]
    return {**result, "weeks": [_widen(w, ratios) for w in result["weeks"]],
            "next_4_weeks": _widen(result["next_4_weeks"], ratios), "track_record": history,
            "accuracy": {"periods": len(ratios), "min_ratio": round(min(ratios), 2) if ratios else None,
                         "max_ratio": round(max(ratios), 2) if ratios else None}}
