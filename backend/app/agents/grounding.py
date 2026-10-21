"""Check that every figure in an answer appears in what the tools returned.

The answer is written for people (₹15.2 lakh, 83%), the tools return raw values (1520000.00, 83.06), so a
claim counts as grounded when some tool value rounds to it at the precision the answer used. Dates and
identifiers such as INV-1009 are not figures and are skipped. Small bare integers (up to 3) are exempt
because they are almost always phrasing ("the top 3"); everything else must match.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

UNITS = {"crore": 10_000_000, "crores": 10_000_000, "cr": 10_000_000, "lakh": 100_000, "lakhs": 100_000,
         "lac": 100_000, "lacs": 100_000, "k": 1_000, "thousand": 1_000, "million": 1_000_000}
MONTH = r"(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?"
DATE_PATTERNS = [
    re.compile(r"\b\d{4}-\d{2}-\d{2}\b"),
    re.compile(rf"\b{MONTH}\s+\d{{4}}\b", re.I),  # before day patterns: "July 2026" is not "July 20" + "26"
    re.compile(rf"\b\d{{1,2}}(?:st|nd|rd|th)?\s+{MONTH}(?:,?\s+\d{{4}})?", re.I),
    re.compile(rf"\b{MONTH}\s+\d{{1,2}}(?!\d)(?:st|nd|rd|th)?(?:,?\s+\d{{4}})?", re.I),
]
CLAIM = re.compile(
    r"(?<![\w.\-/])(?P<rupee>(?:₹|rs\.?|inr)\s*)?(?P<num>\d+(?:,\d+)*(?:\.\d+)?)"
    r"(?:\s*(?P<pct>%|percent\b|per\s+cent\b)|\s*(?P<unit>crores?\b|cr\b|lakhs?\b|lacs?\b|k\b|thousand\b|million\b))?"
    r"(?![\w\-/])", re.I)
RAW_NUMBER = re.compile(r"-?\d+(?:\.\d+)?")
SMALL_EXEMPT = 3


@dataclass(frozen=True)
class Claim:
    text: str
    value: float
    tolerance: float
    percent: bool
    start: int


@dataclass
class GroundingReport:
    claims: list[Claim] = field(default_factory=list)
    ungrounded: list[Claim] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.ungrounded


def _mask_dates(text: str) -> str:
    for pattern in DATE_PATTERNS:
        text = pattern.sub(lambda m: " " * len(m.group(0)), text)
    return text


_SMALL = {w: i for i, w in enumerate(
    "zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen "
    "seventeen eighteen nineteen".split())}
_TENS = {w: 10 * (i + 2) for i, w in enumerate("twenty thirty forty fifty sixty seventy eighty ninety".split())}
_SCALES = {"hundred": 100, "thousand": 1_000, "lakh": 100_000, "lakhs": 100_000, "million": 1_000_000,
           "crore": 10_000_000, "crores": 10_000_000}
_NUMBER_WORD = "|".join([*_SMALL, *_TENS, *_SCALES])
_LEADING_WORD = "|".join([*_SMALL, *_TENS])  # "lakh" alone, as in "₹15.2 lakh", is a unit, not a figure
WORD_CLAIM = re.compile(
    rf"(?<![\w-])(?P<words>(?:{_LEADING_WORD})(?:(?:[\s-]+|\s+and\s+)(?:{_NUMBER_WORD}))*)"
    r"(?P<pct>\s+(?:percent|per\s+cent)\b|\s*%)?(?![\w-])", re.I)


def _words_value(words: str) -> float | None:
    total = current = 0
    for word in re.split(r"[\s-]+", words.lower()):
        if word == "and":
            continue
        if word in _SMALL:
            current += _SMALL[word]
        elif word in _TENS:
            current += _TENS[word]
        elif word == "hundred":
            current = (current or 1) * 100
        elif word in _SCALES:
            total += (current or 1) * _SCALES[word]
            current = 0
        else:
            return None
    return float(total + current)


def extract_claims(text: str, exempt_small: bool = True) -> list[Claim]:
    """Figures written as digits ("₹15.2 lakh", "83%") or as words ("ninety-one percent").

    Bare whole numbers up to 3 are skipped unless exempt_small is False, since they are usually phrasing.
    """
    claims = []
    for m in WORD_CLAIM.finditer(text):
        value = _words_value(m.group("words"))
        if value is None or (exempt_small and value <= SMALL_EXEMPT and not m.group("pct")):
            continue
        claims.append(Claim(text=m.group(0).strip(), value=value, tolerance=0.5, percent=bool(m.group("pct")),
                            start=m.start()))
    for m in CLAIM.finditer(_mask_dates(text)):
        raw = m.group("num")
        digits = raw.replace(",", "")
        decimals = len(digits.split(".")[1]) if "." in digits else 0
        number = float(digits)
        unit = (m.group("unit") or "").lower()
        multiplier = UNITS.get(unit, 1)
        percent = bool(m.group("pct"))
        if exempt_small and not (m.group("rupee") or unit or percent) and decimals == 0 and number <= SMALL_EXEMPT:
            continue
        claims.append(Claim(text=m.group(0).strip(), value=number * multiplier, percent=percent, start=m.start(),
                            tolerance=max(0.5 * 10 ** -decimals * multiplier, 1e-9)))
    return sorted(claims, key=lambda c: c.start)


def grounded_values(*sources) -> set[float]:
    """Every number that appears anywhere in the sources (tool results, the question). Text sources also count
    their figures with units, so an answer may repeat the user's own "₹99 crore" when it refutes it."""
    values: set[float] = set()
    for source in sources:
        if isinstance(source, str):
            values |= {c.value for c in extract_claims(source, exempt_small=False)}
        text = source if isinstance(source, str) else json.dumps(source, default=str)
        for raw in RAW_NUMBER.findall(text):
            values.add(abs(float(raw)))
    return values


def _matches(claim: Claim, values: set[float]) -> bool:
    candidates = values
    if claim.percent:
        candidates = values | {v * 100 for v in values if v <= 1}
    return any(abs(claim.value - v) <= claim.tolerance + 1e-9 for v in candidates)


def check_grounding(answer: str, values: set[float], exempt_small: bool = True) -> GroundingReport:
    claims = extract_claims(answer, exempt_small)
    return GroundingReport(claims, [c for c in claims if not _matches(c, values)])


SENTENCE_END = re.compile(r"(?<=[.!?])\s+|\n+")


def drop_ungrounded(answer: str, report: GroundingReport) -> str:
    """Remove each sentence that carries an ungrounded figure.

    Returns "" when the first sentence goes: it carries the direct answer, and what follows usually refers
    back to it ("That month's billing..."), so the remainder would read as nonsense.
    """
    return drop_sentences_at(answer, [c.start for c in report.ungrounded])


def drop_sentences_at(answer: str, bad: list[int]) -> str:
    """Remove each sentence containing one of the character positions; "" if the first sentence goes."""
    kept, start = [], 0
    for i, boundary in enumerate([*SENTENCE_END.finditer(answer), None]):
        end = boundary.start() if boundary else len(answer)
        if not any(start <= pos < end for pos in bad):
            kept.append(answer[start:end].strip())
        elif i == 0:
            return ""
        start = boundary.end() if boundary else end
    return " ".join(k for k in kept if k)
