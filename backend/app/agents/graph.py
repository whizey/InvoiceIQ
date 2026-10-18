"""The LangGraph recovery workflow.

One graph invocation = one recovery cycle for a single case, triggered
manually or by a scheduler (POST /recovery-cases/{id}/run) — there is no
long-running consumer or persisted graph state in V1 (see
docs/ARCHITECTURE.md). Every node re-fetches what it needs from
Postgres via the IDs in state and writes its own audit trail as it goes, so
the sequence of events is fully reconstructable from recovery_cases'
audit_logs afterward.

Node order: check_terminal -> load_customer_context -> calculate_recovery_risk
-> diagnose_case -> recommend_intervention -> policy_check -> execute_action
-> record_outcome. The LLM (diagnose_case, recommend_intervention) only ever
*recommends*; policy_check is the sole gate that decides what actually runs
in execute_action.
"""

import asyncio
import logging
from datetime import datetime, timezone
from decimal import Decimal
from uuid import UUID, uuid4
from app.core.time import utc_today

from langgraph.graph import END, StateGraph
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.llm_client import diagnose, recommend
from app.agents.guardrails import assert_action_can_execute
from app.agents.state import RecoveryState
from app.agents.schemas import PersistedRecoveryRecommendation
from app.events.outbox import TransactionalEventPublisher
from app.events import topics
from app.models import (
    AgentDecision,
    AuditLog,
    Company,
    Contact,
    Invoice,
    PolicyDecision,
    PromiseToPay,
    RecoveryAction,
    RecoveryCase,
)
from app.models.enums import (
    AgentDecisionStage,
    AuditActor,
    PolicyDecisionResult,
    ProposedBy,
    RecoveryActionStatus,
    RecoveryActionType,
    RecoveryCaseStatus,
)
from app.services.policy_engine import evaluate_policy
from app.services.promise_tracking import has_unresolved_broken_promise
from app.services.risk_context import build_risk_features
from app.services.invoice_risk_bridge import apply_invoice_risk_to_case
from app.services.payment_risk_scoring import ensure_payment_risk_score
from app.tools.mock_tools import execute_mock_action

TERMINAL_STATUSES = {
    RecoveryCaseStatus.CLOSED,
    RecoveryCaseStatus.CLOSED_UNRECOVERED,
    RecoveryCaseStatus.RECOVERED,
}

logger = logging.getLogger("app.recovery_workflow")


def _assert_case_scope(session: AsyncSession, case: RecoveryCase) -> None:
    # ORM SELECT filtering does not run for an identity-map cache hit.
    scope = session.info.get("analytics_scope")
    if scope is not None and case.company_id not in scope:
        raise PermissionError("Recovery case is outside authorized scope")

_ACTION_EVENT_TYPE = {
    RecoveryActionType.SEND_EMAIL: "EMAIL_SENT",
    RecoveryActionType.SEND_PAYMENT_LINK: "PAYMENT_LINK_SENT",
    RecoveryActionType.TRACK_PROMISE_TO_PAY: "PROMISE_TO_PAY_RECORDED",
    RecoveryActionType.ESCALATE: "ESCALATED",
    RecoveryActionType.WAIT: "WAIT_RECORDED",
    RecoveryActionType.CLOSE_CASE: "CLOSE_REQUESTED",
}


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def _primary_contact(session: AsyncSession, company_id: UUID) -> Contact | None:
    stmt = (
        select(Contact)
        .where(Contact.company_id == company_id)
        .order_by(Contact.is_primary.desc(), Contact.created_at)
        .limit(1)
    )
    return (await session.execute(stmt)).scalars().first()


# Actions that constitute actually reaching out to the customer. Single
# source of truth: _reminder_count counts these and _last_action_at times
# them, and the two silently disagreeing is what broke the cooldown.
_CONTACT_ACTION_TYPES = [
    RecoveryActionType.SEND_EMAIL,
    RecoveryActionType.SEND_PAYMENT_LINK,
]


async def _reminder_count(session: AsyncSession, case_id: UUID) -> int:
    stmt = select(func.count(RecoveryAction.id)).where(
        RecoveryAction.recovery_case_id == case_id,
        RecoveryAction.action_type.in_(_CONTACT_ACTION_TYPES),
        RecoveryAction.status == RecoveryActionStatus.EXECUTED,
    )
    return (await session.execute(stmt)).scalar_one()


async def _last_action_at(session: AsyncSession, case_id: UUID) -> datetime | None:
    """When did we last actually CONTACT this customer?

    The action_type filter is load-bearing and its absence was a real bug.
    execute_action_node stamps status=EXECUTED and executed_at=now() on
    every action it runs, WAIT included. Without this filter, each cycle
    that the policy engine rejected still wrote an executed WAIT, that
    WAIT became "the last action", and the reminder cooldown clock reset
    to zero on every tick.

    Consequence before the fix: a case got reminder 1, then every
    subsequent cycle computed days_since_last_action from the most recent
    WAIT instead of the most recent email. With the scheduler enabled
    (60-second ticks) the value was pinned near zero permanently, so
    MIN_TIME_BETWEEN_REMINDERS_DAYS never elapsed and reminders 2 and 3
    were unreachable -- MAX_EMAIL_REMINDERS was dead configuration.

    Only contact actions reset the cooldown, which is the same set
    _reminder_count counts. The two must agree: one counts the contacts,
    the other times them.
    """
    stmt = (
        select(RecoveryAction.executed_at)
        .where(
            RecoveryAction.recovery_case_id == case_id,
            RecoveryAction.action_type.in_(_CONTACT_ACTION_TYPES),
            RecoveryAction.status == RecoveryActionStatus.EXECUTED,
        )
        .order_by(RecoveryAction.executed_at.desc())
        .limit(1)
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def _latest_promise_summary(session: AsyncSession, case_id: UUID) -> dict | None:
    stmt = (
        select(PromiseToPay)
        .where(PromiseToPay.recovery_case_id == case_id)
        .order_by(PromiseToPay.created_at.desc())
        .limit(1)
    )
    promise = (await session.execute(stmt)).scalars().first()
    if promise is None:
        return None
    return {
        "status": promise.status.value,
        "promised_amount": float(promise.promised_amount),
        "promised_date": promise.promised_date.isoformat(),
    }


async def _next_sequence_number(session: AsyncSession, case_id: UUID) -> int:
    stmt = select(func.coalesce(func.max(RecoveryAction.sequence_number), 0)).where(
        RecoveryAction.recovery_case_id == case_id
    )
    return (await session.execute(stmt)).scalar_one() + 1


def _describe_execution(action_type: RecoveryActionType, invoice: Invoice, result: dict) -> str:
    if result.get('simulated'):
        return f"Simulated {action_type.value.replace('_', ' ').lower()} for invoice {invoice.invoice_number} — no outbound communication or financial action executed."
    if action_type == RecoveryActionType.SEND_EMAIL:
        return f"Reminder email sent regarding invoice {invoice.invoice_number}."
    if action_type == RecoveryActionType.SEND_PAYMENT_LINK:
        return f"Payment link sent for invoice {invoice.invoice_number}: {result.get('payment_link')}."
    if action_type == RecoveryActionType.TRACK_PROMISE_TO_PAY:
        return f"Customer promise to pay recorded: ₹{result.get('promised_amount')} by {result.get('promised_date')}."
    if action_type == RecoveryActionType.ESCALATE:
        return "Case escalated to human finance team."
    if action_type == RecoveryActionType.WAIT:
        return "No action taken this cycle (WAIT)."
    if action_type == RecoveryActionType.CLOSE_CASE:
        return "Case closed without recovery."
    return "Action executed."


# --- nodes -------------------------------------------------------------


async def check_terminal_node(state: RecoveryState, session: AsyncSession) -> dict:
    case = await session.get(RecoveryCase, UUID(state["case_id"]))
    if case is None:
        raise ValueError(f"RecoveryCase {state['case_id']} not found")
    if case.status in TERMINAL_STATUSES:
        return {"terminal": True, "outcome_summary": "case already terminal, no action taken"}

    invoice = await session.get(Invoice, case.invoice_id)
    if invoice is None:
        raise ValueError("Recovery case references a missing invoice")

    if (
        invoice.amount_total > 0
        and invoice.amount_paid >= invoice.amount_total
    ):
        now = _now()
        case.status = RecoveryCaseStatus.CLOSED
        case.recovered_amount = case.revenue_at_risk
        case.closed_at = now

        session.add(
            AuditLog(
                recovery_case_id=case.id,
                entity_type="recovery_case",
                entity_id=case.id,
                event_type="CASE_CLOSED",
                actor=AuditActor.SYSTEM,
                description="Case closed after verified full invoice settlement.",
                occurred_at=now,
            )
        )
        await session.flush()
        return {
            "terminal": True,
            "case_status": case.status.value,
            "outcome_summary": "reconciled_full_payment",
        }

    pending = (
        await session.execute(
            select(func.count(RecoveryAction.id)).where(
                RecoveryAction.recovery_case_id == case.id,
                RecoveryAction.status == RecoveryActionStatus.PENDING_APPROVAL,
            )
        )
    ).scalar_one()
    if pending:
        return {
            "terminal": True,
            "awaiting_review": True,
            "outcome_summary": "an action is waiting for human approval, no new action proposed",
        }
    return {"terminal": False}


async def load_customer_context_node(
    state: RecoveryState,
    session: AsyncSession,
) -> dict:
    case_id = UUID(state["case_id"])
    case = await session.get(
        RecoveryCase,
        case_id,
    )
    _assert_case_scope(session, case)
    invoice = await session.get(
        Invoice,
        case.invoice_id,
    )
    company = await session.get(
        Company,
        case.company_id,
    )
    if invoice is None or invoice.company_id != case.company_id:
        raise PermissionError("Invoice does not belong to the recovery case company")

    # Posting-time late-payment prediction.
    # This is a separate signal from the existing
    # post-due recovery-probability model.
    await ensure_payment_risk_score(
        session,
        invoice,
    )

    # Existing collections/recovery model context.
    features = await build_risk_features(
        session,
        invoice,
    )

    days_overdue = max(
        (
            utc_today()
            - invoice.due_date
        ).days,
        0,
    )

    promise = await _latest_promise_summary(
        session,
        case_id,
    )

    late_payment_probability = (
        float(
            invoice.late_payment_probability
        )
        if invoice.late_payment_probability
        is not None
        else None
    )

    return {
        "invoice_context": {
            "invoice_id": str(invoice.id),
            "company_id": str(invoice.company_id),
            "payment_risk_scored_at": invoice.payment_risk_scored_at.isoformat() if invoice.payment_risk_scored_at else None,
            "payment_risk_artifact_sha256": invoice.payment_risk_artifact_sha256,
            "payment_risk_features": invoice.payment_risk_features,
            "payment_risk_explanation_metadata": invoice.payment_risk_explanation_metadata,
            "invoice_number": (
                invoice.invoice_number
            ),
            "amount_total": float(
                invoice.amount_total
            ),
            "amount_paid": float(
                invoice.amount_paid
            ),
            "posting_date": (
                invoice.posting_date.isoformat()
                if invoice.posting_date
                is not None
                else None
            ),
            "due_date": (
                invoice.due_date.isoformat()
            ),
            "days_overdue": days_overdue,
            "currency": invoice.currency,

            "late_payment_probability": (
                late_payment_probability
            ),
            "payment_risk_model": (
                invoice.payment_risk_model
            ),
            "payment_risk_model_version": (
                invoice.payment_risk_model_version
            ),
            "payment_risk_drivers": (
                invoice.payment_risk_drivers
                or []
            ),
        },
        "customer_context": {
            **features.to_row(),
            "company_name": company.name,
            "segment": (
                company.segment.value
            ),
            "promise_to_pay": promise,
        },
    }


async def calculate_recovery_risk_node(
    state: RecoveryState,
    session: AsyncSession,
) -> dict:
    case = await session.get(
        RecoveryCase,
        UUID(state["case_id"]),
    )
    if case is None:
        raise ValueError("Recovery case not found")

    invoice = await session.get(Invoice, case.invoice_id)
    if invoice is None:
        raise ValueError("Invoice not found")

    await ensure_payment_risk_score(session, invoice)
    apply_invoice_risk_to_case(invoice, case)

    drivers = invoice.payment_risk_drivers or []

    session.add(
        AuditLog(
            recovery_case_id=case.id,
            entity_type="recovery_case",
            entity_id=case.id,
            event_type="RISK_SCORED",
            actor=AuditActor.SYSTEM,
            description=(
                "Using the invoice's historical B2B "
                "late-payment prediction; "
                f"model={invoice.payment_risk_model_version}. "
                "Recovery probability is not estimated. "
                + (
                    "Top SHAP drivers: "
                    + ", ".join(
                        str(
                            driver.get("label")
                            or driver.get("feature")
                            or "unknown"
                        )
                        for driver in drivers[:3]
                    )
                    if drivers
                    else "SHAP drivers unavailable."
                )
            ),
            occurred_at=_now(),
        )
    )
    await session.flush()

    return {
        "risk_score": (
            float(case.risk_score)
            if case.risk_score is not None else None
        ),
        "risk_level": (
            case.risk_level.value
            if case.risk_level is not None else None
        ),
        "recovery_probability": None,
        "risk_drivers": drivers,
    }

async def diagnose_case_node(state: RecoveryState, session: AsyncSession) -> dict:
    case_id = UUID(state["case_id"])
    case = await session.get(RecoveryCase, case_id)
    if case is None or state["invoice_context"].get("invoice_id") != str(case.invoice_id):
        raise ValueError("Invoice evidence does not belong to the recovery case")
    # This is database provenance, not a retrieved document: do not fabricate
    # document hashes or line numbers for an invoice record.
    invoice_source = {"source_id": "invoice:" + str(case.invoice_id),
                      "source_type": "invoice", "source_invoice_id": str(case.invoice_id)}
    from app.tools.knowledge import retrieve_investigation_evidence
    from starlette.concurrency import run_in_threadpool

    try:
        evidence = await run_in_threadpool(
            retrieve_investigation_evidence,
            "late payment calibration limitations historical features recovery reminder approval policy",
        )
    except Exception:
        logger.warning("Knowledge retrieval unavailable")
        evidence = []
    from app.tools.read_only_queries import investigate_question
    from app.core.db import async_session_factory
    from app.core.json_values import json_value

    try:
        query_result = await investigate_question(
            async_session_factory, question="summarize invoice history", company_id=case.company_id,
            as_of=utc_today(),
            authorized_company_ids=tuple(session.info.get("analytics_scope", (case.company_id,))),
        )
        # Normalize dates/Decimal before sending structured data to the provider or audit JSON.
        query_result = {**query_result, "rows": [
            {key: float(value) if isinstance(value, Decimal) else value for key, value in row.items()}
            for row in query_result["rows"]
        ]}
    except Exception:
        logger.warning("Scoped read-only investigation query unavailable")
        query_result = {"status": "unavailable", "rows": []}
    customer_context = json_value({**state["customer_context"], "knowledge_evidence": evidence,
                                  "read_only_investigation": query_result})
    result, model_name = await diagnose(state["invoice_context"], customer_context)
    from app.agents.recordings import response_provenance

    session.add(
        AgentDecision(
            recovery_case_id=case_id,
            stage=AgentDecisionStage.DIAGNOSIS,
            model_name=model_name,
            input_context={"invoice": state["invoice_context"], "customer": customer_context},
            output={**result.model_dump(), "provenance": response_provenance(), "investigation_id": state.get("investigation_id"),
                "source_invoice_id": state["invoice_context"]["invoice_id"],
                "uncertainty": "Diagnosis is an interpretation of available history; the cause and recovery outcome are unverified.",
                "verified_invoice_context": {key: state["invoice_context"].get(key)
                    for key in ("invoice_id", "amount_total", "amount_paid", "due_date", "currency")},
                "sources": [invoice_source] + [
                {key: item[key] for key in ("source_id", "source_sha256", "chunk_id", "start_line", "end_line")}
                for item in evidence
            ]},
            rationale=result.reason,
        )
    )
    session.add(
        AuditLog(
            recovery_case_id=case_id,
            entity_type="recovery_case",
            entity_id=case_id,
            event_type="DIAGNOSIS_GENERATED",
            actor=AuditActor.AI_AGENT,
            description=f"Diagnosis: {result.diagnosis}.",
            occurred_at=_now(),
        )
    )
    await session.flush()

    return {"diagnosis": result.model_dump(), "knowledge_evidence": evidence}


async def recommend_intervention_node(state: RecoveryState, session: AsyncSession) -> dict:
    if "investigation_id" not in state:
        state["investigation_id"] = str(uuid4())
    case_id = UUID(state["case_id"])
    reminder_count = await _reminder_count(session, case_id)

    result, model_name = await recommend(
        state["invoice_context"], {**state["customer_context"], "knowledge_evidence": state.get("knowledge_evidence", [])},
        state["diagnosis"], reminder_count
    )
    from app.agents.recordings import response_provenance
    from app.services.recommendation_evidence import recommendation_evidence
    from app.ml.champion_serving import verified_contract
    try:
        contract = verified_contract()
    except Exception:
        contract = None
    evidence = recommendation_evidence(state["invoice_context"], timestamp=_now(), contract=contract)

    session.add(
        AgentDecision(
            recovery_case_id=case_id,
            stage=AgentDecisionStage.INTERVENTION_RECOMMENDATION,
            model_name=model_name,
            input_context={"diagnosis": state["diagnosis"], "reminder_count": reminder_count},
            output=PersistedRecoveryRecommendation(
                **result.model_dump(), investigation_id=state["investigation_id"], reasons=[result.rationale],
                evidence_references=["invoice:" + state["invoice_context"]["invoice_id"]] + [item["source_id"] for item in state.get("knowledge_evidence", []) if item.get("source_id")],
                uncertainty="Recovery outcomes are not modeled. Recommendation uses available invoice history and policy constraints.",
                execution_mode=("recorded" if model_name.startswith("recorded:") else "deterministic_fallback" if model_name == "rule-based-fallback" else "external_llm"),
            ).model_dump() | {"provenance": response_provenance(),
                "verified_invoice_context": {key: state["invoice_context"].get(key)
                    for key in ("invoice_id", "amount_total", "amount_paid", "due_date", "currency")},
                **evidence},
            rationale=result.rationale,
        )
    )
    session.add(
        AuditLog(
            recovery_case_id=case_id,
            entity_type="recovery_case",
            entity_id=case_id,
            event_type="INTERVENTION_RECOMMENDED",
            actor=AuditActor.AI_AGENT,
            description=f"Recommended action: {result.action}.",
            occurred_at=_now(),
        )
    )
    await session.flush()

    return {
        "recommended_action": result.action,
        "recommendation_rationale": result.rationale,
        "reminder_count": reminder_count,
    }


async def policy_check_node(state: RecoveryState, session: AsyncSession) -> dict:
    case_id = UUID(state["case_id"])
    case = await session.get(RecoveryCase, case_id)

    last_action_at = await _last_action_at(session, case_id)
    from app.core.time import days_since_workflow_action
    days_since_last_action = days_since_workflow_action(last_action_at) if last_action_at else None
    has_broken_promise = await has_unresolved_broken_promise(session, case_id)

    outcome = evaluate_policy(
        recommended_action=RecoveryActionType(state["recommended_action"]),
        reminder_count=state["reminder_count"],
        days_overdue=state["invoice_context"]["days_overdue"],
        revenue_at_risk=float(case.revenue_at_risk),
        case_status=case.status,
        days_since_last_action=days_since_last_action,
        has_broken_promise=has_broken_promise,
    )

    recommendation = (await session.execute(
        select(AgentDecision).where(
            AgentDecision.recovery_case_id == case_id,
            AgentDecision.stage == AgentDecisionStage.INTERVENTION_RECOMMENDATION,
        ).order_by(AgentDecision.created_at.desc(), AgentDecision.id.desc()).limit(1)
    )).scalar_one_or_none()
    if recommendation is not None and recommendation.output.get("investigation_id") == state.get("investigation_id"):
        recommendation.output = {**recommendation.output,
            "policy_decision": outcome.decision.value,
            "policy_reason": outcome.reason,
            "suggested_next_action": outcome.final_action.value,
            "approval_required": outcome.decision == PolicyDecisionResult.REQUIRES_HUMAN_REVIEW,
        }

    seq = await _next_sequence_number(session, case_id)
    action = RecoveryAction(
        recovery_case_id=case_id,
        action_type=outcome.final_action,
        recommended_action_type=RecoveryActionType(state["recommended_action"]),
        status=RecoveryActionStatus.PROPOSED,
        proposed_by=ProposedBy.AI,
        sequence_number=seq,
    )
    session.add(action)
    await session.flush()

    if outcome.decision == PolicyDecisionResult.REJECTED:
        action.status = RecoveryActionStatus.POLICY_REJECTED
    elif outcome.decision == PolicyDecisionResult.REQUIRES_HUMAN_REVIEW:
        # A real gate: nothing executes until a person approves it.
        action.status = RecoveryActionStatus.PENDING_APPROVAL
    else:
        action.status = RecoveryActionStatus.POLICY_APPROVED

    session.add(
        PolicyDecision(
            recovery_action_id=action.id,
            policy_name="deterministic_policy_engine",
            decision=outcome.decision,
            reason=outcome.reason,
            rule=outcome.rule,
            evaluated_at=_now(),
        )
    )
    session.add(
        AuditLog(
            recovery_case_id=case_id,
            entity_type="recovery_action",
            entity_id=action.id,
            event_type=f"POLICY_{outcome.decision.value}",
            actor=AuditActor.POLICY_ENGINE,
            description=outcome.reason,
            occurred_at=_now(),
        )
    )
    await session.flush()

    result = {
        "action_id": str(action.id),
        "final_action": outcome.final_action.value,
        "policy_decision": outcome.decision.value,
        "policy_reason": outcome.reason,
    }
    if outcome.decision == PolicyDecisionResult.REJECTED:
        # The graph stops here. Expose the simulated policy block to callers
        # without claiming that the rejected action was executed.
        result["action_result"] = {"simulated": True, "blocked": True, "success": False}
    return result


async def execute_action_node(state: RecoveryState, session: AsyncSession) -> dict:
    action = (await session.execute(select(RecoveryAction).where(
        RecoveryAction.id == UUID(state["action_id"])
    ).with_for_update().execution_options(populate_existing=True))).scalar_one_or_none()
    case_id = UUID(state["case_id"])
    assert_action_can_execute(action, case_id)
    case = await session.get(RecoveryCase, case_id)
    _assert_case_scope(session, case)
    invoice = await session.get(Invoice, case.invoice_id)
    if invoice is None or invoice.company_id != case.company_id:
        raise PermissionError("Invoice does not belong to the recovery case company")
    if state["final_action"] != action.action_type.value:
        raise PermissionError("Execution state differs from authorized action")
    contact = await _primary_contact(session, case.company_id)

    from app.services.action_execution import execute_authorized_action
    result = await execute_authorized_action(
        session, action, case, invoice, contact, executor=execute_mock_action,
    )
    if action.status == RecoveryActionStatus.FAILED:
        return {"action_result": result}

    session.add(
        AuditLog(
            recovery_case_id=case.id,
            entity_type="recovery_action",
            entity_id=action.id,
            event_type=_ACTION_EVENT_TYPE[action.action_type],
            actor=AuditActor.SYSTEM,
            description=_describe_execution(action.action_type, invoice, result),
            occurred_at=_now(),
        )
    )
    await session.flush()

    return {"action_result": result}


async def record_outcome_node(state: RecoveryState, session: AsyncSession) -> dict:
    case = await session.get(RecoveryCase, UUID(state["case_id"]))
    invoice = await session.get(Invoice, case.invoice_id)
    now = _now()
    final_action = state["final_action"]
    if state.get("action_result", {}).get("success") is False:
        return {"case_status": case.status.value, "outcome_summary": "action_failed"}

    if invoice.amount_paid >= invoice.amount_total:
        case.status = RecoveryCaseStatus.CLOSED
        # Credit this case only with what IT was opened to recover, not the
        # invoice's lifetime payments. risk_engine opens a case with
        # revenue_at_risk = amount_total - amount_paid (the outstanding
        # balance at that moment), and mark_overdue_invoices deliberately
        # includes PARTIALLY_PAID invoices, so the two are different numbers
        # whenever anything was paid before the case existed.
        #
        # Using invoice.amount_paid here double-counted that earlier money:
        # a 100,000 invoice with 40,000 already paid opens a case at 60,000
        # at risk, and on full payment was credited with 100,000 recovered.
        # metrics.py divides summed recovered_amount by (at_risk +
        # recovered), so the dashboard reported a recovery rate above 100%.
        case.recovered_amount = case.revenue_at_risk
        case.closed_at = now
        outcome_summary = "recovered"
        session.add(
            AuditLog(
                recovery_case_id=case.id,
                entity_type="recovery_case",
                entity_id=case.id,
                event_type="CASE_CLOSED",
                actor=AuditActor.SYSTEM,
                description=f"Case closed: ₹{case.recovered_amount:,.2f} recovered.",
                occurred_at=now,
            )
        )
    elif final_action == RecoveryActionType.ESCALATE.value:
        case.status = RecoveryCaseStatus.ESCALATED
        outcome_summary = "escalated"
    elif final_action == RecoveryActionType.CLOSE_CASE.value:
        case.status = RecoveryCaseStatus.CLOSED_UNRECOVERED
        case.closed_at = now
        outcome_summary = "closed_unrecovered"
        session.add(
            AuditLog(
                recovery_case_id=case.id,
                entity_type="recovery_case",
                entity_id=case.id,
                event_type="CASE_CLOSED",
                actor=AuditActor.SYSTEM,
                description="Case closed without recovery.",
                occurred_at=now,
            )
        )
    elif case.recovery_window_deadline and utc_today() > case.recovery_window_deadline:
        case.status = RecoveryCaseStatus.CLOSED_UNRECOVERED
        case.closed_at = now
        outcome_summary = "window_expired"
        session.add(
            AuditLog(
                recovery_case_id=case.id,
                entity_type="recovery_case",
                entity_id=case.id,
                event_type="RECOVERY_WINDOW_EXPIRED",
                actor=AuditActor.SYSTEM,
                description=(
                    f"Recovery window ({case.recovery_window_deadline.isoformat()}) passed "
                    f"without recovery; case closed."
                ),
                occurred_at=now,
            )
        )
    elif final_action == RecoveryActionType.TRACK_PROMISE_TO_PAY.value:
        case.status = RecoveryCaseStatus.MONITORING
        outcome_summary = "monitoring_promise"
    else:
        if case.status not in (RecoveryCaseStatus.MONITORING, RecoveryCaseStatus.ESCALATED):
            case.status = RecoveryCaseStatus.OPEN
        outcome_summary = "open"

    await session.flush()
    return {"case_status": case.status.value, "outcome_summary": outcome_summary}


# --- graph assembly ------------------------------------------------------


def _build_graph(session: AsyncSession):
    graph = StateGraph(RecoveryState)
    from app.agents.state import validate_transition, validate_node_output

    def guarded(name, node):
        async def invoke(state):
            validated = validate_transition(name, state)
            from time import perf_counter_ns
            started_ns = perf_counter_ns()
            try:
                result = await node(validated, session=session)
                validate_node_output(name, validated, result)
            except BaseException:
                logger.warning(
                    "Recovery graph node failed; cycle will roll back",
                    extra={
                        "trace_node": name,
                        "investigation_id": validated.get("investigation_id"),
                    },
                )
                raise
            from app.services.agent_tracing import (
                tracing_enabled,
                persist_span,
            )
            if tracing_enabled():
                await persist_span(
                    session,
                    validated,
                    name,
                    started_ns,
                    result,
                )
            return result  # INVOICEIQ_PHASE7C_NODE_TRACING_V1
        return invoke

    for name, node in (
        ("check_terminal", check_terminal_node),
        ("load_customer_context", load_customer_context_node),
        ("calculate_recovery_risk", calculate_recovery_risk_node),
        ("diagnose_case", diagnose_case_node),
        ("recommend_intervention", recommend_intervention_node),
        ("policy_check", policy_check_node),
        ("execute_action", execute_action_node),
        ("record_outcome", record_outcome_node),
    ):
        graph.add_node(name, guarded(name, node))

    graph.set_entry_point("check_terminal")
    graph.add_conditional_edges(
        "check_terminal",
        lambda s: "stop" if s.get("terminal") else "continue",
        {"stop": END, "continue": "load_customer_context"},
    )
    graph.add_edge("load_customer_context", "calculate_recovery_risk")
    graph.add_edge("calculate_recovery_risk", "diagnose_case")
    graph.add_edge("diagnose_case", "recommend_intervention")
    graph.add_edge("recommend_intervention", "policy_check")
    graph.add_conditional_edges(
        "policy_check",
        lambda s: "hold" if s.get("policy_decision") != PolicyDecisionResult.APPROVED.value
        else "execute",
        {"hold": END, "execute": "execute_action"},
    )
    graph.add_edge("execute_action", "record_outcome")
    graph.add_edge("record_outcome", END)

    return graph.compile()


async def run_recovery_cycle(session: AsyncSession, case_id: UUID) -> RecoveryState:
    case = await session.get(RecoveryCase, case_id)
    if case is None:
        raise ValueError(f"RecoveryCase {case_id} not found")
    _assert_case_scope(session, case)
    # API and scheduler share this transaction lock; Redis leases can expire.
    case = (await session.execute(select(RecoveryCase).where(
        RecoveryCase.id == case_id
    ).with_for_update().execution_options(populate_existing=True))).scalar_one_or_none()
    if case is None:
        raise PermissionError("Recovery case is unavailable in authorized scope")
    _assert_case_scope(session, case)

    logger.info("recovery cycle starting", extra={"case_id": str(case_id)})
    graph = _build_graph(session)
    try:
        final_state: RecoveryState = await asyncio.wait_for(
            graph.ainvoke({"case_id": str(case_id), "investigation_id": str(uuid4())}, config={"recursion_limit": 16}), timeout=60,
        )
    except BaseException:
        logger.warning("Recovery cycle failed; transaction rolled back")
        # Cancellation and tool failures must not leave a half-written recovery cycle.
        await session.rollback()
        raise
    logger.info(
        "recovery cycle finished",
        extra={
            "case_id": str(case_id),
            "final_action": final_state.get("final_action"),
            "policy_decision": final_state.get("policy_decision"),
            "case_status": final_state.get("case_status"),
            "outcome": final_state.get("outcome_summary"),
        },
    )


    if final_state.get("outcome_summary") == "reconciled_full_payment":
        publisher = TransactionalEventPublisher(session)
        await publisher.publish(
            topics.RECOVERY_CASE_CLOSED,
            str(case_id),
            {
                "case_id": str(case_id),
                "status": RecoveryCaseStatus.CLOSED.value,
                "recovered_amount": str(case.recovered_amount),
            },
        )

    if not final_state.get("terminal") and "action_id" in final_state:
        publisher = TransactionalEventPublisher(session)
        await publisher.publish(
            topics.RISK_SCORED,
            str(case_id),
            {
                "case_id": str(case_id),
                "risk_score": final_state.get("risk_score"),
                "risk_level": final_state.get("risk_level"),
                "recovery_probability": final_state.get("recovery_probability"),
                "top_drivers": final_state.get("risk_drivers"),
            },
        )
        await publisher.publish(
            topics.ACTION_PROPOSED,
            str(case_id),
            {
                "case_id": str(case_id),
                "action_id": final_state["action_id"],
                "action_type": final_state.get("final_action"),
                "policy_decision": final_state.get("policy_decision"),
                "policy_reason": final_state.get("policy_reason"),
            },
        )
        if final_state.get("policy_decision") == PolicyDecisionResult.REQUIRES_HUMAN_REVIEW.value:
            await publisher.publish(
                topics.REVIEW_REQUESTED,
                str(case_id),
                {
                    "case_id": str(case_id),
                    "action_id": final_state["action_id"],
                    "action_type": final_state.get("final_action"),
                    "reason": final_state.get("policy_reason"),
                },
            )
            await session.commit()
            return final_state
        if final_state.get("policy_decision") == PolicyDecisionResult.REJECTED.value or final_state.get("action_result", {}).get("success") is False:
            await session.commit()
            return final_state
        await publisher.publish(
            topics.RECOVERY_ACTION_COMPLETED,
            str(case_id),
            {
                "case_id": str(case_id),
                "action_id": final_state["action_id"],
                "action_type": final_state.get("final_action"),
                "outcome": final_state.get("outcome_summary"),
            },
        )
        if final_state.get("final_action") == RecoveryActionType.TRACK_PROMISE_TO_PAY.value:
            result = final_state.get("action_result") or {}
            await publisher.publish(
                topics.PROMISE_TO_PAY_CREATED,
                str(case_id),
                {
                    "case_id": str(case_id),
                    "promised_amount": result.get("promised_amount"),
                    "promised_date": result.get("promised_date"),
                },
            )
        if final_state.get("case_status") in ("CLOSED", "CLOSED_UNRECOVERED"):
            await publisher.publish(
                topics.RECOVERY_CASE_CLOSED,
                str(case_id),
                {
                    "case_id": str(case_id),
                    "status": final_state.get("case_status"),
                    "outcome": final_state.get("outcome_summary"),
                },
            )

    await session.commit()
    return final_state
