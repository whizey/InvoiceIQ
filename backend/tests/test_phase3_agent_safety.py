import json
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from app.agents.evaluation import DATASET, evaluate_case
from app.agents.guardrails import validate_model_output, redact_text
from app.agents.schemas import InterventionRecommendation
from app.agents.state import validate_transition
from app.agents.recordings import minimal_context


@pytest.mark.parametrize("case", json.loads(DATASET.read_text())["cases"], ids=lambda c:c["id"])
def test_versioned_security_case(case):
    evaluate_case(case)


def test_constructed_model_cannot_skip_validation():
    output = InterventionRecommendation.model_construct(action="WAIVE_BALANCE", rationale="forged")
    assert validate_model_output(output, InterventionRecommendation) is None


def test_state_contract_rejects_unknown_actions_and_missing_predecessors():
    with pytest.raises(ValueError):
        validate_transition("execute_action", {"case_id":str(uuid4())})
    with pytest.raises(ValueError):
        validate_transition("policy_check", {"case_id":str(uuid4()), "invoice_context":{},
            "recommended_action":"WAIVE_BALANCE", "reminder_count":0})


@pytest.mark.parametrize("field", ["case_id", "investigation_id", "action_id"])
@pytest.mark.parametrize("invalid", ["synthetic-integration-test", "", "not-a-uuid"])
def test_state_contract_rejects_invalid_identifiers(field, invalid):
    with pytest.raises(ValueError):
        validate_transition("check_terminal", {"case_id": str(uuid4()), field: invalid})


@pytest.mark.asyncio
async def test_diagnosis_rejects_invoice_from_another_case_before_retrieval(monkeypatch):
    from types import SimpleNamespace
    from app.agents import graph
    from app.tools import knowledge
    case_id, invoice_id = uuid4(), uuid4()
    session = SimpleNamespace(get=AsyncMock(return_value=SimpleNamespace(invoice_id=invoice_id)))
    monkeypatch.setattr(knowledge, "retrieve_investigation_evidence",
                        lambda *args: pytest.fail("Mismatched invoice must not retrieve evidence"))
    monkeypatch.setattr(graph, "diagnose", AsyncMock(side_effect=AssertionError("Must not invoke diagnosis")))
    with pytest.raises(ValueError, match="does not belong"):
        await graph.diagnose_case_node({"case_id": str(case_id),
            "invoice_context": {"invoice_id": str(uuid4())}}, session)
    graph.diagnose.assert_not_awaited()


@pytest.mark.parametrize("field", ["amount_total", "currency", "due_date", "payment_risk_model_version"])
def test_allowlisted_field_cannot_carry_private_free_text(field):
    with pytest.raises(ValueError):
        minimal_context({field:"secret@example.invalid +919876543210"}, {})


def test_redaction_preserves_financial_context():
    assert "example" not in redact_text("email secret@example.invalid account 123456789012")
    invoice, _ = minimal_context({"amount_total":123456789012, "due_date":"2026-01-01"}, {})
    assert invoice["amount_total"] == 123456789012


@pytest.mark.asyncio
async def test_malformed_output_has_no_unbounded_retry():
    from app.agents.llm_client import _invoke_structured_llm
    model = AsyncMock()
    model.ainvoke.return_value = {"action":"WAIVE_BALANCE", "rationale":"forged"}
    assert await _invoke_structured_llm(model, [], stage="recommend", provider="test", model_name="test", schema=InterventionRecommendation) is None
    assert model.ainvoke.await_count == 1


@pytest.mark.parametrize("text", ["Email customer@example.invalid", "Call +91 9876543210", "Account 123456789012"])
def test_provider_output_pii_is_rejected(text):
    assert validate_model_output({"action":"WAIT", "rationale":text}, InterventionRecommendation) is None


def test_observed_due_date_is_not_redacted_as_phone():
    assert redact_text("Due on 2026-01-01.") == "Due on 2026-01-01."


@pytest.mark.asyncio
async def test_provider_unsupported_claim_fails_closed():
    from app.agents.llm_client import _invoke_structured_llm
    model = AsyncMock()
    model.ainvoke.return_value = InterventionRecommendation(action="WAIT", rationale="Test only",
        financial_claims=[{"source_invoice_id":"other-tenant", "field":"amount_paid", "value":"100"}])
    result = await _invoke_structured_llm(model, [], stage="recommend", provider="test", model_name="test",
        schema=InterventionRecommendation, invoice_context={"invoice_id":"authorized", "amount_paid":100})
    assert result is None
    assert model.ainvoke.await_count == 1


@pytest.mark.asyncio
async def test_voice_provider_receives_minimized_redacted_context(monkeypatch):
    from unittest.mock import MagicMock
    from app.agents import llm_client
    from app.agents.schemas import VoiceTurnResult
    structured = AsyncMock()
    structured.ainvoke.return_value = VoiceTurnResult(agent_line_hi="Test only", concluded=False, proposed_action="NONE")
    chat = MagicMock()
    chat.with_structured_output.return_value = structured
    monkeypatch.setattr(llm_client, "_llm_chat", lambda:(chat,"test"))
    await llm_client.voice_turn({"amount_total":100, "email":"private@example.invalid", "account_number":"123456789012"},
        {"phone":"9876543210"}, [{"speaker":"customer", "text":"Reach me at private@example.invalid +91 9876543210"}], 1)
    prompt = str(structured.ainvoke.call_args.args[0])
    assert "private@example.invalid" not in prompt
    assert "9876543210" not in prompt
    assert "123456789012" not in prompt
    assert "100" in prompt


def test_incomplete_node_output_and_identity_replacement_are_rejected():
    from app.agents.state import validate_node_output
    state = {"case_id":str(uuid4())}
    for result in ({}, {"terminal":False, "case_id":str(uuid4())}, {"terminal":"false"}):
        with pytest.raises(ValueError):
            validate_node_output("check_terminal", state, result)
    validate_node_output("check_terminal", state, {"terminal":False})


@pytest.mark.asyncio
async def test_read_only_tool_rejects_unauthorized_scope_before_database_access():
    from datetime import date
    from unittest.mock import MagicMock
    from app.tools.read_only_queries import investigate_question
    factory = MagicMock()
    for scope in ((), (uuid4(),)):
        with pytest.raises(PermissionError):
            await investigate_question(factory, question="invoice summary", company_id=uuid4(),
                as_of=date.today(), authorized_company_ids=scope)
    factory.assert_not_called()


@pytest.mark.parametrize('schema,payload,field', [
    ('DiagnosisResult', {'diagnosis':'Test','reason':'Test','recommended_priority':'low'}, 'reason'),
    ('InterventionRecommendation', {'action':'WAIT','rationale':'Test'}, 'rationale'),
    ('VoiceTurnResult', {'agent_line_hi':'Test','concluded':False,'proposed_action':'NONE'}, 'agent_line_hi'),
])
@pytest.mark.parametrize('value', ['', 'x'*2001])
def test_schema_itself_bounds_provider_narratives(schema, payload, field, value):
    from app.agents import schemas
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        getattr(schemas, schema).model_validate({**payload,field:value})


@pytest.mark.asyncio
async def test_structured_client_setup_error_uses_safe_fallback(monkeypatch, caplog):
    from unittest.mock import MagicMock
    from app.agents import llm_client
    client=MagicMock()
    client.with_structured_output.side_effect=RuntimeError('private-key@example.invalid')
    monkeypatch.setattr(llm_client, '_llm_chat', lambda: (client, 'synthetic'))
    result, model=await llm_client.recommend({'amount_total':100,'days_overdue':1},{},{},0)
    assert result.action=='SEND_EMAIL' and model=='rule-based-fallback'
    assert 'private-key@example.invalid' not in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize('provider', ['email', 'tts', 'stt'])
async def test_provider_failures_do_not_log_private_exception_data(monkeypatch, caplog, provider):
    from unittest.mock import MagicMock
    from app.tools import email_provider, sarvam_client
    import httpx
    secret='private@example.invalid credential=private-test-value'
    client=AsyncMock()
    client.post.side_effect=RuntimeError(secret)
    factory=MagicMock()
    factory.return_value.__aenter__=AsyncMock(return_value=client)
    factory.return_value.__aexit__=AsyncMock(return_value=False)
    monkeypatch.setattr(httpx,'AsyncClient',factory)
    monkeypatch.setattr(sarvam_client.settings,'sarvam_api_key','synthetic-only')
    if provider=='email':
        assert await email_provider.ResendEmailProvider('synthetic-only','test@example.invalid').send(
            to='private@example.invalid',subject='private subject',body='private body')=='FAILED'
    elif provider=='tts':
        assert await sarvam_client.text_to_speech('private body') is None
    else:
        assert await sarvam_client.speech_to_text(b'synthetic','test.wav','audio/wav') is None
    assert secret not in caplog.text and 'private@example.invalid' not in caplog.text
    assert all(record.exc_info is None for record in caplog.records)
