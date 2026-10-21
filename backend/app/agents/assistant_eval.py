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
import math
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
from app.agents.grounding import check_grounding, extract_claims, grounded_values
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
        issue_date=date.fromisoformat(row["posted"]), posting_date=date.fromisoformat(row["posted"]),
        due_date=date.fromisoformat(row["due"]),
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

    result = {"id": case["id"], "category": case.get("category", "core"),
              "tool_figures_ok": not missing, "missing": missing}
    if "fallback_route" in case:
        result.update(_fallback_check(p, case))
    if "proposal" in case:
        spec = case["proposal"]
        verdict = check_proposal(p, Proposal(spec["invoice_number"], spec["decision"], spec["reason"]))
        result["policy_ok"] = verdict.allowed == spec["allowed"]
        result["policy_violations"] = verdict.violations
    result["passed"] = all(v for k, v in result.items() if k.endswith("_ok") or k == "fallback_grounded")
    return result


def _fallback_check(p: Portfolio, case: dict) -> dict:
    chosen = route(case["question"], p)
    fallback = _rule_based(p, case["question"], TraceRecorder(), attempted=False)
    kind, _, ref = chosen.partition(":")
    tool, args = ROUTE_CALLS[kind]
    if ref:
        args = {"account": ref} if kind == "customer" else {"invoice_number": ref}
    route_output = run_tool(p, tool, args)
    fallback_report = check_grounding(fallback.answer, grounded_values(route_output))

    return {"route_ok": chosen == case["fallback_route"], "route": chosen,
            "fallback_grounded": fallback_report.ok,
            "fallback_ungrounded": [c.text for c in fallback_report.ungrounded]}


def run_offline(path: Path = FIXTURE) -> dict:
    p, cases, version = load_fixture(path)
    results = [offline_case(p, c) for c in cases]
    return {"mode": "offline", "fixture": version, "cases": len(results),
            "passed": sum(r["passed"] for r in results), "results": results}


def score_case(case: dict, out) -> dict:
    """Did the answer get the case right? Every rule is a plain check against the hand-written case."""
    answer = out.answer.lower()
    def stated(number: float) -> bool:
        report = check_grounding(out.answer, {float(number)})
        return any(c not in report.ungrounded for c in report.claims)

    if "answer_numbers" in case:  # every key figure must be stated
        missing_figures = [n for n in case["answer_numbers"] if not stated(n)]
    else:  # at least one of the case's figures
        missing_figures = [] if not case["numbers"] or any(stated(n) for n in case["numbers"]) else case["numbers"]
    figures_ok = not missing_figures
    missing_names = [n for n in case.get("answer_names", case.get("names", []))
                     if not any(alt.lower() in answer for alt in ([n] if isinstance(n, str) else n))]
    forbidden = [t for t in case.get("forbidden", []) if t.lower() in answer]
    stray_figures = [c.text for c in extract_claims(out.answer)] if case.get("no_figures") else []
    allowed = {(x["invoice"], x["decision"]) for x in out.proposals}
    leaked = [pair for pair in case.get("must_not_allow", []) if tuple(pair) in allowed]
    expected_tools = {name for name, _ in case["calls"]}
    return {"correct": figures_ok and not (missing_names or forbidden or stray_figures or leaked),
            "missing_figures": missing_figures, "missing_names": missing_names, "forbidden_found": forbidden,
            "stray_figures": stray_figures, "allowed_blocked_proposal": leaked,
            "tool_recall": len(expected_tools & set(out.tools_called)) / len(expected_tools) if expected_tools else 1.0,
            "grounded": not out.verification.get("removed"),
            "repaired": bool(out.verification.get("repaired")),
            "policy_trimmed": bool(out.verification.get("policy_removed")),
            "policy_repaired": bool(out.verification.get("policy_repaired"))}


def provider_for(spec: str):
    """'gemini:<model>' or 'groq:<model>' as a single provider with no fallback, to measure that model alone."""
    from app.agents.finance_agent import Provider
    from app.core.config import settings

    name, _, model = spec.partition(":")
    if name == "gemini":
        from langchain_google_genai import ChatGoogleGenerativeAI

        client = ChatGoogleGenerativeAI(model=model, api_key=settings.google_api_key, max_retries=0, timeout=25,
                                        max_output_tokens=4096)
    elif name == "groq":
        from langchain_openai import ChatOpenAI

        client = ChatOpenAI(model=model, api_key=settings.groq_api_key, base_url="https://api.groq.com/openai/v1",
                            temperature=0, max_tokens=2048, max_retries=0, timeout=25)
    else:
        raise SystemExit(f"unknown provider in {spec!r}; use gemini:<model> or groq:<model>")
    return Provider(name, model, client)


def _slug(spec: str) -> str:
    return spec.replace(":", "_").replace("/", "_")


async def run_live(specs: list[str], runs: int, out_dir: Path, path: Path = FIXTURE,
                   pause_seconds: float = 4.0, max_wait: float = 120.0) -> None:
    """Append one JSON line per answered case to <out_dir>/<model>-run<N>.jsonl; resumes where it stopped."""

    p, cases, version = load_fixture(path)
    out_dir.mkdir(parents=True, exist_ok=True)
    for spec in specs:
        for run in range(1, runs + 1):
            log = out_dir / f"{_slug(spec)}-run{run}.jsonl"
            done = {json.loads(line)["id"] for line in log.read_text().splitlines()} if log.exists() else set()
            stopped = False
            for _ in range(6):  # passes: a rate-limited case is retried on the next one
                if stopped or len(done) == len(cases):
                    break
                stopped = await _one_pass(spec, run, p, cases, version, log, done, pause_seconds, max_wait)
            print(f"{spec} run {run}: {len(done)}/{len(cases)} cases answered")


async def _one_pass(spec, run, p, cases, version, log, done, pause_seconds, max_wait) -> bool:
    """Answer each case not yet done; True when the model's quota is out for longer than max_wait."""
    from app.agents.finance_agent import cooldown_remaining

    for case in cases:
        if case["id"] in done:
            continue
        provider = provider_for(spec)
        wait = cooldown_remaining(provider)
        if wait > max_wait:
            print(f"{spec} run {run}: stopped at {len(done)}/{len(cases)}, quota resets in {wait / 3600:.1f} h")
            return True
        if wait:
            await asyncio.sleep(wait)
        started = perf_counter()
        out = await answer_question(p, case["question"], [tuple(t) for t in case.get("history", [])],
                                    [provider], TraceRecorder())
        seconds = round(perf_counter() - started, 2)
        if out.provider == "rules":  # the model never answered (rate limit, error): not its score
            wait = cooldown_remaining(provider)
            if wait > max_wait:
                print(f"{spec} run {run}: stopped at {len(done)}/{len(cases)}, "
                      f"quota resets in {wait / 3600:.1f} h")
                return True
            continue  # retried on the next pass
        row = {"id": case["id"], "category": case.get("category", "core"), "fixture": version,
               "model": spec, "run": run, "seconds": seconds, "tools_called": out.tools_called,
               **score_case(case, out), "answer": out.answer}
        with log.open("a") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        done.add(case["id"])
        await asyncio.sleep(pause_seconds)
    return False


def _percentile(ordered: list[float], q: float) -> float:
    """Nearest-rank percentile of an already sorted list."""
    return ordered[max(0, min(len(ordered) - 1, math.ceil(q * len(ordered)) - 1))] if ordered else 0.0


def _mean_sd(values: list[float]) -> tuple[float, float]:
    if not values:
        return 0.0, 0.0
    mean = sum(values) / len(values)
    sd = (sum((v - mean) ** 2 for v in values) / (len(values) - 1)) ** 0.5 if len(values) > 1 else 0.0
    return mean, sd


def summarise(out_dir: Path, path: Path = FIXTURE) -> dict:
    """Mean and sample standard deviation across runs, per model and per category."""
    _, cases, version = load_fixture(path)
    total = len(cases)
    by_model: dict[str, dict[int, list[dict]]] = {}
    for log in sorted(out_dir.glob("*-run*.jsonl")):
        rows = [json.loads(line) for line in log.read_text().splitlines() if line.strip()]
        rows = [r for r in rows if r.get("fixture") == version]
        if rows:
            by_model.setdefault(rows[0]["model"], {})[rows[0]["run"]] = rows
    report = {"fixture": version, "cases": total, "models": {}}
    for model, runs in by_model.items():
        per_run = {}
        for run, rows in sorted(runs.items()):
            seconds = sorted(r["seconds"] for r in rows)
            per_run[run] = {
                "answered": len(rows), "accuracy": sum(r["correct"] for r in rows) / len(rows),
                "grounded": sum(r["grounded"] for r in rows) / len(rows),
                "tool_recall": sum(r["tool_recall"] for r in rows) / len(rows),
                "policy_trimmed": sum(r["policy_trimmed"] for r in rows) / len(rows),
                "p50_seconds": _percentile(seconds, 0.5), "p95_seconds": _percentile(seconds, 0.95),
                "by_category": {cat: sum(r["correct"] for r in rows if r["category"] == cat)
                                / max(1, sum(r["category"] == cat for r in rows))
                                for cat in sorted({r["category"] for r in rows})}}
        stats = {}
        for key in ("accuracy", "grounded", "tool_recall", "policy_trimmed", "p50_seconds", "p95_seconds"):
            mean, sd = _mean_sd([r[key] for r in per_run.values()])
            stats[key] = {"mean": round(mean, 3), "sd": round(sd, 3)}
        categories = sorted({c for r in per_run.values() for c in r["by_category"]})
        stats["by_category"] = {c: dict(zip(("mean", "sd"), map(lambda v: round(v, 3), _mean_sd(
            [r["by_category"][c] for r in per_run.values() if c in r["by_category"]])))) for c in categories}
        report["models"][model] = {"runs": len(per_run), "answered_per_run": [r["answered"] for r in per_run.values()],
                                   **stats}
    return report


def markdown(report: dict) -> str:
    lines = ["| Model | Runs | Cases answered | Accuracy | Figures verified | Tool recall | Median / p95 time |",
             "|---|---|---|---|---|---|---|"]
    for model, m in report["models"].items():
        pct = lambda k: f"{m[k]['mean'] * 100:.1f}% ± {m[k]['sd'] * 100:.1f}"  # noqa: E731
        lines.append(f"| `{model.split(':', 1)[1]}` | {m['runs']} | {' / '.join(map(str, m['answered_per_run']))} "
                     f"of {report['cases']} | {pct('accuracy')} | {pct('grounded')} | {pct('tool_recall')} | "
                     f"{m['p50_seconds']['mean']:.1f} s / {m['p95_seconds']['mean']:.1f} s |")
    cats = sorted({c for m in report["models"].values() for c in m["by_category"]})
    lines += ["", "| Model | " + " | ".join(cats) + " |", "|---|" + "---|" * len(cats)]
    for model, m in report["models"].items():
        cells = [f"{m['by_category'][c]['mean'] * 100:.0f}% ± {m['by_category'][c]['sd'] * 100:.0f}"
                 if c in m["by_category"] else "–" for c in cats]
        lines.append(f"| `{model.split(':', 1)[1]}` | " + " | ".join(cells) + " |")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("report", nargs="?", type=Path, help="offline: write the JSON report here")
    parser.add_argument("--live", action="append", metavar="PROVIDER:MODEL",
                        help="run the cases against this model alone; repeat for several models")
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--out-dir", type=Path, default=Path("eval-runs"))
    parser.add_argument("--summary", action="store_true", help="summarise the runs in --out-dir")
    args = parser.parse_args(argv)
    if args.live:
        asyncio.run(run_live(args.live, args.runs, args.out_dir))
        return 0
    if args.summary:
        report = summarise(args.out_dir)
        if args.report:
            args.report.write_text(json.dumps(report, indent=2))
        print(markdown(report))
        return 0
    report = run_offline()
    if args.report:
        args.report.write_text(json.dumps(report, indent=2))
    failed = [r for r in report["results"] if not r["passed"]]
    print(f"{report['passed']}/{report['cases']} offline assistant eval cases passed")
    for r in failed:
        print("FAILED", json.dumps(r))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
