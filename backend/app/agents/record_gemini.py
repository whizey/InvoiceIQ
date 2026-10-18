"""Explicit Gemini capture and network-free replay against an isolated historical DB.

Capture never commits database changes. Verification runs the existing compiled
LangGraph in a transaction and rolls it back, preserving immutable prior audits.
"""
import argparse
import hashlib
import asyncio
from datetime import date, datetime, time
import json
import logging
import os
from pathlib import Path
import re
import sys
import subprocess
from uuid import UUID, uuid4

from app.agents import recordings
from app.agents.guardrails import validate_model_output, minimal_diagnosis, grounded_claims
from app.agents.schemas import DiagnosisResult, InterventionRecommendation
from app.agents.capture_diagnostics import SafeCaptureError, PHASE, classify_failure

MODEL = "gemini-3.5-flash-lite"
REPO = Path(__file__).resolve().parents[3]
class CaptureError(SafeCaptureError):
    """Safe local prerequisite/validation message; never SDK exception text."""


PII = re.compile(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}|\b(?:phone|account number|api key)\s*[:=]", re.I)


def private_paths(root: Path, pins: Path):
    root, pins = root.expanduser().resolve(), pins.expanduser().resolve()
    if root.is_relative_to(REPO) or pins.is_relative_to(REPO) or pins.is_relative_to(root):
        raise CaptureError("Recordings and independent pins must be outside Git, in separate directories", category="recording_write_error")
    return root, pins


def write_private(path: Path, value: dict, *, exclusive=False):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    target = path if exclusive else path.with_name(path.name + ".tmp")
    with open(target, "x", opener=lambda p, flags: os.open(p, flags, 0o600)) as handle:
        json.dump(value, handle, sort_keys=True, allow_nan=False)
    if not exclusive:
        os.replace(target, path)


def configure_database(use_container=False):
    from sqlalchemy.engine import make_url
    value = os.environ.get("PHASE1_TEST_DATABASE_URL", "")
    if use_container:
        from sqlalchemy.engine import URL
        container = json.loads(subprocess.check_output(
            ['docker', 'inspect', 'invoiceiq-phase1-test-db'], stderr=subprocess.DEVNULL))[0]
        if container['NetworkSettings']['Ports']['5432/tcp'] != [{'HostIp': '127.0.0.1', 'HostPort': '55435'}]:
            raise CaptureError("Unexpected isolated container binding")
        environment = dict(item.split('=', 1) for item in container['Config']['Env'] if '=' in item)
        value = URL.create('postgresql+asyncpg', username=environment['POSTGRES_USER'],
            password=environment['POSTGRES_PASSWORD'], host='127.0.0.1', port=55435,
            database='invoiceiq_phase1_replay_test').render_as_string(hide_password=False)
        os.environ['PHASE1_TEST_DATABASE_URL'] = value
    if not value:
        raise CaptureError("Set PHASE1_TEST_DATABASE_URL to the isolated historical *_replay_test database")
    url = make_url(value)
    if (url.host not in {"localhost", "127.0.0.1"} or url.port != 55435
            or not (url.database or "").endswith("_replay_test")
            or url.drivername != "postgresql+asyncpg"):
        raise CaptureError("Only the isolated localhost:55435 historical *_replay_test database is allowed")
    os.environ["DATABASE_URL"] = value
    from app.core.config import settings
    settings.database_url = value
    settings.google_api_key = None
    settings.openai_api_key = None
    settings.groq_api_key = None
    settings.resend_api_key = None
    settings.sarvam_api_key = None
    settings.scheduler_enabled = False
    settings.outbox_dispatch_enabled = False
    settings.langchain_tracing_v2 = False
    os.environ["LANGCHAIN_TRACING_V2"] = "false"
    os.environ["LANGSMITH_TRACING"] = "false"
    print("DATABASE_IDENTITY:", json.dumps({"host": url.host, "port": url.port,
          "database": url.database, "environment": "isolated-historical-verification"}), flush=True)
    return settings


def messages_for(stage, invoice, customer, diagnosis=None, reminder_count=0):
    from langchain_core.messages import SystemMessage, HumanMessage
    from app.agents.prompts import (DIAGNOSIS_SYSTEM_PROMPT, INTERVENTION_SYSTEM_PROMPT,
                                   build_diagnosis_prompt, build_intervention_prompt)
    if stage == "diagnose":
        return [SystemMessage(content=DIAGNOSIS_SYSTEM_PROMPT),
                HumanMessage(content=build_diagnosis_prompt(invoice, customer))]
    return [SystemMessage(content=INTERVENTION_SYSTEM_PROMPT),
            HumanMessage(content=build_intervention_prompt(invoice, customer, minimal_diagnosis(diagnosis or {}), reminder_count))]


async def historical_context(session, case_id, as_of):
    from sqlalchemy import select, text
    from app.agents.graph import load_customer_context_node, _reminder_count, check_terminal_node
    from app.models import DatasetImport, Invoice, RecoveryCase, Payment
    from app.seed.load_real_dataset import read_dataset, identity, resolve_dataset_path
    await session.execute(text("SET TRANSACTION READ ONLY"))
    imported = await session.get(DatasetImport, "public_b2b_historical_replay")
    if imported is None or imported.dataset_as_of != as_of:
        raise CaptureError("Historical cutoff must match the reconstructed real-source cohort", category="historical_context_unavailable", reason_code="historical_cutoff_mismatch")
    if imported.source_sha256 != hashlib.sha256(resolve_dataset_path().read_bytes()).hexdigest():
        raise CaptureError("Historical cohort source hash differs from the real CSV", category="historical_context_unavailable", reason_code="source_fingerprint_mismatch")
    if await session.scalar(select(Invoice.id).where(Invoice.posting_date > as_of).limit(1)) is not None:
        raise CaptureError("Replay cohort contains invoices posted after the historical cutoff", category="historical_context_unavailable", reason_code="future_invoice_detected")
    future = await session.scalar(select(Payment.id).where(Payment.payment_date >=
        datetime.combine(as_of, time.max)).limit(1))
    if future is not None:
        raise CaptureError("Replay database contains future payments", category="historical_context_unavailable", reason_code="future_payment_detected")
    case = await session.get(RecoveryCase, case_id)
    if case is None:
        raise CaptureError("Historical recovery case not found", category="historical_context_unavailable", reason_code="missing_historical_case")
    invoice = await session.get(Invoice, case.invoice_id)
    frame = read_dataset()
    row = next((r for r in frame.itertuples(index=False) if identity('invoice', r.Document_No) == invoice.id), None)
    if row is None or invoice.posting_date > as_of or invoice.due_date >= as_of:
        raise CaptureError("Case must reference an actual overdue public invoice at the cutoff", category="historical_context_unavailable", reason_code="source_invoice_not_eligible")
    paid = row.Amount if row.Clearing_date.date() <= as_of else 0
    if (invoice.amount_total != row.Amount or invoice.amount_paid != paid
            or invoice.posting_date != row.Posting_Date.date() or invoice.due_date != row.Net_Due_Date.date()
            or case.company_id != identity('customer', int(row.Cust_Num))):
        raise CaptureError("Invoice does not reconcile with source facts observable at the cutoff", category="historical_context_unavailable", reason_code="historical_balance_mismatch")
    if not invoice.payment_risk_scored_at or not invoice.payment_risk_artifact_sha256:
        raise CaptureError("A trusted persisted prediction is required; capture cannot perform scoring writes", category="historical_context_unavailable", reason_code="missing_prediction_provenance")
    from app.ml.champion_serving import verified_contract
    contract = verified_contract()
    if invoice.payment_risk_artifact_sha256 != contract['artifact_sha256'] or invoice.payment_risk_model_version != contract['version']:
        raise CaptureError("Stored prediction provenance does not match the trusted champion", category="historical_context_unavailable", reason_code="prediction_fingerprint_mismatch")
    state = {"case_id": str(case_id), "investigation_id": str(uuid4())}
    if (await check_terminal_node(state, session)).get("terminal"):
        raise CaptureError("Choose an active case with no pending approval", category="historical_context_unavailable", reason_code="case_not_active")
    state.update(await load_customer_context_node(state, session))
    invoice_context, customer_context = recordings.minimal_context(state['invoice_context'], state['customer_context'])
    context = {"invoice": invoice_context, "customer": customer_context,
               "reminder_count": await _reminder_count(session, case_id), "historical_as_of": as_of.isoformat()}
    if PII.search(json.dumps(context)):
        raise CaptureError("PII detected in redacted context")
    return context


async def capture_stage(stage, client, model, context, diagnosis, pins):
    PHASE.set('provider_diagnose' if stage == 'diagnose' else 'provider_recommend')
    schema = DiagnosisResult if stage == 'diagnose' else InterventionRecommendation
    messages = messages_for(stage, context['invoice'], context['customer'], diagnosis, context['reminder_count'])
    # include_raw retains actual response metadata without storing prompts or raw SDK objects.
    # SDK/parser logs can contain response bodies. This standalone capture CLI
    # emits only our allowlisted diagnostic, never third-party request logging.
    previous_logging = logging.root.manager.disable
    logging.disable(logging.CRITICAL)
    try:
        output = await asyncio.wait_for(client.with_structured_output(schema, method="json_schema", include_raw=True).ainvoke(messages), timeout=30)
    finally:
        logging.disable(previous_logging)
    from pydantic import ValidationError
    if not isinstance(output, dict):
        raise CaptureError("Provider response envelope is invalid", category="response_schema_validation", validation_fields=('response',))
    parsing_error = output.get('parsing_error')
    if parsing_error is not None:
        raw = output.get('raw')
        finish = (getattr(raw, 'response_metadata', None) or {}).get('finish_reason')
        error = CaptureError("Provider response failed schema validation; no fallback is recorded",
            category="response_schema_validation", finish_reason=finish)
        if isinstance(parsing_error, BaseException):
            raise error from parsing_error
        raise error
    try:
        result = schema.model_validate(output.get('parsed'))
    except ValidationError as exc:
        raise CaptureError("Provider response failed schema validation; no fallback is recorded",
                           category="response_schema_validation") from exc
    if validate_model_output(result, schema) is None or not grounded_claims(result, context["invoice"]):
        raise CaptureError("Provider response exceeds validation limits", category="response_schema_validation", validation_fields=('response',))
    if PII.search(result.model_dump_json()) or os.environ['GOOGLE_API_KEY'] in result.model_dump_json():
        raise CaptureError("Provider response contains sensitive text; it will not be recorded", category="privacy_validation")
    raw = output['raw']
    usage = getattr(raw, 'usage_metadata', None) or {}
    metadata = {"usage": {k: v for k, v in usage.items() if k in {'input_tokens', 'output_tokens', 'total_tokens'} and isinstance(v, int)}}
    response_metadata = getattr(raw, 'response_metadata', None) or {}
    for key in ('model_name', 'finish_reason'):
        value = response_metadata.get(key)
        if isinstance(value, str) and len(value) < 200 and not PII.search(value) and os.environ['GOOGLE_API_KEY'] not in value:
            metadata[key] = value
    request_id = getattr(raw, 'id', None)
    if isinstance(request_id, str) and len(request_id) < 200 and not PII.search(request_id) and os.environ['GOOGLE_API_KEY'] not in request_id:
        metadata['response_id'] = request_id
    PHASE.set('recording_write')
    provenance = recordings.record_response(stage, messages, 'gemini', model, result,
        context=context, metadata=metadata)
    allowlist = json.loads(pins.read_text()) if pins.exists() else {}
    allowlist[provenance['prompt_sha256']] = provenance['integrity_sha256']
    write_private(pins, allowlist)
    print("CAPTURED:", json.dumps({"stage": stage, "model": model,
          "prompt_sha256": provenance['prompt_sha256'], "integrity_sha256": provenance['integrity_sha256']}))
    return result


async def run(args):
    token = PHASE.set('preflight')
    try:
        return await _run_impl(args)
    except (Exception, KeyboardInterrupt) as exc:
        # asyncio.run's task context does not propagate back into main().
        exc.capture_phase = PHASE.get()
        raise
    finally:
        PHASE.reset(token)


async def _run_impl(args):
    PHASE.set('preflight')
    if args.command in {'capture', 'resume'}:
        if args.command == 'resume' and args.max_recordings != 1:
            raise CaptureError('Resume requires --max-recordings 1', category='capture_authorization')
        if not args.confirm_provider_calls:
            raise CaptureError("Explicit --confirm-provider-calls authorization is required", category="capture_authorization")
        key = os.environ.get('GOOGLE_API_KEY', '')
        if not key:
            raise CaptureError("GOOGLE_API_KEY must be set privately in Terminal", category="missing_credentials")
        if any(char.isspace() or not char.isascii() for char in key):
            raise CaptureError("Credential contains invalid whitespace or characters", category="invalid_credentials")
    root, pins = private_paths(args.recordings_dir, args.pins_file)
    if args.command == 'capture' and root.exists() and any(root.glob('*.json')):
        raise CaptureError("Preserve existing recordings; choose a new empty private capture directory", category="recording_write_error")
    os.environ['LLM_RECORDINGS_DIR'] = str(root)
    os.environ['LLM_RECORDING_ALLOWLIST_FILE'] = str(pins)
    PHASE.set('database')
    settings = configure_database(args.isolated_container)
    from app.core.db import async_session_factory, engine
    from app.core.time import historical_as_of
    from app.agents import llm_client
    manifest_path = root / 'capture-context.json'
    try:
        with historical_as_of(args.as_of):
            async with async_session_factory() as session:
                PHASE.set('historical_context')
                context = await historical_context(session, args.case_id, args.as_of)
                await session.rollback()
                if args.command == 'capture':
                    if not args.confirm_provider_calls or not os.environ.get('GOOGLE_API_KEY'):
                        raise CaptureError("Capture requires --confirm-provider-calls and GOOGLE_API_KEY from the environment")
                    if manifest_path.exists():
                        raise CaptureError("Capture already exists; preserve it and select a new private directory")
                    os.environ['GOOGLE_GENAI_USE_VERTEXAI'] = 'false'
                    settings.google_api_key = os.environ['GOOGLE_API_KEY']
                    settings.llm_model = args.model
                    # Reuse the existing supported client; do not use the fallback-capable invocation wrapper.
                    PHASE.set('client_configuration')
                    client, model = llm_client._build_llm_chat()
                    manifest = {"case_id": str(args.case_id), "historical_as_of": args.as_of.isoformat(),
                                "context_sha256": recordings.digest(context), "model": model}
                    PHASE.set('recording_write')
                    write_private(manifest_path, manifest, exclusive=True)
                    allowlist = json.loads(pins.read_text()) if pins.exists() else {}
                    allowlist['capture-context'] = recordings.digest(manifest)
                    write_private(pins, allowlist)
                    PHASE.set('provider_diagnose')
                    diagnosis = await capture_stage('diagnose', client, model, context, None, pins)
                    if getattr(args, 'max_recordings', 2) == 2:
                        PHASE.set('provider_recommend')
                        await capture_stage('recommend', client, model, context, diagnosis.model_dump(), pins)
                        print('CAPTURE_COMPLETE: 2 genuine validated responses; no database changes or financial actions')
                    else:
                        print('CAPTURE_PARTIAL: 1 genuine diagnosis; intervention replay remains incomplete; no database changes or financial actions')
                else:
                    PHASE.set('replay_manifest')
                    if not manifest_path.is_file() or not pins.is_file():
                        raise CaptureError("Missing genuine private recordings or independent pins; run the explicit capture command first", category="recording_missing", reason_code="missing_manifest_or_pins")
                    manifest = json.loads(manifest_path.read_text())
                    allowlist = json.loads(pins.read_text())
                    if allowlist.get('capture-context') != recordings.digest(manifest):
                        raise CaptureError("Capture manifest integrity failed", category="recording_integrity_error", reason_code="manifest_integrity_mismatch")
                    if (manifest['case_id'] != str(args.case_id)
                            or manifest['historical_as_of'] != args.as_of.isoformat()
                            or manifest['context_sha256'] != recordings.digest(context)):
                        raise CaptureError("Capture manifest or historical context mismatch", category="prompt_context_mismatch")
                    stages = [('diagnose', DiagnosisResult)]
                    if args.command == 'verify':
                        stages.append(('recommend', InterventionRecommendation))
                    for stage, schema in stages:
                        messages = messages_for(stage, context['invoice'], context['customer'],
                            diagnosis.model_dump() if stage == 'recommend' else None, context['reminder_count'])
                        PHASE.set('replay_response')
                        path = root / (recordings.prompt_hash(stage, messages) + '.json')
                        if not path.is_file():
                            raise CaptureError(f"Missing genuine {stage} recording for this historical context", category="recording_missing",
                                reason_code="missing_diagnosis_recording" if stage == "diagnose" else "missing_intervention_recording")
                        record = json.loads(path.read_text())
                        if (record.get('redacted_context_sha256') != recordings.digest(context)
                                or record.get('schema_validation') != 'passed'
                                or record.get('model') != manifest['model']
                                or record.get('provider') != 'gemini'):
                            raise CaptureError("Missing validated context provenance", category="prompt_context_mismatch", reason_code="redacted_context_mismatch")
                        diagnosis, _ = recordings.replay_response(stage, messages, schema)
                    if args.command == 'verify-diagnosis':
                        print('DIAGNOSIS_VERIFIED: pinned schema, model, prompt and current historical context; provider_calls=0')
                        return
                    if args.command == 'resume':
                        if args.model != manifest['model']:
                            raise CaptureError('Resume model differs from pinned capture', category='prompt_context_mismatch')
                        messages = messages_for('recommend', context['invoice'], context['customer'],
                                                diagnosis.model_dump(), context['reminder_count'])
                        prompt = recordings.prompt_hash('recommend', messages)
                        if prompt in allowlist or (root / (prompt + '.json')).exists():
                            raise CaptureError('Intervention already exists; preserve existing recordings', category='recording_write_error')
                        os.environ['GOOGLE_GENAI_USE_VERTEXAI'] = 'false'
                        settings.google_api_key = os.environ['GOOGLE_API_KEY']
                        settings.llm_model = manifest['model']
                        PHASE.set('client_configuration')
                        client, model = llm_client._build_llm_chat()
                        if model != manifest['model']:
                            raise CaptureError('Provider model differs from pinned capture', category='prompt_context_mismatch')
                        await capture_stage('recommend', client, model, context, diagnosis.model_dump(), pins)
                        print('CAPTURE_COMPLETE: reused pinned diagnosis; 1 intervention request; no database changes or financial actions')
                        return
                    os.environ['LLM_MODE'] = 'recorded'
                    # Fail loudly if replay ever attempts to construct a provider client.
                    def forbidden_provider():
                        raise AssertionError("Outbound provider call attempted during offline replay")
                    llm_client._llm_chat = forbidden_provider
                    from app.agents.graph import _build_graph
                    from sqlalchemy import select
                    from app.models import AgentDecision
                    from app.models.enums import AgentDecisionStage
                    initial_ids = set((await session.scalars(select(AgentDecision.id))).all())
                    PHASE.set('replay_graph')
                    result = await _build_graph(session).ainvoke({'case_id': str(args.case_id), 'investigation_id': str(uuid4())})
                    decisions = (await session.scalars(select(AgentDecision).where(AgentDecision.id.not_in(initial_ids)))).all()
                    recorded = [d for d in decisions if d.stage in {AgentDecisionStage.DIAGNOSIS, AgentDecisionStage.INTERVENTION_RECOMMENDATION}]
                    if len(recorded) != 2 or any(d.output.get('provenance', {}).get('source') != 'recorded' for d in recorded):
                        raise CaptureError("Graph did not preserve genuine recorded provenance")
                    if not result.get('policy_decision'):
                        raise CaptureError("Deterministic policy was not applied")
                    action_result = result.get('action_result') or {}
                    if action_result and not action_result.get('simulated'):
                        raise CaptureError("Non-simulated execution is forbidden")
                    await session.rollback()
                    print('OFFLINE_REPLAY_PASS:', json.dumps({"recorded_decisions": len(recorded),
                        "policy_decision": result['policy_decision'], "final_action": result['final_action'],
                        "provider_calls": 0, "database_changes_committed": 0,
                        "execution": "simulated workflow on real invoices"}))
    finally:
        await engine.dispose()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['capture', 'resume', 'verify-diagnosis', 'verify'])
    parser.add_argument('--case-id', type=UUID, required=True)
    parser.add_argument('--as-of', type=date.fromisoformat, required=True)
    parser.add_argument('--recordings-dir', type=Path, default=recordings.PRIVATE_ROOT)
    parser.add_argument('--pins-file', type=Path, default=Path.home()/'.local/share/invoiceiq/recording-pins/pins.json')
    parser.add_argument('--isolated-container', action='store_true', help='Use the existing verified Phase 1 Docker DB without printing its credentials')
    parser.add_argument('--model', default=MODEL)
    parser.add_argument('--max-recordings', type=int, choices=[1, 2], default=2, help='Maximum provider requests; use 1 for a diagnosis-only diagnostic capture')
    parser.add_argument('--confirm-provider-calls', action='store_true', help='Explicitly authorize bounded Gemini requests; no automatic retry')
    args = parser.parse_args()
    try:
        asyncio.run(run(args))
    except (Exception, KeyboardInterrupt) as exc:
        print('BLOCKED: ' + json.dumps(classify_failure(exc, phase=getattr(exc, 'capture_phase', None)), sort_keys=True), file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
