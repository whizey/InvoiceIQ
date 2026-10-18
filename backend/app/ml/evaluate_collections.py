"""Reproducible offline, observational B2B collections ranking evaluation.

Run: python -m app.ml.evaluate_collections /path/to/decision-snapshot.json --budget 50
The source export is read only. Results contain aggregates, not customer records.
Required source contract is validated below; BPI clearance is never accepted.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timedelta
from decimal import Decimal
import hashlib
import json
from pathlib import Path

from app.services.receivable_ledger_audit import day, money


def timestamp(value):
    result = datetime.fromisoformat(value) if isinstance(value, str) else value
    if not isinstance(result, datetime) or result.tzinfo is None or result.utcoffset() is None:
        raise ValueError("An aware source timestamp is required")
    return result


def evaluate(snapshot: dict, *, budget: int) -> dict:
    if snapshot.get("dataset_kind") != "commercial_b2b_receivables":
        raise ValueError("Only commercial B2B receivables are eligible; BPI clearance is separate")
    if isinstance(budget, bool) or not isinstance(budget, int) or budget < 1:
        raise ValueError("Budget must be a positive invoice count")
    decision = timestamp(snapshot["decision_at"])
    observed = timestamp(snapshot["observed_through"])
    days = snapshot["window_days"]
    if isinstance(days, bool) or not isinstance(days, int) or not 1 <= days <= 365:
        raise ValueError("Window must be 1–365 days")
    end = decision + timedelta(days=days)
    if observed < end:
        raise ValueError("Observation window is censored")
    companies = set(snapshot["company_ids"])
    currency = snapshot["currency"]
    if not companies or not isinstance(currency, str) or len(currency) != 3:
        raise ValueError("Explicit company and currency scope required")
    excluded, versions = Counter(), Counter()
    eligible, common = [], []
    seen, payment_ids = set(), set()
    for row in snapshot["invoices"]:
        identity = row["id"]
        if identity in seen:
            raise ValueError("Duplicate invoice")
        seen.add(identity)
        if row["company_id"] not in companies or row["currency"] != currency:
            raise ValueError("Invoice outside evaluation scope")
        if not row.get("source_reference") or not row.get("payment_history_complete"):
            excluded["missing_source_history"] += 1
            continue
        if timestamp(row["snapshot_at"]) != decision:
            raise ValueError("Invoice status must come from the decision-time snapshot")
        if timestamp(row["known_at"]) > decision:
            excluded["invoice_not_known_at_decision"] += 1
            continue
        if row["status"] in {"CANCELLED", "WRITTEN_OFF", "DRAFT", "PAID"}:
            excluded["ineligible_status"] += 1
            continue
        due = day(row["due_date"], "due_date")
        if due >= decision.date() or day(row["posting_date"], "posting_date") > decision.date():
            excluded["not_overdue_at_decision"] += 1
            continue
        if row.get("adjustments") or row.get("disputed"):
            # No assumed cash allocation for credits, disputes or write-offs.
            excluded["requires_adjustment_allocation"] += 1
            continue
        original = money(row["amount_total"], "amount_total")
        paid, collected = Decimal("0.00"), Decimal("0.00")
        invalid_history = False
        for payment in row["payments"]:
            pid = payment["id"]
            if not pid or pid in payment_ids:
                raise ValueError("Duplicate payment application")
            payment_ids.add(pid)
            if (payment["invoice_id"] != identity or payment["company_id"] != row["company_id"]
                    or payment["currency"] != currency or not payment.get("source_reference")):
                raise ValueError("Payment provenance or scope mismatch")
            occurred, known = timestamp(payment["occurred_at"]), timestamp(payment["known_at"])
            amount = money(payment["amount"], "payment.amount")
            if known < occurred:
                raise ValueError("Payment observation predates the payment")
            if payment["status"] != "SUCCESS":
                continue
            if occurred <= decision:
                if known > decision:
                    invalid_history = True
                else:
                    paid += amount
            elif occurred <= end and known <= observed:
                collected += amount
            elif occurred <= end:
                invalid_history = True
        if invalid_history:
            excluded["late_arriving_payment_history"] += 1
            continue
        balance = original - paid
        if balance <= 0:
            excluded["no_outstanding_balance"] += 1
            continue
        if collected > balance:
            excluded["payments_exceed_balance"] += 1
            continue
        item = {"id": identity, "balance": balance, "collected": collected,
                "days_overdue": (decision.date() - due).days}
        eligible.append(item)
        model, risk = row.get("model_prediction"), row.get("current_risk")
        if not model or not risk:
            excluded["missing_decision_time_ranking"] += 1
            continue
        if not model.get("model_version") or not model.get("artifact_sha256"):
            excluded["missing_model_provenance"] += 1
            continue
        if (timestamp(model["scored_at"]) > decision or timestamp(model["features_known_through"]) > decision
                or timestamp(risk["scored_at"]) > decision):
            excluded["ranking_temporal_leakage"] += 1
            continue
        probability = Decimal(str(model["late_payment_probability"]))
        current_risk = Decimal(str(risk["score"]))
        if (not probability.is_finite() or not 0 <= probability <= 1
                or not current_risk.is_finite() or not 0 <= current_risk <= 100):
            raise ValueError("Invalid ranking score")
        item.update(probability=probability, risk=current_risk)
        common.append(item)
        versions[model["model_version"]] += 1

    total_collected = sum((r["collected"] for r in common), Decimal("0.00"))
    strategies = {}
    for name, key in (("oldest_overdue", "days_overdue"), ("largest_balance", "balance"),
                      ("current_risk", "risk"), ("model_late_payment", "probability")):
        selected = sorted(common, key=lambda row: (-row[key], row["id"]))[:budget]
        collected = sum((r["collected"] for r in selected), Decimal("0.00"))
        balance = sum((r["balance"] for r in selected), Decimal("0.00"))
        strategies[name] = {
            "selected_invoices": len(selected), "outstanding_at_decision": str(balance),
            "observed_collected": str(collected),
            "observed_collection_share": str(collected / total_collected) if total_collected else None,
            "observed_balance_collection_ratio": str(collected / balance) if balance else None,
        }
    return {
        "evaluation_version": "collections-observational-v1", "dataset_kind": snapshot["dataset_kind"],
        "decision_at": decision.isoformat(), "window_end": end.isoformat(), "currency": currency,
        "budget": budget, "source_invoices": len(seen), "eligible_invoices": len(eligible),
        "common_comparison_invoices": len(common), "exclusions_and_coverage": dict(excluded),
        "model_versions": dict(versions), "strategies": strategies,
        "interpretation": "Observational ranking comparison on the same covered population. Actual collections may reflect historical interventions. No causal recovery improvement is established. Late-payment probability is not recovery probability.",
        "status": "evaluated" if common else "BLOCKED_NO_ELIGIBLE_DECISION_TIME_SCORES",
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("snapshot", type=Path)
    parser.add_argument("--budget", type=int, required=True)
    args = parser.parse_args()
    raw = args.snapshot.read_bytes()
    report = evaluate(json.loads(raw), budget=args.budget)
    report["source_sha256"] = hashlib.sha256(raw).hexdigest()
    print(json.dumps(report, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
