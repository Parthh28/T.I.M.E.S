"""
Google Calendar service module.

Handles OAuth 2.0 authentication and Calendar API operations:
- Creating events
- Listing upcoming events
- Listing today's events
"""

import os
import logging
from datetime import datetime, timedelta
from typing import Optional

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from config import (
    GOOGLE_CREDENTIALS_FILE,
    GOOGLE_TOKEN_FILE,
    CALENDAR_SCOPES,
    TIMEZONE,
    TIMEZONE_NAME,
)
from event_parser import ValidatedEvent

logger = logging.getLogger("calendar_bot.calendar")


class CalendarAuthError(Exception):
    """Raised when Google Calendar authentication fails."""
    pass


class CalendarAPIError(Exception):
    """Raised when a Calendar API operation fails."""
    pass


def _get_credentials() -> Credentials:
    """
    Load or refresh OAuth credentials.

    On first run, this will open a browser for Google sign-in.
    The token is saved to GOOGLE_TOKEN_FILE for subsequent runs.

    Raises CalendarAuthError if credentials cannot be obtained.
    """
    creds = None

    if os.path.exists(GOOGLE_TOKEN_FILE):
        try:
            creds = Credentials.from_authorized_user_file(
                GOOGLE_TOKEN_FILE, CALENDAR_SCOPES
            )
        except Exception as e:
            logger.warning("Failed to load saved token: %s", e)
            creds = None

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
                logger.info("Successfully refreshed OAuth token.")
            except Exception as e:
                logger.warning("Token refresh failed: %s. Re-authorizing.", e)
                creds = None

        if not creds or not creds.valid:
            if not os.path.exists(GOOGLE_CREDENTIALS_FILE):
                raise CalendarAuthError(
                    f"Google OAuth credentials file '{GOOGLE_CREDENTIALS_FILE}' not found.\n"
                    "Download it from Google Cloud Console > APIs & Services > Credentials.\n"
                    "See README.md for detailed instructions."
                )
            try:
                flow = InstalledAppFlow.from_client_secrets_file(
                    GOOGLE_CREDENTIALS_FILE, CALENDAR_SCOPES
                )
                print("\n" + "=" * 60)
                print("GOOGLE CALENDAR AUTHORIZATION")
                print("=" * 60)
                print("A browser window will open for Google sign-in.")
                print("Sign in with your Google account and grant calendar access.")
                print("After authorization, you can close the browser window.")
                print("=" * 60 + "\n")
                creds = flow.run_local_server(port=0)
                logger.info("Successfully completed OAuth authorization.")
            except Exception as e:
                raise CalendarAuthError(
                    "Failed to complete Google OAuth authorization. "
                    "Make sure credentials.json is valid and try again."
                ) from e

        # Save the token for future runs
        try:
            with open(GOOGLE_TOKEN_FILE, "w") as token_file:
                token_file.write(creds.to_json())
            logger.info("OAuth token saved to %s", GOOGLE_TOKEN_FILE)
        except IOError as e:
            logger.warning("Could not save OAuth token: %s", e)

    return creds


def _get_service():
    """Build and return a Google Calendar API service instance."""
    creds = _get_credentials()
    return build("calendar", "v3", credentials=creds)


def create_event(event: ValidatedEvent) -> dict:
    """
    Create an event in Google Calendar.

    Args:
        event: A fully validated event.

    Returns:
        The Google Calendar API response dict containing 'id' and 'htmlLink'.

    Raises:
        CalendarAPIError on failure.
    """
    service = _get_service()

    event_body = {
        "summary": event.title,
        "start": {
            "dateTime": event.start_datetime.isoformat(),
            "timeZone": event.timezone,
        },
        "end": {
            "dateTime": event.end_datetime.isoformat(),
            "timeZone": event.timezone,
        },
    }

    if event.description:
        event_body["description"] = event.description

    # Set Google Calendar's built-in popup reminder
    reminder_list = event.get_reminder_minutes_list()
    if reminder_list:
        event_body["reminders"] = {
            "useDefault": False,
            "overrides": [
                {"method": "popup", "minutes": m}
                for m in reminder_list
                if m > 0
            ][:5],
        }

    try:
        created = (
            service.events()
            .insert(calendarId="primary", body=event_body)
            .execute()
        )
        logger.info("Created Google Calendar event: %s", created.get("id"))
        return created
    except HttpError as e:
        logger.error("Calendar API error creating event: %s", e)
        raise CalendarAPIError(
            "Failed to create the event in Google Calendar. "
            "Please check your internet connection and try again."
        ) from e
    except Exception as e:
        logger.error("Unexpected error creating calendar event: %s", e)
        raise CalendarAPIError(
            "An unexpected error occurred while creating the event."
        ) from e


def get_calendar_event(google_event_id: str) -> Optional[dict]:
    """Fetch an event by ID from Google Calendar."""
    service = _get_service()
    try:
        return service.events().get(calendarId="primary", eventId=google_event_id).execute()
    except HttpError as e:
        if getattr(e, "resp", None) and e.resp.status in (404, 410):
            return None
        logger.error("Error retrieving event %s: %s", google_event_id, e)
        raise CalendarAPIError("Failed to fetch event from Google Calendar.") from e
    except Exception as e:
        logger.error("Unexpected error retrieving event %s: %s", google_event_id, e)
        raise CalendarAPIError("An unexpected error occurred while fetching the event.") from e


def delete_calendar_event(google_event_id: str) -> bool:
    """Delete an event from Google Calendar."""
    service = _get_service()
    try:
        service.events().delete(calendarId="primary", eventId=google_event_id).execute()
        logger.info("Deleted Google Calendar event: %s", google_event_id)
        return True
    except HttpError as e:
        if getattr(e, "resp", None) and e.resp.status in (404, 410):
            logger.info("Event %s already deleted or not found.", google_event_id)
            return True
        logger.error("Error deleting event %s: %s", google_event_id, e)
        raise CalendarAPIError("Failed to delete event from Google Calendar.") from e
    except Exception as e:
        logger.error("Unexpected error deleting event %s: %s", google_event_id, e)
        raise CalendarAPIError("An unexpected error occurred while deleting the event.") from e


def update_calendar_event(google_event_id: str, event: ValidatedEvent) -> dict:
    """Update/patch an event in Google Calendar."""
    service = _get_service()
    patch_body = {
        "summary": event.title,
        "start": {
            "dateTime": event.start_datetime.isoformat(),
            "timeZone": event.timezone,
        },
        "end": {
            "dateTime": event.end_datetime.isoformat(),
            "timeZone": event.timezone,
        },
    }
    if event.description:
        patch_body["description"] = event.description

    reminder_list = event.get_reminder_minutes_list()
    if reminder_list:
        patch_body["reminders"] = {
            "useDefault": False,
            "overrides": [
                {"method": "popup", "minutes": m}
                for m in reminder_list
                if m > 0
            ][:5],
        }

    try:
        updated = (
            service.events()
            .patch(calendarId="primary", eventId=google_event_id, body=patch_body)
            .execute()
        )
        logger.info("Updated Google Calendar event: %s", google_event_id)
        return updated
    except HttpError as e:
        logger.error("Error updating event %s: %s", google_event_id, e)
        raise CalendarAPIError("Failed to update event in Google Calendar.") from e
    except Exception as e:
        logger.error("Unexpected error updating event %s: %s", google_event_id, e)
        raise CalendarAPIError("An unexpected error occurred while updating the event.") from e


def search_upcoming_events(query: str, max_results: int = 10, weeks_ahead: int = 4) -> list[dict]:
    """Search upcoming non-birthday events matching a query string."""
    service = _get_service()
    now = datetime.now(TIMEZONE)
    end_window = now + timedelta(weeks=weeks_ahead)

    try:
        events_result = (
            service.events()
            .list(
                calendarId="primary",
                q=query,
                timeMin=now.isoformat(),
                timeMax=end_window.isoformat(),
                maxResults=max_results,
                singleEvents=True,
                orderBy="startTime",
            )
            .execute()
        )
        items = events_result.get("items", [])
        return [e for e in items if not _is_birthday_event(e)]
    except HttpError as e:
        logger.error("Calendar API error searching events: %s", e)
        raise CalendarAPIError("Failed to search Google Calendar events.") from e
    except Exception as e:
        logger.error("Unexpected error searching events: %s", e)
        raise CalendarAPIError("An unexpected error occurred while searching events.") from e


def check_conflicts(
    start_dt: datetime,
    end_dt: datetime,
) -> list[dict]:
    """
    Check for scheduling conflicts using the Google Calendar freebusy API.

    Args:
        start_dt: Proposed event start (timezone-aware datetime).
        end_dt:   Proposed event end   (timezone-aware datetime).

    Returns:
        A list of dicts with keys ``start`` and ``end`` (ISO strings)
        representing busy periods that overlap with the proposed window.
        Empty list means no conflicts.

    Raises:
        CalendarAPIError on failure.
    """
    service = _get_service()
    body = {
        "timeMin": start_dt.isoformat(),
        "timeMax": end_dt.isoformat(),
        "timeZone": TIMEZONE_NAME,
        "items": [{"id": "primary"}],
    }
    try:
        result = service.freebusy().query(body=body).execute()
        busy_periods = result.get("calendars", {}).get("primary", {}).get("busy", [])
        logger.info(
            "Freebusy check %s – %s: %d conflicts found.",
            start_dt.isoformat(), end_dt.isoformat(), len(busy_periods),
        )
        return busy_periods
    except HttpError as e:
        logger.error("Calendar API freebusy error: %s", e)
        raise CalendarAPIError(
            "Failed to check for scheduling conflicts."
        ) from e
    except Exception as e:
        logger.error("Unexpected error during freebusy check: %s", e)
        raise CalendarAPIError(
            "An unexpected error occurred while checking conflicts."
        ) from e


def get_conflicting_event_details(
    start_dt: datetime,
    end_dt: datetime,
) -> list[dict]:
    """
    Return full event objects that overlap with the proposed time window.

    Uses ``events().list()`` instead of freebusy so we can show event titles.
    """
    service = _get_service()
    try:
        result = (
            service.events()
            .list(
                calendarId="primary",
                timeMin=start_dt.isoformat(),
                timeMax=end_dt.isoformat(),
                singleEvents=True,
                orderBy="startTime",
                maxResults=5,
                timeZone=TIMEZONE_NAME,
            )
            .execute()
        )
        items = result.get("items", [])
        return [e for e in items if not _is_birthday_event(e)]
    except HttpError as e:
        logger.error("Error fetching conflicting events: %s", e)
        return []
    except Exception as e:
        logger.error("Unexpected error fetching conflicting events: %s", e)
        return []


def suggest_alternative_time(
    start_dt: datetime,
    end_dt: datetime,
    buffer_minutes: int = 15,
    search_window_hours: int = 8,
) -> Optional[datetime]:
    """
    Suggest the nearest available start time after any conflicts.

    Scans forward from ``start_dt`` in 15-minute increments for up to
    ``search_window_hours`` looking for a free slot of the same duration.

    Returns:
        A timezone-aware datetime for the suggested start, or None if
        no free slot was found within the search window.
    """
    event_duration = end_dt - start_dt
    search_end = start_dt + timedelta(hours=search_window_hours)

    try:
        conflicts = check_conflicts(start_dt, search_end)
    except CalendarAPIError:
        return None

    # Parse and sort all busy intervals in the search window
    busy_intervals = []
    for b in conflicts:
        b_start = datetime.fromisoformat(b["start"].replace("Z", "+00:00"))
        b_end = datetime.fromisoformat(b["end"].replace("Z", "+00:00"))
        if b_start.tzinfo is None:
            b_start = TIMEZONE.localize(b_start)
        else:
            b_start = b_start.astimezone(TIMEZONE)
        if b_end.tzinfo is None:
            b_end = TIMEZONE.localize(b_end)
        else:
            b_end = b_end.astimezone(TIMEZONE)
        busy_intervals.append((b_start, b_end))

    busy_intervals.sort(key=lambda x: x[0])

    candidate = start_dt + timedelta(minutes=buffer_minutes)

    # Round up to the nearest 15 minutes for a clean suggestion
    minute = candidate.minute
    remainder = minute % 15
    if remainder:
        candidate += timedelta(minutes=15 - remainder)
        candidate = candidate.replace(second=0, microsecond=0)

    while candidate + event_duration <= search_end:
        candidate_end = candidate + event_duration

        # Find any overlapping busy intervals
        overlapping = [
            (b_start, b_end) for (b_start, b_end) in busy_intervals
            if candidate < b_end and candidate_end > b_start
        ]

        if not overlapping:
            return candidate

        # Jump past the latest overlapping busy period
        latest_end = max(b_end for (_, b_end) in overlapping)
        candidate = latest_end + timedelta(minutes=buffer_minutes)
        # Round up
        minute = candidate.minute
        remainder = minute % 15
        if remainder:
            candidate += timedelta(minutes=15 - remainder)
            candidate = candidate.replace(second=0, microsecond=0)

    return None


def _is_birthday_event(event: dict) -> bool:
    """Check if an event is a birthday event to exclude it from event listings."""
    if event.get("eventType") == "birthday":
        return True
    summary = (event.get("summary") or "").lower()
    for kw in ("birthday", "b'day", "bday"):
        if kw in summary:
            return True
    return False


def list_upcoming_events(max_results: int = 10, weeks_ahead: int = 3) -> list[dict]:
    """
    List upcoming events from the primary Google Calendar.

    Filters:
    - Only events within the next `weeks_ahead` weeks (default 3 weeks).
    - Excludes birthday events.

    Returns a list of event dicts, each with 'summary', 'start', 'end', 'id'.
    """
    service = _get_service()
    now_dt = datetime.now(TIMEZONE)
    time_min = now_dt.isoformat()
    time_max = (now_dt + timedelta(weeks=weeks_ahead)).isoformat()

    try:
        # Request more items to account for birthdays being filtered out
        fetch_limit = min(max_results * 3, 50)
        result = (
            service.events()
            .list(
                calendarId="primary",
                timeMin=time_min,
                timeMax=time_max,
                maxResults=fetch_limit,
                singleEvents=True,
                orderBy="startTime",
                timeZone=TIMEZONE_NAME,
            )
            .execute()
        )
        raw_events = result.get("items", [])
        # Filter out birthdays and take up to max_results
        events = [e for e in raw_events if not _is_birthday_event(e)][:max_results]
        logger.info(
            "Retrieved %d upcoming non-birthday events within next %d weeks.",
            len(events),
            weeks_ahead,
        )
        return events
    except HttpError as e:
        logger.error("Calendar API error listing events: %s", e)
        raise CalendarAPIError(
            "Failed to retrieve upcoming events from Google Calendar."
        ) from e
    except Exception as e:
        logger.error("Unexpected error listing events: %s", e)
        raise CalendarAPIError(
            "An unexpected error occurred while listing events."
        ) from e


def list_today_events() -> list[dict]:
    """
    List today's events from the primary Google Calendar.

    Returns a list of non-birthday event dicts for today in the configured timezone.
    """
    service = _get_service()
    now = datetime.now(TIMEZONE)
    start_of_day = now.replace(hour=0, minute=0, second=0, microsecond=0)
    end_of_day = start_of_day + timedelta(days=1)

    try:
        result = (
            service.events()
            .list(
                calendarId="primary",
                timeMin=start_of_day.isoformat(),
                timeMax=end_of_day.isoformat(),
                singleEvents=True,
                orderBy="startTime",
                timeZone=TIMEZONE_NAME,
            )
            .execute()
        )
        raw_events = result.get("items", [])
        events = [e for e in raw_events if not _is_birthday_event(e)]
        logger.info("Retrieved %d non-birthday events for today.", len(events))
        return events
    except HttpError as e:
        logger.error("Calendar API error listing today's events: %s", e)
        raise CalendarAPIError(
            "Failed to retrieve today's events from Google Calendar."
        ) from e
    except Exception as e:
        logger.error("Unexpected error listing today's events: %s", e)
        raise CalendarAPIError(
            "An unexpected error occurred while listing today's events."
        ) from e


def format_calendar_event(event: dict) -> str:
    """Format a Google Calendar event dict into a readable string."""
    summary = event.get("summary", "Untitled Event")
    start_raw = event.get("start", {})
    end_raw = event.get("end", {})

    # Handle all-day vs. timed events
    if "dateTime" in start_raw:
        start_dt = datetime.fromisoformat(start_raw["dateTime"])
        end_dt = datetime.fromisoformat(end_raw.get("dateTime", start_raw["dateTime"]))
        date_str = start_dt.strftime("%b %d, %Y")
        time_str = f"{start_dt.strftime('%I:%M %p')} ➡️ {end_dt.strftime('%I:%M %p')}"
    elif "date" in start_raw:
        date_str = start_raw["date"]
        time_str = "All day"
    else:
        date_str = "Unknown date"
        time_str = ""

    lines = [f"📌 *{_escape_md(summary)}*"]
    lines.append(f"   📆 {_escape_md(date_str)}")
    if time_str:
        lines.append(f"   🕐 {_escape_md(time_str)}")

    description = event.get("description", "")
    if description:
        short_desc = description[:100] + ("…" if len(description) > 100 else "")
        lines.append(f"   📝 {_escape_md(short_desc)}")

    return "\n".join(lines)


def _escape_md(text: str) -> str:
    """Escape MarkdownV2 special characters."""
    special_chars = r"_*[]()~`>#+-=|{}.!"
    result = []
    for char in text:
        if char in special_chars:
            result.append(f"\\{char}")
        else:
            result.append(char)
    return "".join(result)


def check_auth_status() -> bool:
    """Check if Google Calendar is authorized without triggering OAuth flow."""
    if not os.path.exists(GOOGLE_TOKEN_FILE):
        return False
    try:
        creds = Credentials.from_authorized_user_file(
            GOOGLE_TOKEN_FILE, CALENDAR_SCOPES
        )
        if creds and creds.valid:
            return True
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
            return True
    except Exception:
        return False
    return False


if __name__ == "__main__":
    print("Checking Google Calendar authorization...")
    if check_auth_status():
        print("Google Calendar is already connected and token is valid!")
    else:
        print("Starting OAuth flow. A browser window will open...")
        creds = _get_credentials()
        print("Successfully authorized and saved token to", GOOGLE_TOKEN_FILE)

