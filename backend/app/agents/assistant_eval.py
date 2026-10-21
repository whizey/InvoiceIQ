"""Evaluate the receivables agent against a fixed question set with hand-computed answers.

Offline (CI, no API key): every case's tool calls must return its expected figures, the rule-based fallback
must route the question to the right tool and give a fully grounded answer, and proposal cases must get the
expected policy verdict.

Live (``--live``, uses the configured providers): runs the full agent per question and reports tool recall,
answer accuracy (expected figures stated), grounding, latency and which provider answered. Free-tier rate
limits apply, so it paces itself between questions.

    python -m app.agents.assistant_eval [report.json] [--live] [--min-accuracy 0.8]
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import uuid
from datetime import date, datetime, time, timezone
from decimal import Decimal
from pathlib import Path
from time import perf_counter
from types import SimpleNamespace

from app.agents.finance_agent import _rule_based, answer_question, route
from app.agents.finance_tools import Portfolio, run_tool
from app.agents.grounding import check_grounding, grounded_values
from app.agents.proposal_policy import Proposal, check_proposal
from app.services.assistant_tracing import TraceRecorder

FIXTURE = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "assistant_eval_v1.json"
ROUTE_CALLS = {"summary": ("get_portfolio_summary", {}), "aging": ("get_aging_breakdown", {}),
               "review": ("get_review_status", {}), "accounts": ("list_overdue_accounts", {"limit": 3}),
               "invoices": ("search_invoices", {"status": "overdue", "limit": 3}),
               "upcoming": ("search_invoices", {"status": "not_due", "sort": "due_date_asc", "limit": 3}),
               "trend": ("get_collections_trend", {"months": 6}),
               "invoice": ("get_invoice", {}), "customer": ("get_customer_profile", {})}


def load_fixture(path: Path = FIXTURE) -> tuple[Portfolio, list[dict], str]:
    data = json.loads(path.read_text())
    invoices = [SimpleNamespace(
        id=uuid.uuid5(uuid.NAMESPACE_URL, row["number"]), invoice_number=row["number"], counterparty=row["account"],
        posting_date=date.fromisoformat(row["posted"]), due_date=date.fromisoformat(row["due"]),
        amount_total=Decimal(row["total"]), amount_paid=Decimal(row["paid"]),
        risk_probability=Decimal(row["risk"]) if row["risk"] else None,
        risk_provenance="fixture" if row["risk"] else None) for row in data["invoices"]]
    ids = {r.invoice_number: r.id for r in invoices}
    events = [SimpleNamespace(invoice_id=ids[e["invoice"]], decision=e["decision"], note=e["note"],
                              created_at=datetime.combine(date.fromisoformat(e["on"]), time(12), timezone.utc))
              for e in data["events"]]
    payments = [SimpleNamespace(invoice_id=ids[x["invoice"]], paid_on=date.fromisoformat(x["on"]),
                                amount=Decimal(x["amount"])) for x in data.get("payments", [])]
    for inv in invoices:  # the fixture's ledger must reconcile, as the portal's does
        assert sum((x.amount for x in payments if x.invoice_id == inv.id), Decimal(0)) == inv.amount_paid, inv
    return (Portfolio(invoices, events, date.fromisoformat(data["today"]), payments=payments),
            data["cases"], data["version"])


def _found(number: float, values: set[float]) -> bool:
    return any(abs(number - v) <= 0.006 for v in values)


def offline_case(p: Portfolio, case: dict) -> dict:
    outputs = [run_tool(p, name, args) for name, args in case["calls"]]
    values = grounded_values(outputs)
    missing = [n for n in case["numbers"] if not _found(n, values)]
    text = json.dumps(outputs)
    missing += [s for s in case.get("names", []) + case.get("contains", []) if s not in text]

    chosen = route(case["question"], p)
    fallback = _rule_based(p, case["question"], TraceRecorder(), attempted=False)
    kind, _, ref = chosen.partition(":")
    tool, args = ROUTE_CALLS[kind]
    if ref:
        args = {"account": ref} if kind == "customer" else {"invoice_number": ref}
    route_output = run_tool(p, tool, args)
    fallback_report = check_grounding(fallback.answer, grounded_values(route_output))

    result = {"id": case["id"], "tool_figures_ok": not missing, "missing": missing,
              "route_ok": chosen == case["fallback_route"], "route": chosen,
              "fallback_grounded": fallback_report.ok,
              "fallback_ungrounded": [c.text for c in fallback_report.ungrounded]}
    if "proposal" in case:
        spec = case["proposal"]
        verdict = check_proposal(p, Proposal(spec["invoice_number"], spec["decision"], spec["reason"]))
        result["policy_ok"] = verdict.allowed == spec["allowed"]
        result["policy_violations"] = verdict.violations
    result["passed"] = all(v for k, v in result.items() if k.endswith("_ok") or k == "fallback_grounded")
    return result


def run_offline(path: Path = FIXTURE) -> dict:
    p, cases, version = load_fixture(path)
    results = [offline_case(p, c) for c in cases]
    return {"mode": "offline", "fixture": version, "cases": len(results),
            "passed": sum(r["passed"] for r in results), "results": results}


def _live_providers():
    from app.agents.finance_agent import build_providers

    return build_providers()


async def run_live(path: Path = FIXTURE, pause_seconds: float = 8.0) -> dict:
    p, cases, version = load_fixture(path)
    results = []
    from app.agents.finance_agent import cooldown_remaining

    for case in cases:
        providers = _live_providers()
        # Measure the models, not the free-tier per-minute quota: wait out a short cooldown first.
        # Wait only when every model is cooling down, and only until the first one is free again.
        wait = min((cooldown_remaining(x) for x in providers), default=0)
        if 0 < wait <= 90:
            await asyncio.sleep(wait)
        recorder = TraceRecorder()
        started = perf_counter()
        out = await answer_question(p, case["question"], [], providers, recorder)
        seconds = round(perf_counter() - started, 2)
        expected_tools = {name for name, _ in case["calls"]}
        expected_numbers = case.get("answer_numbers", case["numbers"])
        stated = check_grounding(out.answer, set(map(float, expected_numbers)))
        hits = len({c.value for c in stated.claims if c not in stated.ungrounded})
        answer = out.answer.lower()
        names = [n for n in case.get("answer_names", case.get("names", []))
                 if not any(alt.lower() in answer for alt in ([n] if isinstance(n, str) else n))]
        results.append({
            "id": case["id"], "provider": out.provider, "model": out.model, "seconds": seconds,
            "tool_recall": len(expected_tools & set(out.tools_called)) / len(expected_tools) if expected_tools else 1.0,
            "tools_called": out.tools_called, "expected_figures_stated": hits,
            "answer_correct": (hits >= 1 or not expected_numbers) and not names, "grounded": not out.verification.get("removed"),
            "repaired": out.verification.get("repaired", False), "fallback_used": out.fallback_used,
            "steps": len(recorder.spans), "answer": out.answer})
        await asyncio.sleep(pause_seconds)
    n = len(results) or 1
    latencies = sorted(r["seconds"] for r in results)
    return {"mode": "live", "fixture": version, "cases": len(results),
            "tool_recall": round(sum(r["tool_recall"] for r in results) / n, 3),
            "answer_accuracy": round(sum(r["answer_correct"] for r in results) / n, 3),
            "grounded_rate": round(sum(r["grounded"] for r in results) / n, 3),
            "repair_rate": round(sum(r["repaired"] for r in results) / n, 3),
            "fallback_rate": round(sum(r["fallback_used"] for r in results) / n, 3),
            "p50_seconds": latencies[len(latencies) // 2] if latencies else 0,
            "p95_seconds": latencies[min(len(latencies) - 1, int(len(latencies) * 0.95))] if latencies else 0,
            "results": results}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("report", nargs="?", type=Path)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--min-accuracy", type=float, default=0.0, help="live mode: fail below this accuracy")
    args = parser.parse_args(argv)
    report = asyncio.run(run_live()) if args.live else run_offline()
    if args.report:
        args.report.write_text(json.dumps(report, indent=2))
    if args.live:
        print(json.dumps({k: v for k, v in report.items() if k != "results"}, indent=2))
        return 0 if report["answer_accuracy"] >= args.min_accuracy else 1
    failed = [r for r in report["results"] if not r["passed"]]
    print(f"{report['passed']}/{report['cases']} offline assistant eval cases passed")
    for r in failed:
        print("FAILED", json.dumps(r))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
