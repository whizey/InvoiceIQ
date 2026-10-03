"""Defensive, not functional: the scheduler ticks every ~60s and must never
trigger a real email or a voice call on its own (see
app/tools/email_provider.py, app/tools/sarvam_client.py, app/api/voice.py)
— only the case-page buttons may. A source-level check here is cheap
insurance against a future edit accidentally wiring one of those in.

Kept in its own file, deliberately not appended to tests/test_scheduler.py:
that module's other tests share a module-scoped async event loop
(`pytest.mark.asyncio(loop_scope="module")`), and a plain sync test placed
after them there disrupted that loop's teardown. This file needs no DB/event
loop at all — it's a pure source-text check.
"""

import inspect

from app.services import scheduler as scheduler_mod

# Specific identifiers, not bare "voice" — that also matches "invoices".
FORBIDDEN_REFERENCES = [
    "send_reminder_email",
    "get_email_provider",
    "ResendEmailProvider",
    "sarvam_client",
    "PLACE_VOICE_CALL",
    "voice_turn",
    "app.api.voice",
]


def test_scheduler_never_references_real_send_or_voice_paths():
    source = inspect.getsource(scheduler_mod)
    for name in FORBIDDEN_REFERENCES:
        assert name not in source, f"scheduler.py must never reference {name!r}"
