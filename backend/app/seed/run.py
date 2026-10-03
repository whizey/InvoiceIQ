"""Seed the database with a realistic, narrative demo dataset.

Run with:  python -m app.seed.run

This clears all domain tables first, so it is safe to re-run at any time —
each run rebuilds the same story with dates recalculated relative to
"today", so scenarios like "5 days overdue" stay true no matter when you
seed. This is demo/dev data only; it is not the synthetic *training* set
used for the Phase 6 ML model (that will be generated separately, at a
larger scale, purely for model fitting).
"""

import asyncio
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal

from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import async_session_factory, engine
from app.models import (
    AgentDecision,
    AuditLog,
    CommunicationLog,
    Company,
    Contact,
    Invoice,
    Payment,
    PaymentEvent,
    PolicyDecision,
    PromiseToPay,
    RecoveryAction,
    RecoveryCase,
)
from app.models.enums import (
    AgentDecisionStage,
    AuditActor,
    CommunicationChannel,
    CommunicationDirection,
    CommunicationStatus,
    InvoiceStatus,
    PaymentEventType,
    PaymentMethod,
    PaymentStatus,
    PolicyDecisionResult,
    ProposedBy,
    PromiseToPayStatus,
    RecoveryActionStatus,
    RecoveryActionType,
    RecoveryCaseStatus,
    RiskLevel,
)
from app.seed.data import COMPANIES, CompanySpec
from app.services.policy_engine import (
    RULE_HIGH_VALUE_OVERDUE_FORCED_ESCALATE,
    RULE_HIGH_VALUE_REVIEW,
    RULE_NO_RESTRICTION,
    RULE_REMINDER_APPROVED,
)
from sqlalchemy import select
from app.services.payment_risk_scoring import ensure_payment_risk_score

TODAY = datetime.now(timezone.utc).date()


def d(days_ago: int) -> date:
    return TODAY - timedelta(days=days_ago)


def at(day: date, hour: int = 10, minute: int = 0) -> datetime:
    return datetime.combine(day, time(hour, minute), tzinfo=timezone.utc)


DOMAIN_MODELS = [
    AuditLog,
    PolicyDecision,
    CommunicationLog,
    PromiseToPay,
    AgentDecision,
    RecoveryAction,
    RecoveryCase,
    PaymentEvent,
    Payment,
    Invoice,
    Contact,
    Company,
]


async def clear_all(session: AsyncSession) -> None:
    for model in DOMAIN_MODELS:
        await session.execute(delete(model))
    await session.commit()


async def create_companies(session: AsyncSession) -> dict[str, tuple[Company, dict[str, Contact]]]:
    created: dict[str, tuple[Company, dict[str, Contact]]] = {}

    for spec in COMPANIES:
        company = Company(
            name=spec.name,
            industry=spec.industry,
            segment=spec.segment,
            region="DEMO_REGION",
            city=f"DEMO_{spec.key.upper()}",
            zipcode=f"DEMO_{spec.key.upper()}_ZIP",
        )
        session.add(company)
        await session.flush()

        contacts: dict[str, Contact] = {}
        for c in spec.contacts:
            contact = Contact(
                company_id=company.id,
                name=c.name,
                email=c.email,
                phone=c.phone,
                role=c.role,
                is_primary=c.is_primary,
            )
            session.add(contact)
            await session.flush()
            contacts[c.name] = contact

        created[spec.key] = (company, contacts)
    return created


async def seed_history(session: AsyncSession, spec: CompanySpec, company: Company) -> None:
    for idx, h in enumerate(spec.history, start=1):
        due = d(h.due_days_ago)
        issue = due - timedelta(days=h.term_days)
        invoice = Invoice(
            company_id=company.id,
            invoice_number=f"INV-{spec.key.upper()}-{1000 + idx}",
            amount_total=h.amount,
            amount_paid=h.amount,
            issue_date=issue,
            posting_date=issue,
            due_date=due,
            payment_term=h.term_days,
            payment_method_description=PaymentMethod.BANK_TRANSFER.value,
            status=InvoiceStatus.PAID,
        )
        session.add(invoice)
        await session.flush()

        paid_date = due + timedelta(days=h.paid_days_after_due)
        session.add(
            Payment(
                invoice_id=invoice.id,
                amount=h.amount,
                payment_date=at(paid_date),
                method=PaymentMethod.BANK_TRANSFER,
                status=PaymentStatus.SUCCESS,
            )
        )
        session.add(
            PaymentEvent(
                invoice_id=invoice.id,
                event_type=PaymentEventType.INVOICE_CREATED,
                payload={"amount": str(h.amount)},
                occurred_at=at(issue),
            )
        )
        session.add(
            PaymentEvent(
                invoice_id=invoice.id,
                event_type=PaymentEventType.PAYMENT_RECEIVED,
                payload={"amount": str(h.amount), "days_after_due": h.paid_days_after_due},
                occurred_at=at(paid_date),
            )
        )


def audit(case: RecoveryCase, entity_type: str, entity_id, event_type: str, actor: AuditActor, description: str, occurred_at: datetime, metadata: dict | None = None) -> AuditLog:
    return AuditLog(
        recovery_case_id=case.id,
        entity_type=entity_type,
        entity_id=entity_id,
        event_type=event_type,
        actor=actor,
        description=description,
        metadata_=metadata,
        occurred_at=occurred_at,
    )


async def scenario_a_low_risk(session: AsyncSession, company: Company, contacts: dict[str, Contact]) -> None:
    """Northwind: reliable payer, invoice overdue only 5 days, low risk, single reminder in flight."""
    due = d(5)
    issue = due - timedelta(days=30)
    invoice = Invoice(
        company_id=company.id,
        invoice_number="INV-NORTHWIND-2001",
        amount_total=Decimal("450000.00"),
        amount_paid=Decimal("0.00"),
        issue_date=issue,
        posting_date=issue,
        due_date=due,
        payment_term=30,
        payment_method_description=PaymentMethod.BANK_TRANSFER.value,
        status=InvoiceStatus.OVERDUE,
    )
    session.add(invoice)
    await session.flush()
    session.add(PaymentEvent(invoice_id=invoice.id, event_type=PaymentEventType.INVOICE_CREATED, payload={}, occurred_at=at(issue)))
    session.add(PaymentEvent(invoice_id=invoice.id, event_type=PaymentEventType.INVOICE_OVERDUE, payload={}, occurred_at=at(due + timedelta(days=1))))

    opened_at = at(due + timedelta(days=1))
    case = RecoveryCase(
        invoice_id=invoice.id,
        company_id=company.id,
        status=RecoveryCaseStatus.OPEN,
        opened_at=opened_at,
        revenue_at_risk=invoice.amount_total,
        recovered_amount=Decimal("0.00"),
        risk_score=Decimal("22.50"),
        risk_level=RiskLevel.LOW,
        recovery_probability=Decimal("0.9100"),
        recovery_window_deadline=due + timedelta(days=90),
    )
    session.add(case)
    await session.flush()

    action = RecoveryAction(
        recovery_case_id=case.id,
        action_type=RecoveryActionType.SEND_EMAIL,
        recommended_action_type=RecoveryActionType.SEND_EMAIL,
        status=RecoveryActionStatus.EXECUTED,
        proposed_by=ProposedBy.AI,
        sequence_number=1,
        executed_at=opened_at,
        result={"channel": "email", "simulated": True},
    )
    session.add(action)
    await session.flush()

    session.add(
        PolicyDecision(
            recovery_action_id=action.id,
            policy_name="MAX_EMAIL_REMINDERS",
            decision=PolicyDecisionResult.APPROVED,
            reason="Reminder count (1) below MAX_EMAIL_REMINDERS (3); amount below high-value review threshold.",
            rule=RULE_REMINDER_APPROVED,
            evaluated_at=opened_at,
        )
    )
    session.add(
        AgentDecision(
            recovery_case_id=case.id,
            stage=AgentDecisionStage.DIAGNOSIS,
            model_name="gpt-4o-mini",
            input_context={"days_overdue": 5, "prior_late_payments": 0, "amount": str(invoice.amount_total)},
            output={"diagnosis": "Isolated late payment", "recommended_priority": "low"},
            rationale="Customer has a clean four-invoice payment history with no prior lateness; likely an administrative delay.",
        )
    )
    session.add(
        AgentDecision(
            recovery_case_id=case.id,
            stage=AgentDecisionStage.INTERVENTION_RECOMMENDATION,
            model_name="gpt-4o-mini",
            input_context={"diagnosis": "Isolated late payment"},
            output={"action": "SEND_EMAIL"},
            rationale="A single friendly reminder is proportionate for a first-time, short delay from a reliable payer.",
        )
    )
    session.add(
        CommunicationLog(
            recovery_case_id=case.id,
            contact_id=contacts["Priya Sharma"].id,
            channel=CommunicationChannel.EMAIL,
            direction=CommunicationDirection.OUTBOUND,
            subject="Friendly reminder: Invoice INV-NORTHWIND-2001 is now overdue",
            body="Hi Priya, our records show Invoice INV-NORTHWIND-2001 (₹4,50,000) was due on "
            f"{due.isoformat()} and remains unpaid. Could you confirm expected payment timing?",
            status=CommunicationStatus.SIMULATED,
            sent_at=opened_at,
        )
    )

    for entity_type, entity_id, event_type, actor, desc, ts in [
        ("recovery_case", case.id, "CASE_CREATED", AuditActor.SYSTEM, "Recovery case opened for overdue invoice INV-NORTHWIND-2001 (₹4,50,000).", opened_at),
        ("recovery_case", case.id, "RISK_CALCULATED", AuditActor.SYSTEM, "Risk scored: LOW (22.5), recovery probability 91%.", opened_at),
        ("recovery_case", case.id, "DIAGNOSIS_GENERATED", AuditActor.AI_AGENT, "Diagnosis: isolated late payment, low priority.", opened_at),
        ("recovery_action", action.id, "INTERVENTION_RECOMMENDED", AuditActor.AI_AGENT, "Recommended action: SEND_EMAIL.", opened_at),
        ("recovery_action", action.id, "POLICY_APPROVED", AuditActor.POLICY_ENGINE, "Policy approved SEND_EMAIL (reminder 1 of 3).", opened_at),
        ("recovery_action", action.id, "EMAIL_SENT", AuditActor.SYSTEM, "Reminder email sent to Priya Sharma.", opened_at),
    ]:
        session.add(audit(case, entity_type, entity_id, event_type, actor, desc, ts))


async def scenario_b_medium_risk(session: AsyncSession, company: Company, contacts: dict[str, Contact]) -> None:
    """Bluepeak: repeated late payer, invoice 30 days overdue, two escalating reminders sent."""
    due = d(30)
    issue = due - timedelta(days=30)
    invoice = Invoice(
        company_id=company.id,
        invoice_number="INV-BLUEPEAK-2005",
        amount_total=Decimal("210000.00"),
        amount_paid=Decimal("0.00"),
        issue_date=issue,
        posting_date=issue,
        due_date=due,
        payment_term=30,
        payment_method_description=PaymentMethod.BANK_TRANSFER.value,
        status=InvoiceStatus.OVERDUE,
    )
    session.add(invoice)
    await session.flush()
    session.add(PaymentEvent(invoice_id=invoice.id, event_type=PaymentEventType.INVOICE_CREATED, payload={}, occurred_at=at(issue)))
    session.add(PaymentEvent(invoice_id=invoice.id, event_type=PaymentEventType.INVOICE_OVERDUE, payload={}, occurred_at=at(due + timedelta(days=1))))

    opened_at = at(due + timedelta(days=1))
    case = RecoveryCase(
        invoice_id=invoice.id,
        company_id=company.id,
        status=RecoveryCaseStatus.OPEN,
        opened_at=opened_at,
        revenue_at_risk=invoice.amount_total,
        recovered_amount=Decimal("0.00"),
        risk_score=Decimal("55.00"),
        risk_level=RiskLevel.MEDIUM,
        recovery_probability=Decimal("0.6200"),
        recovery_window_deadline=due + timedelta(days=90),
    )
    session.add(case)
    await session.flush()

    action1_time = opened_at
    action1 = RecoveryAction(
        recovery_case_id=case.id,
        action_type=RecoveryActionType.SEND_EMAIL,
        recommended_action_type=RecoveryActionType.SEND_EMAIL,
        status=RecoveryActionStatus.EXECUTED,
        proposed_by=ProposedBy.AI,
        sequence_number=1,
        executed_at=action1_time,
        result={"channel": "email", "simulated": True},
    )
    session.add(action1)
    await session.flush()
    session.add(
        PolicyDecision(
            recovery_action_id=action1.id,
            policy_name="MAX_EMAIL_REMINDERS",
            decision=PolicyDecisionResult.APPROVED,
            reason="Reminder count (1) below MAX_EMAIL_REMINDERS (3).",
            rule=RULE_REMINDER_APPROVED,
            evaluated_at=action1_time,
        )
    )

    action2_time = at(due + timedelta(days=15))
    action2 = RecoveryAction(
        recovery_case_id=case.id,
        action_type=RecoveryActionType.SEND_PAYMENT_LINK,
        recommended_action_type=RecoveryActionType.SEND_PAYMENT_LINK,
        status=RecoveryActionStatus.EXECUTED,
        proposed_by=ProposedBy.AI,
        sequence_number=2,
        executed_at=action2_time,
        result={"payment_link": "https://pay.example.com/mock/bluepeak-2005", "simulated": True},
    )
    session.add(action2)
    await session.flush()
    session.add(
        PolicyDecision(
            recovery_action_id=action2.id,
            policy_name="MIN_TIME_BETWEEN_REMINDERS",
            decision=PolicyDecisionResult.APPROVED,
            reason="14 days elapsed since previous reminder, exceeding MIN_TIME_BETWEEN_REMINDERS (7 days); reminder count (2) below cap.",
            rule=RULE_REMINDER_APPROVED,
            evaluated_at=action2_time,
        )
    )

    session.add(
        AgentDecision(
            recovery_case_id=case.id,
            stage=AgentDecisionStage.DIAGNOSIS,
            model_name="gpt-4o-mini",
            input_context={"days_overdue": 30, "prior_late_payments": 3, "prior_on_time_payments": 1},
            output={"diagnosis": "Recurring late-payment pattern", "recommended_priority": "medium"},
            rationale="Customer has paid late in 3 of the last 4 invoices (15-18 days late); this is consistent behavior, not an anomaly.",
        )
    )
    session.add(
        AgentDecision(
            recovery_case_id=case.id,
            stage=AgentDecisionStage.INTERVENTION_RECOMMENDATION,
            model_name="gpt-4o-mini",
            input_context={"reminder_count": 1, "days_since_last_action": 14},
            output={"action": "SEND_PAYMENT_LINK"},
            rationale="First reminder had no response after two weeks; a direct payment link lowers friction for a customer who habitually pays late rather than disputes.",
        )
    )
    session.add(
        CommunicationLog(
            recovery_case_id=case.id,
            contact_id=contacts["Karan Mehta"].id,
            channel=CommunicationChannel.EMAIL,
            direction=CommunicationDirection.OUTBOUND,
            subject="Invoice INV-BLUEPEAK-2005 overdue",
            body=f"Hi Karan, Invoice INV-BLUEPEAK-2005 (₹2,10,000) was due on {due.isoformat()} and is now overdue.",
            status=CommunicationStatus.SIMULATED,
            sent_at=action1_time,
        )
    )
    session.add(
        CommunicationLog(
            recovery_case_id=case.id,
            contact_id=contacts["Karan Mehta"].id,
            channel=CommunicationChannel.EMAIL,
            direction=CommunicationDirection.OUTBOUND,
            subject="Payment link for Invoice INV-BLUEPEAK-2005",
            body="Hi Karan, for your convenience here is a direct payment link to settle the outstanding ₹2,10,000.",
            status=CommunicationStatus.SIMULATED,
            sent_at=action2_time,
        )
    )

    for entity_type, entity_id, event_type, actor, desc, ts in [
        ("recovery_case", case.id, "CASE_CREATED", AuditActor.SYSTEM, "Recovery case opened for overdue invoice INV-BLUEPEAK-2005 (₹2,10,000).", opened_at),
        ("recovery_case", case.id, "RISK_CALCULATED", AuditActor.SYSTEM, "Risk scored: MEDIUM (55.0), recovery probability 62%.", opened_at),
        ("recovery_case", case.id, "DIAGNOSIS_GENERATED", AuditActor.AI_AGENT, "Diagnosis: recurring late-payment pattern.", opened_at),
        ("recovery_action", action1.id, "POLICY_APPROVED", AuditActor.POLICY_ENGINE, "Policy approved SEND_EMAIL (reminder 1 of 3).", action1_time),
        ("recovery_action", action1.id, "EMAIL_SENT", AuditActor.SYSTEM, "Reminder email sent to Karan Mehta.", action1_time),
        ("recovery_action", action2.id, "INTERVENTION_RECOMMENDED", AuditActor.AI_AGENT, "Recommended action: SEND_PAYMENT_LINK.", action2_time),
        ("recovery_action", action2.id, "POLICY_APPROVED", AuditActor.POLICY_ENGINE, "Policy approved SEND_PAYMENT_LINK (reminder 2 of 3).", action2_time),
        ("recovery_action", action2.id, "PAYMENT_LINK_SENT", AuditActor.SYSTEM, "Payment link generated and sent to Karan Mehta.", action2_time),
    ]:
        session.add(audit(case, entity_type, entity_id, event_type, actor, desc, ts))


async def scenario_c_high_risk_escalated(session: AsyncSession, company: Company, contacts: dict[str, Contact]) -> None:
    """Vertex: large enterprise invoice, 60 days overdue, escalated to human finance team."""
    due = d(60)
    issue = due - timedelta(days=30)
    invoice = Invoice(
        company_id=company.id,
        invoice_number="INV-VERTEX-3010",
        amount_total=Decimal("1800000.00"),
        amount_paid=Decimal("0.00"),
        issue_date=issue,
        posting_date=issue,
        due_date=due,
        payment_term=30,
        payment_method_description=PaymentMethod.BANK_TRANSFER.value,
        status=InvoiceStatus.OVERDUE,
    )
    session.add(invoice)
    await session.flush()
    session.add(PaymentEvent(invoice_id=invoice.id, event_type=PaymentEventType.INVOICE_CREATED, payload={}, occurred_at=at(issue)))
    session.add(PaymentEvent(invoice_id=invoice.id, event_type=PaymentEventType.INVOICE_OVERDUE, payload={}, occurred_at=at(due + timedelta(days=1))))

    opened_at = at(due + timedelta(days=1))
    case = RecoveryCase(
        invoice_id=invoice.id,
        company_id=company.id,
        status=RecoveryCaseStatus.ESCALATED,
        opened_at=opened_at,
        revenue_at_risk=invoice.amount_total,
        recovered_amount=Decimal("0.00"),
        risk_score=Decimal("82.00"),
        risk_level=RiskLevel.HIGH,
        recovery_probability=Decimal("0.3800"),
        recovery_window_deadline=due + timedelta(days=90),
    )
    session.add(case)
    await session.flush()

    action1_time = opened_at
    action1 = RecoveryAction(
        recovery_case_id=case.id, action_type=RecoveryActionType.SEND_EMAIL,
        recommended_action_type=RecoveryActionType.SEND_EMAIL, status=RecoveryActionStatus.EXECUTED,
        proposed_by=ProposedBy.AI, sequence_number=1, executed_at=action1_time, result={"simulated": True},
    )
    session.add(action1)
    await session.flush()
    session.add(PolicyDecision(recovery_action_id=action1.id, policy_name="MAX_EMAIL_REMINDERS", decision=PolicyDecisionResult.APPROVED, reason="Reminder count (1) below cap.", rule=RULE_REMINDER_APPROVED, evaluated_at=action1_time))

    action2_time = at(due + timedelta(days=20))
    action2 = RecoveryAction(
        recovery_case_id=case.id, action_type=RecoveryActionType.SEND_PAYMENT_LINK,
        recommended_action_type=RecoveryActionType.SEND_PAYMENT_LINK, status=RecoveryActionStatus.EXECUTED,
        proposed_by=ProposedBy.AI, sequence_number=2, executed_at=action2_time,
        result={"payment_link": "https://pay.example.com/mock/vertex-3010", "simulated": True},
    )
    session.add(action2)
    await session.flush()
    session.add(PolicyDecision(recovery_action_id=action2.id, policy_name="HIGH_VALUE_THRESHOLD", decision=PolicyDecisionResult.REQUIRES_HUMAN_REVIEW, reason="Invoice amount (₹18,00,000) exceeds HIGH_VALUE_THRESHOLD (₹10,00,000); action logged, human review flagged in parallel.", rule=RULE_HIGH_VALUE_REVIEW, evaluated_at=action2_time))

    # The flagship override example: the reason text says "regardless of AI
    # recommendation" — recommended_action_type records what that
    # recommendation actually was (another reminder), so the AI-oversight
    # dashboard has a real, non-zero override to show right after reseeding.
    action3_time = at(due + timedelta(days=45))
    action3 = RecoveryAction(
        recovery_case_id=case.id, action_type=RecoveryActionType.ESCALATE,
        recommended_action_type=RecoveryActionType.SEND_PAYMENT_LINK, status=RecoveryActionStatus.EXECUTED,
        proposed_by=ProposedBy.AI, sequence_number=3, executed_at=action3_time,
        result={"escalated_to": "finance-team@example.com", "simulated": True},
    )
    session.add(action3)
    await session.flush()
    session.add(PolicyDecision(
        recovery_action_id=action3.id,
        policy_name="ESCALATION_THRESHOLD",
        decision=PolicyDecisionResult.APPROVED,
        reason="Days overdue (45 at evaluation) exceeds ESCALATION_THRESHOLD (45) and amount exceeds HIGH_VALUE_THRESHOLD; escalation forced regardless of AI recommendation.",
        rule=RULE_HIGH_VALUE_OVERDUE_FORCED_ESCALATE,
        evaluated_at=action3_time,
    ))

    session.add(AgentDecision(
        recovery_case_id=case.id, stage=AgentDecisionStage.DIAGNOSIS, model_name="gpt-4o-mini",
        input_context={"days_overdue": 60, "amount": "1800000.00", "prior_late_payments": 2},
        output={"diagnosis": "High-value invoice significantly overdue with no response to two prior interventions", "recommended_priority": "high"},
        rationale="Two reminders over 45 days produced no payment or communication from a customer with an otherwise acceptable but inconsistent payment history at this value tier.",
    ))
    session.add(AgentDecision(
        recovery_case_id=case.id, stage=AgentDecisionStage.INTERVENTION_RECOMMENDATION, model_name="gpt-4o-mini",
        input_context={"reminder_count": 2, "days_since_last_action": 25},
        output={"action": "ESCALATE"},
        rationale="Recommend escalation to human finance team given amount, elapsed time, and lack of response to automated outreach.",
    ))

    session.add(CommunicationLog(
        recovery_case_id=case.id, contact_id=contacts["Anjali Nair"].id, channel=CommunicationChannel.EMAIL,
        direction=CommunicationDirection.OUTBOUND, subject="Invoice INV-VERTEX-3010 overdue",
        body=f"Dear Anjali, Invoice INV-VERTEX-3010 (₹18,00,000) was due {due.isoformat()} and remains unpaid.",
        status=CommunicationStatus.SIMULATED, sent_at=action1_time,
    ))
    session.add(CommunicationLog(
        recovery_case_id=case.id, contact_id=contacts["Anjali Nair"].id, channel=CommunicationChannel.EMAIL,
        direction=CommunicationDirection.OUTBOUND, subject="Payment link for Invoice INV-VERTEX-3010",
        body="Dear Anjali, please find a direct payment link for the outstanding ₹18,00,000 below.",
        status=CommunicationStatus.SIMULATED, sent_at=action2_time,
    ))

    for entity_type, entity_id, event_type, actor, desc, ts in [
        ("recovery_case", case.id, "CASE_CREATED", AuditActor.SYSTEM, "Recovery case opened for overdue invoice INV-VERTEX-3010 (₹18,00,000).", opened_at),
        ("recovery_case", case.id, "RISK_CALCULATED", AuditActor.SYSTEM, "Risk scored: HIGH (82.0), recovery probability 38%.", opened_at),
        ("recovery_action", action1.id, "EMAIL_SENT", AuditActor.SYSTEM, "Reminder email sent to Anjali Nair.", action1_time),
        ("recovery_action", action2.id, "PAYMENT_LINK_SENT", AuditActor.SYSTEM, "Payment link sent to Anjali Nair.", action2_time),
        ("recovery_case", case.id, "DIAGNOSIS_GENERATED", AuditActor.AI_AGENT, "Diagnosis: high-value invoice significantly overdue, no response to prior outreach.", action3_time),
        ("recovery_action", action3.id, "INTERVENTION_RECOMMENDED", AuditActor.AI_AGENT, "Recommended action: ESCALATE.", action3_time),
        ("recovery_action", action3.id, "POLICY_APPROVED", AuditActor.POLICY_ENGINE, "Escalation forced by policy (high value + 45+ days overdue).", action3_time),
        ("recovery_case", case.id, "ESCALATED", AuditActor.SYSTEM, "Case escalated to human finance team — ₹18,00,000 at risk, awaiting manual follow-up.", action3_time),
    ]:
        session.add(audit(case, entity_type, entity_id, event_type, actor, desc, ts))


async def scenario_d_promise_to_pay(session: AsyncSession, company: Company, contacts: dict[str, Contact]) -> None:
    """Sundial: customer promised payment by a specific future date after one reminder."""
    due = d(15)
    issue = due - timedelta(days=30)
    invoice = Invoice(
        company_id=company.id,
        invoice_number="INV-SUNDIAL-4002",
        amount_total=Decimal("320000.00"),
        amount_paid=Decimal("0.00"),
        issue_date=issue,
        posting_date=issue,
        due_date=due,
        payment_term=30,
        payment_method_description=PaymentMethod.BANK_TRANSFER.value,
        status=InvoiceStatus.OVERDUE,
    )
    session.add(invoice)
    await session.flush()
    session.add(PaymentEvent(invoice_id=invoice.id, event_type=PaymentEventType.INVOICE_CREATED, payload={}, occurred_at=at(issue)))
    session.add(PaymentEvent(invoice_id=invoice.id, event_type=PaymentEventType.INVOICE_OVERDUE, payload={}, occurred_at=at(due + timedelta(days=1))))

    opened_at = at(due + timedelta(days=1))
    case = RecoveryCase(
        invoice_id=invoice.id,
        company_id=company.id,
        status=RecoveryCaseStatus.MONITORING,
        opened_at=opened_at,
        revenue_at_risk=invoice.amount_total,
        recovered_amount=Decimal("0.00"),
        risk_score=Decimal("48.00"),
        risk_level=RiskLevel.MEDIUM,
        recovery_probability=Decimal("0.7000"),
        recovery_window_deadline=due + timedelta(days=90),
    )
    session.add(case)
    await session.flush()

    action1_time = opened_at
    action1 = RecoveryAction(
        recovery_case_id=case.id, action_type=RecoveryActionType.SEND_EMAIL,
        recommended_action_type=RecoveryActionType.SEND_EMAIL, status=RecoveryActionStatus.EXECUTED,
        proposed_by=ProposedBy.AI, sequence_number=1, executed_at=action1_time, result={"simulated": True},
    )
    session.add(action1)
    await session.flush()
    session.add(PolicyDecision(recovery_action_id=action1.id, policy_name="MAX_EMAIL_REMINDERS", decision=PolicyDecisionResult.APPROVED, reason="Reminder count (1) below cap.", rule=RULE_REMINDER_APPROVED, evaluated_at=action1_time))

    response_time = at(due + timedelta(days=2))
    session.add(CommunicationLog(
        recovery_case_id=case.id, contact_id=contacts["Meera Joshi"].id, channel=CommunicationChannel.EMAIL,
        direction=CommunicationDirection.OUTBOUND, subject="Invoice INV-SUNDIAL-4002 overdue",
        body=f"Hi Meera, Invoice INV-SUNDIAL-4002 (₹3,20,000) was due {due.isoformat()} and is now overdue.",
        status=CommunicationStatus.SIMULATED, sent_at=action1_time,
    ))
    promise_date = TODAY + timedelta(days=5)
    session.add(CommunicationLog(
        recovery_case_id=case.id, contact_id=contacts["Meera Joshi"].id, channel=CommunicationChannel.EMAIL,
        direction=CommunicationDirection.INBOUND, subject="Re: Invoice INV-SUNDIAL-4002 overdue",
        body=f"Hi, apologies for the delay — we have a temporary cash-flow gap and will settle this in full by {promise_date.isoformat()}.",
        status=CommunicationStatus.SIMULATED, sent_at=response_time,
    ))

    action2_time = at(due + timedelta(days=3))
    action2 = RecoveryAction(
        recovery_case_id=case.id, action_type=RecoveryActionType.TRACK_PROMISE_TO_PAY,
        recommended_action_type=RecoveryActionType.TRACK_PROMISE_TO_PAY, status=RecoveryActionStatus.EXECUTED,
        proposed_by=ProposedBy.AI, sequence_number=2, executed_at=action2_time,
        result={"promised_date": promise_date.isoformat(), "promised_amount": "320000.00"},
    )
    session.add(action2)
    await session.flush()
    session.add(PolicyDecision(recovery_action_id=action2.id, policy_name="PROMISE_TO_PAY_RULES", decision=PolicyDecisionResult.APPROVED, reason="Promised date is within MAX_RECOVERY_DAYS window; tracking accepted.", rule=RULE_NO_RESTRICTION, evaluated_at=action2_time))

    session.add(PromiseToPay(
        recovery_case_id=case.id,
        invoice_id=invoice.id,
        promised_amount=Decimal("320000.00"),
        promised_date=promise_date,
        status=PromiseToPayStatus.PENDING,
    ))

    session.add(AgentDecision(
        recovery_case_id=case.id, stage=AgentDecisionStage.DIAGNOSIS, model_name="gpt-4o-mini",
        input_context={"days_overdue": 15, "customer_response": "cash-flow delay, requested extension"},
        output={"diagnosis": "Customer communicated a temporary cash-flow delay and requested an extension", "recommended_priority": "medium"},
        rationale="Direct, specific commitment from a contact with a mostly clean payment history suggests genuine short-term delay rather than avoidance.",
    ))
    session.add(AgentDecision(
        recovery_case_id=case.id, stage=AgentDecisionStage.INTERVENTION_RECOMMENDATION, model_name="gpt-4o-mini",
        input_context={"customer_committed_date": promise_date.isoformat()},
        output={"action": "TRACK_PROMISE_TO_PAY"},
        rationale="Customer gave a concrete date; track the commitment rather than sending another reminder that could damage the relationship.",
    ))

    for entity_type, entity_id, event_type, actor, desc, ts in [
        ("recovery_case", case.id, "CASE_CREATED", AuditActor.SYSTEM, "Recovery case opened for overdue invoice INV-SUNDIAL-4002 (₹3,20,000).", opened_at),
        ("recovery_case", case.id, "RISK_CALCULATED", AuditActor.SYSTEM, "Risk scored: MEDIUM (48.0), recovery probability 70%.", opened_at),
        ("recovery_action", action1.id, "EMAIL_SENT", AuditActor.SYSTEM, "Reminder email sent to Meera Joshi.", action1_time),
        ("communication_log", case.id, "CUSTOMER_RESPONSE_RECEIVED", AuditActor.SYSTEM, "Customer replied committing to pay by " + promise_date.isoformat() + ".", response_time),
        ("recovery_action", action2.id, "PROMISE_TO_PAY_RECORDED", AuditActor.AI_AGENT, f"Customer promised payment of ₹3,20,000 by {promise_date.isoformat()}.", action2_time),
        ("recovery_action", action2.id, "POLICY_APPROVED", AuditActor.POLICY_ENGINE, "Promise-to-pay tracking approved.", action2_time),
    ]:
        session.add(audit(case, entity_type, entity_id, event_type, actor, desc, ts))


async def scenario_e_recovered(session: AsyncSession, company: Company, contacts: dict[str, Contact]) -> None:
    """Aarav: invoice was overdue, recovered after a single reminder, case closed."""
    due = d(10)
    issue = due - timedelta(days=30)
    invoice = Invoice(
        company_id=company.id,
        invoice_number="INV-AARAV-1004",
        amount_total=Decimal("95000.00"),
        amount_paid=Decimal("95000.00"),
        issue_date=issue,
        posting_date=issue,
        due_date=due,
        payment_term=30,
        payment_method_description=PaymentMethod.BANK_TRANSFER.value,
        status=InvoiceStatus.PAID,
    )
    session.add(invoice)
    await session.flush()
    session.add(PaymentEvent(invoice_id=invoice.id, event_type=PaymentEventType.INVOICE_CREATED, payload={}, occurred_at=at(issue)))
    session.add(PaymentEvent(invoice_id=invoice.id, event_type=PaymentEventType.INVOICE_OVERDUE, payload={}, occurred_at=at(due + timedelta(days=1))))

    opened_at = at(due + timedelta(days=1))
    paid_date = due + timedelta(days=7)
    payment_time = at(paid_date)
    closed_at = at(paid_date + timedelta(days=1))

    case = RecoveryCase(
        invoice_id=invoice.id,
        company_id=company.id,
        status=RecoveryCaseStatus.CLOSED,
        opened_at=opened_at,
        closed_at=closed_at,
        revenue_at_risk=invoice.amount_total,
        recovered_amount=Decimal("95000.00"),
        risk_score=Decimal("30.00"),
        risk_level=RiskLevel.LOW,
        recovery_probability=Decimal("0.8500"),
        recovery_window_deadline=due + timedelta(days=90),
    )
    session.add(case)
    await session.flush()

    action = RecoveryAction(
        recovery_case_id=case.id, action_type=RecoveryActionType.SEND_EMAIL,
        recommended_action_type=RecoveryActionType.SEND_EMAIL, status=RecoveryActionStatus.EXECUTED,
        proposed_by=ProposedBy.AI, sequence_number=1, executed_at=opened_at, result={"simulated": True},
    )
    session.add(action)
    await session.flush()
    session.add(PolicyDecision(recovery_action_id=action.id, policy_name="MAX_EMAIL_REMINDERS", decision=PolicyDecisionResult.APPROVED, reason="Reminder count (1) below cap.", rule=RULE_REMINDER_APPROVED, evaluated_at=opened_at))

    session.add(Payment(
        invoice_id=invoice.id, amount=Decimal("95000.00"), payment_date=payment_time,
        method=PaymentMethod.BANK_TRANSFER, status=PaymentStatus.SUCCESS,
    ))
    session.add(PaymentEvent(invoice_id=invoice.id, event_type=PaymentEventType.PAYMENT_RECEIVED, payload={"amount": "95000.00"}, occurred_at=payment_time))

    session.add(AgentDecision(
        recovery_case_id=case.id, stage=AgentDecisionStage.DIAGNOSIS, model_name="gpt-4o-mini",
        input_context={"days_overdue": 10, "prior_late_payments": 0},
        output={"diagnosis": "Isolated late payment", "recommended_priority": "low"},
        rationale="Small business customer with a consistently on-time history; single reminder is proportionate.",
    ))
    session.add(AgentDecision(
        recovery_case_id=case.id, stage=AgentDecisionStage.INTERVENTION_RECOMMENDATION, model_name="gpt-4o-mini",
        input_context={"diagnosis": "Isolated late payment"},
        output={"action": "SEND_EMAIL"},
        rationale="A single reminder is expected to resolve this given the customer's history.",
    ))
    session.add(CommunicationLog(
        recovery_case_id=case.id, contact_id=contacts["Vikram Desai"].id, channel=CommunicationChannel.EMAIL,
        direction=CommunicationDirection.OUTBOUND, subject="Invoice INV-AARAV-1004 overdue",
        body=f"Hi Vikram, Invoice INV-AARAV-1004 (₹95,000) was due {due.isoformat()} and is now overdue.",
        status=CommunicationStatus.SIMULATED, sent_at=opened_at,
    ))

    for entity_type, entity_id, event_type, actor, desc, ts in [
        ("recovery_case", case.id, "CASE_CREATED", AuditActor.SYSTEM, "Recovery case opened for overdue invoice INV-AARAV-1004 (₹95,000).", opened_at),
        ("recovery_case", case.id, "RISK_CALCULATED", AuditActor.SYSTEM, "Risk scored: LOW (30.0), recovery probability 85%.", opened_at),
        ("recovery_action", action.id, "EMAIL_SENT", AuditActor.SYSTEM, "Reminder email sent to Vikram Desai.", opened_at),
        ("recovery_case", case.id, "PAYMENT_RECEIVED", AuditActor.SYSTEM, "₹95,000 recovered — invoice paid in full 7 days after due date.", payment_time),
        ("recovery_case", case.id, "CASE_CLOSED", AuditActor.SYSTEM, "Case closed: full amount recovered.", closed_at),
    ]:
        session.add(audit(case, entity_type, entity_id, event_type, actor, desc, ts))


async def seed_healthy_current_invoice(session: AsyncSession, company: Company) -> None:
    """Orbit: current invoice paid on time, no recovery case — a clean account for dashboard contrast."""
    due = d(20)
    issue = due - timedelta(days=30)
    invoice = Invoice(
        company_id=company.id,
        invoice_number="INV-ORBIT-5006",
        amount_total=Decimal("715000.00"),
        amount_paid=Decimal("715000.00"),
        issue_date=issue,
        posting_date=issue,
        due_date=due,
        payment_term=30,
        payment_method_description=PaymentMethod.BANK_TRANSFER.value,
        status=InvoiceStatus.PAID,
    )
    session.add(invoice)
    await session.flush()
    paid_date = due - timedelta(days=1)
    session.add(Payment(invoice_id=invoice.id, amount=Decimal("715000.00"), payment_date=at(paid_date), method=PaymentMethod.BANK_TRANSFER, status=PaymentStatus.SUCCESS))
    session.add(PaymentEvent(invoice_id=invoice.id, event_type=PaymentEventType.INVOICE_CREATED, payload={}, occurred_at=at(issue)))
    session.add(PaymentEvent(invoice_id=invoice.id, event_type=PaymentEventType.PAYMENT_RECEIVED, payload={"amount": "715000.00"}, occurred_at=at(paid_date)))


SCENARIO_BUILDERS = {
    "northwind": scenario_a_low_risk,
    "bluepeak": scenario_b_medium_risk,
    "vertex": scenario_c_high_risk_escalated,
    "sundial": scenario_d_promise_to_pay,
    "aarav": scenario_e_recovered,
}


async def seed() -> None:
    async with async_session_factory() as session:
        print("Clearing existing seed data...")
        await clear_all(session)

        print("Creating companies and contacts...")
        created = await create_companies(session)

        print("Seeding payment history...")
        for spec in COMPANIES:
            company, _ = created[spec.key]
            await seed_history(session, spec, company)

        print("Building scenario narratives...")
        for key, builder in SCENARIO_BUILDERS.items():
            company, contacts = created[key]
            await builder(session, company, contacts)

        healthy_company, _ = created["orbit"]
        await seed_healthy_current_invoice(session, healthy_company)

        demo_current_invoice_numbers = [
            "INV-NORTHWIND-2001",
            "INV-BLUEPEAK-2005",
            "INV-VERTEX-3010",
            "INV-SUNDIAL-4002",
            "INV-AARAV-1004",
            "INV-ORBIT-5006",
        ]

        result = await session.execute(
            select(Invoice).where(
                Invoice.invoice_number.in_(
                    demo_current_invoice_numbers
                )
            )
        )

        for invoice in result.scalars().all():
            await ensure_payment_risk_score(
                session,
                invoice,
            )

        await session.commit()
        print("Seed complete.")


async def main() -> None:
    await seed()
    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
