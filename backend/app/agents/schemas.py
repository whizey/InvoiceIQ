"""Structured LLM outputs.

These are the hard guarantee behind "the LLM must not invent arbitrary
actions": InterventionRecommendation.action is a closed Literal, so
Pydantic validation rejects anything outside the fixed action set,
regardless of what a model tries to return.
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class FinancialClaim(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    source_invoice_id: str = Field(min_length=1, max_length=100)
    field: Literal["amount_total", "amount_paid", "due_date"]
    value: str = Field(min_length=1, max_length=100)


class VersionedAgentOutput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    schema_version: Literal["1"] = "1"
    financial_claims: list[FinancialClaim] = Field(default_factory=list, max_length=10)


class DiagnosisResult(VersionedAgentOutput):
    model_config = ConfigDict(extra="forbid", strict=True)
    diagnosis: str = Field(min_length=1, max_length=1000, description="One-sentence description of the likely situation")
    reason: str = Field(min_length=1, max_length=2000, description="Brief supporting rationale, 1-2 sentences")
    recommended_priority: Literal["low", "medium", "high"]


class InterventionRecommendation(VersionedAgentOutput):
    model_config = ConfigDict(extra="forbid", strict=True)
    action: Literal[
        "SEND_EMAIL",
        "SEND_PAYMENT_LINK",
        "TRACK_PROMISE_TO_PAY",
        "ESCALATE",
        "WAIT",
        "CLOSE_CASE",
    ]
    rationale: str = Field(min_length=1, max_length=2000, description="Brief explanation for why this action, 1-2 sentences")


class VoiceTurnResult(VersionedAgentOutput):
    """One turn of the Hinglish voice-call sample (app/api/voice.py).

    agent_line_hi is spoken via Sarvam TTS, never executed as a tool.
    proposed_action is only a recommendation — evaluate_policy() decides
    whether it actually runs, exactly like InterventionRecommendation.action
    does for the LangGraph workflow. "NONE" (still talking, nothing to
    decide yet) never reaches the policy engine at all.
    """

    agent_line_hi: str = Field(min_length=1, max_length=2000, description="What the agent says next, in Hinglish (Devanagari-light, code-mixed)")
    concluded: bool = Field(description="True if this turn ends the call with a decision")
    proposed_action: Literal["TRACK_PROMISE_TO_PAY", "SEND_PAYMENT_LINK", "ESCALATE", "NONE"]


class PersistedRecoveryRecommendation(InterventionRecommendation):
    """Auditable recommendation envelope, distinct from the provider response."""
    investigation_id: str
    reasons: list[str] = Field(min_length=1)
    evidence_references: list[str]
    uncertainty: str = Field(min_length=1)
    execution_mode: Literal["deterministic_fallback", "external_llm", "recorded"]
