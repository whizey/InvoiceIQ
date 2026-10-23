"""LangSmith tracing for the receivables assistant: one trace per answer, with every step nested inside it.

Each answer becomes a root run ("assistant_answer") holding the model calls (traced by LangChain), each tool
call with its arguments and result, the figure and policy checks, and any provider fallback, plus metadata to
filter on (who answered, fallbacks, figures removed, latency). Off unless LANGCHAIN_TRACING_V2 and
LANGCHAIN_API_KEY are set, and then:

- customer workspaces send structure and metadata only: questions, answers and tool contents are replaced
  before they leave the server, unless LANGSMITH_LOG_CUSTOMER_CONTENT=true;
- the public workspace and the eval fixture send full content.

The metadata-only ``assistant_traces`` table keeps working either way; LangSmith adds the full picture.
"""
from __future__ import annotations

import hashlib
import logging
from contextlib import contextmanager, nullcontext

from app.core.config import settings

logger = logging.getLogger(__name__)
REDACTED = {"redacted": "customer workspace content is not sent to LangSmith"}
_clients: dict[bool, object] = {}


def enabled() -> bool:
    return bool(settings.langchain_tracing_v2 and settings.langchain_api_key)


def redacts(workspace) -> bool:
    """Customer content stays on the server unless the operator opts in; the public workspace and fixtures hold no customer data."""
    kind = getattr(workspace, "kind", None)
    return kind == "customer" and not settings.langsmith_log_customer_content


def client(redact: bool):
    """One LangSmith client per privacy mode; the redacting one hides inputs and outputs of every nested run."""
    if redact not in _clients:
        from langsmith import Client

        hide = (lambda _payload: dict(REDACTED)) if redact else None
        _clients[redact] = Client(api_key=settings.langchain_api_key, hide_inputs=hide, hide_outputs=hide)
    return _clients[redact]


@contextmanager
def answer_trace(workspace, question: str, history_turns: int):
    """The root run for one answer; yields it (or None when tracing is off) so the caller can add results."""
    if not enabled():
        yield None
        return
    from langsmith import trace, tracing_context

    kind = getattr(workspace, "kind", None) or "eval"
    workspace_id = getattr(workspace, "id", None)
    redact = redacts(workspace)
    metadata = {"workspace_kind": kind, "history_turns": history_turns, "content_redacted": redact,
                # A one-way tag lets traces from one workspace be grouped without naming it.
                "workspace": hashlib.sha256(str(workspace_id).encode()).hexdigest()[:12] if workspace_id else kind}
    try:
        context = tracing_context(enabled=True, client=client(redact), project_name=settings.langchain_project)
        root = trace("assistant_answer", run_type="chain", inputs={"question": question},
                     metadata=metadata, tags=["assistant", kind])
    except Exception:  # noqa: BLE001 - observability must never break an answer
        logger.warning("LangSmith tracing unavailable; answering untraced", exc_info=True)
        yield None
        return
    with context, root as run:
        yield run


def step(name: str, run_type: str = "chain", inputs: dict | None = None):
    """A nested run inside the current answer trace; a no-op context when tracing is off."""
    if not enabled():
        return nullcontext()
    from langsmith import get_current_run_tree, trace

    if get_current_run_tree() is None:  # only ever nested under an answer
        return nullcontext()
    return trace(name, run_type=run_type, inputs=inputs or {})


@contextmanager
def untraced():
    """Keep a model call out of LangSmith even when global LangChain tracing is on (for example, voice audio)."""
    try:
        from langsmith import tracing_context
    except ImportError:  # pragma: no cover
        yield
        return
    with tracing_context(enabled=False):
        yield


def finish(run, outputs: dict, metadata: dict) -> None:
    if run is None:
        return
    try:
        run.add_metadata(metadata)
        run.add_outputs(outputs)
    except Exception:  # noqa: BLE001
        logger.warning("Could not attach LangSmith run results", exc_info=True)
