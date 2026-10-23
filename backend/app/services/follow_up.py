"""Who to chase today: a ranked follow-up list, and an honest choice between a simple baseline and the model.

The baseline is each customer's late share over its last few settled invoices, known at posting time; it is easy
to explain and works with little data. The model is used only when it ranks this workspace's own recent settled
invoices better than the baseline, and the comparison is reported either way, with calibration for whichever is
used. Lateness is strict: an invoice is late if it was settled after its due date, or is past due and unpaid.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal

BASELINE_WINDOW = 6  # settled invoices per customer the baseline averages over
PRIOR_WEIGHT = 2  # pseudo-invoices at the workspace late rate, so one late invoice is not "100% late"
EVAL_DAYS = 182  # compare on invoices due in roughly the last six months
MIN_CLASS = 15  # each outcome needs this many invoices before an AUC means anything
MODEL_MARGIN = 0.02  # the model must beat the baseline by this much AUC to be used
DUE_SOON_DAYS = 7
# The served model was refit on public-dataset invoices posted up to this date; a workspace holding those same
# invoices compares the model on data it has seen, which flatters it.
MODEL_TRAINED_THROUGH = date(2015, 12, 8)


def settled_on(invoices, payments) -> dict:
    """Date each fully paid invoice was settled: when its running payments first reached the total."""
    by_invoice = defaultdict(list)
    for payment in payments:
        by_invoice[payment.invoice_id].append(payment)
    result = {}
    for row in invoices:
        running = Decimal(0)
        for payment in sorted(by_invoice[row.id], key=lambda p: p.paid_on):
            running += payment.amount
            if running >= row.amount_total:
                result[row.id] = payment.paid_on
                break
    return result


def outcome(row, settled: date | None, today: date) -> bool | None:
    """True late, False on time, None while it can still go either way."""
    if settled is not None:
        return settled > row.due_date
    return True if row.due_date < today else None


def baseline_scores(invoices, settled: dict, today: date) -> dict:
    """Posting-time late probability per invoice from the customer's last settled invoices."""
    known = [outcome(r, settled.get(r.id), today) for r in invoices if settled.get(r.id) is not None]
    base_rate = sum(known) / len(known) if known else 0.3
    history = defaultdict(list)  # customer -> [(settled date, late)]
    for row in invoices:
        if row.id in settled:
            history[row.counterparty.casefold()].append((settled[row.id], settled[row.id] > row.due_date))
    for items in history.values():
        items.sort()
    scores = {}
    for row in invoices:
        prior = [late for when, late in history[row.counterparty.casefold()] if when < row.posting_date]
        recent = prior[-BASELINE_WINDOW:]
        scores[row.id] = (sum(recent) + base_rate * PRIOR_WEIGHT) / (len(recent) + PRIOR_WEIGHT)
    return scores


def auc(scores: list[float], labels: list[bool]) -> float | None:
    """Area under the ROC curve (Mann-Whitney, ties shared); None without enough of both outcomes."""
    positives = sum(labels)
    negatives = len(labels) - positives
    if positives < MIN_CLASS or negatives < MIN_CLASS:
        return None
    order = sorted(range(len(scores)), key=lambda i: scores[i])
    ranks = [0.0] * len(scores)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and scores[order[j + 1]] == scores[order[i]]:
            j += 1
        for k in range(i, j + 1):
            ranks[order[k]] = (i + j) / 2 + 1
        i = j + 1
    rank_sum = sum(r for r, label in zip(ranks, labels) if label)
    return (rank_sum - positives * (positives + 1) / 2) / (positives * negatives)


def calibration(scores: list[float], labels: list[bool]) -> list[dict]:
    """Predicted against actual late share, in five bands of predicted probability."""
    bands = []
    for low in (0.0, 0.2, 0.4, 0.6, 0.8):
        members = [label for score, label in zip(scores, labels)
                   if low <= score < low + 0.2 or (low == 0.8 and score == 1.0)]
        predicted = [score for score in scores if low <= score < low + 0.2 or (low == 0.8 and score == 1.0)]
        if members:
            bands.append({"band": f"{int(low * 100)}–{int(low * 100) + 20}%", "invoices": len(members),
                          "predicted": round(sum(predicted) / len(predicted), 3),
                          "actual": round(sum(members) / len(members), 3)})
    return bands


def choose_scorer(invoices, settled: dict, baseline: dict, today: date) -> dict:
    """Compare model and baseline on invoices due in the last six months whose outcome is known."""
    window = [r for r in invoices if today - timedelta(days=EVAL_DAYS) <= r.due_date < today
              and r.risk_probability is not None]
    labelled = [(r, outcome(r, settled.get(r.id), today)) for r in window]
    labelled = [(r, late) for r, late in labelled if late is not None]
    labels = [late for _, late in labelled]
    model = [float(r.risk_probability) for r, _ in labelled]
    simple = [baseline[r.id] for r, _ in labelled]
    model_auc, baseline_auc = auc(model, labels), auc(simple, labels)
    use_model = model_auc is not None and baseline_auc is not None and model_auc >= baseline_auc + MODEL_MARGIN
    used = "model" if use_model else "baseline"
    if model_auc is None or baseline_auc is None:
        verdict = (f"Not enough settled invoices in the last six months to compare ({len(labels)}); "
                   "using each customer's recent payment record.")
    elif use_model:
        verdict = (f"On your last six months, the model ranked late payers better than the simple average: "
                   f"AUC {model_auc:.2f} vs {baseline_auc:.2f}. Using the model.")
    else:
        verdict = (f"On your last six months, the model did not beat the simple average "
                   f"(AUC {model_auc:.2f} vs {baseline_auc:.2f}), so the simple average is used.")
    by_scorer = {"model": calibration(model, labels), "baseline": calibration(simple, labels)}
    seen = bool(labelled) and max(r.posting_date for r, _ in labelled) <= MODEL_TRAINED_THROUGH
    if seen:
        verdict += (" These invoices come from the data the model was trained on, so its score here is optimistic; "
                    "on unseen invoices it scored 0.71 against 0.52.")
    return {"used": used, "model_saw_these": seen, "model_auc": model_auc, "baseline_auc": baseline_auc, "evaluated": len(labels),
            "late_share": round(sum(labels) / len(labels), 3) if labels else None,
            "verdict": verdict, "calibration": by_scorer[used], "calibration_by_scorer": by_scorer,
            "calibration_note": _calibration_note(by_scorer[used])}


def _calibration_note(bands: list[dict]) -> str | None:
    """One sentence from the busiest high band, e.g. 'When we said 70%, 64% actually paid late.'"""
    high = [b for b in bands if b["predicted"] >= 0.4] or bands
    if not high:
        return None
    band = max(high, key=lambda b: b["invoices"])
    return (f"When we said {round(band['predicted'] * 100)}%, {round(band['actual'] * 100)}% actually paid late "
            f"({band['invoices']} invoices).")


def priority(balance: Decimal, chance: float, days_overdue: int) -> float:
    """Amount × chance of being late × days overdue: big, likely and old rises to the top."""
    return float(balance) * chance * max(days_overdue, 0)


def days(n: int) -> str:
    return f"{n} day" if n == 1 else f"{n} days"


def short_rupees(value) -> str:
    value = float(value)
    if value >= 1_00_00_000:
        return f"₹{value / 1_00_00_000:.1f}Cr"
    if value >= 1_00_000:
        return f"₹{value / 1_00_000:.1f}L"
    if value >= 1_000:
        return f"₹{value / 1_000:.0f}k"
    return f"₹{value:.0f}"
