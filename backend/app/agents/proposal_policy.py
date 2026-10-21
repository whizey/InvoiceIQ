"""The model proposes, code decides: rules every suggested review decision must pass before it is shown.

A passing proposal is still only a suggestion; a person applies it through ``POST /portal/decisions``.
"""
from __future__ import annotations

from dataclasses import dataclass, field

PROPOSABLE = ("APPROVE", "REJECT", "ESCALATE")
MIN_ESCALATION_REASON = 20
MAX_PROPOSALS = 3


@dataclass(frozen=True)
class Proposal:
    invoice_number: str
    decision: str
    reason: str


@dataclass
class Verdict:
    proposal: Proposal
    allowed: bool
    violations: list[str] = field(default_factory=list)
    invoice_id: str | None = None

    def as_tool_result(self) -> dict:
        result = {"invoice": self.proposal.invoice_number, "decision": self.proposal.decision,
                  "allowed": self.allowed}
        if self.violations:
            result["blocked_because"] = self.violations
            result["instruction"] = "Do not suggest this decision to the user."
        return result

    def as_response(self) -> dict:
        return {"invoice": self.proposal.invoice_number, "invoice_id": self.invoice_id,
                "decision": self.proposal.decision, "reason": self.proposal.reason,
                "allowed": self.allowed, "violations": self.violations}


def check_proposal(portfolio, proposal: Proposal) -> Verdict:
    """Apply every rule and report all violations, not just the first."""
    violations: list[str] = []
    if proposal.decision not in PROPOSABLE:
        violations.append(f"decision must be one of {', '.join(PROPOSABLE)}")
    row = portfolio.by_number.get(proposal.invoice_number)
    if row is None:
        return Verdict(proposal, False, violations + ["invoice is not in this workspace"])
    paid = portfolio.balance(row) <= 0
    if paid and proposal.decision in {"APPROVE", "ESCALATE"}:
        violations.append("invoice is fully paid; there is nothing to recover")
    if proposal.decision == "ESCALATE":
        if portfolio.days_past_due(row) <= 0 and not paid:
            violations.append("invoice is not overdue yet; escalation needs it to be past due")
        if len(proposal.reason) < MIN_ESCALATION_REASON:
            violations.append(f"escalation needs a stated reason of at least {MIN_ESCALATION_REASON} characters")
    elif not proposal.reason:
        violations.append("a reason is required")
    if portfolio.review(row) == proposal.decision:
        violations.append(f"invoice is already marked {proposal.decision}")
    return Verdict(proposal, not violations, violations, str(row.id))
