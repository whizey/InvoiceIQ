"""The model proposes, code decides: rules every suggested review decision must pass before it is shown.

A passing proposal is still only a suggestion; a person applies it through ``POST /portal/decisions``.
"""
from __future__ import annotations

import re
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


# Written advice gets the same rules as a proposal: the model must not talk around the Apply button.

_NEGATION = re.compile(r"\b(not|no need|don't|do not|never|avoid|shouldn't|needn't|no action|nothing to)\b", re.I)
_CUE = re.compile(r"\b(recommend|suggest|should|consider|propose|advise|next step|best to|start (?:by|with)|"
                  r"prioriti[sz]e|focus on|i'd|i would|let's|please|urge)\b", re.I)
_IMPERATIVE = re.compile(r"^\s*(escalate|chase|approve|reject|follow up|send|call|contact|pursue)\b", re.I)
_ACTIONS = [
    ("ESCALATE", re.compile(r"\bescalat\w*", re.I)),
    ("APPROVE", re.compile(r"\b(chas\w+|chase|follow(?:ing)?[- ]up|remind\w*|pursu\w+|approv\w*)\b", re.I)),
    ("REJECT", re.compile(r"\b(reject\w*|writ\w* off)\b", re.I)),
]
# Invoice-number-like references: letters, then digits, keeping the separators real numbers use (SO-1001/2,
# INV/2026/0042, INV-2026-001.A).
_REF = re.compile(r"\b[A-Za-z]{2,}[-/ ]?\d[\w\-/.]*")
_SENTENCE_END = re.compile(r"(?<=[.!?])\s+|\n+")
_CLAUSE = re.compile(r";|,?\s+(?:and|then|while|but)\s+|,\s+(?=[a-z]+\s)", re.I)


@dataclass(frozen=True)
class WrittenViolation:
    start: int
    end: int
    sentence: str
    target: str
    decision: str
    reasons: tuple[str, ...]


def _canon(text: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", text.upper())


def _index(portfolio) -> dict[str, object]:
    index = getattr(portfolio, "_invoice_index", None)
    if index is None:
        index = {_canon(n): row for n, row in portfolio.by_number.items()}
        try:
            portfolio._invoice_index = index
        except AttributeError:
            pass
    return index


def resolve_invoice(text: str, portfolio):
    """The workspace invoice a reference means, ignoring case and separators; None if it matches nothing.

    A reference that is part of a real invoice number (a sentence that cut it short) counts as that invoice
    when only one invoice fits, and as known (but unresolved) when several do."""
    canon = _canon(text.rstrip(".,;:)/-"))
    index = _index(portfolio)
    if canon in index:
        return index[canon]
    partial = [row for key, row in index.items() if len(canon) >= 4 and (key.startswith(canon) or canon.startswith(key))]
    return partial[0] if len(partial) == 1 else ("ambiguous" if partial else None)


def unknown_invoice_refs(answer: str, portfolio) -> list[tuple[int, int, str]]:
    """References in the workspace's numbering style that match no invoice: (start, end, text)."""
    prefixes = _prefixes(portfolio)
    found = []
    for m in _REF.finditer(answer):
        text = m.group(0).rstrip(".,;:)/-")
        letters = re.match(r"[A-Za-z]+", text).group(0).upper()
        if letters in prefixes and resolve_invoice(text, portfolio) is None:
            found.append((m.start(), m.start() + len(text), text))
    return found


NOT_FOUND = " (not found in your records)"


def mark_unknown_invoices(answer: str, refs: list[tuple[int, int, str]]) -> str:
    """Keep every reference, but say plainly which ones match nothing; nothing is removed."""
    for _, end, _ in sorted(refs, reverse=True):
        if not answer[end:].startswith(NOT_FOUND):
            answer = answer[:end] + NOT_FOUND + answer[end:]
    return answer


def _prefixes(portfolio) -> set[str]:
    return {re.match(r"[A-Za-z]+", n).group(0).upper() for n in portfolio.by_number if re.match(r"[A-Za-z]+", n)}


def check_written_recommendations(answer: str, portfolio) -> list[WrittenViolation]:
    """Recommendations in the answer text that the proposal rules would block."""
    violations, start = [], 0
    prefixes = _prefixes(portfolio)
    for boundary in [*_SENTENCE_END.finditer(answer), None]:
        end = boundary.start() if boundary else len(answer)
        sentence = answer[start:end]
        if sentence.strip() and not _NEGATION.search(sentence) and (_CUE.search(sentence)
                                                                    or _IMPERATIVE.search(sentence)):
            # Pair each action with the invoices and accounts in its own clause:
            # "escalate INV-1009 and follow up on INV-1006" escalates only INV-1009.
            last: list[str] = []
            for clause in _CLAUSE.split(sentence):
                decisions = [d for d, pattern in _ACTIONS if pattern.search(clause)] or last
                if decisions is not last:
                    last = decisions
                for decision in decisions:
                    for target, reasons in _targets(clause, decision, portfolio, prefixes):
                        if reasons:
                            violations.append(WrittenViolation(start, end, sentence.strip(), target, decision,
                                                               tuple(reasons)))
        start = boundary.end() if boundary else end
    return violations


def _targets(sentence: str, decision: str, portfolio, prefixes: set[str]):
    for ref in _REF.findall(sentence):
        row = resolve_invoice(ref, portfolio)
        if row is None or row == "ambiguous":  # unknown references are marked, never removed, by the caller
            continue
        yield row.invoice_number, _invoice_reasons(portfolio, row, decision)
    lowered = sentence.lower()
    for account in getattr(portfolio, "accounts", []):
        if account.lower() in lowered:
            rows = [r for r in portfolio.invoices if r.counterparty == account]
            if decision == "ESCALATE" and not any(portfolio.status(r) == "overdue" for r in rows):
                yield account, ["account has nothing overdue to escalate"]
            elif decision == "APPROVE" and not any(portfolio.balance(r) > 0 for r in rows):
                yield account, ["account has nothing outstanding to recover"]
            else:
                yield account, []


def _invoice_reasons(portfolio, row, decision: str) -> list[str]:
    reasons = []
    paid = portfolio.balance(row) <= 0
    if paid and decision in {"APPROVE", "ESCALATE"}:
        reasons.append("invoice is fully paid; there is nothing to recover")
    elif decision == "ESCALATE" and portfolio.days_past_due(row) <= 0:
        reasons.append("invoice is not overdue yet; escalation needs it to be past due")
    return reasons
