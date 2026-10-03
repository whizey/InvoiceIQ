"""app/tools/sarvam_client.py — never a live Sarvam call in this suite
(conftest.py nulls sarvam_api_key for every test). Every failure mode
(missing key, HTTP error, malformed response) must return None, not raise."""

import base64
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.core.config import settings
from app.tools.sarvam_client import speech_to_text, text_to_speech

pytestmark = pytest.mark.asyncio


async def test_tts_returns_none_when_no_key():
    assert await text_to_speech("namaste") is None


async def test_stt_returns_none_when_no_key():
    assert await speech_to_text(b"fake-audio-bytes", "reply.webm", "audio/webm") is None


async def test_tts_returns_decoded_audio_bytes(monkeypatch):
    monkeypatch.setattr(settings, "sarvam_api_key", "sk_fake")
    fake_audio = base64.b64encode(b"fake-mp3-bytes").decode("ascii")

    mock_response = MagicMock()
    mock_response.raise_for_status = MagicMock()
    mock_response.json = MagicMock(return_value={"audios": [fake_audio]})
    mock_post = AsyncMock(return_value=mock_response)

    with patch("httpx.AsyncClient.post", new=mock_post):
        audio = await text_to_speech("namaste, kaise hain aap")

    assert audio == b"fake-mp3-bytes"
    _args, kwargs = mock_post.call_args
    assert kwargs["headers"]["api-subscription-key"] == "sk_fake"
    assert kwargs["json"]["target_language_code"] == "hi-IN"
    assert kwargs["json"]["speaker"] == "aditya"
    assert kwargs["json"]["model"] == "bulbul:v3"


async def test_tts_malformed_response_returns_none_not_raise(monkeypatch):
    monkeypatch.setattr(settings, "sarvam_api_key", "sk_fake")
    mock_response = MagicMock()
    mock_response.raise_for_status = MagicMock()
    mock_response.json = MagicMock(return_value={"unexpected": "shape"})

    with patch("httpx.AsyncClient.post", new=AsyncMock(return_value=mock_response)):
        assert await text_to_speech("namaste") is None


async def test_tts_http_error_returns_none_not_raise(monkeypatch):
    monkeypatch.setattr(settings, "sarvam_api_key", "sk_fake")
    with patch("httpx.AsyncClient.post", new=AsyncMock(side_effect=RuntimeError("network down"))):
        assert await text_to_speech("namaste") is None


async def test_stt_returns_transcript(monkeypatch):
    monkeypatch.setattr(settings, "sarvam_api_key", "sk_fake")
    mock_response = MagicMock()
    mock_response.raise_for_status = MagicMock()
    mock_response.json = MagicMock(return_value={"transcript": "haan main kal pay kar dunga"})
    mock_post = AsyncMock(return_value=mock_response)

    with patch("httpx.AsyncClient.post", new=mock_post):
        transcript = await speech_to_text(b"fake-audio-bytes", "reply.webm", "audio/webm")

    assert transcript == "haan main kal pay kar dunga"
    _args, kwargs = mock_post.call_args
    assert kwargs["headers"]["api-subscription-key"] == "sk_fake"
    assert kwargs["data"]["model"] == "saaras:v3"
    assert kwargs["data"]["mode"] == "codemix"


async def test_stt_malformed_response_returns_none_not_raise(monkeypatch):
    monkeypatch.setattr(settings, "sarvam_api_key", "sk_fake")
    mock_response = MagicMock()
    mock_response.raise_for_status = MagicMock()
    mock_response.json = MagicMock(return_value={"unexpected": "shape"})

    with patch("httpx.AsyncClient.post", new=AsyncMock(return_value=mock_response)):
        assert await speech_to_text(b"fake-audio-bytes", "reply.webm", "audio/webm") is None
