"""Rollback-only adversarial fixtures on the existing isolated PostgreSQL DB."""

from datetime import date, datetime, timezone
from decimal import Decimal
from uuid import uuid4
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import text
from app.agents.guardrails import grounded_claims
from app.agents.schemas import InterventionRecommendation
from app.tools.read_only_queries import compile_query, plan_question, investigate_question


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["PARTIALLY_PAID", "CANCELLED", "WRITTEN_OFF"])
async def test_scoped_cutoff_payment_facts(status):
    from app.core.db import async_session_factory, engine
    from app.models import Company, Invoice, Payment
    from app.models.enums import CompanySegment, InvoiceStatus, PaymentMethod, PaymentStatus

    await engine.dispose()
    try:
        async with async_session_factory() as db:
            assert (await db.scalar(text("SELECT current_database()"))).endswith("_test")
            companies = [
                Company(name="Synthetic Phase 3 boundary", segment=CompanySegment.UNKNOWN) for _ in range(2)
            ]
            db.add_all(companies)
            await db.flush()
            invoices = [
                Invoice(
                    company_id=c.id,
                    invoice_number="P3-" + uuid4().hex,
                    amount_total=Decimal("100"),
                    amount_paid=Decimal("100"),
                    currency="INR",
                    issue_date=date(2015, 1, 1),
                    posting_date=date(2015, 1, 1),
                    due_date=date(2015, 1, 8),
                    status=InvoiceStatus[status],
                )
                for c in companies
            ]
            db.add_all(invoices)
            await db.flush()
            invoice = invoices[0]
            for day, amount, payment_status, currency in [
                (7, "40", PaymentStatus.SUCCESS, "INR"),
                (20, "60", PaymentStatus.SUCCESS, "INR"),
                (7, "999", PaymentStatus.FAILED, "INR"),
                (7, "999", PaymentStatus.SUCCESS, "USD"),
            ]:
                db.add(
                    Payment(
                        invoice_id=invoice.id,
                        amount=Decimal(amount),
                        currency=currency,
                        payment_date=datetime(2015, 1, day, tzinfo=timezone.utc),
                        method=PaymentMethod.BANK_TRANSFER,
                        status=payment_status,
                    )
                )
            await db.flush()
            rows = (
                (
                    await db.execute(
                        compile_query(
                            plan_question("show outstanding invoices"),
                            company_id=companies[0].id,
                            as_of=date(2015, 1, 15),
                        )
                    )
                )
                .mappings()
                .all()
            )
            if status == "PARTIALLY_PAID":
                assert len(rows) == 1 and rows[0]["outstanding_amount"] == Decimal("60")
                assert rows[0]["invoice_number"] == invoice.invoice_number
            else:
                assert rows == []
            facts = {
                "invoice_id": str(invoice.id),
                "amount_total": invoice.amount_total,
                "amount_paid": Decimal("40"),
                "due_date": invoice.due_date.isoformat(),
            }
            for field, value, identity, accepted in [
                ("amount_total", "100", invoice.id, True),
                ("amount_total", "101", invoice.id, False),
                ("amount_paid", "40", invoice.id, True),
                ("amount_paid", "100", invoice.id, False),
                ("due_date", "2015-01-08", invoice.id, True),
                ("due_date", "2015-01-09", invoice.id, False),
                ("amount_total", "100", invoices[1].id, False),
            ]:
                output = InterventionRecommendation(
                    action="WAIT",
                    rationale="Synthetic structured claim",
                    financial_claims=[{"source_invoice_id": str(identity), "field": field, "value": value}],
                )
                assert grounded_claims(output, facts) is accepted
            # Denial must happen before the detached query session can open.
            factory = AsyncMock(side_effect=AssertionError("Unauthorized tool opened session"))
            with pytest.raises(PermissionError):
                await investigate_question(
                    factory,
                    question="invoice summary",
                    company_id=companies[1].id,
                    as_of=date(2015, 1, 15),
                    authorized_company_ids=(companies[0].id,),
                )
            await db.rollback()
    finally:
        await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("attack", ["cached_scope", "invoice_company"])
async def test_graph_rejects_unauthorized_cached_case_and_substituted_invoice(monkeypatch, attack):
    from app.agents import graph
    from app.core.db import async_session_factory, engine
    from app.models import Company, Invoice, RecoveryCase
    from app.models.enums import CompanySegment, InvoiceStatus

    await engine.dispose()
    monkeypatch.setattr(
        graph,
        "ensure_payment_risk_score",
        AsyncMock(side_effect=AssertionError("Must reject before scoring")),
    )
    try:
        async with async_session_factory() as db:
            assert (await db.scalar(text("SELECT current_database()"))).endswith("_test")
            companies = [
                Company(name="Synthetic unauthorized case", segment=CompanySegment.UNKNOWN) for _ in range(2)
            ]
            db.add_all(companies)
            await db.flush()
            invoice = Invoice(
                company_id=companies[1].id,
                invoice_number="P3-" + uuid4().hex,
                amount_total=Decimal("100"),
                amount_paid=0,
                currency="INR",
                issue_date=date(2015, 1, 1),
                due_date=date(2015, 1, 8),
                status=InvoiceStatus.OVERDUE,
            )
            db.add(invoice)
            await db.flush()
            case = RecoveryCase(
                invoice_id=invoice.id,
                company_id=companies[1 if attack == "cached_scope" else 0].id,
                revenue_at_risk=Decimal("100"),
            )
            db.add(case)
            await db.flush()
            if attack == "cached_scope":
                db.info["analytics_scope"] = (companies[0].id,)
                with pytest.raises(PermissionError):
                    await graph.run_recovery_cycle(db, case.id)
            else:
                with pytest.raises(PermissionError):
                    await graph.load_customer_context_node({"case_id": str(case.id)}, db)
            db.info.pop("analytics_scope", None)
            await db.rollback()
    finally:
        await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "attack", ["pending", "rejected", "executed", "cross_case", "changed_action", "cached_review"]
)
async def test_persisted_execution_denies_forged_state_and_replay(monkeypatch, attack):
    from app.agents import graph
    from app.core.db import async_session_factory, engine
    from app.models import Company, Invoice, RecoveryCase, RecoveryAction
    from app.models.enums import CompanySegment, InvoiceStatus, RecoveryActionStatus, RecoveryActionType

    await engine.dispose()
    execute = AsyncMock(side_effect=AssertionError("Unauthorized execution"))
    monkeypatch.setattr(graph, "execute_mock_action", execute)
    try:
        async with async_session_factory() as db:
            assert (await db.scalar(text("SELECT current_database()"))).endswith("_test")
            company = Company(name="Synthetic action attack", segment=CompanySegment.UNKNOWN)
            db.add(company)
            await db.flush()
            invoice = Invoice(
                company_id=company.id,
                invoice_number="P3-" + uuid4().hex,
                amount_total=100,
                amount_paid=0,
                currency="INR",
                issue_date=date(2015, 1, 1),
                due_date=date(2015, 1, 8),
                status=InvoiceStatus.OVERDUE,
            )
            db.add(invoice)
            await db.flush()
            case = RecoveryCase(invoice_id=invoice.id, company_id=company.id, revenue_at_risk=100)
            db.add(case)
            await db.flush()
            status = {
                "pending": RecoveryActionStatus.PENDING_APPROVAL,
                "rejected": RecoveryActionStatus.POLICY_REJECTED,
                "executed": RecoveryActionStatus.EXECUTED,
            }.get(attack, RecoveryActionStatus.POLICY_APPROVED)
            action = RecoveryAction(
                recovery_case_id=case.id,
                action_type=RecoveryActionType.SEND_EMAIL,
                recommended_action_type=RecoveryActionType.SEND_EMAIL,
                status=status,
                sequence_number=1,
            )
            db.add(action)
            await db.flush()
            if attack == "cached_review":
                from app.services import review_gate

                action.status = RecoveryActionStatus.PENDING_APPROVAL
                await db.flush()
                # Raw SQL emulates a persisted status change while this session
                # retains the earlier pending ORM object. No commit is needed.
                await db.execute(
                    text("UPDATE recovery_actions SET status='EXECUTED' WHERE id=:id"), {"id": action.id}
                )
                with pytest.raises(review_gate.ReviewError):
                    await review_gate._load_pending(db, action.id)
                await db.rollback()
                return
            state = {
                "case_id": str(uuid4() if attack == "cross_case" else case.id),
                "action_id": str(action.id),
                "final_action": "SEND_PAYMENT_LINK" if attack == "changed_action" else "SEND_EMAIL",
                "policy_decision": "APPROVED",
            }
            with pytest.raises(PermissionError):
                await graph.execute_action_node(state, db)
            execute.assert_not_awaited()
            assert action.status == status
            await db.rollback()
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_external_investigation_requires_existing_company_scope(monkeypatch):
    import json
    from httpx import ASGITransport, AsyncClient
    from app.main import app
    from app.core import db as core_db
    from app.models import Company, Invoice, RecoveryCase
    from app.models.enums import CompanySegment, InvoiceStatus

    await core_db.engine.dispose()
    try:
        async with core_db.async_session_factory() as db:
            assert (await db.scalar(text("SELECT current_database()"))).endswith("_test")
            companies = [
                Company(name="Synthetic external scope", segment=CompanySegment.UNKNOWN) for _ in range(2)
            ]
            db.add_all(companies)
            await db.flush()
            invoice = Invoice(
                company_id=companies[1].id,
                invoice_number="P3-" + uuid4().hex,
                amount_total=100,
                amount_paid=0,
                issue_date=date(2015, 1, 1),
                due_date=date(2015, 1, 8),
                status=InvoiceStatus.OVERDUE,
            )
            db.add(invoice)
            await db.flush()
            case = RecoveryCase(company_id=companies[1].id, invoice_id=invoice.id, revenue_at_risk=100)
            db.add(case)
            await db.flush()

            # Dependency override retains the real PostgreSQL session and real
            # ORM scope hook, while keeping all adversarial fixtures rollback-only.
            async def scoped_db(request):
                db.info["analytics_scope"] = request.state.analytics_scope
                yield db

            from fastapi import Request

            scoped_db.__annotations__["request"] = Request
            app.dependency_overrides[core_db.get_db] = scoped_db
            monkeypatch.setenv(
                "ANALYTICS_API_KEYS",
                json.dumps({"a": {"token": "synthetic-a", "company_ids": [str(companies[0].id)]}}),
            )
            monkeypatch.setattr(core_db.settings, "env", "test")
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                assert (await client.post(f"/recovery-cases/{case.id}/run")).status_code == 401
                # Expunge the unauthorized cached object to exercise SQL filtering
                # here; the separate cached-case test covers that service boundary.
                case_id = case.id
                db.expunge(case)
                response = await client.post(
                    f"/recovery-cases/{case_id}/run", headers={"Authorization": "Bearer synthetic-a"}
                )
                assert response.status_code == 404
                assert (
                    await client.get(
                        f"/invoices/{invoice.id}", headers={"Authorization": "Bearer synthetic-a"}
                    )
                ).status_code == 404
            app.dependency_overrides.pop(core_db.get_db, None)
            db.info.pop("analytics_scope", None)
            await db.rollback()
    finally:
        app.dependency_overrides.pop(core_db.get_db, None)
        await core_db.engine.dispose()
