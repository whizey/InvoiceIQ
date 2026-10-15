"""Every diagnostic category uses mocks/synthetic SDK errors; zero provider calls."""
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock
import warnings

import pytest
from google.genai.errors import ClientError, ServerError
from langchain_core.messages import HumanMessage
from pydantic import ValidationError

from app.agents import record_gemini as cli, recordings, llm_client
from app.agents.capture_diagnostics import classify_failure, PHASE
from app.agents.schemas import DiagnosisResult, InterventionRecommendation
from app.core.config import settings

SECRET = 'synthetic-do-not-log-request-details'


def provider_error(code, message):
    klass = ServerError if code >= 500 else ClientError
    return klass(code, {'error': {'status': 'TEST_STATUS', 'message': message + SECRET,
                  'details': [{'Authorization': SECRET, 'prompt': SECRET, 'amount': SECRET}]}})


@pytest.mark.parametrize('exc,phase,category,status', [
    (cli.CaptureError(SECRET, category='missing_credentials'), 'preflight', 'missing_credentials', None),
    (provider_error(400, 'API key not valid. '), 'provider_diagnose', 'invalid_credentials', 400),
    (provider_error(401, 'Unauthenticated '), 'provider_diagnose', 'provider_authentication', 401),
    (provider_error(403, 'Permission denied '), 'provider_diagnose', 'provider_permission', 403),
    (provider_error(429, 'Quota exhausted '), 'provider_diagnose', 'quota_exhausted', 429),
    (provider_error(429, 'Rate limit exceeded '), 'provider_diagnose', 'rate_limit', 429),
    (provider_error(429, 'Resource unavailable '), 'provider_diagnose', 'rate_limit_or_quota', 429),
    (provider_error(400, 'Unsupported temperature '), 'provider_diagnose', 'unsupported_request_parameters', 400),
    (provider_error(404, 'Model unavailable '), 'provider_diagnose', 'unsupported_request_parameters', 404),
    (TimeoutError(SECRET), 'provider_diagnose', 'network_timeout', None),
    (ConnectionError(SECRET), 'provider_diagnose', 'network_error', None),
    (cli.CaptureError(SECRET), 'historical_context', 'historical_context_unavailable', None),
    (cli.CaptureError(SECRET, category='prompt_context_mismatch'), 'replay_manifest', 'prompt_context_mismatch', None),
    (PermissionError(SECRET), 'recording_write', 'recording_write_error', None),
    (json.JSONDecodeError(SECRET, SECRET, 0), 'replay_manifest', 'recording_integrity_error', None),
    (FileNotFoundError(SECRET), 'replay_response', 'recording_missing', None),
    (provider_error(503, 'Service unavailable '), 'provider_diagnose', 'provider_unavailable', 503),
])
def test_all_categories_never_serialize_provider_messages(exc, phase, category, status):
    result = classify_failure(exc, phase=phase)
    assert result['category'] == category and result['http_status'] == status
    assert result['phase'] == phase and SECRET not in json.dumps(result)
    assert set(result) == {'category', 'http_status', 'phase', 'exception_type', 'validation_fields'}


def test_validation_paths_redact_extra_keys_and_input_values():
    with pytest.raises(ValidationError) as caught:
        InterventionRecommendation.model_validate({'action': SECRET, 'rationale': SECRET, SECRET: SECRET})
    result = classify_failure(caught.value, phase='provider_recommend')
    assert result['category'] == 'response_schema_validation'
    assert result['validation_fields'] == ['[field]', 'action']
    assert SECRET not in json.dumps(result)
    assert result['exception_type'] == 'ValidationError'


@pytest.mark.asyncio
async def test_parser_failure_preserves_field_paths_without_raw_response(tmp_path, monkeypatch):
    from langchain_core.exceptions import OutputParserException
    try:
        DiagnosisResult.model_validate({'diagnosis': SECRET, 'reason': SECRET, 'recommended_priority': SECRET})
    except ValidationError as cause:
        parser = OutputParserException(SECRET)
        parser.__cause__ = cause
    response = {'parsed': None, 'parsing_error': parser,
        'raw': SimpleNamespace(content=SECRET, response_metadata={'finish_reason': 'MAX_TOKENS'})}
    invoke = AsyncMock(return_value=response)
    client = SimpleNamespace(with_structured_output=lambda *args, **kwargs: SimpleNamespace(ainvoke=invoke))
    context = {'invoice': {}, 'customer': {}, 'reminder_count': 0}
    with pytest.raises(cli.CaptureError) as caught:
        await cli.capture_stage('diagnose', client, 'unit-test-model', context, None, tmp_path/'pins.json')
    result = classify_failure(caught.value, phase='provider_diagnose')
    assert result['category'] == 'response_schema_validation'
    assert result['exception_type'] == 'OutputParserException'
    assert result['validation_fields'] == ['recommended_priority']
    assert result['finish_reason'] == 'MAX_TOKENS'
    assert SECRET not in json.dumps(result) and not list(tmp_path.iterdir())
    invoke.assert_awaited_once()


@pytest.mark.parametrize('schema', [DiagnosisResult, InterventionRecommendation])
def test_real_sdk_request_preparation_has_no_temperature_and_strict_json_schema(schema, monkeypatch):
    """Installed SDK prepares requests locally; generation is never invoked."""
    monkeypatch.setattr(settings, 'google_api_key', 'synthetic-sdk-construction-key')
    monkeypatch.setattr(settings, 'llm_model', cli.MODEL)
    monkeypatch.setenv('GOOGLE_GENAI_USE_VERTEXAI', 'false')
    client, model = llm_client._build_llm_chat()
    assert model == cli.MODEL and client.max_retries == 0
    assert client.timeout == 15 and client.max_output_tokens == 1024
    with warnings.catch_warnings(record=True) as observed:
        request = client._prepare_request([HumanMessage(content='Synthetic schema configuration test only')],
            response_mime_type='application/json', response_json_schema=schema.model_json_schema())
    config = request['config']
    assert config.temperature is None
    assert config.response_mime_type == 'application/json'
    assert config.response_json_schema['additionalProperties'] is False
    assert config.response_json_schema['required'] == [name for name, field in schema.model_fields.items() if field.is_required()]
    assert config.response_json_schema['properties']['schema_version']['const'] == '1'
    assert config.http_options.timeout == 15000
    assert config.http_options.retry_options.attempts == 0
    assert not any('sampling' in str(w.message) for w in observed)
    client.with_structured_output(schema, method='json_schema', include_raw=True)


@pytest.mark.asyncio
@pytest.mark.parametrize('key,category', [('', 'missing_credentials'), ('bad\nkey', 'invalid_credentials')])
async def test_credentials_fail_before_db_or_sdk_access(key, category, monkeypatch):
    monkeypatch.setenv('GOOGLE_API_KEY', key)
    monkeypatch.setattr(cli, 'configure_database', lambda *a: pytest.fail('No DB/SDK access expected'))
    with pytest.raises(cli.CaptureError) as caught:
        await cli.run(SimpleNamespace(command='capture', confirm_provider_calls=True))
    assert classify_failure(caught.value)['category'] == category


def test_async_failure_phase_survives_cli_boundary(monkeypatch, capsys):
    async def failure(_):
        PHASE.set('provider_diagnose')
        raise provider_error(403, 'Permission denied ')
    monkeypatch.setattr(cli, '_run_impl', failure)
    monkeypatch.setattr(cli.sys, 'argv', ['record_gemini', 'capture', '--case-id',
        'edd05c6b-2c8a-4fb0-a60b-6fd7a94fd083', '--as-of', '2015-02-03', '--confirm-provider-calls'])
    assert cli.main() == 1
    output = capsys.readouterr()
    result = json.loads(output.err.removeprefix('BLOCKED: '))
    assert result['category'] == 'provider_permission' and result['phase'] == 'provider_diagnose'
    assert result['exception_type'] == 'ClientError' and result['http_status'] == 403
    assert SECRET not in output.out + output.err


def test_replay_distinguishes_missing_context_and_bad_integrity(tmp_path, monkeypatch):
    monkeypatch.setenv('LLM_RECORDINGS_DIR', str(tmp_path/'recordings'))
    monkeypatch.delenv('LLM_RECORDING_ALLOWLIST_FILE', raising=False)
    messages = [HumanMessage(content='Synthetic test only')]
    monkeypatch.setenv('LLM_RECORDING_ALLOWLIST', '{}')
    with pytest.raises(recordings.RecordingUnavailable) as caught:
        recordings.replay_response('diagnose', messages, DiagnosisResult)
    assert classify_failure(caught.value)['category'] == 'recording_missing'
    monkeypatch.setenv('LLM_RECORDING_ALLOWLIST', json.dumps({'a'*64: 'b'*64}))
    with pytest.raises(recordings.RecordingUnavailable) as caught:
        recordings.replay_response('diagnose', messages, DiagnosisResult)
    assert classify_failure(caught.value)['category'] == 'prompt_context_mismatch'
    monkeypatch.setenv('LLM_RECORDING_ALLOWLIST', 'invalid JSON '+SECRET)
    with pytest.raises(recordings.RecordingUnavailable) as caught:
        recordings.replay_response('diagnose', messages, DiagnosisResult)
    result = classify_failure(caught.value)
    assert result['category'] == 'recording_integrity_error' and SECRET not in json.dumps(result)

@pytest.mark.asyncio
@pytest.mark.parametrize('code,message,category', [
    (400, 'API key not valid. ', 'invalid_credentials'),
    (401, 'Unauthenticated ', 'provider_authentication'),
    (403, 'Permission denied ', 'provider_permission'),
    (429, 'Quota exhausted ', 'quota_exhausted'),
    (400, 'Unsupported parameter ', 'unsupported_request_parameters'),
    (503, 'Service unavailable ', 'provider_unavailable'),
])
async def test_real_sdk_wrapped_errors_are_sanitized_and_never_retried(code, message, category, tmp_path, monkeypatch):
    """Run installed LangChain/GenAI error handling with mocked HTTP generation."""
    monkeypatch.setattr(settings, 'google_api_key', 'synthetic-sdk-construction-key')
    monkeypatch.setattr(settings, 'llm_model', cli.MODEL)
    monkeypatch.setenv('GOOGLE_API_KEY', 'synthetic-sdk-construction-key')
    monkeypatch.setenv('GOOGLE_GENAI_USE_VERTEXAI', 'false')
    client, model = llm_client._build_llm_chat()
    generation = AsyncMock(side_effect=provider_error(code, message))
    monkeypatch.setattr(client.client.aio.models, 'generate_content', generation)
    with pytest.raises(Exception) as caught:
        await cli.capture_stage('diagnose', client, model,
            {'invoice': {}, 'customer': {}, 'reminder_count': 0}, None, tmp_path/'pins.json')
    result = classify_failure(caught.value, phase='provider_diagnose')
    assert result['category'] == category and result['http_status'] == code
    assert result['exception_type'].startswith('Google')
    assert SECRET not in json.dumps(result)
    generation.assert_awaited_once()
    assert not list(tmp_path.iterdir())


def test_pinned_bad_schema_preserves_safe_validation_path(tmp_path, monkeypatch):
    monkeypatch.setenv('LLM_RECORDINGS_DIR', str(tmp_path/'recordings'))
    monkeypatch.delenv('LLM_RECORDING_ALLOWLIST_FILE', raising=False)
    messages = [HumanMessage(content='Synthetic unit test only')]
    response = InterventionRecommendation(action='WAIT', rationale='Synthetic unit output')
    pin = recordings.record_response('recommend', messages, 'gemini', 'unit-test-model', response)
    path = tmp_path/'recordings'/(pin['prompt_sha256']+'.json')
    record = json.loads(path.read_text())
    record.pop('integrity_sha256')
    record['response']['action'] = SECRET
    record['response_sha256'] = recordings.digest(record['response'])
    fingerprint = recordings.digest(record)
    path.write_text(json.dumps({**record, 'integrity_sha256': fingerprint}))
    monkeypatch.setenv('LLM_RECORDING_ALLOWLIST', json.dumps({pin['prompt_sha256']: fingerprint}))
    with pytest.raises(recordings.RecordingUnavailable) as caught:
        recordings.replay_response('recommend', messages, InterventionRecommendation)
    diagnostic = classify_failure(caught.value, phase='replay_response')
    assert diagnostic['category'] == 'response_schema_validation'
    assert diagnostic['validation_fields'] == ['action']
    assert SECRET not in json.dumps(diagnostic)

@pytest.mark.asyncio
async def test_provider_raw_logging_is_suppressed_and_logging_restored(tmp_path, caplog):
    import logging
    previous = logging.root.manager.disable
    async def failure(_messages):
        logging.getLogger('langchain_google_genai').warning(SECRET)
        raise provider_error(401, 'Unauthenticated ')
    client = SimpleNamespace(with_structured_output=lambda *a, **kw: SimpleNamespace(ainvoke=AsyncMock(side_effect=failure)))
    with pytest.raises(ClientError) as caught:
        await cli.capture_stage('diagnose', client, 'unit-test-model',
            {'invoice': {}, 'customer': {}, 'reminder_count': 0}, None, tmp_path/'pins.json')
    assert classify_failure(caught.value)['category'] == 'provider_authentication'
    assert SECRET not in caplog.text
    assert logging.root.manager.disable == previous

@pytest.mark.asyncio
async def test_diagnosis_only_capture_makes_exactly_one_request(tmp_path, monkeypatch, capsys):
    """CLI mechanics only: synthetic schema output and mocked database/provider."""
    from datetime import date
    from uuid import UUID
    from app.core import db
    response = DiagnosisResult(diagnosis='Synthetic test only', reason='Not genuine provider evidence', recommended_priority='low')
    invoke = AsyncMock(return_value={'parsed': response, 'parsing_error': None, 'raw': SimpleNamespace()})
    client = SimpleNamespace(with_structured_output=lambda *a, **kw: SimpleNamespace(ainvoke=invoke))
    monkeypatch.setenv('GOOGLE_API_KEY', 'synthetic-sdk-construction-key')
    monkeypatch.setenv('LLM_RECORDINGS_DIR', str(tmp_path/'recordings'))
    monkeypatch.setenv('LLM_RECORDING_ALLOWLIST_FILE', str(tmp_path/'pins.json'))
    monkeypatch.setenv('GOOGLE_GENAI_USE_VERTEXAI', 'false')
    monkeypatch.setattr(cli, 'configure_database', lambda *_: SimpleNamespace())
    monkeypatch.setattr(llm_client, '_build_llm_chat', lambda: (client, 'unit-test-model'))
    monkeypatch.setattr(cli, 'historical_context', AsyncMock(return_value={'invoice': {}, 'customer': {}, 'reminder_count': 0}))
    class Session:
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        rollback = AsyncMock()
    monkeypatch.setattr(db, 'async_session_factory', Session)
    monkeypatch.setattr(db, 'engine', SimpleNamespace(dispose=AsyncMock()))
    args = SimpleNamespace(command='capture', confirm_provider_calls=True, isolated_container=True,
        recordings_dir=tmp_path/'recordings', pins_file=tmp_path/'pins.json',
        case_id=UUID('edd05c6b-2c8a-4fb0-a60b-6fd7a94fd083'), as_of=date(2015, 2, 3),
        model='unit-test-model', max_recordings=1)
    await cli.run(args)
    invoke.assert_awaited_once()
    assert 'CAPTURE_PARTIAL: 1 genuine diagnosis' in capsys.readouterr().out
    # Manifest plus diagnosis; there is no invented intervention response.
    assert len(list((tmp_path/'recordings').glob('*.json'))) == 2


def test_only_allowlisted_local_reason_codes_are_reported():
    safe = cli.CaptureError(SECRET, category='historical_context_unavailable', reason_code='missing_historical_case')
    assert classify_failure(safe)['reason_code'] == 'missing_historical_case'
    unsafe = cli.CaptureError(SECRET, reason_code=SECRET)
    assert SECRET not in json.dumps(classify_failure(unsafe))

@pytest.mark.asyncio
@pytest.mark.parametrize('failure', [None, 'tampered', 'model', 'context', 'completed', 'limit'])
async def test_resume_one_request_preserves_completed_stage(tmp_path, monkeypatch, failure):
    """Synthetic unit fixtures only; provider and database are mocked."""
    from datetime import date
    from uuid import UUID
    from unittest.mock import Mock
    from app.core import db
    root, pins = tmp_path/'recordings', tmp_path/'pins.json'
    monkeypatch.setenv('LLM_RECORDINGS_DIR', str(root))
    monkeypatch.setenv('LLM_RECORDING_ALLOWLIST_FILE', str(pins))
    monkeypatch.setenv('GOOGLE_API_KEY', 'synthetic-unit-key')
    monkeypatch.setenv('GOOGLE_GENAI_USE_VERTEXAI', 'false')
    context = {'invoice': {}, 'customer': {}, 'reminder_count': 0}
    diagnosis = DiagnosisResult(diagnosis='Synthetic fixture', reason='Unit test only', recommended_priority='low')
    provenance = recordings.record_response('diagnose', cli.messages_for('diagnose', {}, {}, reminder_count=0),
        'gemini', 'unit-test-model', diagnosis, context=context)
    manifest = {'case_id': 'edd05c6b-2c8a-4fb0-a60b-6fd7a94fd083', 'historical_as_of': '2015-02-03',
                'context_sha256': recordings.digest(context), 'model': 'unit-test-model'}
    cli.write_private(root/'capture-context.json', manifest, exclusive=True)
    original = {'capture-context': recordings.digest(manifest), provenance['prompt_sha256']: provenance['integrity_sha256']}
    if failure == 'completed':
        original[recordings.prompt_hash('recommend', cli.messages_for('recommend', {}, {}, diagnosis.model_dump(), 0))] = 'already-pinned'
    cli.write_private(pins, original)
    path = root/(provenance['prompt_sha256']+'.json')
    if failure == 'tampered':
        path.write_text(path.read_text().replace('Synthetic fixture', 'Tampered fixture'))
    before, manifest_before, pins_before = path.read_bytes(), (root/'capture-context.json').read_bytes(), pins.read_bytes()
    invoke = AsyncMock(return_value={'parsed': InterventionRecommendation(action='WAIT', rationale='Synthetic unit test'),
                                    'parsing_error': None, 'raw': SimpleNamespace()})
    build = Mock(return_value=(SimpleNamespace(with_structured_output=lambda *a, **kw: SimpleNamespace(ainvoke=invoke)), 'unit-test-model'))
    monkeypatch.setattr(llm_client, '_build_llm_chat', build)
    monkeypatch.setattr(cli, 'configure_database', lambda *_: SimpleNamespace())
    monkeypatch.setattr(cli, 'historical_context', AsyncMock(return_value={**context, 'reminder_count': 1} if failure == 'context' else context))
    class Session:
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        rollback = AsyncMock()
    monkeypatch.setattr(db, 'async_session_factory', Session)
    monkeypatch.setattr(db, 'engine', SimpleNamespace(dispose=AsyncMock()))
    args = SimpleNamespace(command='resume', confirm_provider_calls=True, isolated_container=True,
        recordings_dir=root, pins_file=pins, case_id=UUID(manifest['case_id']), as_of=date(2015, 2, 3),
        model='wrong-model' if failure == 'model' else 'unit-test-model', max_recordings=2 if failure == 'limit' else 1)
    if failure:
        with pytest.raises((cli.CaptureError, recordings.RecordingUnavailable)):
            await cli.run(args)
        invoke.assert_not_awaited()
        build.assert_not_called()
        assert pins.read_bytes() == pins_before
    else:
        await cli.run(args)
        invoke.assert_awaited_once()
        assert recordings.prompt_hash('recommend', invoke.await_args.args[0]) != provenance['prompt_sha256']
        updated = json.loads(pins.read_text())
        assert all(updated[k] == v for k, v in original.items())
        assert len(updated) == len(original)+1
    assert path.read_bytes() == before
    assert (root/'capture-context.json').read_bytes() == manifest_before
