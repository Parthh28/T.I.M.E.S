"""
Unit tests for Text-to-Speech (TTS) speaker and phone voice alerts.

Covers:
- `speak_text` and `speak_event_start` (computer speakers) in `tts_service.py`.
- `generate_speech_audio` and `send_voice_announcement` (phone voice notes) in `tts_service.py`.
- Formatter handling for `0` minutes in `event_parser.py`.
- `_send_reminder` triggering `speak_event_start` and `send_voice_announcement` at `reminder_minutes == 0`.
- `schedule_reminders_for_event` ensuring `0` minute tier is scheduled.
"""

import asyncio
from datetime import datetime, timedelta
from unittest.mock import MagicMock, AsyncMock, patch

from config import TIMEZONE
from tts_service import (
    speak_text,
    speak_event_start,
    generate_speech_audio,
    send_voice_announcement,
)
from event_parser import _format_reminder_time
from reminder_service import _send_reminder, schedule_reminders_for_event


def _run_coroutine_simple(coro):
    """Synchronously advance a non-blocking coroutine to completion."""
    try:
        coro.send(None)
    except StopIteration as e:
        return e.value
    finally:
        coro.close()


def test_speak_event_start_with_title():
    """Test that speak_event_start passes expected phrase to speak_text."""
    with patch("tts_service.speak_text", new_callable=AsyncMock) as mock_speak:
        mock_speak.return_value = True
        result = _run_coroutine_simple(speak_event_start("Sprint Planning"))
        assert result is True
        mock_speak.assert_called_once_with("The event is gonna start now: Sprint Planning")


def test_speak_event_start_without_title():
    """Test that speak_event_start passes default phrase when title is empty."""
    with patch("tts_service.speak_text", new_callable=AsyncMock) as mock_speak:
        mock_speak.return_value = True
        result = _run_coroutine_simple(speak_event_start(None))
        assert result is True
        mock_speak.assert_called_once_with("The event is gonna start now.")


def test_speak_text_disabled_by_default():
    """Test that speak_text returns False when TTS_COMPUTER_SPEAKER_ENABLED is False (default)."""
    with patch("tts_service.TTS_COMPUTER_SPEAKER_ENABLED", False):
        result = _run_coroutine_simple(speak_text("Hello world"))
        assert result is False


def test_speak_text_empty():
    """Test that speak_text returns False for blank text."""
    result = _run_coroutine_simple(speak_text("   "))
    assert result is False


def test_generate_speech_audio():
    """Test that generate_speech_audio produces audio bytes."""
    with patch("tts_service.asyncio.to_thread", new_callable=AsyncMock) as mock_thread:
        mock_thread.return_value = b"fake_mp3_data"
        audio = _run_coroutine_simple(generate_speech_audio("Test audio"))
        assert audio == b"fake_mp3_data"


def test_send_voice_announcement():
    """Test that send_voice_announcement calls bot.send_voice with audio."""
    mock_bot = MagicMock()
    mock_bot.send_voice = AsyncMock()

    with patch("tts_service.generate_speech_audio", new_callable=AsyncMock) as mock_gen:
        mock_gen.return_value = b"mp3_bytes_here"
        success = _run_coroutine_simple(send_voice_announcement(mock_bot, 12345, "Important Meeting"))
        assert success is True
        mock_bot.send_voice.assert_called_once()
        call_kwargs = mock_bot.send_voice.call_args[1]
        assert call_kwargs["chat_id"] == 12345
        assert "The event is gonna start now: Important Meeting" in call_kwargs["caption"]


def test_format_reminder_time_zero_minutes():
    """Test that 0 minutes returns the humanized 'at start' label."""
    formatted = _format_reminder_time(0)
    assert formatted == "at start"


def test_send_reminder_at_zero_minutes():
    """Test that _send_reminder with reminder_minutes=0 triggers speak_event_start and sends a message."""
    mock_bot = MagicMock()
    mock_bot.send_message = AsyncMock()

    with patch("reminder_service._bot", mock_bot), \
         patch("reminder_service.mark_reminder_sent", return_value=True), \
         patch("tts_service.speak_event_start", new_callable=AsyncMock) as mock_tts:

        event_start = (datetime.now(TIMEZONE) + timedelta(minutes=1)).isoformat()
        _run_coroutine_simple(
            _send_reminder(
                reminder_id="rem_zero_123",
                telegram_user_id=12345,
                event_title="Hackathon Demo",
                event_start_time=event_start,
                reminder_minutes=0,
                is_late=False,
            )
        )

        # speak_event_start should be called for the at-start reminder
        mock_tts.assert_called_once_with("Hackathon Demo")

        # A Telegram message should also be sent
        mock_bot.send_message.assert_called_once()
        sent_text = mock_bot.send_message.call_args[1]["text"]
        assert "Starting now" in sent_text
        assert "Hackathon Demo" in sent_text


def test_schedule_reminders_includes_zero():
    """Test that schedule_reminders_for_event includes 0 in tiers so event start is scheduled."""
    now = datetime.now(TIMEZONE)
    future_start = now + timedelta(hours=2)

    with patch("reminder_service.schedule_reminder") as mock_sched:
        mock_sched.return_value = "rem_mock_id"
        scheduled = schedule_reminders_for_event(
            event_id="ev_1",
            google_event_id="g_ev_1",
            telegram_user_id=12345,
            event_title="Standup",
            event_start_time=future_start,
            reminder_minutes_list=[15],
        )

        # Should schedule 15m and 0m (at event start)
        assert len(scheduled) == 2
        calls = mock_sched.call_args_list
        minutes_scheduled = [c[1]["reminder_minutes"] for c in calls]
        assert 15 in minutes_scheduled
        assert 0 in minutes_scheduled
