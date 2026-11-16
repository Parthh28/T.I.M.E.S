"""
Event parser and validator module.

Validates and normalizes event data extracted by Gemini before any
calendar operation. Treats Gemini output as untrusted input.
"""

import logging
from datetime import datetime, timedelta
from dataclasses import dataclass
from typing import Optional

from config import TIMEZONE, TIMEZONE_NAME

logger = logging.getLogger("calendar_bot.event_parser")


@dataclass
class ValidatedEvent:
    """A fully validated and normalized event ready for calendar creation."""
    title: str
    description: Optional[str]
    start_datetime: datetime  # timezone-aware
    end_datetime: datetime    # timezone-aware
    timezone: str
    reminder_minutes: Optional[int | list[int]] = None

    def get_reminder_minutes_list(self) -> list[int]:
        """Return unique, positive reminder tiers sorted descending."""
        if self.reminder_minutes is None:
            return []
        if isinstance(self.reminder_minutes, int):
            return [self.reminder_minutes] if self.reminder_minutes > 0 else []
        if isinstance(self.reminder_minutes, (list, tuple)):
            cleaned = sorted(
                list({int(m) for m in self.reminder_minutes if isinstance(m, (int, float)) and m > 0}),
                reverse=True,
            )
            return cleaned
        return []


class EventValidationError(Exception):
    """Raised when event data fails validation with a user-friendly message."""
    pass


def validate_and_build_event(
    title: Optional[str] = None,
    description: Optional[str] = None,
    start_date: Optional[str] = None,
    start_time: Optional[str] = None,
    end_date: Optional[str] = None,
    end_time: Optional[str] = None,
    duration_minutes: Optional[int] = None,
    reminder_minutes: Optional[int | list[int]] = None,
    timezone_name: str = TIMEZONE_NAME,
) -> ValidatedEvent:
    """
    Validate all event fields and produce a ValidatedEvent.

    Raises EventValidationError with a user-friendly message for any problem.
    """
    # --- Title ---
    if not title or not title.strip():
        raise EventValidationError("Event title is required. What should I call this event?")
    title = title.strip()
    if len(title) > 200:
        raise EventValidationError("Event title is too long. Please keep it under 200 characters.")

    # --- Description ---
    if description:
        description = description.strip()
        if len(description) > 2000:
            description = description[:2000]

    # --- Start date ---
    if not start_date:
        raise EventValidationError("Start date is required. When is this event?")
    try:
        parsed_start_date = datetime.strptime(start_date, "%Y-%m-%d").date()
    except ValueError:
        raise EventValidationError(
            f"Invalid date format: '{start_date}'. Expected YYYY-MM-DD."
        )

    # --- Start time ---
    if not start_time:
        raise EventValidationError("Start time is required. What time does this event start?")
    try:
        parsed_start_time = datetime.strptime(start_time, "%H:%M").time()
    except ValueError:
        raise EventValidationError(
            f"Invalid time format: '{start_time}'. Expected HH:MM (24-hour)."
        )

    # --- Combine start date + time and localize ---
    tz = TIMEZONE
    start_dt = datetime.combine(parsed_start_date, parsed_start_time)
    start_dt = tz.localize(start_dt)

    # --- End datetime ---
    if end_date and end_time:
        try:
            parsed_end_date = datetime.strptime(end_date, "%Y-%m-%d").date()
        except ValueError:
            raise EventValidationError(
                f"Invalid end date format: '{end_date}'. Expected YYYY-MM-DD."
            )
        try:
            parsed_end_time = datetime.strptime(end_time, "%H:%M").time()
        except ValueError:
            raise EventValidationError(
                f"Invalid end time format: '{end_time}'. Expected HH:MM (24-hour)."
            )
        end_dt = datetime.combine(parsed_end_date, parsed_end_time)
        end_dt = tz.localize(end_dt)
    elif end_time and not end_date:
        # Same day, different end time
        try:
            parsed_end_time = datetime.strptime(end_time, "%H:%M").time()
        except ValueError:
            raise EventValidationError(
                f"Invalid end time format: '{end_time}'. Expected HH:MM (24-hour)."
            )
        end_dt = datetime.combine(parsed_start_date, parsed_end_time)
        end_dt = tz.localize(end_dt)
        # If end time is before start time, assume next day
        if end_dt <= start_dt:
            end_dt += timedelta(days=1)
    elif duration_minutes is not None:
        if not isinstance(duration_minutes, int) or duration_minutes <= 0:
            raise EventValidationError(
                f"Invalid duration: {duration_minutes} minutes. Duration must be positive."
            )
        if duration_minutes > 10080:  # 7 days
            raise EventValidationError("Duration cannot exceed 7 days.")
        end_dt = start_dt + timedelta(minutes=duration_minutes)
    else:
        # Default: 1-hour event
        end_dt = start_dt + timedelta(hours=1)

    # --- Validate end > start ---
    if end_dt <= start_dt:
        raise EventValidationError(
            "End time must be after start time. Please check the times."
        )

    # --- Check not in the past ---
    now = datetime.now(tz)
    if start_dt < now:
        raise EventValidationError(
            f"The event start time ({start_dt.strftime('%b %d, %Y %I:%M %p')}) "
            f"is in the past. Please provide a future date and time."
        )

    # --- Validate reminder ---
    if reminder_minutes is not None:
        if isinstance(reminder_minutes, (list, tuple)):
            for m in reminder_minutes:
                if not isinstance(m, int) or m < 0:
                    raise EventValidationError(
                        "Reminder times must be non-negative numbers of minutes."
                    )
                if m > 40320:  # 28 days
                    raise EventValidationError("Reminder time cannot be more than 28 days before the event.")
        elif isinstance(reminder_minutes, int):
            if reminder_minutes < 0:
                raise EventValidationError(
                    "Reminder time must be a non-negative number of minutes."
                )
            if reminder_minutes > 40320:  # 28 days
                raise EventValidationError("Reminder time cannot be more than 28 days before the event.")
        else:
            raise EventValidationError("Invalid reminder format.")

    return ValidatedEvent(
        title=title,
        description=description,
        start_datetime=start_dt,
        end_datetime=end_dt,
        timezone=timezone_name,
        reminder_minutes=reminder_minutes,
    )


def format_event_summary(event: ValidatedEvent) -> str:
    """Format a validated event into a clean, human-friendly summary."""
    date_str = event.start_datetime.strftime("%A, %B %d, %Y")
    start_str = event.start_datetime.strftime("%I:%M %p").lstrip("0")

    if event.end_datetime and event.end_datetime != event.start_datetime:
        end_str = event.end_datetime.strftime("%I:%M %p").lstrip("0")
        time_line = f"⏰ {_escape_md(start_str)} ➡️ {_escape_md(end_str)}"
    else:
        time_line = f"⏰ {_escape_md(start_str)}"

    lines = [
        f"📌 *{_escape_md(event.title)}*",
        f"🗓️ {_escape_md(date_str)}",
        time_line,
    ]

    if event.description:
        lines.append(f"📝 {_escape_md(event.description)}")

    reminder_list = event.get_reminder_minutes_list()
    if len(reminder_list) == 1:
        lines.append(f"🔔 Reminder: {_escape_md(_format_reminder_time(reminder_list[0]))}")
    elif len(reminder_list) > 1:
        labels = [_format_reminder_time(m) for m in reminder_list]
        lines.append(f"🔔 Reminders: {_escape_md(', '.join(labels))}")

    return "\n".join(lines)


def _format_reminder_time(minutes: int) -> str:
    """Human-readable reminder time."""
    if minutes == 0:
        return "at start"
    elif minutes < 60:
        return f"{minutes} minutes before"
    elif minutes == 60:
        return "1 hour before"
    elif minutes < 1440:
        hours = minutes / 60
        if hours == int(hours):
            return f"{int(hours)} hours before"
        return f"{hours:.1f} hours before"
    elif minutes == 1440:
        return "1 day before"
    else:
        days = minutes / 1440
        if days == int(days):
            return f"{int(days)} days before"
        return f"{days:.1f} days before"


def _escape_md(text: str) -> str:
    """Escape special MarkdownV2 characters."""
    special_chars = r"_*[]()~`>#+-=|{}.!"
    result = []
    for char in text:
        if char in special_chars:
            result.append(f"\\{char}")
        else:
            result.append(char)
    return "".join(result)
