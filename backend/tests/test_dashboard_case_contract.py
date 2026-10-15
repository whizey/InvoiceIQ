from datetime import date
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

from app.api.recovery_cases import _to_list_item
from app.models.enums import RecoveryActionStatus, RecoveryActionType, RecoveryCaseStatus, RiskLevel


def test_live_case_list_supplies_balance_reference_and_review_state():
    invoice = SimpleNamespace(id=uuid4(), due_date=date.today(), company=SimpleNamespace(name="Customer"),
                              invoice_number="INV-1", amount_total=Decimal("100"), amount_paid=Decimal("25"))
    case = SimpleNamespace(id=uuid4(), invoice=invoice, status=RecoveryCaseStatus.OPEN,
                           risk_level=RiskLevel.LOW, recovery_probability=None, recovered_amount=Decimal("25"),
                           actions=[SimpleNamespace(action_type=RecoveryActionType.ESCALATE,
                                                    status=RecoveryActionStatus.PENDING_APPROVAL)])
    item = _to_list_item(case)
    assert item.invoice_id == invoice.id
    assert item.outstanding_amount == Decimal("75")
    assert item.pending_approval
    assert item.current_action == RecoveryActionType.ESCALATE
    case.actions[0].status = RecoveryActionStatus.PROPOSED
    item = _to_list_item(case)
    assert not item.pending_approval
    assert item.current_action is None
