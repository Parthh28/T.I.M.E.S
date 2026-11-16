"""
Text-to-Speech (TTS) service module for T.I.M.E.S Calendar Assistant.

Provides spoken audio alerts through:
1. Local computer/laptop speakers via pyttsx3 (native Windows SAPI5).
2. Phone voice messages via Telegram audio notes generated with gTTS.

Runs asynchronously in worker threads so it never blocks the asyncio event loop.
"""

import asyncio
import io
import logging
from typing import Optional

from config import (
    TTS_PHONE_VOICE_ENABLED,
    TTS_COMPUTER_SPEAKER_ENABLED,
    TTS_VOICE_RATE,
)

logger = logging.getLogger("calendar_bot.tts")

try:
    import pythoncom
except ImportError:
    pythoncom = None


def _speak_sync(text: str, rate: int = TTS_VOICE_RATE) -> bool:
    """
    Synchronous helper to synthesize and play speech through speakers.

    Ensures proper COM initialization for Windows worker threads.
    """
    if pythoncom:
        try:
            pythoncom.CoInitialize()
        except Exception:
            pass

    try:
        import pyttsx3
        engine = pyttsx3.init()
        engine.setProperty("rate", rate)
        engine.setProperty("volume", 1.0)
        engine.say(text)
        engine.runAndWait()
        return True
    except Exception as e:
        logger.warning("TTS speech failed: %s", e)
        return False
    finally:
        if pythoncom:
            try:
                pythoncom.CoUninitialize()
            except Exception:
                pass


async def speak_text(text: str, rate: Optional[int] = None) -> bool:
    """
    Asynchronously speak text aloud through computer speakers.

    Args:
        text: The phrase to speak.
        rate: Optional speech rate in words per minute (default from config).

    Returns:
        True if speech completed successfully, False otherwise.
    """
    if not TTS_COMPUTER_SPEAKER_ENABLED:
        logger.debug("Computer speaker TTS is disabled by configuration.")
        return False

    if not text or not text.strip():
        return False

    speech_rate = rate or TTS_VOICE_RATE
    try:
        success = await asyncio.to_thread(_speak_sync, text.strip(), speech_rate)
        if success:
            logger.info("Spoke aloud: '%s'", text.strip())
        return success
    except Exception as e:
        logger.warning("Error running TTS in background thread: %s", e)
        return False


async def speak_event_start(event_title: Optional[str] = None) -> bool:
    """
    Announce event start out loud through speakers.

    Phrase:
        "The event is gonna start now: [event_title]"
    """
    if event_title and event_title.strip():
        phrase = f"The event is gonna start now: {event_title.strip()}"
    else:
        phrase = "The event is gonna start now."

    return await speak_text(phrase)


def _generate_speech_bytes_sync(text: str) -> Optional[bytes]:
    """Generate MP3 audio bytes using gTTS."""
    try:
        from gtts import gTTS
        fp = io.BytesIO()
        tts = gTTS(text=text, lang="en")
        tts.write_to_fp(fp)
        return fp.getvalue()
    except Exception as e:
        logger.warning("gTTS audio generation failed: %s", e)
        return None


async def generate_speech_audio(text: str) -> Optional[bytes]:
    """
    Generate speech audio bytes (MP3) asynchronously for sending to phone.
    """
    if not text or not text.strip():
        return None
    try:
        return await asyncio.to_thread(_generate_speech_bytes_sync, text.strip())
    except Exception as e:
        logger.warning("Error generating speech audio in thread: %s", e)
        return None


async def send_voice_announcement(
    bot, chat_id: int, event_title: Optional[str] = None
) -> bool:
    """
    Generate and send a voice message directly to the user's phone via Telegram.

    Phrase:
        "The event is gonna start now: [event_title]"
    """
    if not TTS_PHONE_VOICE_ENABLED:
        logger.debug("Phone voice announcement is disabled by configuration.")
        return False

    if event_title and event_title.strip():
        phrase = f"The event is gonna start now: {event_title.strip()}"
    else:
        phrase = "The event is gonna start now."

    try:
        audio_bytes = await generate_speech_audio(phrase)
        if not audio_bytes:
            return False

        from telegram import InputFile
        fp = io.BytesIO(audio_bytes)
        await bot.send_voice(
            chat_id=chat_id,
            voice=InputFile(fp, filename="voice.mp3"),
            caption=f"🎙️ *Voice Alert*: \"_{phrase}_\"",
            parse_mode="Markdown",
        )
        logger.info("Sent voice note to Telegram chat %d: '%s'", chat_id, phrase)
        return True
    except Exception as e:
        logger.warning("Failed to send voice note to Telegram: %s", e)
        return False
