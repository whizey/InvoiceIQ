from pydantic import BaseModel

from app.models.enums import PolicyDecisionResult, RecoveryActionType


class VoiceOutcomeOut(BaseModel):
    action_type: RecoveryActionType
    policy_decision: PolicyDecisionResult
    reason: str


class VoiceStartOut(BaseModel):
    started: bool
    turn_number: int
    agent_line: str | None
    agent_audio_base64: str | None
    ended: bool
    reason: str | None = None


class VoiceTurnOut(BaseModel):
    turn_number: int
    transcript_user: str
    agent_line: str | None
    agent_audio_base64: str | None
    ended: bool
    outcome: VoiceOutcomeOut | None = None
    # Set when the policy engine stops the call mid-conversation, which
    # /voice/turn now re-checks on every turn rather than trusting that
    # /voice/start was called first. Mirrors VoiceStartOut.reason.
    reason: str | None = None
