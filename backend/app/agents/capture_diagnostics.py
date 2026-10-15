"""Allowlisted capture diagnostics. Never serialize exception messages or inputs."""
from contextvars import ContextVar

from pydantic import ValidationError

PHASE = ContextVar('capture_diagnostic_phase', default='preflight')
CATEGORIES = frozenset({
    'missing_credentials', 'invalid_credentials', 'provider_authentication',
    'provider_permission', 'rate_limit', 'quota_exhausted', 'rate_limit_or_quota',
    'unsupported_request_parameters', 'network_timeout', 'network_error',
    'response_schema_validation', 'historical_context_unavailable',
    'prompt_context_mismatch', 'recording_missing', 'recording_write_error',
    'recording_integrity_error', 'privacy_validation', 'capture_authorization',
    'database_configuration', 'provider_unavailable', 'unexpected_error', 'interrupted',
})
REASONS = frozenset({'missing_historical_case', 'historical_cutoff_mismatch', 'source_fingerprint_mismatch',
    'future_invoice_detected', 'future_payment_detected', 'source_invoice_not_eligible',
    'historical_balance_mismatch', 'missing_prediction_provenance', 'prediction_fingerprint_mismatch',
    'case_not_active', 'missing_manifest_or_pins', 'missing_diagnosis_recording',
    'missing_intervention_recording', 'manifest_integrity_mismatch', 'redacted_context_mismatch'})
FIELDS = frozenset({'diagnosis', 'reason', 'recommended_priority', 'action', 'rationale', 'response'})
TYPES = frozenset({
    'CaptureError', 'RecordingUnavailable', 'ValidationError', 'OutputParserException',
    'ClientError', 'ServerError', 'APIError', 'HTTPStatusError', 'TimeoutError',
    'ReadTimeout', 'ConnectTimeout', 'WriteTimeout', 'PoolTimeout', 'ConnectError',
    'NetworkError', 'ConnectionError', 'ConnectionResetError', 'PermissionError',
    'FileNotFoundError', 'FileExistsError', 'OSError', 'JSONDecodeError', 'KeyError',
    'ValueError', 'TypeError', 'RuntimeError', 'KeyboardInterrupt',
    'AuthenticationError', 'PermissionDeniedError', 'RateLimitError', 'BadRequestError',
    'OperationalError', 'DBAPIError', 'GoogleGenerativeAIError', 'ChatGoogleGenerativeAIError',
    'GoogleInvalidRequestError', 'GoogleAuthenticationError', 'GooglePermissionDeniedError',
    'GoogleModelNotFoundError', 'GoogleRateLimitError', 'GoogleAPIError', 'GoogleContextOverflowError',
})
PHASES = frozenset({'preflight', 'database', 'historical_context', 'client_configuration',
                   'provider_diagnose', 'provider_recommend', 'recording_write',
                   'replay_manifest', 'replay_response', 'replay_graph'})


class SafeCaptureError(ValueError):
    def __init__(self, message, *, category=None, validation_fields=(), finish_reason=None, reason_code=None):
        super().__init__(message)
        self.category = category
        self.validation_fields = tuple(validation_fields)
        self.finish_reason = finish_reason
        self.reason_code = reason_code


def exception_chain(exc):
    seen = set()
    while exc is not None and id(exc) not in seen and len(seen) < 8:
        seen.add(id(exc))
        yield exc
        exc = exc.__cause__ or exc.__context__


def validation_paths(exc):
    """Pydantic locs may contain raw extra-field keys: allowlist every segment."""
    paths = set()
    for item in exception_chain(exc):
        if isinstance(item, ValidationError):
            for error in item.errors(include_input=False, include_context=False, include_url=False):
                parts = [part if isinstance(part, str) and part in FIELDS else '[field]'
                         for part in error['loc']]
                paths.add('.'.join(parts) or 'response')
    return sorted(paths)


def http_status(exc):
    for item in exception_chain(exc):
        for value in (getattr(item, 'code', None), getattr(item, 'status_code', None),
                      getattr(getattr(item, 'response', None), 'status_code', None)):
            if type(value) is int and 100 <= value <= 599:
                return value
    return None


def classify_failure(exc, *, phase=None):
    phase = phase or PHASE.get()
    chain = list(exception_chain(exc))
    status = http_status(exc)
    declared = next((getattr(item, 'category', None) for item in chain
                     if isinstance(getattr(item, 'category', None), str) and getattr(item, 'category', None) in CATEGORIES), None)
    fields = validation_paths(exc)
    # Only use provider text internally to select a fixed category. Never return it.
    provider_messages = ' '.join(str(getattr(item, 'message', '')).lower() for item in chain)
    if declared:
        category = declared
    elif fields or any(type(item).__name__ == 'OutputParserException' for item in chain):
        category = 'unsupported_request_parameters' if phase == 'client_configuration' else 'response_schema_validation'
    elif 'api key not valid' in provider_messages or 'api_key_invalid' in provider_messages:
        category = 'invalid_credentials'
    elif status == 401:
        category = 'provider_authentication'
    elif status == 403:
        category = 'provider_permission'
    elif status == 429:
        category = ('quota_exhausted' if 'quota' in provider_messages or 'exhaust' in provider_messages
                    else 'rate_limit' if 'rate' in provider_messages else 'rate_limit_or_quota')
    elif status in {400, 404, 422}:
        category = 'unsupported_request_parameters'
    elif status in {408, 504} or any(isinstance(item, TimeoutError) or type(item).__name__ in
         {'ReadTimeout', 'ConnectTimeout', 'WriteTimeout', 'PoolTimeout'} for item in chain):
        category = 'network_timeout'
    elif any(isinstance(item, ConnectionError) or type(item).__name__ in {'ConnectError', 'NetworkError'} for item in chain):
        category = 'network_error'
    elif phase == 'recording_write':
        category = 'recording_write_error' if any(isinstance(item, OSError) for item in chain) else 'recording_integrity_error'
    elif phase in {'replay_manifest', 'replay_response'} and any(isinstance(item, FileNotFoundError) for item in chain):
        category = 'recording_missing'
    elif phase in {'replay_manifest', 'replay_response'}:
        category = 'recording_integrity_error'
    elif phase == 'historical_context':
        category = 'historical_context_unavailable'
    elif phase == 'database':
        category = 'database_configuration'
    elif phase == 'client_configuration':
        category = 'unsupported_request_parameters'
    elif status is not None and status >= 500:
        category = 'provider_unavailable'
    elif isinstance(exc, KeyboardInterrupt):
        category = 'interrupted'
    else:
        category = 'unexpected_error'
    # Prefer the underlying provider/parser/storage type over our safe wrapper.
    selected = next((item for item in chain if type(item).__name__ not in {'CaptureError', 'RecordingUnavailable'}), exc)
    name = type(selected).__name__
    result = {'category': category, 'exception_type': name if name in TYPES else 'Exception',
              'phase': phase if phase in PHASES else 'preflight', 'http_status': status,
              'validation_fields': fields}
    if not fields and any(getattr(item, 'validation_fields', ()) for item in chain):
        result['validation_fields'] = sorted({field if isinstance(field, str) and field in FIELDS else '[field]'
            for item in chain for field in getattr(item, 'validation_fields', ())})
    reason = next((getattr(item, 'finish_reason', None) for item in chain
                   if isinstance(getattr(item, 'finish_reason', None), str) and getattr(item, 'finish_reason', None) in {'MAX_TOKENS', 'SAFETY', 'RECITATION', 'STOP'}), None)
    if reason:
        result['finish_reason'] = reason
    reason_code = next((getattr(item, 'reason_code', None) for item in chain
        if isinstance(getattr(item, 'reason_code', None), str) and getattr(item, 'reason_code', None) in REASONS), None)
    if reason_code:
        result['reason_code'] = reason_code
    return result
