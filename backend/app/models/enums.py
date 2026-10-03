from enum import Enum


class CompanySegment(str, Enum):
    SMB = "SMB"
    MID_MARKET = "MID_MARKET"
    ENTERPRISE = "ENTERPRISE"


class InvoiceStatus(str, Enum):
    DRAFT = "DRAFT"
    SENT = "SENT"
    PAID = "PAID"
    PARTIALLY_PAID = "PARTIALLY_PAID"
    OVERDUE = "OVERDUE"
    WRITTEN_OFF = "WRITTEN_OFF"
    CANCELLED = "CANCELLED"


class PaymentMethod(str, Enum):
    BANK_TRANSFER = "BANK_TRANSFER"
    CARD = "CARD"
    UPI = "UPI"
    CHEQUE = "CHEQUE"
    OTHER = "OTHER"


class PaymentStatus(str, Enum):
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"
    PENDING = "PENDING"


class PaymentEventType(str, Enum):
    INVOICE_CREATED = "INVOICE_CREATED"
    INVOICE_OVERDUE = "INVOICE_OVERDUE"
    PAYMENT_RECEIVED = "PAYMENT_RECEIVED"
    PAYMENT_FAILED = "PAYMENT_FAILED"
    PAYMENT_PARTIAL = "PAYMENT_PARTIAL"


class RecoveryCaseStatus(str, Enum):
    OPEN = "OPEN"
    MONITORING = "MONITORING"
    ESCALATED = "ESCALATED"
    RECOVERED = "RECOVERED"
    CLOSED_UNRECOVERED = "CLOSED_UNRECOVERED"
    CLOSED = "CLOSED"


class RiskLevel(str, Enum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"


class RecoveryActionType(str, Enum):
    SEND_EMAIL = "SEND_EMAIL"
    SEND_PAYMENT_LINK = "SEND_PAYMENT_LINK"
    TRACK_PROMISE_TO_PAY = "TRACK_PROMISE_TO_PAY"
    ESCALATE = "ESCALATE"
    WAIT = "WAIT"
    CLOSE_CASE = "CLOSE_CASE"
    # Human-triggered only (case-page "Start Hinglish recovery call" button)
    # — never something the automated LangGraph diagnose/recommend cycle or
    # the scheduler proposes on its own. See app/api/voice.py.
    PLACE_VOICE_CALL = "PLACE_VOICE_CALL"


class RecoveryActionStatus(str, Enum):
    PROPOSED = "PROPOSED"
    POLICY_APPROVED = "POLICY_APPROVED"
    POLICY_REJECTED = "POLICY_REJECTED"
    PENDING_APPROVAL = "PENDING_APPROVAL"
    EXECUTED = "EXECUTED"
    FAILED = "FAILED"


class ProposedBy(str, Enum):
    AI = "AI"
    SYSTEM = "SYSTEM"
    HUMAN = "HUMAN"


class AgentDecisionStage(str, Enum):
    DIAGNOSIS = "DIAGNOSIS"
    INTERVENTION_RECOMMENDATION = "INTERVENTION_RECOMMENDATION"


class PromiseToPayStatus(str, Enum):
    PENDING = "PENDING"
    FULFILLED = "FULFILLED"
    BROKEN = "BROKEN"
    EXPIRED = "EXPIRED"


class CommunicationChannel(str, Enum):
    EMAIL = "EMAIL"
    SMS = "SMS"
    VOICE = "VOICE"


class CommunicationDirection(str, Enum):
    OUTBOUND = "OUTBOUND"
    INBOUND = "INBOUND"


class CommunicationStatus(str, Enum):
    SENT = "SENT"
    FAILED = "FAILED"
    SIMULATED = "SIMULATED"


class PolicyDecisionResult(str, Enum):
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    REQUIRES_HUMAN_REVIEW = "REQUIRES_HUMAN_REVIEW"


class AuditActor(str, Enum):
    SYSTEM = "SYSTEM"
    AI_AGENT = "AI_AGENT"
    POLICY_ENGINE = "POLICY_ENGINE"
    HUMAN = "HUMAN"
