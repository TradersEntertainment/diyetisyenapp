"""Text-to-speech for voice replies via ElevenLabs.

Voice is a bonus on top of the text reply. It only runs when ELEVENLABS_API_KEY
is set; if the key is removed or the call fails, synthesize() returns None and
the caller simply sends text — the bot never breaks.
"""
import asyncio
import logging
import re

import httpx

from app.config import get_settings

log = logging.getLogger(__name__)

_URL = "https://api.elevenlabs.io/v1/text-to-speech/{voice_id}"
_MAX_CHARS = 800  # cap spoken length (character = quota); full text is in the message


def tts_available() -> bool:
    return bool(get_settings().elevenlabs_api_key)


def clean_for_speech(text: str) -> str:
    """Strip markdown, emojis and sentinels so the spoken version sounds natural."""
    text = text.replace("[SESSIZ]", " ").replace("[PLAN_GORSEL]", " ").replace("[FOTO_KAYDET]", " ")
    text = re.sub(r"[*_`#>]", "", text)               # markdown markers
    text = re.sub(r"https?://\S+", "", text)           # urls
    # Drop emoji / pictographs / symbols.
    text = re.sub(
        "[\U0001F000-\U0001FAFF\U00002600-\U000027BF\U0001F1E6-\U0001F1FF←-⇿⌀-⏿]",
        "", text,
    )
    text = re.sub(r"[ \t]+", " ", text).strip()
    return text[:_MAX_CHARS]


async def _to_ogg_opus(mp3: bytes) -> tuple[bytes, str]:
    """Convert mp3 to OGG/Opus for a real Telegram voice note; fall back to mp3."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-i", "pipe:0", "-c:a", "libopus", "-b:a", "32k", "-f", "ogg", "pipe:1",
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        )
        out, _ = await proc.communicate(mp3)
        if proc.returncode == 0 and out:
            return out, "ogg"
    except Exception:
        log.warning("ffmpeg conversion failed; sending mp3", exc_info=True)
    return mp3, "mp3"


async def synthesize(text: str) -> tuple[bytes, str] | None:
    """Return (audio_bytes, "ogg"|"mp3") for a voice note, or None if unavailable."""
    s = get_settings()
    if not s.elevenlabs_api_key:
        return None
    spoken = clean_for_speech(text)
    if not spoken:
        return None
    try:
        async with httpx.AsyncClient(timeout=45) as client:
            resp = await client.post(
                _URL.format(voice_id=s.elevenlabs_voice_id),
                headers={"xi-api-key": s.elevenlabs_api_key, "accept": "audio/mpeg"},
                json={
                    "text": spoken,
                    "model_id": s.elevenlabs_model,
                    "voice_settings": {"stability": 0.5, "similarity_boost": 0.75},
                },
            )
            if resp.status_code != 200:
                log.warning("elevenlabs tts failed: %s %s", resp.status_code, resp.text[:200])
                return None
            return await _to_ogg_opus(resp.content)
    except Exception:
        log.exception("tts synthesis failed")
        return None
