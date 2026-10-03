"""Applies the human-approval gate, new Kafka events and SHAP-at-decision-time.

Run from backend/:  python3 gate_patch.py
Every edit is an exact-text replacement that must match exactly once, so if
your files differ from what this expects it stops and changes nothing.
"""
import re
import sys
from pathlib import Path

ROOT = Path("app")
if not ROOT.exists():
    sys.exit("run this from the backend folder")

edits = {}  # path -> list of (old, new)


def edit(path, old, new):
    edits.setdefault(path, []).append((old, new))


# 1. enum -------------------------------------------------------------------
edit("app/models/enums.py",
     '    POLICY_REJECTED = "POLICY_REJECTED"\n    EXECUTED',
     '    POLICY_REJECTED = "POLICY_REJECTED"\n    PENDING_APPROVAL = "PENDING_APPROVAL"\n    EXECUTED')

# 2. kafka topics ------------------------------------------------------------
edit("app/events/topics.py",
     'RECOVERY_CASE_CLOSED = "recovery.case_closed"\n',
     'RECOVERY_CASE_CLOSED = "recovery.case_closed"\n'
     'RISK_SCORED = "risk.scored"\n'
     'ACTION_PROPOSED = "action.proposed"\n'
     'REVIEW_REQUESTED = "review.requested"\n'
     'REVIEW_RESOLVED = "review.resolved"\n')
edit("app/events/topics.py",
     '    RECOVERY_CASE_CLOSED,\n]',
     '    RECOVERY_CASE_CLOSED,\n    RISK_SCORED,\n    ACTION_PROPOSED,\n    REVIEW_REQUESTED,\n    REVIEW_RESOLVED,\n]')

# 3. state ---------------------------------------------------------------------
edit("app/agents/state.py",
     "    terminal: bool\n",
     "    terminal: bool\n    awaiting_review: bool\n    risk_drivers: list\n")

# 4. graph -----------------------------------------------------------------------
G = "app/agents/graph.py"
edit(G, "from app.ml.risk_model import score as score_risk\n",
        "from app.ml.risk_model import explain as explain_risk\n")
edit(G,
     '''    if case.status in TERMINAL_STATUSES:
        return {"terminal": True, "outcome_summary": "case already terminal, no action taken"}
    return {"terminal": False}''',
     '''    if case.status in TERMINAL_STATUSES:
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
    return {"terminal": False}''')
edit(G,
     '''    result = score_risk(features)

    case = await session.get(RecoveryCase, UUID(state["case_id"]))''',
     '''    result = explain_risk(features)
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

    case = await session.get(RecoveryCase, UUID(state["case_id"]))''')
edit(G,
     '''                f"recovery probability {result.recovery_probability:.0%}."
            ),''',
     '''                f"recovery probability {result.recovery_probability:.0%}. "
                f"Top SHAP drivers: {driver_text}."
            ),''')
edit(G,
     '''        "recovery_probability": result.recovery_probability,
    }


async def diagnose_case_node''',
     '''        "recovery_probability": result.recovery_probability,
        "risk_drivers": drivers,
    }


async def diagnose_case_node''')
edit(G,
     '''    action.status = (
        RecoveryActionStatus.POLICY_REJECTED
        if outcome.decision == PolicyDecisionResult.REJECTED
        else RecoveryActionStatus.POLICY_APPROVED
    )
''',
     '''    if outcome.decision == PolicyDecisionResult.REJECTED:
        action.status = RecoveryActionStatus.POLICY_REJECTED
    elif outcome.decision == PolicyDecisionResult.REQUIRES_HUMAN_REVIEW:
        # A real gate: nothing executes until a person approves it.
        action.status = RecoveryActionStatus.PENDING_APPROVAL
    else:
        action.status = RecoveryActionStatus.POLICY_APPROVED
''')
edit(G,
     '''    graph.add_edge("policy_check", "execute_action")
''',
     '''    graph.add_conditional_edges(
        "policy_check",
        lambda s: "hold" if s.get("policy_decision") == PolicyDecisionResult.REQUIRES_HUMAN_REVIEW.value
        else "execute",
        {"hold": END, "execute": "execute_action"},
    )
''')
edit(G,
     '''        publisher = get_publisher()
        await publisher.publish(
            topics.RECOVERY_ACTION_COMPLETED,''',
     '''        publisher = get_publisher()
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
            topics.RECOVERY_ACTION_COMPLETED,''')

# 5. API endpoints ---------------------------------------------------------------
A = "app/api/recovery_cases.py"
edit(A,
     '@router.get("/{case_id}", response_model=RecoveryCaseDetailOut)\n',
     '''class ReviewDecisionIn(BaseModel):
    reviewer: str = "reviewer"
    note: str | None = None


@router.get("/pending-reviews")
async def list_pending_reviews(db: AsyncSession = Depends(get_db)) -> list[dict]:
    """Actions held by the policy engine, waiting for a person to approve or reject."""
    return await review_gate.list_pending(db)


@router.post("/actions/{action_id}/approve")
async def approve_action(
    action_id: UUID, body: ReviewDecisionIn, db: AsyncSession = Depends(get_db)
) -> dict:
    try:
        result = await review_gate.approve(db, action_id, body.reviewer, body.note)
    except review_gate.ReviewError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from None
    await db.commit()
    await review_gate.publish_resolution(result)
    return result


@router.post("/actions/{action_id}/reject")
async def reject_action(
    action_id: UUID, body: ReviewDecisionIn, db: AsyncSession = Depends(get_db)
) -> dict:
    try:
        result = await review_gate.reject(db, action_id, body.reviewer, body.note)
    except review_gate.ReviewError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from None
    await db.commit()
    await review_gate.publish_resolution(result)
    return result


@router.get("/{case_id}", response_model=RecoveryCaseDetailOut)
''')
edit(A, "from fastapi import APIRouter, Depends, HTTPException\n",
        "from fastapi import APIRouter, Depends, HTTPException\nfrom pydantic import BaseModel\n")
edit(A, "from app.services.action_policy import evaluate_and_record_action, primary_contact\n",
        "from app.services import review_gate\nfrom app.services.action_policy import evaluate_and_record_action, primary_contact\n")

# 6. check every anchor matches exactly once, then write ---------------------------
for path, pairs in edits.items():
    text = Path(path).read_text()
    for old, _ in pairs:
        if text.count(old) != 1:
            sys.exit(f"STOP: expected exactly 1 match in {path}, found {text.count(old)} for:\n{old[:120]}")
for path, pairs in edits.items():
    text = Path(path).read_text()
    for old, new in pairs:
        text = text.replace(old, new)
    Path(path).write_text(text)
    print("patched", path)

# 7. new files ---------------------------------------------------------------------
versions = Path("alembic/versions")
revs, downs = set(), set()
for f in versions.glob("*.py"):
    s = f.read_text()
    m = re.search(r"^revision(?::[^=]*)?=\s*'([^']+)'", s, re.M)
    d = re.search(r"^down_revision(?::[^=]*)?=\s*(?:'([^']+)'|None)", s, re.M)
    if m:
        revs.add(m.group(1))
    if d and d.group(1):
        downs.add(d.group(1))
heads = revs - downs
if len(heads) != 1:
    sys.exit(f"STOP: expected one alembic head, found {heads}")
head = heads.pop()
(versions / "e1a7c3b94d20_pending_approval_status.py").write_text(f'''"""pending approval status

Revision ID: e1a7c3b94d20
Revises: {head}
"""
from typing import Sequence, Union

from alembic import op

revision: str = "e1a7c3b94d20"
down_revision: Union[str, Sequence[str], None] = "{head}"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("ALTER TYPE recovery_action_status ADD VALUE IF NOT EXISTS 'PENDING_APPROVAL'")


def downgrade() -> None:
    # Postgres cannot drop an enum value without rebuilding the type.
    pass
''')
print("created migration on top of", head)

Path("app/services/review_gate.py").write_text('''"""Human approval gate for actions the policy engine holds.

The policy engine returns REQUIRES_HUMAN_REVIEW for high-value reminders.
graph.policy_check_node stores those as PENDING_APPROVAL and the workflow
stops. Nothing is sent until approve() runs; reject() records the refusal.
Both write an AuditLog row with actor HUMAN.
"""

from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.events import get_publisher, topics
from app.models import AuditLog, Invoice, RecoveryAction, RecoveryCase
from app.models.enums import AuditActor, RecoveryActionStatus
from app.services.action_policy import primary_contact
from app.tools.mock_tools import execute_mock_action


class ReviewError(Exception):
    def __init__(self, message: str, status_code: int = 409) -> None:
        super().__init__(message)
        self.status_code = status_code


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def _load_pending(session: AsyncSession, action_id: UUID) -> RecoveryAction:
    action = (
        await session.execute(
            select(RecoveryAction).where(RecoveryAction.id == action_id).with_for_update()
        )
    ).scalar_one_or_none()
    if action is None:
        raise ReviewError("Action not found", 404)
    if action.status != RecoveryActionStatus.PENDING_APPROVAL:
        raise ReviewError(f"Action is {action.status.value}, not waiting for approval")
    return action


async def list_pending(session: AsyncSession) -> list[dict]:
    rows = (
        await session.execute(
            select(RecoveryAction)
            .where(RecoveryAction.status == RecoveryActionStatus.PENDING_APPROVAL)
            .order_by(RecoveryAction.created_at)
        )
    ).scalars().all()
    return [
        {
            "action_id": str(a.id),
            "case_id": str(a.recovery_case_id),
            "action_type": a.action_type.value,
            "created_at": a.created_at.isoformat() if a.created_at else None,
        }
        for a in rows
    ]


async def approve(session: AsyncSession, action_id: UUID, reviewer: str, note: str | None) -> dict:
    action = await _load_pending(session, action_id)
    case = await session.get(RecoveryCase, action.recovery_case_id)
    invoice = await session.get(Invoice, case.invoice_id)
    contact = await primary_contact(session, case.company_id)

    result = await execute_mock_action(session, action.action_type, case, invoice, contact)
    action.status = RecoveryActionStatus.EXECUTED
    action.executed_at = _now()
    action.result = result
    session.add(
        AuditLog(
            recovery_case_id=case.id,
            entity_type="recovery_action",
            entity_id=action.id,
            event_type="REVIEW_APPROVED",
            actor=AuditActor.HUMAN,
            description=f"{reviewer} approved {action.action_type.value}. {note or ''}".strip(),
            occurred_at=_now(),
        )
    )
    await session.flush()
    return {
        "action_id": str(action.id),
        "case_id": str(case.id),
        "action_type": action.action_type.value,
        "decision": "APPROVED",
        "reviewer": reviewer,
    }


async def reject(session: AsyncSession, action_id: UUID, reviewer: str, note: str | None) -> dict:
    action = await _load_pending(session, action_id)
    action.status = RecoveryActionStatus.POLICY_REJECTED
    session.add(
        AuditLog(
            recovery_case_id=action.recovery_case_id,
            entity_type="recovery_action",
            entity_id=action.id,
            event_type="REVIEW_REJECTED",
            actor=AuditActor.HUMAN,
            description=f"{reviewer} rejected {action.action_type.value}. {note or ''}".strip(),
            occurred_at=_now(),
        )
    )
    await session.flush()
    return {
        "action_id": str(action.id),
        "case_id": str(action.recovery_case_id),
        "action_type": action.action_type.value,
        "decision": "REJECTED",
        "reviewer": reviewer,
    }


async def publish_resolution(result: dict) -> None:
    publisher = get_publisher()
    await publisher.publish(topics.REVIEW_RESOLVED, result["case_id"], result)
    if result["decision"] == "APPROVED":
        await publisher.publish(
            topics.RECOVERY_ACTION_COMPLETED,
            result["case_id"],
            {
                "case_id": result["case_id"],
                "action_id": result["action_id"],
                "action_type": result["action_type"],
                "outcome": "approved_by_human",
            },
        )
''')
print("created app/services/review_gate.py")
