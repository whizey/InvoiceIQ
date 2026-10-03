"""Structured LLM outputs.

These are the hard guarantee behind "the LLM must not invent arbitrary
actions": InterventionRecommendation.action is a closed Literal, so
Pydantic validation rejects anything outside the fixed action set,
regardless of what a model tries to return.
"""

from typing import Literal

from pydantic import BaseModel, Field


class DiagnosisResult(BaseModel):
    diagnosis: str = Field(description="One-sentence description of the likely situation")
    reason: str = Field(description="Brief supporting rationale, 1-2 sentences")
    recommended_priority: Literal["low", "medium", "high"]


class InterventionRecommendation(BaseModel):
    action: Literal[
        "SEND_EMAIL",
        "SEND_PAYMENT_LINK",
        "TRACK_PROMISE_TO_PAY",
        "ESCALATE",
        "WAIT",
        "CLOSE_CASE",
    ]
    rationale: str = Field(description="Brief explanation for why this action, 1-2 sentences")


class VoiceTurnResult(BaseModel):
    """One turn of the Hinglish voice-call demo (app/api/voice.py).

    agent_line_hi is spoken via Sarvam TTS, never executed as a tool.
    proposed_action is only a recommendation — evaluate_policy() decides
    whether it actually runs, exactly like InterventionRecommendation.action
    does for the LangGraph workflow. "NONE" (still talking, nothing to
    decide yet) never reaches the policy engine at all.
    """

    agent_line_hi: str = Field(description="What the agent says next, in Hinglish (Devanagari-light, code-mixed)")
    concluded: bool = Field(description="True if this turn ends the call with a decision")
    proposed_action: Literal["TRACK_PROMISE_TO_PAY", "SEND_PAYMENT_LINK", "ESCALATE", "NONE"]
