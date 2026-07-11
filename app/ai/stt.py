"""Speech-to-text for Telegram voice messages.

Uses Groq's Whisper endpoint when GROQ_API_KEY is set (cheap + fast), otherwise
OpenAI's; both speak the same OpenAI-compatible transcription API. Returns None
when no key is configured or the call fails, so callers can fall back to asking
the user to type.
"""
import logging

import httpx

from app.config import get_settings

log = logging.getLogger(__name__)

_GROQ_URL = "https://api.groq.com/openai/v1/audio/transcriptions"
_OPENAI_URL = "https://api.openai.com/v1/audio/transcriptions"


def stt_available() -> bool:
    s = get_settings()
    return bool(s.groq_api_key or s.openai_api_key)


async def transcribe(audio_bytes: bytes, filename: str = "voice.ogg") -> str | None:
    s = get_settings()
    if s.groq_api_key:
        url, key, model = _GROQ_URL, s.groq_api_key, s.stt_model
    elif s.openai_api_key:
        url, key, model = _OPENAI_URL, s.openai_api_key, "whisper-1"
    else:
        return None
    try:
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(
                url,
                headers={"Authorization": f"Bearer {key}"},
                files={"file": (filename, audio_bytes, "audio/ogg")},
                data={"model": model, "language": "tr", "response_format": "json"},
            )
            resp.raise_for_status()
            return (resp.json().get("text") or "").strip() or None
    except Exception:
        log.exception("transcription failed")
        return None
