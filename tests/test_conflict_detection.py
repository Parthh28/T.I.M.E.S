"""
Tests for Conflict Detection and Smart Suggestions.

Covers:
- `check_conflicts` with mocked freebusy Google Calendar API responses.
- `get_conflicting_event_details` overlapping event discovery.
- `suggest_alternative_time` nearest free slot calculation.
- `_build_conflict_card` Telegram card and inline keyboard generation.
"""

from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch
import pytest

from config import TIMEZONE
from calendar_service import (
    check_conflicts,
    get_conflicting_event_details,
    suggest_alternative_time,
    CalendarAPIError,
)
from event_parser import ValidatedEvent
from bot import _build_conflict_card


@pytest.fixture
def base_times():
    """Returns a baseline start and end time in TIMEZONE."""
    start = datetime(2026, 11, 5, 14, 0, tzinfo=TIMEZONE)  # 2:00 PM
    end = datetime(2026, 11, 5, 15, 0, tzinfo=TIMEZONE)    # 3:00 PM
    return start, end


@pytest.fixture
def mock_validated_event(base_times):
    start, end = base_times
    return ValidatedEvent(
        title="Project Review",
        start_datetime=start,
        end_datetime=end,
        timezone=str(TIMEZONE),
        description="Quarterly review",
        reminder_minutes=[15],
    )


def test_check_conflicts_has_busy(base_times):
    """Test check_conflicts returns busy intervals from freebusy query."""
    start, end = base_times
    busy_response = {
        "calendars": {
            "primary": {
                "busy": [
                    {"start": "2026-11-05T14:15:00+05:30", "end": "2026-11-05T14:45:00+05:30"}
                ]
            }
        }
    }

    mock_service = MagicMock()
    mock_service.freebusy().query().execute.return_value = busy_response

    with patch("calendar_service._get_service", return_value=mock_service):
        conflicts = check_conflicts(start, end)
        assert len(conflicts) == 1
        assert conflicts[0]["start"] == "2026-11-05T14:15:00+05:30"


def test_check_conflicts_free(base_times):
    """Test check_conflicts returns empty list when no busy intervals."""
    start, end = base_times
    free_response = {
        "calendars": {
            "primary": {
                "busy": []
            }
        }
    }

    mock_service = MagicMock()
    mock_service.freebusy().query().execute.return_value = free_response

    with patch("calendar_service._get_service", return_value=mock_service):
        conflicts = check_conflicts(start, end)
        assert conflicts == []


def test_get_conflicting_event_details_filters_birthday(base_times):
    """Test get_conflicting_event_details excludes birthday events."""
    start, end = base_times
    events_response = {
        "items": [
            {
                "id": "ev1",
                "summary": "Team Standup",
                "start": {"dateTime": "2026-11-05T14:00:00+05:30"},
                "end": {"dateTime": "2026-11-05T14:30:00+05:30"},
            },
            {
                "id": "ev2",
                "summary": "Alice's Birthday",
                "start": {"date": "2026-11-05"},
                "end": {"date": "2026-11-06"},
                "eventType": "birthday",
            },
        ]
    }

    mock_service = MagicMock()
    mock_service.events().list().execute.return_value = events_response

    with patch("calendar_service._get_service", return_value=mock_service):
        details = get_conflicting_event_details(start, end)
        assert len(details) == 1
        assert details[0]["summary"] == "Team Standup"


def test_suggest_alternative_time_finds_next_free_slot(base_times):
    """Test suggest_alternative_time steps past conflicting period."""
    start, end = base_times  # 2:00 PM to 3:00 PM (1 hour duration)

    # Let's say 2:00 PM to 3:30 PM is busy:
    # First query for candidate (2:15 PM): busy until 15:30
    # Next candidate after buffer (15:30 + 15m = 15:45): free!
    def mock_query(body):
        req_start = body["timeMin"]
        req_end = body["timeMax"]
        exec_mock = MagicMock()
        # If candidate starts before 15:30, it's busy
        if "14:" in req_start or "15:00" in req_start or "15:15" in req_start:
            exec_mock.execute.return_value = {
                "calendars": {
                    "primary": {
                        "busy": [
                            {"start": "2026-11-05T14:00:00+05:30", "end": "2026-11-05T15:30:00+05:30"}
                        ]
                    }
                }
            }
        else:
            exec_mock.execute.return_value = {
                "calendars": {"primary": {"busy": []}}
            }
        return exec_mock

    mock_service = MagicMock()
    mock_service.freebusy().query.side_effect = mock_query

    with patch("calendar_service._get_service", return_value=mock_service):
        suggested = suggest_alternative_time(start, end)
        assert suggested is not None
        assert suggested.hour == 15
        assert suggested.minute == 45


def test_suggest_alternative_time_none_when_fully_booked(base_times):
    """Test suggest_alternative_time returns None if the entire search window is busy."""
    start, end = base_times

    mock_service = MagicMock()
    mock_service.freebusy().query().execute.return_value = {
        "calendars": {
            "primary": {
                "busy": [
                    {"start": "2026-11-05T14:00:00+05:30", "end": "2026-11-05T23:59:00+05:30"}
                ]
            }
        }
    }

    with patch("calendar_service._get_service", return_value=mock_service):
        suggested = suggest_alternative_time(start, end, search_window_hours=4)
        assert suggested is None


def test_build_conflict_card(mock_validated_event):
    """Test that _build_conflict_card renders proper warnings, conflicts, and buttons."""
    conflicting_events = [
        {
            "summary": "Team Sync",
            "start": {"dateTime": "2026-11-05T14:00:00+05:30"},
            "end": {"dateTime": "2026-11-05T15:00:00+05:30"},
        }
    ]
    suggested_start = datetime(2026, 11, 5, 15, 15, tzinfo=TIMEZONE)

    card_text, reply_markup = _build_conflict_card(
        mock_validated_event,
        confirmation_id="test_conf_123",
        conflicting_events=conflicting_events,
        suggested_start=suggested_start,
    )

    assert "Scheduling Conflict Detected" in card_text
    assert "Team Sync" in card_text
    assert "Suggested alternative" in card_text

    # Verify buttons
    keyboard = reply_markup.inline_keyboard
    assert len(keyboard) == 2  # Row 1: Suggested alternative, Row 2: Override & Cancel
    alt_button = keyboard[0][0]
    assert "03:15 PM" in alt_button.text
    assert alt_button.callback_data == "conflalt_test_conf_123"

    override_button = keyboard[1][0]
    assert "Schedule Anyway" in override_button.text
    assert override_button.callback_data == "conflovr_test_conf_123"

    cancel_button = keyboard[1][1]
    assert "Cancel" in cancel_button.text
    assert cancel_button.callback_data == "cancel_test_conf_123"
