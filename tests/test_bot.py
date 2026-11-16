"""
Tests for bot handler authorization and utility functions.

Tests cover:
- Unauthorized user rejection
- Time parsing
- Duration parsing
"""

import unittest
from unittest.mock import AsyncMock, MagicMock, patch
import asyncio

# We test the utility functions directly from bot module
# Import will need config mocking
import os
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token")
os.environ.setdefault("AUTHORIZED_USER_ID", "12345")
os.environ.setdefault("GEMINI_API_KEY", "test-key")
os.environ.setdefault("DATABASE_FILE", ":memory:")

from bot import (
    _parse_time_input,
    _parse_duration,
    authorized_only,
    _escape_url,
    _safe_edit_message_text,
    _safe_send_message,
)


class TestParseTimeInput(unittest.TestCase):
    """Tests for the _parse_time_input function."""

    def test_24_hour_format(self):
        self.assertEqual(_parse_time_input("14:30"), "14:30")

    def test_12_hour_format_pm(self):
        self.assertEqual(_parse_time_input("3 PM"), "15:00")

    def test_12_hour_format_am(self):
        self.assertEqual(_parse_time_input("9:30 AM"), "09:30")

    def test_12_hour_noon(self):
        self.assertEqual(_parse_time_input("12 PM"), "12:00")

    def test_midnight(self):
        self.assertEqual(_parse_time_input("00:00"), "00:00")

    def test_invalid_time(self):
        self.assertIsNone(_parse_time_input("not a time"))

    def test_empty_string(self):
        self.assertIsNone(_parse_time_input(""))


class TestParseDuration(unittest.TestCase):
    """Tests for the _parse_duration function."""

    def test_hours(self):
        self.assertEqual(_parse_duration("2 hours"), 120)

    def test_hour_singular(self):
        self.assertEqual(_parse_duration("1 hour"), 60)

    def test_minutes(self):
        self.assertEqual(_parse_duration("30 minutes"), 30)

    def test_min_abbreviation(self):
        self.assertEqual(_parse_duration("45 min"), 45)

    def test_fractional_hours(self):
        self.assertEqual(_parse_duration("1.5 hours"), 90)

    def test_plain_number(self):
        self.assertEqual(_parse_duration("60"), 60)

    def test_invalid_duration(self):
        self.assertIsNone(_parse_duration("not a duration"))

    def test_hours_abbreviation(self):
        self.assertEqual(_parse_duration("2h"), 120)

    def test_mins_abbreviation(self):
        self.assertEqual(_parse_duration("30m"), 30)


def _run_coroutine_simple(coro):
    """Synchronously advance a non-blocking coroutine to completion."""
    try:
        coro.send(None)
    except StopIteration as e:
        return e.value
    finally:
        coro.close()


class TestAuthorizedOnly(unittest.TestCase):
    """Tests for the authorized_only decorator."""

    def test_unauthorized_user_rejected(self):
        """Unauthorized user should be silently ignored."""
        @authorized_only
        async def handler(update, context):
            return "should not reach here"

        # Create mock update with unauthorized user
        update = MagicMock()
        update.effective_user = MagicMock()
        update.effective_user.id = 99999  # Not the authorized user
        context = MagicMock()

        result = _run_coroutine_simple(handler(update, context))
        self.assertIsNone(result)

    def test_authorized_user_allowed(self):
        """Authorized user should proceed normally."""
        @authorized_only
        async def handler(update, context):
            return "allowed"

        update = MagicMock()
        update.effective_user = MagicMock()
        update.effective_user.id = 12345  # The authorized user
        context = MagicMock()

        result = _run_coroutine_simple(handler(update, context))
        self.assertEqual(result, "allowed")

    def test_no_user_rejected(self):
        """Update with no user should be rejected."""
        @authorized_only
        async def handler(update, context):
            return "should not reach here"

        update = MagicMock()
        update.effective_user = None
        context = MagicMock()

        result = _run_coroutine_simple(handler(update, context))
        self.assertIsNone(result)


class TestEscapeUrl(unittest.TestCase):
    """Tests for MarkdownV2 URL escaping."""

    def test_escape_url_preserves_dots_and_equals(self):
        url = "https://www.google.com/calendar/event?eid=abc123_456"
        escaped = _escape_url(url)
        # Dots and equals should NOT be backslash-escaped in the URL component
        self.assertEqual(escaped, url)

    def test_escape_url_escapes_closing_parenthesis_and_backslash(self):
        url = r"https://example.com/path(1)\test"
        escaped = _escape_url(url)
        self.assertEqual(escaped, r"https://example.com/path(1\)\\test")

    def test_escape_url_empty_or_none(self):
        self.assertEqual(_escape_url(""), "")
        self.assertEqual(_escape_url(None), "")


class TestSafeMessageHelpers(unittest.TestCase):
    """Tests for safe Telegram message delivery fallbacks."""

    def test_safe_edit_message_text_success(self):
        query = MagicMock()
        query.edit_message_text = AsyncMock()
        _run_coroutine_simple(_safe_edit_message_text(query, "*Bold Text*", parse_mode="MarkdownV2"))
        query.edit_message_text.assert_called_once_with(
            text="*Bold Text*",
            parse_mode="MarkdownV2",
            disable_web_page_preview=True,
        )

    def test_safe_edit_message_text_fallback_on_parse_error(self):
        query = MagicMock()
        # First call with MarkdownV2 raises error (e.g. unescaped character)
        query.edit_message_text = AsyncMock(side_effect=[Exception("Can't parse entities"), None])
        _run_coroutine_simple(_safe_edit_message_text(query, "All set\\! Added *Title*", parse_mode="MarkdownV2"))
        self.assertEqual(query.edit_message_text.call_count, 2)
        # Fallback call should strip escape slashes/markers and pass parse_mode=None
        second_call_kwargs = query.edit_message_text.call_args[1]
        self.assertIsNone(second_call_kwargs["parse_mode"])
        self.assertIn("All set! Added Title", second_call_kwargs["text"])

    def test_safe_send_message_fallback_on_parse_error(self):
        bot = MagicMock()
        bot.send_message = AsyncMock(side_effect=[Exception("Can't parse entities"), None])
        _run_coroutine_simple(_safe_send_message(bot, chat_id=12345, text="All set\\! Added *Title*", parse_mode="MarkdownV2"))
        self.assertEqual(bot.send_message.call_count, 2)
        second_call_kwargs = bot.send_message.call_args[1]
        self.assertIsNone(second_call_kwargs["parse_mode"])
        self.assertIn("All set! Added Title", second_call_kwargs["text"])


if __name__ == "__main__":
    unittest.main()
