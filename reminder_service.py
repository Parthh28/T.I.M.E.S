"""
Reminder service module for the Telegram Calendar Assistant.

Manages scheduling and sending Telegram reminders using APScheduler.
Persists reminders in SQLite and reconciles on startup.

OVERDUE POLICY:
- On startup, any pending reminder whose scheduled time has already passed
  is marked as 'overdue' in the database.
- If the event has NOT yet started, the reminder is sent immediately with
  a note that it is late.
- If the event has ALREADY started or ended, the reminder is marked overdue
  and NOT sent.
- Duplicate reminders are prevented by checking the DB before sending.

IMPORTANT: Reminders require the bot process to be continuously running.
If the bot is stopped, reminders scheduled during downtime will be
reconciled when the bot restarts according to the policy above.
"""

import logging
from datetime import datetime, timedelta
from typing import Optional

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from config import TIMEZONE, TIMEZONE_NAME, DEFAULT_REMINDER_TIERS
from database import (
    create_reminder,
    get_pending_reminders,
    get_pending_reminders_for_user,
    get_pending_reminder_by_google_id,
    get_pending_reminders_by_google_id,
    get_event_by_google_id,
    save_event,
    mark_reminder_sent,
    mark_reminder_overdue,
    mark_reminder_failed,
    create_task_reminder,
    cancel_reminders_by_notion_id,
)

logger = logging.getLogger("calendar_bot.reminder")

# Global scheduler instance
_scheduler: Optional[AsyncIOScheduler] = None

# Reference to the Telegram bot for sending messages
_bot = None


def init_scheduler() -> AsyncIOScheduler:
    """Initialize and return the APScheduler instance."""
    global _scheduler
    if _scheduler is None:
        _scheduler = AsyncIOScheduler(timezone=TIMEZONE)
    return _scheduler


def get_scheduler() -> AsyncIOScheduler:
    """Get the scheduler instance."""
    if _scheduler is None:
        return init_scheduler()
    return _scheduler


def set_bot(bot) -> None:
    """Store a reference to the Telegram bot for sending messages."""
    global _bot
    _bot = bot


async def _send_reminder(reminder_id: str, telegram_user_id: int,
                          event_title: str, event_start_time: str,
                          reminder_minutes: int, is_late: bool = False) -> None:
    """
    Send a single reminder as a Telegram message.

    This is the callback executed by APScheduler.
    """
    if _bot is None:
        logger.error("Bot reference not set. Cannot send reminder %s.", reminder_id)
        mark_reminder_failed(reminder_id)
        return

    # Prevent duplicate sends
    was_updated = mark_reminder_sent(reminder_id)
    if not was_updated:
        logger.info("Reminder %s already sent or cancelled. Skipping.", reminder_id)
        return

    try:
        start_dt = datetime.fromisoformat(event_start_time)
        time_str = start_dt.strftime("%I:%M %p").lstrip("0")

        if is_late:
            message = (
                f"⏰ *Just caught up:* *{_escape_md(event_title)}* was scheduled for {_escape_md(time_str)} today\\."
            )
        elif reminder_minutes == 0:
            message = (
                f"⏰ *Starting now:* *{_escape_md(event_title)}* is beginning\\!"
            )
        elif reminder_minutes == 1440:
            message = (
                f"🔔 *Tomorrow:* *{_escape_md(event_title)}* at {_escape_md(time_str)}\\."
            )
        elif reminder_minutes == 60:
            message = (
                f"🔔 *In 1 hour:* *{_escape_md(event_title)}* at {_escape_md(time_str)}\\."
            )
        elif reminder_minutes == 15:
            message = (
                f"🔔 *In 15 minutes:* *{_escape_md(event_title)}* at {_escape_md(time_str)}\\."
            )
        else:
            from event_parser import _format_reminder_time
            reminder_label = _format_reminder_time(reminder_minutes)
            message = (
                f"🔔 *Reminder \\({_escape_md(reminder_label)}\\):* *{_escape_md(event_title)}* at {_escape_md(time_str)}\\."
            )

        # Trigger computer speaker speech only if enabled
        if reminder_minutes == 0:
            try:
                from tts_service import speak_event_start
                await speak_event_start(event_title)
            except Exception as tts_err:
                logger.debug("TTS speaker alert skipped/failed: %s", tts_err)

        await _bot.send_message(
            chat_id=telegram_user_id,
            text=message,
            parse_mode="MarkdownV2",
        )
        logger.info("Sent reminder %s to user %d.", reminder_id, telegram_user_id)

    except Exception as e:
        logger.error("Failed to send reminder %s: %s", reminder_id, e)
        # Mark as failed so it can be retried or investigated
        mark_reminder_failed(reminder_id)


def schedule_reminder(
    event_id: str,
    google_event_id: str,
    telegram_user_id: int,
    event_title: str,
    event_start_time: datetime,
    reminder_minutes: int,
) -> Optional[str]:
    """
    Schedule a Telegram reminder for an event.

    Creates a DB record and schedules an APScheduler job.
    Returns the reminder ID, or None if the reminder time is in the past.
    """
    reminder_time = event_start_time - timedelta(minutes=reminder_minutes)
    now = datetime.now(TIMEZONE)

    event_start_str = event_start_time.isoformat()
    reminder_time_str = reminder_time.isoformat()

    # Create the DB record
    reminder_id = create_reminder(
        event_id=event_id,
        google_event_id=google_event_id,
        telegram_user_id=telegram_user_id,
        event_title=event_title,
        event_start_time=event_start_str,
        reminder_time=reminder_time_str,
        reminder_minutes=reminder_minutes,
    )

    scheduler = get_scheduler()

    if reminder_time <= now:
        # Reminder time already passed
        if event_start_time > now:
            # Event hasn't started yet — send immediately as late reminder
            scheduler.add_job(
                _send_reminder,
                trigger="date",
                run_date=now + timedelta(seconds=2),
                args=[reminder_id, telegram_user_id, event_title,
                      event_start_str, reminder_minutes, True],
                id=f"reminder_{reminder_id}",
                replace_existing=True,
            )
            logger.info("Scheduled late reminder %s for immediate send.", reminder_id)
        else:
            # Event already passed — mark as overdue
            mark_reminder_overdue(reminder_id)
            logger.info("Reminder %s is overdue (event already passed).", reminder_id)
            return None
    else:
        # Schedule for the correct time
        scheduler.add_job(
            _send_reminder,
            trigger="date",
            run_date=reminder_time,
            args=[reminder_id, telegram_user_id, event_title,
                  event_start_str, reminder_minutes, False],
            id=f"reminder_{reminder_id}",
            replace_existing=True,
        )
        logger.info("Scheduled reminder %s at %s.", reminder_id, reminder_time_str)

    return reminder_id


def schedule_reminders_for_event(
    event_id: str,
    google_event_id: str,
    telegram_user_id: int,
    event_title: str,
    event_start_time: datetime,
    reminder_minutes_list: list[int],
) -> list[str]:
    """
    Schedule multiple Telegram reminders for an event (e.g. 1 day, 1 hour, 15m before).
    Only schedules future reminders.
    If all tiers are in the past and the event is still upcoming, schedules one immediate reminder.
    Returns list of scheduled reminder IDs.
    """
    now = datetime.now(TIMEZONE)
    if event_start_time.tzinfo is None:
        event_start_time = TIMEZONE.localize(event_start_time)

    if event_start_time <= now:
        return []

    scheduled_ids = []
    unique_tiers = sorted(
        list({int(m) for m in reminder_minutes_list if isinstance(m, (int, float)) and m >= 0}),
        reverse=True,
    )
    if 0 not in unique_tiers:
        unique_tiers.append(0)
        unique_tiers.sort(reverse=True)

    for m in unique_tiers:
        rem_time = event_start_time - timedelta(minutes=m)
        if rem_time > now:
            rem_id = schedule_reminder(
                event_id=event_id,
                google_event_id=google_event_id,
                telegram_user_id=telegram_user_id,
                event_title=event_title,
                event_start_time=event_start_time,
                reminder_minutes=m,
            )
            if rem_id:
                scheduled_ids.append(rem_id)

    # Fallback if event starts in less than the smallest tier
    if not scheduled_ids and unique_tiers:
        mins_until_start = int((event_start_time - now).total_seconds() / 60)
        if mins_until_start > 0:
            short_min = max(mins_until_start - 2, 1)
            rem_id = schedule_reminder(
                event_id=event_id,
                google_event_id=google_event_id,
                telegram_user_id=telegram_user_id,
                event_title=event_title,
                event_start_time=event_start_time,
                reminder_minutes=short_min,
            )
            if rem_id:
                scheduled_ids.append(rem_id)

    return scheduled_ids


async def reconcile_reminders_on_startup() -> None:
    """
    On startup, load all pending reminders from DB and re-schedule them.

    Overdue policy:
    - If reminder_time passed but event hasn't started → send immediately (late).
    - If event already started/passed → mark overdue, don't send.
    """
    pending = get_pending_reminders()
    now = datetime.now(TIMEZONE)
    scheduler = get_scheduler()

    reconciled = 0
    overdue = 0
    rescheduled = 0

    for r in pending:
        reminder_id = r["id"]
        reminder_time = datetime.fromisoformat(r["reminder_time"])
        event_start = datetime.fromisoformat(r["event_start_time"])
        telegram_user_id = r["telegram_user_id"]
        event_title = r["event_title"]
        reminder_minutes = r["reminder_minutes"]

        # Ensure timezone-aware
        if reminder_time.tzinfo is None:
            reminder_time = TIMEZONE.localize(reminder_time)
        if event_start.tzinfo is None:
            event_start = TIMEZONE.localize(event_start)

        if event_start <= now:
            # Event already passed — mark overdue
            mark_reminder_overdue(reminder_id)
            overdue += 1
        elif reminder_time <= now:
            # Reminder time passed but event is still upcoming — send late
            scheduler.add_job(
                _send_reminder,
                trigger="date",
                run_date=now + timedelta(seconds=2 + reconciled),
                args=[reminder_id, telegram_user_id, event_title,
                      r["event_start_time"], reminder_minutes, True],
                id=f"reminder_{reminder_id}",
                replace_existing=True,
            )
            rescheduled += 1
        else:
            # Future reminder — reschedule normally
            scheduler.add_job(
                _send_reminder,
                trigger="date",
                run_date=reminder_time,
                args=[reminder_id, telegram_user_id, event_title,
                      r["event_start_time"], reminder_minutes, False],
                id=f"reminder_{reminder_id}",
                replace_existing=True,
            )
            rescheduled += 1

        reconciled += 1

    logger.info(
        "Reminder reconciliation complete: %d total, %d rescheduled, %d overdue.",
        reconciled, rescheduled, overdue,
    )


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


def _escape_url(url: Optional[str]) -> str:
    """Escape special characters inside the URL part of a MarkdownV2 inline link [text](url)."""
    if not url:
        return ""
    return str(url).replace("\\", "\\\\").replace(")", "\\)")


def format_time_remaining(target_dt: datetime, now_dt: datetime) -> str:
    """Format time difference into human-friendly relative string."""
    diff = target_dt - now_dt
    total_seconds = int(diff.total_seconds())
    if total_seconds <= 60:
        return "due now"

    days = total_seconds // 86400
    hours = (total_seconds % 86400) // 3600
    minutes = (total_seconds % 3600) // 60

    parts = []
    if days > 0:
        parts.append(f"{days} day{'s' if days != 1 else ''}")
    if hours > 0 and days < 3:
        parts.append(f"{hours} hr{'s' if hours != 1 else ''}")
    if days == 0 and minutes > 0:
        parts.append(f"{minutes} min")

    return "in " + ", ".join(parts) if parts else "in < 1 min"


def ensure_reminder_for_upcoming_event(
    google_event_id: str,
    title: str,
    start_dt: datetime,
    telegram_user_id: int,
    default_minutes: Optional[int] = None,
    tiers: Optional[list[int]] = None,
) -> Optional[dict]:
    """
    Ensure pending reminders exist for an upcoming Google Calendar event across all tiers
    (default: 1 day before, 1 hour before, 15 minutes before).
    """
    if tiers is None:
        if default_minutes is not None:
            tiers = [default_minutes]
        else:
            tiers = DEFAULT_REMINDER_TIERS

    now = datetime.now(TIMEZONE)
    if start_dt.tzinfo is None:
        start_dt = TIMEZONE.localize(start_dt)
    if start_dt <= now:
        return None

    # Check local events table
    local_event = get_event_by_google_id(google_event_id)
    if local_event:
        local_event_id = local_event["id"]
    else:
        local_event_id = save_event(
            google_event_id=google_event_id,
            title=title,
            start_time=start_dt.isoformat(),
            end_time=(start_dt + timedelta(hours=1)).isoformat(),
            telegram_user_id=telegram_user_id,
            timezone=TIMEZONE_NAME,
        )

    existing_reminders = get_pending_reminders_by_google_id(google_event_id)
    existing_minutes = {r["reminder_minutes"] for r in existing_reminders}

    mins_until_start = int((start_dt - now).total_seconds() / 60)

    for m in sorted(tiers, reverse=True):
        if m in existing_minutes:
            continue
        rem_time = start_dt - timedelta(minutes=m)
        if rem_time > now:
            schedule_reminder(
                event_id=local_event_id,
                google_event_id=google_event_id,
                telegram_user_id=telegram_user_id,
                event_title=title,
                event_start_time=start_dt,
                reminder_minutes=m,
            )

    # Fallback if no reminders exist at all and event is upcoming
    current_pending = get_pending_reminders_by_google_id(google_event_id)
    if not current_pending and mins_until_start > 0:
        actual_min = max(mins_until_start - 2, 1)
        schedule_reminder(
            event_id=local_event_id,
            google_event_id=google_event_id,
            telegram_user_id=telegram_user_id,
            event_title=title,
            event_start_time=start_dt,
            reminder_minutes=actual_min,
        )
        current_pending = get_pending_reminders_by_google_id(google_event_id)

    return current_pending[0] if current_pending else None


def get_upcoming_reminders_summary(
    telegram_user_id: int, query_title: Optional[str] = None
) -> str:
    """
    Get human-readable summary of when Telegram reminders will be sent.
    Ensures upcoming calendar events have reminders scheduled across tiers (1d, 1h, 15m).
    """
    from calendar_service import list_upcoming_events
    from event_parser import _format_reminder_time

    now = datetime.now(TIMEZONE)

    try:
        upcoming_events = list_upcoming_events(max_results=10)
    except Exception as e:
        logger.error("Failed to fetch upcoming events for reminder check: %s", e)
        upcoming_events = []

    # Auto-ensure reminders for all upcoming events
    for ev in upcoming_events:
        summary = ev.get("summary", "Untitled Event")
        start_info = ev.get("start", {})
        start_str = start_info.get("dateTime") or start_info.get("date")
        if not start_str or not ev.get("id"):
            continue

        try:
            if "T" in start_str:
                start_dt = datetime.fromisoformat(start_str)
                if start_dt.tzinfo is None:
                    start_dt = TIMEZONE.localize(start_dt)
            else:
                d = datetime.strptime(start_str, "%Y-%m-%d").date()
                start_dt = TIMEZONE.localize(datetime(d.year, d.month, d.day, 9, 0))

            ensure_reminder_for_upcoming_event(
                google_event_id=ev["id"],
                title=summary,
                start_dt=start_dt,
                telegram_user_id=telegram_user_id,
                tiers=DEFAULT_REMINDER_TIERS,
            )
        except Exception as e:
            logger.warning("Could not process start time for event %s: %s", summary, e)

    # Retrieve all pending reminders for this user
    reminders = get_pending_reminders_for_user(telegram_user_id)

    # Filter to only future or active reminders
    valid_reminders = []
    for r in reminders:
        try:
            rem_dt = datetime.fromisoformat(r["reminder_time"])
            if rem_dt.tzinfo is None:
                rem_dt = TIMEZONE.localize(rem_dt)
            event_dt = datetime.fromisoformat(r["event_start_time"])
            if event_dt.tzinfo is None:
                event_dt = TIMEZONE.localize(event_dt)
            if event_dt > now:
                valid_reminders.append((r, rem_dt, event_dt))
        except Exception:
            continue

    # If specific title query was requested
    if query_title and query_title.strip():
        q = query_title.strip().lower()
        matched = [item for item in valid_reminders if q in item[0]["event_title"].lower()]
        if matched:
            matched.sort(key=lambda x: x[1])
            title = matched[0][0]["event_title"]
            event_dt = matched[0][2]
            event_time_str = event_dt.strftime("%A, %b %d, %Y at %I:%M %p")

            msg_lines = [
                "⏰ *Telegram Reminder Details*\n",
                f"📌 *{_escape_md(title)}*",
                f"📆 Event starts: *{_escape_md(event_time_str)}*\n",
                "🔔 Scheduled Telegram notifications:",
            ]
            for r, rem_dt, _ in matched:
                offset_label = _format_reminder_time(r["reminder_minutes"])
                countdown = format_time_remaining(rem_dt, now)
                date_time_str = rem_dt.strftime("%A, %b %d at %I:%M %p")
                msg_lines.append(
                    f"👉 *{_escape_md(date_time_str)}*\n"
                    f"   ⏳ _{_escape_md(countdown)} \\({_escape_md(offset_label)}\\)_"
                )
            return "\n".join(msg_lines)

    if not valid_reminders:
        return "📭 No upcoming Telegram reminders found for the next 3 weeks\\."

    # Group reminders by event
    events_map = {}
    for r, rem_dt, event_dt in sorted(valid_reminders, key=lambda x: (x[2], x[1])):
        key = r["google_event_id"] or r["event_title"]
        if key not in events_map:
            events_map[key] = {
                "title": r["event_title"],
                "event_dt": event_dt,
                "reminders": [],
            }
        events_map[key]["reminders"].append((r, rem_dt))

    lines = [
        f"⏰ *Your Upcoming Telegram Reminders* \\({_escape_md(str(len(valid_reminders)))} reminders scheduled\\):\n"
    ]
    for ev_data in events_map.values():
        title = ev_data["title"]
        ev_str = ev_data["event_dt"].strftime("%A, %b %d at %I:%M %p")
        item_parts = [
            f"📌 *{_escape_md(title)}*",
            f"   📆 Event: {_escape_md(ev_str)}",
        ]
        for r, rem_dt in ev_data["reminders"]:
            offset_label = _format_reminder_time(r["reminder_minutes"])
            countdown = format_time_remaining(rem_dt, now)
            rem_str = rem_dt.strftime("%b %d, %I:%M %p")
            item_parts.append(
                f"   🔔 Notification: *{_escape_md(rem_str)}* \\({_escape_md(offset_label)}, _{_escape_md(countdown)}_\\)"
            )
        lines.append("\n".join(item_parts))

    lines.append("\n_All notifications will buzz your Telegram right on schedule\\!_ 🚀")
    return "\n\n".join(lines)


def cancel_reminders_for_google_event(google_event_id: str) -> int:
    """Cancel all pending reminders for a Google Calendar event in DB and scheduler."""
    from database import cancel_reminders_by_google_id
    cancelled_ids = cancel_reminders_by_google_id(google_event_id)
    scheduler = get_scheduler()
    for rem_id in cancelled_ids:
        job_id = f"reminder_{rem_id}"
        try:
            if scheduler.get_job(job_id):
                scheduler.remove_job(job_id)
                logger.info("Removed scheduler job %s", job_id)
        except Exception as e:
            logger.warning("Could not remove scheduler job %s: %s", job_id, e)
    return len(cancelled_ids)


def build_daily_briefing_message() -> str:
    """Generate a clean, inspiring morning briefing of today's schedule."""
    from calendar_service import list_today_events, list_upcoming_events
    now = datetime.now(TIMEZONE)
    today_str = now.strftime("%A, %B %d, %Y")

    try:
        events = list_today_events()
    except Exception as e:
        logger.error("Failed to fetch today's events for daily briefing: %s", e)
        events = []

    lines = [
        "🌅 *Good Morning\\! Here's Your Daily Briefing*\n",
        f"📆 *{_escape_md(today_str)}*",
    ]

    if not events:
        lines.append(
            "\n✨ *You have a completely free day today\\!*\n"
            "No scheduled meetings or events\\. Enjoy your day or focus on deep work\\! 🎯"
        )
    else:
        count = len(events)
        event_word = "event" if count == 1 else "events"
        lines.append(f"\n📋 You have *{count} {event_word}* scheduled for today:\n")

        total_meeting_minutes = 0
        for ev in events:
            summary = ev.get("summary", "Untitled Event")
            start_raw = ev.get("start", {})
            end_raw = ev.get("end", {})
            location = ev.get("location", "")
            hangout_link = ev.get("hangoutLink") or ev.get("htmlLink")

            if "dateTime" in start_raw:
                start_dt = datetime.fromisoformat(start_raw["dateTime"])
                end_dt = datetime.fromisoformat(end_raw.get("dateTime", start_raw["dateTime"]))
                time_str = f"{start_dt.strftime('%I:%M %p')} – {end_dt.strftime('%I:%M %p')}"
                dur_mins = int((end_dt - start_dt).total_seconds() / 60)
                if dur_mins > 0:
                    total_meeting_minutes += dur_mins
            elif "date" in start_raw:
                time_str = "All day"
            else:
                time_str = "Scheduled"

            ev_block = [
                f"📌 *{_escape_md(summary)}*",
                f"   🕐 {_escape_md(time_str)}",
            ]
            if location:
                ev_block.append(f"   📍 {_escape_md(location)}")
            if hangout_link:
                ev_block.append(f"   🔗 [Join Meeting]({_escape_url(hangout_link)})")

            lines.append("\n".join(ev_block))

        if total_meeting_minutes > 0:
            hours, mins = divmod(total_meeting_minutes, 60)
            dur_text = f"{hours}h {mins}m" if hours > 0 and mins > 0 else (f"{hours}h" if hours > 0 else f"{mins}m")
            lines.append(f"\n⏱ Total scheduled time: *{_escape_md(dur_text)}*")

    # Add quick lookahead for tomorrow
    try:
        upcoming = list_upcoming_events(max_results=5, weeks_ahead=1)
        tomorrow_date = (now + timedelta(days=1)).date()
        tomorrow_events = []
        for ev in upcoming:
            st = ev.get("start", {}).get("dateTime") or ev.get("start", {}).get("date")
            if st:
                ev_date = datetime.fromisoformat(st).date() if "T" in st else datetime.strptime(st, "%Y-%m-%d").date()
                if ev_date == tomorrow_date:
                    tomorrow_events.append(ev)
        if tomorrow_events:
            lines.append(f"\n🔮 *Tomorrow:* {len(tomorrow_events)} event{'s' if len(tomorrow_events) != 1 else ''} coming up\\.")
    except Exception:
        pass

    lines.append("\n_Have a productive and wonderful day\\!_ 🚀")
    return "\n".join(lines)


async def send_daily_briefing(telegram_user_id: int) -> None:
    """Send the morning briefing message to the user."""
    if _bot is None:
        logger.error("Bot reference not set. Cannot send daily briefing.")
        return

    try:
        message = build_daily_briefing_message()
        await _bot.send_message(
            chat_id=telegram_user_id,
            text=message,
            parse_mode="MarkdownV2",
            disable_web_page_preview=True,
        )
        logger.info("Sent daily briefing to user %d", telegram_user_id)
    except Exception as e:
        logger.error("Failed to send daily briefing to user %d: %s", telegram_user_id, e)


def init_daily_briefing_job(telegram_user_id: int) -> None:
    """Schedule the daily morning briefing cron job in APScheduler."""
    from config import BRIEFING_TIME, BRIEFING_ENABLED
    if not BRIEFING_ENABLED:
        logger.info("Daily briefing is disabled.")
        return

    try:
        hour_str, min_str = BRIEFING_TIME.split(":")
        hour = int(hour_str)
        minute = int(min_str)
    except Exception:
        hour, minute = 8, 0

    scheduler = get_scheduler()
    scheduler.add_job(
        send_daily_briefing,
        trigger="cron",
        hour=hour,
        minute=minute,
        args=[telegram_user_id],
        id="daily_morning_briefing",
        replace_existing=True,
    )
    logger.info("Scheduled daily morning briefing at %02d:%02d (%s)", hour, minute, TIMEZONE_NAME)


# ────────────────────────────────────────────────────────────
# Notion Task Reminders (Shared APScheduler Engine)
# ────────────────────────────────────────────────────────────

async def _send_task_reminder(
    reminder_id: str,
    telegram_user_id: int,
    task_title: str,
    due_datetime_iso: str,
    reminder_minutes: int,
    priority: str = "Medium",
    category: str = "General",
) -> None:
    """Send a task reminder notification to the user via Telegram."""
    if _bot is None:
        logger.error("Bot reference not set. Cannot send task reminder %s.", reminder_id)
        mark_reminder_failed(reminder_id)
        return

    was_updated = mark_reminder_sent(reminder_id)
    if not was_updated:
        return

    try:
        from task_parser import PRIORITY_ICONS
        p_icon = PRIORITY_ICONS.get(priority, "🟡")
        start_dt = datetime.fromisoformat(due_datetime_iso)
        time_str = start_dt.strftime("%I:%M %p").lstrip("0")
        date_str = start_dt.strftime("%A, %B %d")

        if reminder_minutes == 0:
            header = "⚡ *Task Due Now\\!*"
        elif reminder_minutes == 60:
            header = "🔔 *Task Due in 1 Hour:*"
        elif reminder_minutes == 1440:
            header = "📋 *Task Due Tomorrow:*"
        else:
            header = f"📋 *Task Reminder \\({reminder_minutes}m before\\):*"

        msg = (
            f"{header}\n\n"
            f"📌 *{_escape_md(task_title)}*\n"
            f"{p_icon} *Priority:* `{_escape_md(priority)}` \\| `{_escape_md(category)}`\n"
            f"📅 *Deadline:* {_escape_md(date_str)} at {_escape_md(time_str)}"
        )
        await _bot.send_message(
            chat_id=telegram_user_id,
            text=msg,
            parse_mode="MarkdownV2",
        )
        logger.info("Sent task reminder %s for '%s' to user %d", reminder_id, task_title, telegram_user_id)
    except Exception as e:
        logger.error("Failed to send task reminder %s: %s", reminder_id, e)
        mark_reminder_failed(reminder_id)


def schedule_reminders_for_task(
    notion_page_id: str,
    telegram_user_id: int,
    task_title: str,
    due_datetime: datetime,
    reminder_minutes: Optional[int | list[int]] = None,
    priority: str = "Medium",
    category: str = "General",
) -> list[str]:
    """Schedule reminders for a Notion task."""
    scheduler = get_scheduler()
    reminder_ids = []

    # Default tiers: 1 hour before (60) and at due time (0)
    tiers = [60, 0]
    if reminder_minutes is not None:
        if isinstance(reminder_minutes, int):
            tiers = [reminder_minutes]
        elif isinstance(reminder_minutes, list):
            tiers = reminder_minutes

    now = datetime.now(TIMEZONE)
    for m in tiers:
        remind_at = due_datetime - timedelta(minutes=m)
        if remind_at <= now:
            continue
        reminder_id = create_task_reminder(
            notion_page_id=notion_page_id,
            telegram_user_id=telegram_user_id,
            task_title=task_title,
            due_datetime_iso=due_datetime.isoformat(),
            reminder_time_iso=remind_at.isoformat(),
            reminder_minutes=m,
        )
        scheduler.add_job(
            _send_task_reminder,
            "date",
            run_date=remind_at,
            args=[reminder_id, telegram_user_id, task_title, due_datetime.isoformat(), m, priority, category],
            id=f"reminder_task_{reminder_id}",
            replace_existing=True,
        )
        reminder_ids.append(reminder_id)

    return reminder_ids


def cancel_reminders_for_notion_task(notion_page_id: str) -> None:
    """Cancel all scheduled APScheduler jobs and local DB entries for a Notion task."""
    scheduler = get_scheduler()
    cancelled_ids = cancel_reminders_by_notion_id(notion_page_id)
    for rid in cancelled_ids:
        job_id = f"reminder_task_{rid}"
        try:
            scheduler.remove_job(job_id)
        except Exception:
            pass


