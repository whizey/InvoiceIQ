"""Private, pinned replay of validated provider responses; never author demo answers."""
import hashlib
import json
import os
from contextvars import ContextVar
from datetime import datetime, timezone
from pathlib import Path

PROMPT_VERSION = "invoiceiq-recovery-context-v1"
PRIVATE_ROOT = Path.home() / ".local" / "share" / "invoiceiq" / "recordings"
_provenance: ContextVar[dict] = ContextVar("llm_provenance", default={})


def set_provenance(value: dict):
    _provenance.set(value)


def response_provenance() -> dict:
    return dict(_provenance.get())


class RecordingUnavailable(RuntimeError):
    def __init__(self, message, *, category="recording_integrity_error", validation_fields=()):
        super().__init__(message)
        self.category = category
        self.validation_fields = tuple(validation_fields)


def minimal_context(invoice: dict, customer: dict) -> tuple[dict, dict]:
    """Whitelist numeric/date facts. Names, identifiers, contacts and free text stay local."""
    invoice_keys = {"amount_total", "amount_paid", "posting_date", "due_date", "days_overdue",
                    "currency", "late_payment_probability", "payment_risk_model_version"}
    customer_keys = {"num_prior_late_payments", "num_prior_on_time_payments", "prior_invoice_count",
                     "historical_late_rate", "historical_avg_days_late", "has_resolved_history"}
    from datetime import date
    from decimal import Decimal, InvalidOperation
    import re

    def validated(values, keys):
        safe = {}
        for key in values:
            if key not in keys:
                continue
            value = values[key]
            if value is None:
                safe[key] = None
            elif key in {"posting_date", "due_date"}:
                safe[key] = date.fromisoformat(str(value)).isoformat()
            elif key == "currency":
                if not isinstance(value, str) or not re.fullmatch(r"[A-Z]{3}", value):
                    raise ValueError("Invalid provider currency context")
                safe[key] = value
            elif key == "payment_risk_model_version":
                if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,80}", value):
                    raise ValueError("Invalid provider model context")
                safe[key] = value
            else:
                try:
                    if not isinstance(value, (int, float, Decimal)) or not Decimal(str(int(value) if isinstance(value, bool) else value)).is_finite():
                        raise ValueError("Invalid provider numeric context")
                except InvalidOperation:
                    raise ValueError("Invalid provider numeric context") from None
                safe[key] = value
        return safe

    return validated(invoice, invoice_keys), validated(customer, customer_keys)


def prompt_hash(stage: str, messages: list) -> str:
    data = {"stage": stage, "prompt_version": PROMPT_VERSION,
            "messages": [{"type": message.type, "content": message.content} for message in messages]}
    return digest(data)


def digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                    allow_nan=False).encode()).hexdigest()


def recording_root() -> Path:
    root = Path(os.getenv("LLM_RECORDINGS_DIR", str(PRIVATE_ROOT))).expanduser().resolve()
    if root.is_relative_to(Path(__file__).resolve().parents[3]):
        raise RecordingUnavailable("Private recordings must stay outside the repository", category="recording_write_error")
    return root


def record_response(stage, messages, provider, model, response, *, context=None, metadata=None) -> dict:
    if provider not in {"gemini", "openai", "groq"}:
        raise RecordingUnavailable("Only validated genuine provider outputs can be recorded")
    record = {"source": "recorded", "origin": "authorized_provider_call", "stage": stage,
              "prompt_version": PROMPT_VERSION, "prompt_sha256": prompt_hash(stage, messages),
              "provider": provider, "model": model, "response": response.model_dump(mode="json"),
              "recorded_at": datetime.now(timezone.utc).isoformat(),
              "response_sha256": digest(response.model_dump(mode="json")),
              "schema_validation": "passed"}
    if context is not None:
        record["redacted_context_sha256"] = digest(context)
    if metadata is not None:
        record["request_metadata"] = metadata
    fingerprint = digest(record)
    root = recording_root()
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = root / f"{record['prompt_sha256']}.json"
    # Exclusive write: preserve a previous genuine recording, never silently replace it.
    with open(path, "x", opener=lambda p, flags: os.open(p, flags, 0o600)) as handle:
        json.dump({**record, "integrity_sha256": fingerprint}, handle, sort_keys=True)
    return {k: record[k] for k in ("source", "prompt_version", "prompt_sha256", "provider", "model")} | {"integrity_sha256": fingerprint}


def replay_response(stage, messages, schema):
    """Allowlist is a privately configured trust root, separate from the recording itself."""
    try:
        expected_prompt = prompt_hash(stage, messages)
        pin_file = os.getenv("LLM_RECORDING_ALLOWLIST_FILE")
        allowlist = json.loads(Path(pin_file).read_text() if pin_file else os.environ.get("LLM_RECORDING_ALLOWLIST", "{}"))
        if not isinstance(allowlist, dict):
            raise ValueError("Invalid integrity allowlist")
        if expected_prompt not in allowlist:
            has_response_pins = any(isinstance(key, str) and len(key) == 64 for key in allowlist)
            raise RecordingUnavailable("No pinned response matches this prompt",
                category="prompt_context_mismatch" if has_response_pins else "recording_missing")
        expected_integrity = allowlist[expected_prompt]
        record = json.loads((recording_root() / f"{expected_prompt}.json").read_text())
        fingerprint = record.pop("integrity_sha256")
        if fingerprint != expected_integrity or digest(record) != fingerprint:
            raise ValueError("Recording integrity verification failed")
        if (record["prompt_sha256"] != expected_prompt or record["stage"] != stage
                or record["prompt_version"] != PROMPT_VERSION):
            raise RecordingUnavailable("Recording prompt/context is incompatible", category="prompt_context_mismatch")
        if (record["source"] != "recorded" or record["origin"] != "authorized_provider_call"
                or record["provider"] not in {"gemini", "openai", "groq"}):
            raise ValueError("Recording provider provenance failed")
        from app.agents.guardrails import validate_model_output
        from pydantic import ValidationError
        try:
            result = schema.model_validate(record["response"])
        except ValidationError as exc:
            raise RecordingUnavailable("Recorded response failed schema validation",
                category="response_schema_validation") from exc
        if validate_model_output(result, schema) is None:
            raise RecordingUnavailable("Recorded response exceeds validation limits",
                category="response_schema_validation", validation_fields=("response",))
        if "response_sha256" in record and digest(record["response"]) != record["response_sha256"]:
            raise ValueError("Recorded response integrity failed")
        set_provenance({k: record[k] for k in ("source", "prompt_version", "prompt_sha256", "provider", "model")} | {"integrity_sha256": fingerprint})
        return result, f"recorded:{record['provider']}:{record['model']}"
    except RecordingUnavailable:
        raise
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise RecordingUnavailable("No verified genuine recording is available for this prompt",
            category="recording_missing" if isinstance(exc, FileNotFoundError) else "recording_integrity_error") from exc
