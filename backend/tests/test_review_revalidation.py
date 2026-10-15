from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from app.models.enums import InvoiceStatus, RecoveryActionType, RecoveryCaseStatus, PolicyDecisionResult
from app.services import review_gate


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["paid", "closed", "policy_changed", "zero_balance"])
async def test_review_rechecks_eligibility_before_mock_execution(monkeypatch, state):
    case = SimpleNamespace(id=uuid4(), invoice_id=uuid4(), company_id=uuid4(), status=RecoveryCaseStatus.OPEN)
    invoice = SimpleNamespace(status=InvoiceStatus.OVERDUE, amount_total=Decimal("100"), amount_paid=Decimal("0"))
    action = SimpleNamespace(recovery_case_id=case.id, action_type=RecoveryActionType.SEND_EMAIL)
    if state == "paid":
        invoice.status = InvoiceStatus.PAID
    if state == "closed":
        case.status = RecoveryCaseStatus.CLOSED
    if state == "zero_balance":
        invoice.amount_paid = invoice.amount_total
    session = AsyncMock()
    results = []
    for item in [case, invoice]:
        result = MagicMock()
        result.scalar_one_or_none.return_value = item
        results.append(result)
    session.execute.side_effect = results
    monkeypatch.setattr(review_gate, "_load_pending", AsyncMock(return_value=action))
    monkeypatch.setattr(review_gate, "evaluate_policy_for_case", AsyncMock(return_value=SimpleNamespace(
        final_action=RecoveryActionType.ESCALATE, decision=PolicyDecisionResult.APPROVED)))
    execute = AsyncMock()
    monkeypatch.setattr(review_gate, "execute_mock_action", execute)
    with pytest.raises(review_gate.ReviewError):
        await review_gate.approve(session, uuid4(), "verified-reviewer", None)
    execute.assert_not_called()
