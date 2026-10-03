"""Domain event topic names.

Every topic here has a real, currently-firing publish call somewhere in
the app (see app/events/__init__.py's get_publisher() call sites) — no
placeholder topics that nothing ever sends to.
"""

INVOICE_OVERDUE = "invoice.overdue"
PAYMENT_RECEIVED = "payment.received"
RECOVERY_CASE_CREATED = "recovery.case_created"
RECOVERY_ACTION_COMPLETED = "recovery.action_completed"
PROMISE_TO_PAY_CREATED = "promise_to_pay.created"
PROMISE_TO_PAY_BROKEN = "promise_to_pay.broken"
RECOVERY_CASE_CLOSED = "recovery.case_closed"
RISK_SCORED = "risk.scored"
ACTION_PROPOSED = "action.proposed"
REVIEW_REQUESTED = "review.requested"
REVIEW_RESOLVED = "review.resolved"

ALL_TOPICS = [
    INVOICE_OVERDUE,
    PAYMENT_RECEIVED,
    RECOVERY_CASE_CREATED,
    RECOVERY_ACTION_COMPLETED,
    PROMISE_TO_PAY_CREATED,
    PROMISE_TO_PAY_BROKEN,
    RECOVERY_CASE_CLOSED,
    RISK_SCORED,
    ACTION_PROPOSED,
    REVIEW_REQUESTED,
    REVIEW_RESOLVED,
]
