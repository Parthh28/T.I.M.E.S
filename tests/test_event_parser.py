"""
Tests for the event_parser module.

Tests cover:
- Valid event parsing
- Relative date handling
- Missing required fields
- Invalid times and dates
- End time before start time
- Past event rejection
- Duration parsing
- Reminder validation
"""

import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

import pytz

from event_parser import (
    validate_and_build_event,
    EventValidationError,
    format_event_summary,
    _format_reminder_time,
    _escape_md,
)

TIMEZONE = pytz.timezone("Asia/Kolkata")


class TestValidateAndBuildEvent(unittest.TestCase):
    """Tests for the validate_and_build_event function."""

    def _future_date(self, days=1):
        """Return a date string N days from now."""
        dt = datetime.now(TIMEZONE) + timedelta(days=days)
        return dt.strftime("%Y-%m-%d")

    def _future_time(self, hours=1):
        """Return a time string N hours from now."""
        dt = datetime.now(TIMEZONE) + timedelta(hours=hours)
        return dt.strftime("%H:%M")

    def test_valid_event_with_duration(self):
        """Complete event with title, date, time, and duration."""
        event = validate_and_build_event(
            title="Team Meeting",
            start_date=self._future_date(1),
            start_time="10:00",
            duration_minutes=60,
        )
        self.assertEqual(event.title, "Team Meeting")
        self.assertEqual(
            (event.end_datetime - event.start_datetime).total_seconds(),
            3600,
        )

    def test_valid_event_with_end_time(self):
        """Event with explicit end time."""
        event = validate_and_build_event(
            title="Workshop",
            start_date=self._future_date(1),
            start_time="09:00",
            end_time="11:30",
        )
        self.assertEqual(event.title, "Workshop")
        duration = (event.end_datetime - event.start_datetime).total_seconds()
        self.assertEqual(duration, 9000)  # 2.5 hours

    def test_valid_event_with_description(self):
        """Event with optional description."""
        event = validate_and_build_event(
            title="Project Review",
            description="Quarterly review of project milestones",
            start_date=self._future_date(2),
            start_time="14:00",
            duration_minutes=90,
        )
        self.assertEqual(event.description, "Quarterly review of project milestones")

    def test_valid_event_with_reminder(self):
        """Event with reminder specified."""
        event = validate_and_build_event(
            title="Doctor Appointment",
            start_date=self._future_date(3),
            start_time="16:00",
            duration_minutes=30,
            reminder_minutes=30,
        )
        self.assertEqual(event.reminder_minutes, 30)

    def test_valid_event_with_multiple_reminders(self):
        """Event with multiple tiered reminders [1440, 60, 15]."""
        event = validate_and_build_event(
            title="Tech Conference",
            start_date=self._future_date(5),
            start_time="09:00",
            duration_minutes=120,
            reminder_minutes=[1440, 60, 15],
        )
        self.assertEqual(event.reminder_minutes, [1440, 60, 15])
        self.assertEqual(event.get_reminder_minutes_list(), [1440, 60, 15])

    def test_default_duration_one_hour(self):
        """Without explicit end time or duration, default is 1 hour."""
        event = validate_and_build_event(
            title="Quick Sync",
            start_date=self._future_date(1),
            start_time="10:00",
        )
        duration = (event.end_datetime - event.start_datetime).total_seconds()
        self.assertEqual(duration, 3600)

    # ── Missing fields ──

    def test_missing_title(self):
        """Missing title should raise an error."""
        with self.assertRaises(EventValidationError) as ctx:
            validate_and_build_event(
                title=None,
                start_date=self._future_date(1),
                start_time="10:00",
            )
        self.assertIn("title", str(ctx.exception).lower())

    def test_empty_title(self):
        """Empty string title should raise an error."""
        with self.assertRaises(EventValidationError):
            validate_and_build_event(
                title="   ",
                start_date=self._future_date(1),
                start_time="10:00",
            )

    def test_missing_start_date(self):
        """Missing start date should raise an error."""
        with self.assertRaises(EventValidationError) as ctx:
            validate_and_build_event(
                title="Meeting",
                start_date=None,
                start_time="10:00",
            )
        self.assertIn("date", str(ctx.exception).lower())

    def test_missing_start_time(self):
        """Missing start time should raise an error."""
        with self.assertRaises(EventValidationError) as ctx:
            validate_and_build_event(
                title="Meeting",
                start_date=self._future_date(1),
                start_time=None,
            )
        self.assertIn("time", str(ctx.exception).lower())

    # ── Invalid formats ──

    def test_invalid_date_format(self):
        """Invalid date format should raise an error."""
        with self.assertRaises(EventValidationError) as ctx:
            validate_and_build_event(
                title="Meeting",
                start_date="32-13-2026",
                start_time="10:00",
            )
        self.assertIn("Invalid date", str(ctx.exception))

    def test_invalid_time_format(self):
        """Invalid time format should raise an error."""
        with self.assertRaises(EventValidationError) as ctx:
            validate_and_build_event(
                title="Meeting",
                start_date=self._future_date(1),
                start_time="25:70",
            )
        self.assertIn("Invalid time", str(ctx.exception))

    def test_invalid_end_time_format(self):
        """Invalid end time format should raise an error."""
        with self.assertRaises(EventValidationError):
            validate_and_build_event(
                title="Meeting",
                start_date=self._future_date(1),
                start_time="10:00",
                end_time="abc",
            )

    # ── End time before start time ──

    def test_end_time_before_start_time_same_day(self):
        """End time earlier than start time on the same day wraps to next day."""
        # This should NOT raise because end_time < start_time wraps to next day
        event = validate_and_build_event(
            title="Night Shift",
            start_date=self._future_date(1),
            start_time="23:00",
            end_time="02:00",
        )
        self.assertGreater(event.end_datetime, event.start_datetime)

    def test_explicit_end_before_start_raises(self):
        """End datetime explicitly before start datetime should raise."""
        future = self._future_date(2)
        with self.assertRaises(EventValidationError) as ctx:
            validate_and_build_event(
                title="Meeting",
                start_date=future,
                start_time="15:00",
                end_date=self._future_date(1),  # day before
                end_time="10:00",
            )
        self.assertIn("after start time", str(ctx.exception).lower())

    # ── Past event ──

    def test_past_event_rejected(self):
        """Event in the past should be rejected."""
        yesterday = (datetime.now(TIMEZONE) - timedelta(days=1)).strftime("%Y-%m-%d")
        with self.assertRaises(EventValidationError) as ctx:
            validate_and_build_event(
                title="Past Meeting",
                start_date=yesterday,
                start_time="10:00",
            )
        self.assertIn("past", str(ctx.exception).lower())

    # ── Duration validation ──

    def test_negative_duration(self):
        """Negative duration should raise an error."""
        with self.assertRaises(EventValidationError):
            validate_and_build_event(
                title="Meeting",
                start_date=self._future_date(1),
                start_time="10:00",
                duration_minutes=-30,
            )

    def test_zero_duration(self):
        """Zero duration should raise an error."""
        with self.assertRaises(EventValidationError):
            validate_and_build_event(
                title="Meeting",
                start_date=self._future_date(1),
                start_time="10:00",
                duration_minutes=0,
            )

    def test_excessive_duration(self):
        """Duration > 7 days should raise an error."""
        with self.assertRaises(EventValidationError):
            validate_and_build_event(
                title="Marathon",
                start_date=self._future_date(1),
                start_time="10:00",
                duration_minutes=10081,
            )

    # ── Reminder validation ──

    def test_negative_reminder(self):
        """Negative reminder minutes should raise an error."""
        with self.assertRaises(EventValidationError):
            validate_and_build_event(
                title="Meeting",
                start_date=self._future_date(1),
                start_time="10:00",
                reminder_minutes=-10,
            )

    def test_excessive_reminder(self):
        """Reminder > 28 days should raise an error."""
        with self.assertRaises(EventValidationError):
            validate_and_build_event(
                title="Meeting",
                start_date=self._future_date(30),
                start_time="10:00",
                reminder_minutes=40321,
            )

    def test_negative_in_reminder_list(self):
        """Negative value inside reminder list should raise an error."""
        with self.assertRaises(EventValidationError):
            validate_and_build_event(
                title="Meeting",
                start_date=self._future_date(5),
                start_time="10:00",
                reminder_minutes=[1440, -15],
            )

    def test_excessive_in_reminder_list(self):
        """Value > 28 days inside reminder list should raise an error."""
        with self.assertRaises(EventValidationError):
            validate_and_build_event(
                title="Meeting",
                start_date=self._future_date(35),
                start_time="10:00",
                reminder_minutes=[1440, 50000],
            )

    def test_title_too_long(self):
        """Excessively long title should raise an error."""
        with self.assertRaises(EventValidationError):
            validate_and_build_event(
                title="A" * 201,
                start_date=self._future_date(1),
                start_time="10:00",
            )

    def test_description_truncation(self):
        """Long description should be truncated to 2000 chars."""
        event = validate_and_build_event(
            title="Meeting",
            description="X" * 3000,
            start_date=self._future_date(1),
            start_time="10:00",
        )
        self.assertEqual(len(event.description), 2000)


class TestFormatEventSummary(unittest.TestCase):
    """Tests for the format_event_summary function."""

    def test_basic_formatting(self):
        """Summary should contain all key event details."""
        event = validate_and_build_event(
            title="Team Meeting",
            start_date=(datetime.now(TIMEZONE) + timedelta(days=1)).strftime("%Y-%m-%d"),
            start_time="10:00",
            duration_minutes=60,
            reminder_minutes=30,
        )
        summary = format_event_summary(event)
        self.assertIn("Team Meeting", summary)
        self.assertIn("10:00 AM", summary)
        self.assertIn("Reminder", summary)

    def test_multiple_reminders_formatting(self):
        """Summary should display all reminder tiers clearly."""
        event = validate_and_build_event(
            title="Global Summit",
            start_date=(datetime.now(TIMEZONE) + timedelta(days=3)).strftime("%Y-%m-%d"),
            start_time="14:00",
            duration_minutes=60,
            reminder_minutes=[1440, 60, 15],
        )
        summary = format_event_summary(event)
        self.assertIn("Reminders", summary)
        self.assertIn("1 day before", summary)
        self.assertIn("1 hour before", summary)
        self.assertIn("15 minutes before", summary)


class TestFormatReminderTime(unittest.TestCase):
    """Tests for the _format_reminder_time helper."""

    def test_minutes(self):
        self.assertEqual(_format_reminder_time(10), "10 minutes before")

    def test_one_hour(self):
        self.assertEqual(_format_reminder_time(60), "1 hour before")

    def test_multiple_hours(self):
        self.assertEqual(_format_reminder_time(120), "2 hours before")

    def test_one_day(self):
        self.assertEqual(_format_reminder_time(1440), "1 day before")

    def test_multiple_days(self):
        self.assertEqual(_format_reminder_time(2880), "2 days before")


class TestEscapeMarkdown(unittest.TestCase):
    """Tests for MarkdownV2 escaping."""

    def test_escape_special_chars(self):
        result = _escape_md("Hello *world* [test]")
        self.assertIn("\\*", result)
        self.assertIn("\\[", result)
        self.assertIn("\\]", result)

    def test_plain_text_unchanged(self):
        result = _escape_md("Hello world")
        self.assertEqual(result, "Hello world")


if __name__ == "__main__":
    unittest.main()
