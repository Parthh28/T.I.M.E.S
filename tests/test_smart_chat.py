"""
Unit tests for the Smart Assistant & Chatbot capabilities.
"""

from unittest.mock import AsyncMock, MagicMock, patch

from gemini_service import ExtractedEvent, UnifiedActionRequest
from bot import _send_smart_reply, handle_natural_language


def _run_coroutine_simple(coro):
    """Synchronously advance a non-blocking coroutine to completion."""
    try:
        coro.send(None)
    except StopIteration as e:
        return e.value
    finally:
        coro.close()


def test_extracted_event_chat_fields():
    """Verify ExtractedEvent supports chat intent and calendar lookup flag."""
    event = ExtractedEvent(
        intent="chat",
        is_event_request=False,
        chat_response="I'm doing well! How can I help you today?",
        requires_calendar_lookup=False,
    )
    assert event.intent == "chat"
    assert event.is_event_request is False
    assert event.chat_response == "I'm doing well! How can I help you today?"
    assert event.requires_calendar_lookup is False


def test_extracted_event_schedule_query():
    """Verify schedule advice query flags requires_calendar_lookup."""
    event = ExtractedEvent(
        intent="chat",
        is_event_request=False,
        requires_calendar_lookup=True,
    )
    assert event.intent == "chat"
    assert event.requires_calendar_lookup is True


def test_send_smart_reply_markdown_success():
    """Test _send_smart_reply edits message with Markdown."""
    mock_msg = MagicMock()
    mock_msg.edit_text = AsyncMock()
    _run_coroutine_simple(_send_smart_reply(mock_msg, "*Hello!* How are you?"))
    mock_msg.edit_text.assert_called_once_with("*Hello!* How are you?", parse_mode="Markdown")


def test_send_smart_reply_fallback_on_parse_error():
    """Test _send_smart_reply falls back to plain text if Markdown parsing fails."""
    mock_msg = MagicMock()
    # First call fails (e.g. invalid markdown entity)
    mock_msg.edit_text = AsyncMock(side_effect=[Exception("Bad formatting"), None])

    _run_coroutine_simple(_send_smart_reply(mock_msg, "Text with unclosed *markdown"))
    assert mock_msg.edit_text.call_count == 2
    mock_msg.edit_text.assert_called_with("Text with unclosed *markdown")


def test_handle_natural_language_chat_response():
    """Test that free-form chat queries return Gemini's chat response."""
    mock_update = MagicMock()
    mock_update.effective_user.id = 12345
    mock_update.message.text = "Hello, who are you?"
    mock_thinking = MagicMock()
    mock_thinking.edit_text = AsyncMock()
    mock_update.message.reply_text = AsyncMock(return_value=mock_thinking)
    mock_context = MagicMock()

    mock_action = UnifiedActionRequest(
        intent="chat",
        chat_response="I am T.I.M.E.S, your personal assistant!",
        requires_calendar_lookup=False,
    )

    with patch("bot.AUTHORIZED_USER_ID", 12345), \
         patch("bot.find_urls", return_value=[]), \
         patch("bot.extract_action_from_text", new_callable=AsyncMock, return_value=mock_action), \
         patch("bot._send_smart_reply", new_callable=AsyncMock) as mock_reply:

        _run_coroutine_simple(handle_natural_language(mock_update, mock_context))
        mock_reply.assert_called_once_with(mock_thinking, "I am T.I.M.E.S, your personal assistant!")


def test_handle_natural_language_schedule_chat():
    """Test that schedule advice queries fetch calendar events and generate tailored advice."""
    mock_update = MagicMock()
    mock_update.effective_user.id = 12345
    mock_update.message.text = "Can I hit the gym at 6 PM today?"
    mock_thinking = MagicMock()
    mock_thinking.edit_text = AsyncMock()
    mock_update.message.reply_text = AsyncMock(return_value=mock_thinking)
    mock_context = MagicMock()

    mock_action = UnifiedActionRequest(
        intent="chat",
        requires_calendar_lookup=True,
    )

    dummy_events = [
        {
            "summary": "Team Sync",
            "start": {"dateTime": "2026-11-08T15:00:00+05:30"},
            "end": {"dateTime": "2026-11-08T16:00:00+05:30"},
        }
    ]

    with patch("bot.AUTHORIZED_USER_ID", 12345), \
         patch("bot.find_urls", return_value=[]), \
         patch("bot.extract_action_from_text", new_callable=AsyncMock, return_value=mock_action), \
         patch("bot.list_today_events", return_value=dummy_events), \
         patch("bot.list_upcoming_events", return_value=[]), \
         patch("bot.list_notion_tasks", new_callable=AsyncMock, return_value=[]), \
         patch("bot.generate_schedule_chat_response", new_callable=AsyncMock, return_value="You are free at 6 PM!") as mock_gen, \
         patch("bot._send_smart_reply", new_callable=AsyncMock) as mock_reply:

        _run_coroutine_simple(handle_natural_language(mock_update, mock_context))
        mock_gen.assert_called_once()
        mock_reply.assert_called_once_with(mock_thinking, "You are free at 6 PM!")

