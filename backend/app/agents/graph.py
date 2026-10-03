"""The LangGraph recovery workflow.

One graph invocation = one recovery cycle for a single case, triggered
manually or by a scheduler (POST /recovery-cases/{id}/run) — there is no
long-running consumer or persisted graph state in V1 (see
docs/architecture.md decision #2). Every node re-fetches what it needs from
Postgres via the IDs in state and writes its own audit trail as it goes, so
the sequence of events is fully reconstructable from recovery_cases'
audit_logs afterward.

Node order: check_terminal -> load_customer_context -> calculate_recovery_risk
-> diagnose_case -> recommend_intervention -> policy_check -> execute_action
-> record_outcome. The LLM (diagnose_case, recommend_intervention) only ever
*recommends*; policy_check is the sole gate that decides what actually runs
in execute_action.
"""

import functools
import logging
from datetime import datetime, timezone
from decimal import Decimal
from uuid import UUID
from app.core.time import utc_today

from langgraph.graph import END, StateGraph
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.llm_client import diagnose, recommend
from app.agents.state import RecoveryState
from app.events import get_publisher
from app.events import topics
from app.ml.features import FEATURE_NAMES, RiskFeatures
from app.ml.risk_model import explain as explain_risk
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
from app.services.payment_risk_scoring import ensure_payment_risk_score
from app.tools.mock_tools import execute_mock_action

TERMINAL_STATUSES = {
    RecoveryCaseStatus.CLOSED,
    RecoveryCaseStatus.CLOSED_UNRECOVERED,
    RecoveryCaseStatus.RECOVERED,
}

logger = logging.getLogger("app.recovery_workflow")

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
    invoice = await session.get(
        Invoice,
        case.invoice_id,
    )
    company = await session.get(
        Company,
        case.company_id,
    )

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


async def calculate_recovery_risk_node(state: RecoveryState, session: AsyncSession) -> dict:
    customer_context = state["customer_context"]
    features = RiskFeatures(**{name: customer_context[name] for name in FEATURE_NAMES})
    result = explain_risk(features)
    drivers = [
        {
            "feature": c.feature,
            "label": c.label,
            "value": c.value,
            "shap_value": c.shap_value,
            "direction": c.direction,
        }
        for c in result.contributions[:3]
    ]
    driver_text = "; ".join(f"{d['label']} ({d['shap_value']:+.2f})" for d in drivers)

    case = await session.get(RecoveryCase, UUID(state["case_id"]))
    case.risk_score = Decimal(str(result.risk_score))
    case.risk_level = result.risk_level
    case.recovery_probability = Decimal(str(result.recovery_probability))

    session.add(
        AuditLog(
            recovery_case_id=case.id,
            entity_type="recovery_case",
            entity_id=case.id,
            event_type="RISK_SCORED",
            actor=AuditActor.SYSTEM,
            description=(
                f"Risk re-scored via ML model: {result.risk_level.value} ({result.risk_score}), "
                f"recovery probability {result.recovery_probability:.0%}. "
                f"Top SHAP drivers: {driver_text}."
            ),
            occurred_at=_now(),
        )
    )
    await session.flush()

    return {
        "risk_score": result.risk_score,
        "risk_level": result.risk_level.value,
        "recovery_probability": result.recovery_probability,
        "risk_drivers": drivers,
    }


async def diagnose_case_node(state: RecoveryState, session: AsyncSession) -> dict:
    case_id = UUID(state["case_id"])
    result, model_name = await diagnose(state["invoice_context"], state["customer_context"])

    session.add(
        AgentDecision(
            recovery_case_id=case_id,
            stage=AgentDecisionStage.DIAGNOSIS,
            model_name=model_name,
            input_context={"invoice": state["invoice_context"], "customer": state["customer_context"]},
            output=result.model_dump(),
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

    return {"diagnosis": result.model_dump()}


async def recommend_intervention_node(state: RecoveryState, session: AsyncSession) -> dict:
    case_id = UUID(state["case_id"])
    reminder_count = await _reminder_count(session, case_id)

    result, model_name = await recommend(
        state["invoice_context"], state["customer_context"], state["diagnosis"], reminder_count
    )

    session.add(
        AgentDecision(
            recovery_case_id=case_id,
            stage=AgentDecisionStage.INTERVENTION_RECOMMENDATION,
            model_name=model_name,
            input_context={"diagnosis": state["diagnosis"], "reminder_count": reminder_count},
            output=result.model_dump(),
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
    days_since_last_action = (utc_today() - last_action_at.date()).days if last_action_at else None
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

    return {
        "action_id": str(action.id),
        "final_action": outcome.final_action.value,
        "policy_decision": outcome.decision.value,
        "policy_reason": outcome.reason,
    }


async def execute_action_node(state: RecoveryState, session: AsyncSession) -> dict:
    action = await session.get(RecoveryAction, UUID(state["action_id"]))
    case = await session.get(RecoveryCase, UUID(state["case_id"]))
    invoice = await session.get(Invoice, case.invoice_id)
    contact = await _primary_contact(session, case.company_id)

    result = await execute_mock_action(session, action.action_type, case, invoice, contact)

    action.status = RecoveryActionStatus.EXECUTED
    action.executed_at = _now()
    action.result = result
    await session.flush()

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
                description=f"Case closed: ₹{invoice.amount_paid:,.2f} recovered.",
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
    graph.add_node("check_terminal", functools.partial(check_terminal_node, session=session))
    graph.add_node("load_customer_context", functools.partial(load_customer_context_node, session=session))
    graph.add_node("calculate_recovery_risk", functools.partial(calculate_recovery_risk_node, session=session))
    graph.add_node("diagnose_case", functools.partial(diagnose_case_node, session=session))
    graph.add_node("recommend_intervention", functools.partial(recommend_intervention_node, session=session))
    graph.add_node("policy_check", functools.partial(policy_check_node, session=session))
    graph.add_node("execute_action", functools.partial(execute_action_node, session=session))
    graph.add_node("record_outcome", functools.partial(record_outcome_node, session=session))

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
        lambda s: "hold" if s.get("policy_decision") == PolicyDecisionResult.REQUIRES_HUMAN_REVIEW.value
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

    logger.info("recovery cycle starting", extra={"case_id": str(case_id)})
    graph = _build_graph(session)
    final_state: RecoveryState = await graph.ainvoke({"case_id": str(case_id)})
    await session.commit()
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

    if not final_state.get("terminal") and "action_id" in final_state:
        publisher = get_publisher()
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

    return final_state
