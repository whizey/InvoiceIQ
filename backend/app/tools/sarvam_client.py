"""Sarvam speech I/O for the Hinglish voice-call demo.

Sarvam is speech-only here — it never decides anything, it just converts
text to Hinglish speech (TTS) and the customer's recorded reply back to
text (STT). What to say and what to do next is decided by
app/agents/llm_client.voice_turn() (Gemini or a rule-based fallback), and
whether any resulting action actually executes is decided by
evaluate_policy() (app/services/policy_engine.py) — this module has no
opinion on either.

Both functions are gated on settings.sarvam_api_key and wrapped in a broad
except -> None, matching the "no crash" requirement: a missing key, a
network error, or a response Sarvam's API returns in a shape this code
doesn't expect all fall back to None, letting the caller run the fully
simulated (text-only) path instead.

UNCERTAIN: the exact request/response field names below are read from the
user-provided spec (endpoints, models, hi-IN, speaker "aditya", saaras:v3
codemix), not from live Sarvam API docs (not fetchable from here). This is
the one file to adjust if a live smoke test with a real SARVAM_API_KEY
shows a field-name mismatch.
"""

import base64
import logging

from app.core.config import settings

logger = logging.getLogger("app.voice")

TTS_ENDPOINT = "https://api.sarvam.ai/text-to-speech"
STT_ENDPOINT = "https://api.sarvam.ai/speech-to-text"
TTS_MODEL = "bulbul:v3"
TTS_LANGUAGE = "hi-IN"
TTS_SPEAKER = "aditya"
STT_MODEL = "saaras:v3"
STT_MODE = "codemix"


async def text_to_speech(text: str) -> bytes | None:
    """Hinglish text -> audio bytes, or None if unconfigured/unavailable."""
    if not settings.sarvam_api_key:
        return None

    import httpx

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.post(
                TTS_ENDPOINT,
                headers={"api-subscription-key": settings.sarvam_api_key},
                json={
                    "text": text,
                    "target_language_code": TTS_LANGUAGE,
                    "model": TTS_MODEL,
                    "speaker": TTS_SPEAKER,
                },
            )
            response.raise_for_status()
            data = response.json()
        audio_b64 = data.get("audios", [None])[0] or data.get("audio")
        if not audio_b64:
            logger.warning("Sarvam TTS response had no audio field: keys=%s", list(data.keys()))
            return None
        return base64.b64decode(audio_b64)
    except Exception:
        logger.warning("Sarvam TTS call failed", exc_info=True)
        return None


async def speech_to_text(audio_bytes: bytes, filename: str, content_type: str) -> str | None:
    """Recorded audio -> Hinglish/English transcript, or None if unconfigured/unavailable."""
    if not settings.sarvam_api_key:
        return None

    import httpx

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.post(
                STT_ENDPOINT,
                headers={"api-subscription-key": settings.sarvam_api_key},
                data={"model": STT_MODEL, "mode": STT_MODE},
                files={"file": (filename, audio_bytes, content_type)},
            )
            response.raise_for_status()
            data = response.json()
        transcript = data.get("transcript")
        if not transcript:
            logger.warning("Sarvam STT response had no transcript field: keys=%s", list(data.keys()))
            return None
        return transcript
    except Exception:
        logger.warning("Sarvam STT call failed", exc_info=True)
        return None
