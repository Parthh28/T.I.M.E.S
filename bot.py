"""
Main Telegram bot module for the Calendar Assistant.

Handles all Telegram commands, conversation flows, inline buttons,
and natural-language event creation.

Commands:
  /start    - Welcome message and instructions
  /new      - Guided event creation
  /upcoming - Show upcoming Google Calendar events
  /today    - Show today's events
  /cancel   - Cancel the current conversation or pending event
"""

import asyncio
import json
import logging
from datetime import datetime
from typing import Optional

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ConversationHandler,
    ContextTypes,
    filters,
)

from config import (
    TELEGRAM_BOT_TOKEN,
    AUTHORIZED_USER_ID,
    TIMEZONE,
    DEFAULT_REMINDER_OPTIONS,
    DEFAULT_REMINDER_TIERS,
    BRIEFING_TIME,
    BRIEFING_ENABLED,
    logger,
)
from database import (
    init_db,
    create_pending_confirmation,
    get_pending_confirmation,
    resolve_confirmation,
    save_event,
    get_event_by_google_id,
    delete_event_by_google_id,
    update_event_times,
    cancel_reminders_by_google_id,
    save_task,
    get_task_by_notion_id,
    update_task_status,
    delete_task_by_notion_id,
    create_generic_pending_confirmation,
)
from gemini_service import (
    extract_action_from_text,
    extract_event_from_text,
    extract_event_from_url_content,
    generate_schedule_chat_response,
    UnifiedActionRequest,
)
from url_scraper import find_urls, scrape_event_page
from event_parser import (
    validate_and_build_event,
    format_event_summary,
    EventValidationError,
    ValidatedEvent,
    _escape_md,
)
from task_parser import (
    validate_and_build_task,
    format_task_summary,
    format_task_item,
    TaskValidationError,
    ValidatedTask,
    PRIORITY_ICONS,
    CATEGORY_ICONS,
)
from notion_service import (
    create_notion_task,
    list_notion_tasks,
    get_notion_task,
    update_notion_task,
    complete_notion_task,
    delete_notion_task,
    find_task_by_title,
    check_notion_auth,
    NotionConfigError,
    NotionAPIError,
)
from calendar_service import (
    create_event as create_calendar_event,
    delete_calendar_event,
    update_calendar_event,
    search_upcoming_events,
    get_calendar_event,
    check_conflicts,
    get_conflicting_event_details,
    suggest_alternative_time,
    list_upcoming_events,
    list_today_events,
    format_calendar_event,
    CalendarAuthError,
    CalendarAPIError,
    check_auth_status,
)
from reminder_service import (
    init_scheduler,
    set_bot,
    schedule_reminder,
    schedule_reminders_for_event,
    cancel_reminders_for_google_event,
    build_daily_briefing_message,
    init_daily_briefing_job,
    reconcile_reminders_on_startup,
    get_scheduler,
    get_upcoming_reminders_summary,
    schedule_reminders_for_task,
    cancel_reminders_for_notion_task,
)

# Conversation states for guided event creation
(
    STATE_TITLE,
    STATE_DATE,
    STATE_TIME,
    STATE_DURATION,
    STATE_REMINDER,
    STATE_CUSTOM_REMINDER,
) = range(6)


# ────────────────────────────────────────────────────────────
# Authorization decorator
# ────────────────────────────────────────────────────────────

def authorized_only(func):
    """Decorator to restrict handlers to the authorized Telegram user."""
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = None
        if update.effective_user:
            user_id = update.effective_user.id
        if user_id != AUTHORIZED_USER_ID:
            logger.warning("Unauthorized access attempt from user ID: %s", user_id)
            # Silently ignore unauthorized users
            return
        return await func(update, context)
    return wrapper


def _escape_url(url: Optional[str]) -> str:
    """Escape special characters inside the URL part of a MarkdownV2 inline link [text](url).

    According to Telegram Bot API documentation:
    In MarkdownV2, inside the '(url)' part of an inline link, only ')' and '\\' must be escaped.
    """
    if not url:
        return ""
    return str(url).replace("\\", "\\\\").replace(")", "\\)")


async def _safe_edit_message_text(
    query,
    text: str,
    parse_mode: Optional[str] = "MarkdownV2",
    disable_web_page_preview: bool = True,
    **kwargs,
) -> None:
    """Safely edit message text with MarkdownV2.

    If Telegram rejects the formatted Markdown (e.g. syntax / entity parsing error),
    automatically falls back to plain text editing so the user never gets a silent failure.
    """
    try:
        await query.edit_message_text(
            text=text,
            parse_mode=parse_mode,
            disable_web_page_preview=disable_web_page_preview,
            **kwargs,
        )
    except Exception as e:
        logger.warning(
            "Failed to edit message with parse_mode=%s (%s). Falling back to plain text.",
            parse_mode,
            e,
        )
        plain_text = text.replace("\\", "").replace("*", "")
        try:
            await query.edit_message_text(
                text=plain_text,
                parse_mode=None,
                disable_web_page_preview=disable_web_page_preview,
                **kwargs,
            )
        except Exception as fallback_err:
            logger.error("Plain text edit fallback also failed: %s", fallback_err)


async def _safe_send_message(
    bot,
    chat_id: int,
    text: str,
    parse_mode: Optional[str] = "MarkdownV2",
    disable_web_page_preview: bool = True,
    **kwargs,
) -> None:
    """Safely send a message with MarkdownV2, falling back to plain text if parsing fails."""
    try:
        await bot.send_message(
            chat_id=chat_id,
            text=text,
            parse_mode=parse_mode,
            disable_web_page_preview=disable_web_page_preview,
            **kwargs,
        )
    except Exception as e:
        logger.warning(
            "Failed to send message with parse_mode=%s (%s). Falling back to plain text.",
            parse_mode,
            e,
        )
        plain_text = text.replace("\\", "").replace("*", "")
        try:
            await bot.send_message(
                chat_id=chat_id,
                text=plain_text,
                parse_mode=None,
                disable_web_page_preview=disable_web_page_preview,
                **kwargs,
            )
        except Exception as fallback_err:
            logger.error("Plain text send fallback also failed: %s", fallback_err)


# ────────────────────────────────────────────────────────────
# /start command
# ────────────────────────────────────────────────────────────

@authorized_only
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Send a welcome message with instructions."""
    welcome_text = (
        "Hey! 👋 I'm T\\.I\\.M\\.E\\.S, your AI assistant for Google Calendar and Notion\\.\n\n"
        "You can tell me events, tasks, or combined requests naturally:\n"
        "  • _\"Lunch with Rahul tomorrow at 1 PM\"_ \\(Calendar\\)\n"
        "  • _\"Add task submit tax report by Friday priority High\"_ \\(Notion\\)\n"
        "  • _\"Meeting tomorrow at 3 PM and add preparing slides to tasks\"_ \\(Both\\!\\)\n"
        "  • _\"Mark tax report as done\"_ or _\"Show my tasks\"_\n"
        "  • _\"Am I free tonight for gym?\"_ \\(Smart advice\\)\n\n"
        "Quick commands:\n"
        "  /today — Today's schedule\n"
        "  /upcoming — Next 3 weeks\n"
        "  /tasks — Pending Notion tasks\n"
        "  /briefing — Morning agenda\n"
        "  /reminders — Scheduled notification timings\n"
        "  /new — Step\\-by\\-step event setup\n"
        "  /delete — Remove an event"
    )

    auth_status = "Connected" if check_auth_status() else "Not connected"
    welcome_text += f"\n\nGoogle Calendar: *{_escape_md(auth_status)}*"

    await update.message.reply_text(welcome_text, parse_mode="MarkdownV2")


# ────────────────────────────────────────────────────────────
# /reminders command
# ────────────────────────────────────────────────────────────

@authorized_only
async def cmd_briefing(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Send today's daily morning briefing."""
    thinking = await update.message.reply_text("🌅 Generating your daily briefing\\.\\.\\.", parse_mode="MarkdownV2")
    message = build_daily_briefing_message()
    await thinking.edit_text(message, parse_mode="MarkdownV2", disable_web_page_preview=True)


@authorized_only
async def cmd_tasks(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """List pending Notion tasks."""
    thinking = await update.message.reply_text("📋 Fetching your tasks from Notion\\.\\.\\.", parse_mode="MarkdownV2")
    try:
        tasks = await list_notion_tasks(max_results=20)
    except NotionConfigError:
        await thinking.edit_text(
            "⚠️ Notion is not configured yet\\. Please set `NOTION_API_KEY` and `NOTION_DATABASE_ID` in `.env`\\.",
            parse_mode="MarkdownV2",
        )
        return
    except NotionAPIError as e:
        await thinking.edit_text(f"❌ {str(e)}")
        return

    if not tasks:
        await thinking.edit_text("🎉 You have no pending tasks in Notion\\! Great job\\.", parse_mode="MarkdownV2")
        return

    header = "📋 *Your Pending Notion Tasks:*\n\n"
    items = [format_task_item(t, idx + 1) for idx, t in enumerate(tasks)]
    msg = header + "\n\n".join(items)
    await thinking.edit_text(msg, parse_mode="MarkdownV2")


@authorized_only
async def cmd_delete(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /delete command with optional search query or interactive menu."""
    args = context.args
    thinking = await update.message.reply_text("🔍 Searching events\\.\\.\\.", parse_mode="MarkdownV2")
    if args:
        target_title = " ".join(args)
        await _prompt_event_deletion(target_title, thinking, AUTHORIZED_USER_ID)
    else:
        try:
            events = list_upcoming_events(max_results=5)
        except Exception as e:
            await thinking.edit_text(f"❌ {str(e)}")
            return

        if not events:
            await thinking.edit_text("📭 No upcoming events found to delete\\.", parse_mode="MarkdownV2")
            return

        keyboard = []
        for ev in events:
            summary = ev.get("summary", "Untitled Event")
            st = ev.get("start", {}).get("dateTime") or ev.get("start", {}).get("date")
            time_label = ""
            if st and "T" in st:
                time_label = f" ({datetime.fromisoformat(st).strftime('%b %d')})"
            btn_text = f"🗑️ {summary}{time_label}"
            if len(btn_text) > 40:
                btn_text = btn_text[:37] + "…"
            keyboard.append([InlineKeyboardButton(btn_text, callback_data=f"deleteconf_{ev['id']}")])
        keyboard.append([InlineKeyboardButton("❌ Cancel", callback_data="cancel_del_menu")])

        await thinking.edit_text(
            "🗑️ *Select an event to delete:*",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="MarkdownV2",
        )


@authorized_only
async def cmd_reminders(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Display upcoming Telegram reminders and notification timings."""
    msg = get_upcoming_reminders_summary(AUTHORIZED_USER_ID)
    await update.message.reply_text(msg, parse_mode="MarkdownV2")


@authorized_only
async def cmd_testvoice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Send a test voice note to the user's phone via Telegram."""
    from tts_service import send_voice_announcement
    await update.message.reply_text(
        "🎙️ Generating voice note for your phone\\.\\.\\.",
        parse_mode="MarkdownV2",
    )
    phone_success = await send_voice_announcement(context.bot, update.effective_user.id, "Test Event")

    if phone_success:
        await update.message.reply_text(
            "✅ *Voice Note Sent\\!*\n\n"
            "📱 Check the voice message right above to listen on your phone\\!",
            parse_mode="MarkdownV2",
        )
    else:
        await update.message.reply_text(
            "⚠️ Failed to send voice note to your phone\\. Please try again\\.",
            parse_mode="MarkdownV2",
        )


# ────────────────────────────────────────────────────────────
# /upcoming command
# ────────────────────────────────────────────────────────────

@authorized_only
async def cmd_upcoming(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Display upcoming Google Calendar events."""
    try:
        events = list_upcoming_events(max_results=10)
    except CalendarAuthError:
        await update.message.reply_text(
            "❌ Google Calendar is not connected\\. "
            "Please run the bot locally first to complete OAuth setup\\.",
            parse_mode="MarkdownV2",
        )
        return
    except CalendarAPIError as e:
        await update.message.reply_text(f"❌ {str(e)}")
        return

    if not events:
        await update.message.reply_text("Your calendar is completely open for the next 3 weeks\\! 🎉", parse_mode="MarkdownV2")
        return

    header = "📅 *Coming up in the next 3 weeks:*\n\n"
    event_texts = [format_calendar_event(e) for e in events]
    message = header + "\n\n".join(event_texts)

    await update.message.reply_text(message, parse_mode="MarkdownV2")


# ────────────────────────────────────────────────────────────
# /today command
# ────────────────────────────────────────────────────────────

@authorized_only
async def cmd_today(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Display today's Google Calendar events."""
    try:
        events = list_today_events()
    except CalendarAuthError:
        await update.message.reply_text(
            "Google Calendar is not connected yet\\. "
            "Please complete OAuth setup on your computer\\.",
            parse_mode="MarkdownV2",
        )
        return
    except CalendarAPIError as e:
        await update.message.reply_text(f"❌ {str(e)}")
        return

    today_str = datetime.now(TIMEZONE).strftime("%A, %B %d")

    if not events:
        await update.message.reply_text(
            "Your schedule is completely clear for today\\! Enjoy your day ☀️",
            parse_mode="MarkdownV2",
        )
        return

    header = f"📅 *Here's your schedule for today \\({_escape_md(today_str)}\\):*\n\n"
    event_texts = [format_calendar_event(e) for e in events]
    message = header + "\n\n".join(event_texts)

    await update.message.reply_text(message, parse_mode="MarkdownV2")


# ────────────────────────────────────────────────────────────
# Guided event creation (/new)
# ────────────────────────────────────────────────────────────

@authorized_only
async def cmd_new(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Start the guided event creation flow."""
    context.user_data.clear()
    await update.message.reply_text(
        "Sure, let's set up a new event\\!\n\n"
        "What would you like to call it?\n\n"
        "_(You can type /cancel anytime to exit)_",
        parse_mode="MarkdownV2",
    )
    return STATE_TITLE


@authorized_only
async def guided_title(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle the event title input."""
    title = update.message.text.strip()
    if not title:
        await update.message.reply_text("What name should we give this event?", parse_mode="MarkdownV2")
        return STATE_TITLE

    context.user_data["title"] = title
    await update.message.reply_text(
        f"Got it: *{_escape_md(title)}*\n\n"
        f"What date is it on? \\(e\\.g\\. tomorrow, next Friday, or Nov 15\\)",
        parse_mode="MarkdownV2",
    )
    return STATE_DATE


@authorized_only
async def guided_date(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle the event date input — use Gemini for natural-language dates."""
    date_text = update.message.text.strip()

    # Try parsing directly as YYYY-MM-DD
    try:
        parsed = datetime.strptime(date_text, "%Y-%m-%d")
        context.user_data["start_date"] = date_text
        await update.message.reply_text(
            f"🗓 *{_escape_md(parsed.strftime('%A, %B %d'))}*\n\n"
            f"What time does it start? \\(e\\.g\\. 10 AM, 3:30 PM\\)",
            parse_mode="MarkdownV2",
        )
        return STATE_TIME
    except ValueError:
        pass

    # Use Gemini to parse relative/natural date
    try:
        extracted = await extract_event_from_text(
            f"Event on {date_text}. Just tell me the date."
        )
        if extracted.start_date:
            context.user_data["start_date"] = extracted.start_date
            parsed = datetime.strptime(extracted.start_date, "%Y-%m-%d")
            await update.message.reply_text(
                f"🗓 *{_escape_md(parsed.strftime('%A, %B %d'))}*\n\n"
                f"What time does it start? \\(e\\.g\\. 10 AM, 3:30 PM\\)",
                parse_mode="MarkdownV2",
            )
            return STATE_TIME
    except Exception:
        pass

    await update.message.reply_text(
        "I couldn't quite catch that date\\. Try typing it like 'tomorrow', 'Friday', or '2026-11-05'\\.",
        parse_mode="MarkdownV2",
    )
    return STATE_DATE


@authorized_only
async def guided_time(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle the event start time input."""
    time_text = update.message.text.strip().upper()

    # Parse common time formats
    parsed_time = _parse_time_input(time_text)
    if parsed_time:
        context.user_data["start_time"] = parsed_time
        await update.message.reply_text(
            f"⏰ Starts at *{_escape_md(parsed_time)}*\n\n"
            f"How long will it run? \\(e\\.g\\. 1 hour, 30 mins, or an end time like 4 PM\\)",
            parse_mode="MarkdownV2",
        )
        return STATE_DURATION

    await update.message.reply_text(
        "I didn't catch that time\\. Try entering something like '10:00 AM' or '3 PM'\\.",
        parse_mode="MarkdownV2",
    )
    return STATE_TIME


@authorized_only
async def guided_duration(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle the duration or end time input."""
    text = update.message.text.strip()

    # Try to parse as end time
    parsed_time = _parse_time_input(text.upper())
    if parsed_time:
        context.user_data["end_time"] = parsed_time
    else:
        # Try to parse as duration
        minutes = _parse_duration(text)
        if minutes:
            context.user_data["duration_minutes"] = minutes
        else:
            await update.message.reply_text(
                "Could you give me a duration \\(like '1 hour' or '45 mins'\\) or an end time?",
                parse_mode="MarkdownV2",
            )
            return STATE_DURATION

    # Show reminder options
    keyboard = [
        [InlineKeyboardButton("🔔 Standard (1d, 1h, 15m before)", callback_data="guided_rem_default")],
    ]
    for opt in DEFAULT_REMINDER_OPTIONS:
        keyboard.append([InlineKeyboardButton(
            opt["label"], callback_data=f"guided_rem_{opt['minutes']}"
        )])
    keyboard.append([InlineKeyboardButton(
        "⏲ Custom time", callback_data="guided_rem_custom"
    )])
    keyboard.append([InlineKeyboardButton(
        "🚫 No reminder", callback_data="guided_rem_none"
    )])

    await update.message.reply_text(
        "When should I remind you before it starts?",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )
    return STATE_REMINDER


@authorized_only
async def guided_reminder_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """Handle the reminder selection callback."""
    query = update.callback_query
    await query.answer()
    data = query.data

    if data == "guided_rem_default":
        context.user_data["reminder_minutes"] = DEFAULT_REMINDER_TIERS
    elif data == "guided_rem_none":
        context.user_data["reminder_minutes"] = None
    elif data == "guided_rem_custom":
        await query.edit_message_text(
            "⏲ Enter the reminder time in minutes before the event:\n"
            "_Example: 15, 45, 120_",
            parse_mode="MarkdownV2",
        )
        return STATE_CUSTOM_REMINDER
    elif data.startswith("guided_rem_"):
        try:
            minutes = int(data.replace("guided_rem_", ""))
            context.user_data["reminder_minutes"] = minutes
        except ValueError:
            context.user_data["reminder_minutes"] = None
    else:
        context.user_data["reminder_minutes"] = None

    # Build and show confirmation
    return await _show_guided_confirmation(query, context)


@authorized_only
async def guided_custom_reminder(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """Handle custom reminder time input."""
    text = update.message.text.strip()
    try:
        minutes = int(text)
        if minutes <= 0:
            raise ValueError("Must be positive")
        context.user_data["reminder_minutes"] = minutes
    except ValueError:
        await update.message.reply_text(
            "❓ Please enter a positive number of minutes\\.\n"
            "_Example: 15, 45, 120_",
            parse_mode="MarkdownV2",
        )
        return STATE_CUSTOM_REMINDER

    # Build and show confirmation
    return await _show_guided_confirmation_from_message(update, context)


async def _show_guided_confirmation(query, context) -> int:
    """Build the event from guided data and show confirmation."""
    try:
        event = validate_and_build_event(
            title=context.user_data.get("title"),
            start_date=context.user_data.get("start_date"),
            start_time=context.user_data.get("start_time"),
            end_time=context.user_data.get("end_time"),
            duration_minutes=context.user_data.get("duration_minutes"),
            reminder_minutes=context.user_data.get("reminder_minutes"),
        )
    except EventValidationError as e:
        await query.edit_message_text(f"❌ {str(e)}")
        return ConversationHandler.END

    # Store the validated event and create pending confirmation
    context.user_data["validated_event"] = event
    confirmation_id = create_pending_confirmation(
        telegram_user_id=AUTHORIZED_USER_ID,
        title=event.title,
        start_time=event.start_datetime.isoformat(),
        end_time=event.end_datetime.isoformat(),
        timezone=event.timezone,
        description=event.description,
        reminder_minutes=event.reminder_minutes,
    )
    context.user_data["confirmation_id"] = confirmation_id

    # ── Conflict detection ──
    has_conflict = await _check_conflicts_and_show(
        event=event,
        confirmation_id=confirmation_id,
        message_target=query,
        context=context,
        is_edit=True,
    )
    if has_conflict:
        return ConversationHandler.END

    summary = format_event_summary(event)
    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Confirm", callback_data=f"confirm_{confirmation_id}"),
            InlineKeyboardButton("❌ Cancel", callback_data=f"cancel_{confirmation_id}"),
        ],
    ])

    await query.edit_message_text(
        f"📅 *Confirm Event*\n\n{summary}",
        reply_markup=keyboard,
        parse_mode="MarkdownV2",
    )
    return ConversationHandler.END


async def _show_guided_confirmation_from_message(update, context) -> int:
    """Same as above but for regular message replies."""
    try:
        event = validate_and_build_event(
            title=context.user_data.get("title"),
            start_date=context.user_data.get("start_date"),
            start_time=context.user_data.get("start_time"),
            end_time=context.user_data.get("end_time"),
            duration_minutes=context.user_data.get("duration_minutes"),
            reminder_minutes=context.user_data.get("reminder_minutes"),
        )
    except EventValidationError as e:
        await update.message.reply_text(f"❌ {str(e)}")
        return ConversationHandler.END

    context.user_data["validated_event"] = event
    confirmation_id = create_pending_confirmation(
        telegram_user_id=AUTHORIZED_USER_ID,
        title=event.title,
        start_time=event.start_datetime.isoformat(),
        end_time=event.end_datetime.isoformat(),
        timezone=event.timezone,
        description=event.description,
        reminder_minutes=event.reminder_minutes,
    )
    context.user_data["confirmation_id"] = confirmation_id

    # ── Conflict detection ──
    has_conflict = await _check_conflicts_and_show(
        event=event,
        confirmation_id=confirmation_id,
        message_target=update.message,
        context=context,
        is_edit=False,
    )
    if has_conflict:
        return ConversationHandler.END

    summary = format_event_summary(event)
    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Confirm", callback_data=f"confirm_{confirmation_id}"),
            InlineKeyboardButton("❌ Cancel", callback_data=f"cancel_{confirmation_id}"),
        ],
    ])

    await update.message.reply_text(
        f"📅 *Confirm Event*\n\n{summary}",
        reply_markup=keyboard,
        parse_mode="MarkdownV2",
    )
    return ConversationHandler.END


@authorized_only
async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Cancel the current conversation or pending operation."""
    context.user_data.clear()
    await update.message.reply_text(
        "Cancelled\\! Let me know if you need anything else\\.", parse_mode="MarkdownV2"
    )
    return ConversationHandler.END


async def _send_smart_reply(msg_target, text: str) -> None:
    """Send or edit a message with fallback to plain text if Markdown parsing fails."""
    try:
        await msg_target.edit_text(text, parse_mode="Markdown")
    except Exception:
        try:
            await msg_target.edit_text(text)
        except Exception as e:
            logger.error("Failed to edit message in smart reply: %s", e)


# ────────────────────────────────────────────────────────────
# Natural-language event creation (free-text messages)
# ────────────────────────────────────────────────────────────

@authorized_only
async def handle_natural_language(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Handle free-text messages as potential event creation requests."""
    text = update.message.text.strip()
    if not text:
        return

    import re
    if re.search(r"^\s*(/testvoice|test\s+(voice|speaker|speech)|speak\s+to\s+me)\b", text, re.IGNORECASE):
        await cmd_testvoice(update, context)
        return

    # Send a friendly indicator
    thinking_msg = await update.message.reply_text("On it\\.\\.\\.", parse_mode="MarkdownV2")

    # Check if user provided an event link (URL)
    urls = find_urls(text)
    extracted = None

    if urls:
        target_url = urls[0]
        await thinking_msg.edit_text("Checking that link\\.\\.\\.", parse_mode="MarkdownV2")
        scraped_content = await scrape_event_page(target_url)
        if scraped_content:
            try:
                extracted = await extract_event_from_url_content(
                    url=target_url, page_content=scraped_content, user_text=text
                )
            except RuntimeError as e:
                await thinking_msg.edit_text(f"❌ {str(e)}")
                return
        else:
            await thinking_msg.edit_text(
                "Couldn't reach that link directly, checking your message text instead\\.\\.\\.",
                parse_mode="MarkdownV2",
            )

    if not extracted:
        try:
            action = await extract_action_from_text(text)
        except RuntimeError as e:
            await thinking_msg.edit_text(f"❌ {str(e)}")
            return
    else:
        # Wrap scraped event into a UnifiedActionRequest
        action = UnifiedActionRequest(
            intent="create_event",
            event=ExtractedEventData(
                title=extracted.title,
                description=extracted.description,
                start_date=extracted.start_date,
                start_time=extracted.start_time,
                end_date=extracted.end_date,
                end_time=extracted.end_time,
                duration_minutes=extracted.duration_minutes,
            )
        )

    # 1. Handle Ambiguity & Clarification Questions
    if action.needs_clarification:
        clarification = action.clarification_message or "Could you clarify what you'd like to do?"
        await thinking_msg.edit_text(f"❓ {_escape_md(clarification)}", parse_mode="MarkdownV2")
        return

    # 2. Handle Task Intents (Notion)
    if action.intent == "create_task":
        if not action.task or not action.task.title:
            await thinking_msg.edit_text("❓ What task would you like to add to Notion?", parse_mode="MarkdownV2")
            return

        try:
            task_obj = validate_and_build_task(
                title=action.task.title,
                description=action.task.description,
                due_date=action.task.due_date,
                due_time=action.task.due_time,
                priority=action.task.priority,
                category=action.task.category,
                status=action.task.status,
                reminder_minutes=action.task.reminder_minutes,
            )
        except TaskValidationError as e:
            await thinking_msg.edit_text(f"❌ {str(e)}")
            return

        payload = {
            "title": task_obj.title,
            "description": task_obj.description,
            "due_date": task_obj.due_date,
            "due_time": task_obj.due_time,
            "priority": task_obj.priority,
            "category": task_obj.category,
            "status": task_obj.status,
            "reminder_minutes": task_obj.reminder_minutes,
        }
        cid = create_generic_pending_confirmation(
            telegram_user_id=AUTHORIZED_USER_ID,
            target_type="task",
            action_type="create",
            title=task_obj.title,
            payload=payload,
            description=task_obj.description,
            reminder_minutes=task_obj.reminder_minutes,
        )

        summary = format_task_summary(task_obj)
        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("✅ Confirm Task", callback_data=f"confirm_task_{cid}"),
                InlineKeyboardButton("❌ Cancel", callback_data=f"cancel_task_{cid}"),
            ],
        ])
        await thinking_msg.edit_text(
            f"📋 *Confirm Task*\n\n{summary}",
            reply_markup=keyboard,
            parse_mode="MarkdownV2",
        )
        return

    if action.intent == "list_tasks":
        await cmd_tasks(update, context)
        return

    if action.intent == "complete_task":
        target = (action.task.target_task_title or action.task.title) if action.task else text
        if not target:
            await thinking_msg.edit_text("❓ Which task would you like to mark as complete?", parse_mode="MarkdownV2")
            return

        try:
            found = await find_task_by_title(target)
        except Exception as e:
            await thinking_msg.edit_text(f"❌ {str(e)}")
            return

        if not found:
            await thinking_msg.edit_text(f"🔍 Could not find any task matching *'{_escape_md(target)}'* in Notion\\.", parse_mode="MarkdownV2")
            return

        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("✅ Mark Done", callback_data=f"confirm_task_done_{found['id']}"),
                InlineKeyboardButton("❌ Cancel", callback_data="cancel_task_action"),
            ],
        ])
        await thinking_msg.edit_text(
            f"✅ *Mark Done?*\n\n📋 *{_escape_md(found['title'])}*",
            reply_markup=keyboard,
            parse_mode="MarkdownV2",
        )
        return

    if action.intent == "delete_task":
        target = (action.task.target_task_title or action.task.title) if action.task else text
        if not target:
            await thinking_msg.edit_text("❓ Which task would you like to delete from Notion?", parse_mode="MarkdownV2")
            return

        try:
            found = await find_task_by_title(target)
        except Exception as e:
            await thinking_msg.edit_text(f"❌ {str(e)}")
            return

        if not found:
            await thinking_msg.edit_text(f"🔍 Could not find any task matching *'{_escape_md(target)}'* in Notion\\.", parse_mode="MarkdownV2")
            return

        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("🗑️ Delete Task", callback_data=f"confirm_task_del_{found['id']}"),
                InlineKeyboardButton("❌ Cancel", callback_data="cancel_task_action"),
            ],
        ])
        await thinking_msg.edit_text(
            f"🗑️ *Delete Task?*\n\n📋 *{_escape_md(found['title'])}*",
            reply_markup=keyboard,
            parse_mode="MarkdownV2",
        )
        return

    # 3. Handle Combined Action (Event + Task)
    if action.intent == "combined_action" and action.event and action.task:
        try:
            event_obj = validate_and_build_event(
                title=action.event.title,
                description=action.event.description,
                start_date=action.event.start_date,
                start_time=action.event.start_time,
                end_date=action.event.end_date,
                end_time=action.event.end_time,
                duration_minutes=action.event.duration_minutes,
                reminder_minutes=action.event.reminder_minutes,
            )
        except EventValidationError as e:
            await thinking_msg.edit_text(f"❌ Event error: {str(e)}")
            return

        try:
            task_obj = validate_and_build_task(
                title=action.task.title,
                description=action.task.description,
                due_date=action.task.due_date,
                due_time=action.task.due_time,
                priority=action.task.priority,
                category=action.task.category,
                status=action.task.status,
                reminder_minutes=action.task.reminder_minutes,
            )
        except TaskValidationError as e:
            await thinking_msg.edit_text(f"❌ Task error: {str(e)}")
            return

        payload = {
            "event": {
                "title": event_obj.title,
                "description": event_obj.description,
                "start_date": event_obj.start_datetime.strftime("%Y-%m-%d"),
                "start_time": event_obj.start_datetime.strftime("%H:%M"),
                "end_date": event_obj.end_datetime.strftime("%Y-%m-%d"),
                "end_time": event_obj.end_datetime.strftime("%H:%M"),
                "duration_minutes": action.event.duration_minutes or 60,
                "reminder_minutes": event_obj.reminder_minutes,
            },
            "task": {
                "title": task_obj.title,
                "description": task_obj.description,
                "due_date": task_obj.due_date,
                "due_time": task_obj.due_time,
                "priority": task_obj.priority,
                "category": task_obj.category,
                "status": task_obj.status,
                "reminder_minutes": task_obj.reminder_minutes,
            },
        }

        cid = create_generic_pending_confirmation(
            telegram_user_id=AUTHORIZED_USER_ID,
            target_type="combined",
            action_type="create",
            title=f"{event_obj.title} + {task_obj.title}",
            payload=payload,
        )

        has_conflict = await _check_conflicts_and_show(
            event=event_obj,
            confirmation_id=cid,
            message_target=thinking_msg,
            context=context,
            is_edit=True,
        )
        if has_conflict:
            return

        ev_summary = format_event_summary(event_obj)
        tk_summary = format_task_summary(task_obj)

        card = (
            f"🗓️ *Calendar Event*\n{ev_summary}\n\n"
            f"📋 *Notion Task*\n{tk_summary}"
        )
        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("✅ Confirm Both", callback_data=f"confirm_comb_{cid}"),
                InlineKeyboardButton("❌ Cancel", callback_data=f"cancel_comb_{cid}"),
            ],
        ])
        await thinking_msg.edit_text(card, reply_markup=keyboard, parse_mode="MarkdownV2")
        return

    # 4. Handle Upcoming events
    if action.intent == "list_upcoming":
        try:
            events = await asyncio.to_thread(list_upcoming_events, max_results=10)
        except CalendarAuthError:
            await thinking_msg.edit_text("Google Calendar is not connected yet\\.", parse_mode="MarkdownV2")
            return
        except CalendarAPIError as e:
            await thinking_msg.edit_text(f"❌ {str(e)}")
            return

        if not events:
            await thinking_msg.edit_text("Your calendar is completely open for the next 3 weeks\\! 🎉", parse_mode="MarkdownV2")
            return

        header = "📅 *Coming up in the next 3 weeks:*\n\n"
        event_texts = [format_calendar_event(e) for e in events]
        message = header + "\n\n".join(event_texts)
        await thinking_msg.edit_text(message, parse_mode="MarkdownV2")
        return

    # 5. Handle Today's events
    if action.intent == "list_today":
        try:
            events = await asyncio.to_thread(list_today_events)
        except CalendarAuthError:
            await thinking_msg.edit_text("Google Calendar is not connected yet\\.", parse_mode="MarkdownV2")
            return
        except CalendarAPIError as e:
            await thinking_msg.edit_text(f"❌ {str(e)}")
            return

        today_str = datetime.now(TIMEZONE).strftime("%A, %B %d")
        if not events:
            await thinking_msg.edit_text(
                "Your schedule is completely clear for today\\! ☀️",
                parse_mode="MarkdownV2",
            )
            return

        header = f"📅 *Here's your schedule for today \\({_escape_md(today_str)}\\):*\n\n"
        event_texts = [format_calendar_event(e) for e in events]
        message = header + "\n\n".join(event_texts)
        await thinking_msg.edit_text(message, parse_mode="MarkdownV2")
        return

    # 6. Handle Check reminders
    if action.intent == "check_reminders":
        title_query = action.event.title if action.event else None
        msg = get_upcoming_reminders_summary(AUTHORIZED_USER_ID, query_title=title_query)
        await thinking_msg.edit_text(msg, parse_mode="MarkdownV2")
        return

    # 7. Handle Daily briefing
    if action.intent == "daily_briefing":
        message = await asyncio.to_thread(build_daily_briefing_message)
        await thinking_msg.edit_text(message, parse_mode="MarkdownV2", disable_web_page_preview=True)
        return

    # 8. Handle Delete event
    if action.intent == "delete_event":
        target = (action.event.target_event_title or action.event.title) if action.event else text
        await _prompt_event_deletion(target, thinking_msg, AUTHORIZED_USER_ID)
        return

    # 9. Handle Reschedule event
    if action.intent == "reschedule_event":
        # Pass compatible event model
        ev_compat = ExtractedEvent(
            intent="reschedule_event",
            title=action.event.title if action.event else None,
            target_event_title=action.event.target_event_title if action.event else None,
            start_date=action.event.start_date if action.event else None,
            start_time=action.event.start_time if action.event else None,
            end_date=action.event.end_date if action.event else None,
            end_time=action.event.end_time if action.event else None,
            duration_minutes=action.event.duration_minutes if action.event else None,
        )
        await _prompt_event_reschedule(ev_compat, thinking_msg, AUTHORIZED_USER_ID, context)
        return

    # 10. Handle Chat & Questions
    if action.intent == "chat":
        calendar_summary = ""
        tasks_summary = ""

        if action.requires_calendar_lookup or action.requires_task_lookup:
            await thinking_msg.edit_text("Checking your schedule & tasks\\.\\.\\.", parse_mode="MarkdownV2")
            if action.requires_calendar_lookup:
                try:
                    today_events = await asyncio.to_thread(list_today_events)
                    upcoming_events = await asyncio.to_thread(list_upcoming_events, max_results=6, weeks_ahead=1)
                    c_parts = []
                    if today_events:
                        c_parts.append("Today's Events:")
                        for e in today_events:
                            st = e.get("start", {}).get("dateTime", e.get("start", {}).get("date", ""))
                            c_parts.append(f"- {e.get('summary', 'Untitled')} ({st})")
                    if upcoming_events:
                        c_parts.append("Upcoming Events:")
                        for e in upcoming_events:
                            st = e.get("start", {}).get("dateTime", e.get("start", {}).get("date", ""))
                            c_parts.append(f"- {e.get('summary', 'Untitled')} ({st})")
                    calendar_summary = "\n".join(c_parts)
                except Exception:
                    pass

            if action.requires_task_lookup:
                try:
                    notion_tasks = await list_notion_tasks(max_results=8)
                    t_parts = [f"- [{t['priority']}] {t['title']} (Due: {t.get('due_date', 'None')})" for t in notion_tasks]
                    tasks_summary = "\n".join(t_parts)
                except Exception:
                    pass

            chat_reply = await generate_schedule_chat_response(text, calendar_summary, tasks_summary)
            await _send_smart_reply(thinking_msg, chat_reply)
            return

        reply = action.chat_response or "Hello! I'm T.I.M.E.S, your AI assistant for Google Calendar and Notion. How can I help you today?"
        await _send_smart_reply(thinking_msg, reply)
        return

    # 11. Handle Event Creation (create_event)
    ev_data = action.event
    if not ev_data or not ev_data.title:
        await thinking_msg.edit_text("❓ What event would you like to add to your calendar?", parse_mode="MarkdownV2")
        return

    reminder_mins = ev_data.reminder_minutes
    if reminder_mins is None:
        import re
        if not re.search(r"\b(no|without)\s+reminders?\b", text, re.IGNORECASE):
            reminder_mins = DEFAULT_REMINDER_TIERS
    elif reminder_mins == 0:
        reminder_mins = None

    try:
        event = validate_and_build_event(
            title=ev_data.title,
            description=ev_data.description,
            start_date=ev_data.start_date,
            start_time=ev_data.start_time,
            end_date=ev_data.end_date,
            end_time=ev_data.end_time,
            duration_minutes=ev_data.duration_minutes,
            reminder_minutes=reminder_mins,
        )
    except EventValidationError as e:
        await thinking_msg.edit_text(f"❌ {str(e)}")
        return

    # Create pending confirmation
    context.user_data["validated_event"] = event
    confirmation_id = create_pending_confirmation(
        telegram_user_id=AUTHORIZED_USER_ID,
        title=event.title,
        start_time=event.start_datetime.isoformat(),
        end_time=event.end_datetime.isoformat(),
        timezone=event.timezone,
        description=event.description,
        reminder_minutes=event.reminder_minutes,
    )
    context.user_data["confirmation_id"] = confirmation_id

    # Conflict detection
    has_conflict = await _check_conflicts_and_show(
        event=event,
        confirmation_id=confirmation_id,
        message_target=thinking_msg,
        context=context,
        is_edit=True,
    )
    if has_conflict:
        return

    summary = format_event_summary(event)
    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Confirm", callback_data=f"confirm_{confirmation_id}"),
            InlineKeyboardButton("❌ Cancel", callback_data=f"cancel_{confirmation_id}"),
        ],
    ])

    await thinking_msg.edit_text(
        f"📅 *Confirm Event*\n\n{summary}",
        reply_markup=keyboard,
        parse_mode="MarkdownV2",
    )
    return


# ────────────────────────────────────────────────────────────
# Confirmation callback handlers
# ────────────────────────────────────────────────────────────

@authorized_only
async def handle_confirm_task(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle confirmed creation of a Notion task."""
    query = update.callback_query
    await query.answer()

    confirmation_id = query.data.replace("confirm_task_", "")
    was_resolved = resolve_confirmation(confirmation_id, "confirmed")
    if not was_resolved:
        await _safe_edit_message_text(query, "⚠️ This task has already been processed\\.", parse_mode="MarkdownV2")
        return

    # Immediate feedback
    try:
        await query.edit_message_text("⏳ Adding task to Notion\\.\\.\\.", parse_mode="MarkdownV2")
    except Exception:
        pass

    conf = get_pending_confirmation(confirmation_id)
    if not conf:
        await _safe_edit_message_text(query, "❌ Task confirmation not found\\.", parse_mode="MarkdownV2")
        return

    payload = json.loads(conf.get("payload_json") or "{}")
    task_obj = ValidatedTask(
        title=payload.get("title") or conf.get("title", ""),
        description=payload.get("description"),
        due_date=payload.get("due_date"),
        due_time=payload.get("due_time"),
        priority=payload.get("priority", "Medium"),
        category=payload.get("category", "General"),
        status=payload.get("status", "To Do"),
        reminder_minutes=payload.get("reminder_minutes"),
    )

    try:
        created = await create_notion_task(task_obj)
    except (NotionConfigError, NotionAPIError) as e:
        resolve_confirmation(confirmation_id, "pending")
        await _safe_edit_message_text(query, f"❌ {str(e)}", parse_mode=None)
        return

    # Save to local SQLite
    save_task(
        notion_page_id=created["id"],
        title=created["title"],
        telegram_user_id=AUTHORIZED_USER_ID,
        description=created.get("description"),
        due_date=created.get("due_date"),
        due_time=created.get("due_time"),
        priority=created.get("priority", "Medium"),
        category=created.get("category", "General"),
        status=created.get("status", "To Do"),
    )

    # Schedule reminder if deadline present
    due_dt = task_obj.get_due_datetime()
    reminders_scheduled = 0
    if due_dt:
        rem_ids = schedule_reminders_for_task(
            notion_page_id=created["id"],
            telegram_user_id=AUTHORIZED_USER_ID,
            task_title=created["title"],
            due_datetime=due_dt,
            reminder_minutes=task_obj.reminder_minutes,
            priority=created["priority"],
            category=created["category"],
        )
        reminders_scheduled = len(rem_ids)

    summary = format_task_summary(task_obj)
    msg = f"✅ *Added to Notion*\n\n{summary}"
    if reminders_scheduled > 0:
        msg += f"\n🔔 *Reminder:* {reminders_scheduled} alert{'s' if reminders_scheduled > 1 else ''} set"
    if created.get("url"):
        msg += f"\n\n🔗 [View in Notion]({_escape_url(created['url'])})"

    await _safe_edit_message_text(query, msg, parse_mode="MarkdownV2", disable_web_page_preview=True)


@authorized_only
async def handle_cancel_task(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle cancellation of task creation."""
    query = update.callback_query
    await query.answer()
    confirmation_id = query.data.replace("cancel_task_", "")
    resolve_confirmation(confirmation_id, "cancelled")
    await _safe_edit_message_text(query, "❌ Cancelled", parse_mode="MarkdownV2")


@authorized_only
async def handle_confirm_task_done(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle confirmed task completion."""
    query = update.callback_query
    await query.answer()

    page_id = query.data.replace("confirm_task_done_", "")
    try:
        await query.edit_message_text("⏳ Updating task in Notion\\.\\.\\.", parse_mode="MarkdownV2")
    except Exception:
        pass

    try:
        updated = await complete_notion_task(page_id)
        update_task_status(page_id, "Done")
        cancel_reminders_for_notion_task(page_id)
        title = updated.get("title", "Task")
        await _safe_edit_message_text(query, f"✅ *Marked Done*\n\n📋 *{_escape_md(title)}*", parse_mode="MarkdownV2")
    except Exception as e:
        await _safe_edit_message_text(query, f"❌ Failed to complete task: {str(e)}", parse_mode=None)


@authorized_only
async def handle_confirm_task_delete(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle confirmed task deletion."""
    query = update.callback_query
    await query.answer()

    page_id = query.data.replace("confirm_task_del_", "")
    try:
        await query.edit_message_text("⏳ Deleting task from Notion\\.\\.\\.", parse_mode="MarkdownV2")
    except Exception:
        pass

    try:
        await delete_notion_task(page_id)
        delete_task_by_notion_id(page_id)
        cancel_reminders_for_notion_task(page_id)
        await _safe_edit_message_text(query, "🗑️ *Task Deleted*", parse_mode="MarkdownV2")
    except Exception as e:
        await _safe_edit_message_text(query, f"❌ Failed to delete task: {str(e)}", parse_mode=None)


@authorized_only
async def handle_cancel_task_action(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Cancel interactive task update/delete action."""
    query = update.callback_query
    await query.answer()
    await _safe_edit_message_text(query, "❌ Cancelled", parse_mode="MarkdownV2")


@authorized_only
async def handle_confirm_combined(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle confirmed combined action (Google Calendar Event + Notion Task)."""
    query = update.callback_query
    await query.answer()

    confirmation_id = query.data.replace("confirm_comb_", "")
    was_resolved = resolve_confirmation(confirmation_id, "confirmed")
    if not was_resolved:
        await _safe_edit_message_text(query, "⚠️ This request has already been processed\\.", parse_mode="MarkdownV2")
        return

    # Immediate feedback
    try:
        await query.edit_message_text("⏳ Processing calendar event & Notion task\\.\\.\\.", parse_mode="MarkdownV2")
    except Exception:
        pass

    conf = get_pending_confirmation(confirmation_id)
    if not conf:
        await _safe_edit_message_text(query, "❌ Confirmation not found\\.", parse_mode="MarkdownV2")
        return

    payload = json.loads(conf.get("payload_json") or "{}")
    event_dict = payload.get("event")
    task_dict = payload.get("task")

    event_obj = None
    if event_dict:
        event_obj = validate_and_build_event(
            title=event_dict["title"],
            description=event_dict.get("description"),
            start_date=event_dict["start_date"],
            start_time=event_dict["start_time"],
            end_date=event_dict.get("end_date"),
            end_time=event_dict.get("end_time"),
            duration_minutes=event_dict.get("duration_minutes", 60),
            reminder_minutes=event_dict.get("reminder_minutes"),
        )

    task_obj = None
    if task_dict:
        task_obj = validate_and_build_task(
            title=task_dict["title"],
            description=task_dict.get("description"),
            due_date=task_dict.get("due_date"),
            due_time=task_dict.get("due_time"),
            priority=task_dict.get("priority", "Medium"),
            category=task_dict.get("category", "General"),
            status=task_dict.get("status", "To Do"),
            reminder_minutes=task_dict.get("reminder_minutes"),
        )

    # 1. Create Google Calendar event
    if event_obj:
        try:
            google_event = await asyncio.to_thread(create_calendar_event, event_obj)
            google_id = google_event.get("id", "")
            local_id = save_event(
                google_event_id=google_id,
                title=event_obj.title,
                start_time=event_obj.start_datetime.isoformat(),
                end_time=event_obj.end_datetime.isoformat(),
                timezone=event_obj.timezone,
                telegram_user_id=AUTHORIZED_USER_ID,
                description=event_obj.description,
            )
            schedule_reminders_for_event(
                event_id=local_id,
                google_event_id=google_id,
                telegram_user_id=AUTHORIZED_USER_ID,
                event_title=event_obj.title,
                event_start_time=event_obj.start_datetime,
                reminder_minutes_list=event_obj.get_reminder_minutes_list(),
            )
        except Exception as e:
            resolve_confirmation(confirmation_id, "pending")
            await _safe_edit_message_text(query, f"❌ Failed to create Google Calendar event: {str(e)}", parse_mode=None)
            return

    # 2. Create Notion Task
    if task_obj:
        try:
            notion_task = await create_notion_task(task_obj)
            save_task(
                notion_page_id=notion_task["id"],
                title=notion_task["title"],
                telegram_user_id=AUTHORIZED_USER_ID,
                description=notion_task.get("description"),
                due_date=notion_task.get("due_date"),
                due_time=notion_task.get("due_time"),
                priority=notion_task.get("priority", "Medium"),
                category=notion_task.get("category", "General"),
                status=notion_task.get("status", "To Do"),
            )
            due_dt = task_obj.get_due_datetime()
            if due_dt:
                schedule_reminders_for_task(
                    notion_page_id=notion_task["id"],
                    telegram_user_id=AUTHORIZED_USER_ID,
                    task_title=notion_task["title"],
                    due_datetime=due_dt,
                    reminder_minutes=task_obj.reminder_minutes,
                    priority=notion_task["priority"],
                    category=notion_task["category"],
                )
        except Exception as e:
            await _safe_edit_message_text(query, f"⚠️ Event created, but Notion task failed: {str(e)}", parse_mode=None)
            return

    msg = "✅ *Created*\n\n"
    if event_obj:
        msg += f"🗓️ *Calendar:* {_escape_md(event_obj.title)}\n"
    if task_obj:
        msg += f"📋 *Notion:* {_escape_md(task_obj.title)}\n"
    await _safe_edit_message_text(query, msg, parse_mode="MarkdownV2")


@authorized_only
async def handle_cancel_combined(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle cancellation of combined action."""
    query = update.callback_query
    await query.answer()
    confirmation_id = query.data.replace("cancel_comb_", "")
    resolve_confirmation(confirmation_id, "cancelled")
    await _safe_edit_message_text(query, "❌ Cancelled", parse_mode="MarkdownV2")


@authorized_only
async def handle_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle the 'Confirm' inline button click."""
    query = update.callback_query
    await query.answer()

    confirmation_id = query.data.replace("confirm_", "")

    # Prevent duplicate confirmation processing
    was_resolved = resolve_confirmation(confirmation_id, "confirmed")
    if not was_resolved:
        await _safe_edit_message_text(
            query,
            "⚠️ This event has already been processed\\.",
            parse_mode="MarkdownV2",
        )
        return

    # Immediate feedback & remove action buttons
    try:
        await query.edit_message_text("⏳ Adding event to Google Calendar\\.\\.\\.", parse_mode="MarkdownV2")
    except Exception:
        pass

    # Get the confirmation data from DB
    conf = get_pending_confirmation(confirmation_id)
    if not conf:
        await _safe_edit_message_text(query, "❌ Confirmation not found\\.", parse_mode="MarkdownV2")
        return

    # Rebuild the validated event from the confirmation record
    stored_rem = conf.get("reminder_minutes")
    reminder_val = None
    if stored_rem is not None:
        if isinstance(stored_rem, int):
            reminder_val = stored_rem
        elif isinstance(stored_rem, str):
            try:
                reminder_val = json.loads(stored_rem)
            except Exception:
                try:
                    reminder_val = int(stored_rem)
                except Exception:
                    reminder_val = None
        elif isinstance(stored_rem, list):
            reminder_val = stored_rem

    try:
        event = validate_and_build_event(
            title=conf["title"],
            description=conf["description"],
            start_date=datetime.fromisoformat(conf["start_time"]).strftime("%Y-%m-%d"),
            start_time=datetime.fromisoformat(conf["start_time"]).strftime("%H:%M"),
            end_date=datetime.fromisoformat(conf["end_time"]).strftime("%Y-%m-%d"),
            end_time=datetime.fromisoformat(conf["end_time"]).strftime("%H:%M"),
            reminder_minutes=reminder_val,
        )
    except EventValidationError as e:
        await _safe_edit_message_text(query, f"❌ Validation error: {str(e)}", parse_mode=None)
        return

    # Create the event in Google Calendar
    try:
        google_event = await asyncio.to_thread(create_calendar_event, event)
    except (CalendarAuthError, CalendarAPIError) as e:
        # Roll back the confirmation status
        resolve_confirmation(confirmation_id, "pending")
        await _safe_edit_message_text(query, f"❌ {str(e)}", parse_mode=None)
        return

    google_event_id = google_event.get("id", "")
    event_link = google_event.get("htmlLink", "")

    # Save to local DB
    local_event_id = save_event(
        google_event_id=google_event_id,
        title=event.title,
        start_time=event.start_datetime.isoformat(),
        end_time=event.end_datetime.isoformat(),
        telegram_user_id=AUTHORIZED_USER_ID,
        timezone=event.timezone,
        description=event.description,
    )

    # Schedule reminders if requested
    reminder_info = ""
    reminder_tiers = event.get_reminder_minutes_list()
    if reminder_tiers:
        scheduled_ids = schedule_reminders_for_event(
            event_id=local_event_id,
            google_event_id=google_event_id,
            telegram_user_id=AUTHORIZED_USER_ID,
            event_title=event.title,
            event_start_time=event.start_datetime,
            reminder_minutes_list=reminder_tiers,
        )
        if scheduled_ids:
            from event_parser import _format_reminder_time
            if len(reminder_tiers) == 1:
                reminder_info = f"🔔 Reminder set: {_format_reminder_time(reminder_tiers[0])}"
            else:
                labels = [_format_reminder_time(m) for m in reminder_tiers]
                reminder_info = f"🔔 Reminders set: {', '.join(labels)}"

    date_str = event.start_datetime.strftime("%A, %b %d")
    start_str = event.start_datetime.strftime("%I:%M %p").lstrip("0")

    success_msg = (
        f"✅ *Added to Calendar*\n\n"
        f"📌 *{_escape_md(event.title)}*\n"
        f"🗓️ {_escape_md(date_str)}\n"
        f"⏰ {_escape_md(start_str)}"
    )

    if reminder_info:
        success_msg += f"\n{_escape_md(reminder_info)}"

    if event_link:
        success_msg += f"\n\n🔗 [Open in Google Calendar]({_escape_url(event_link)})"

    await _safe_edit_message_text(query, success_msg, parse_mode="MarkdownV2", disable_web_page_preview=True)

    # Send a new message so the user receives an instant push notification on their phone/desktop
    notify_text = (
        f"✅ *Added to Calendar*\n\n"
        f"📌 *{_escape_md(event.title)}*\n"
        f"🗓️ {_escape_md(date_str)}\n"
        f"⏰ {_escape_md(start_str)}"
    )
    if reminder_info:
        notify_text += f"\n{_escape_md(reminder_info)}"
    if event_link:
        notify_text += f"\n\n🔗 [Open in Google Calendar]({_escape_url(event_link)})"

    await _safe_send_message(
        context.bot,
        chat_id=query.message.chat_id,
        text=notify_text,
        parse_mode="MarkdownV2",
        disable_web_page_preview=True,
    )


@authorized_only
async def handle_cancel_event(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Handle the 'Cancel' inline button click."""
    query = update.callback_query
    await query.answer()

    confirmation_id = query.data.replace("cancel_", "")

    was_resolved = resolve_confirmation(confirmation_id, "cancelled")
    if not was_resolved:
        await _safe_edit_message_text(
            query,
            "⚠️ This event has already been processed\\.",
            parse_mode="MarkdownV2",
        )
        return

    await _safe_edit_message_text(
        query,
        "❌ Cancelled",
        parse_mode="MarkdownV2",
    )
    context.user_data.clear()


# ────────────────────────────────────────────────────────────
# Deletion & Reschedule helper functions & callbacks
# ────────────────────────────────────────────────────────────

async def _prompt_event_deletion(target_title: Optional[str], thinking_msg, user_id: int) -> None:
    """Find the event by title and show a delete confirmation card."""
    if not target_title or not target_title.strip():
        await thinking_msg.edit_text("❓ Please specify which event you'd like to delete\\.\n_Example: 'cancel AWS event'_", parse_mode="MarkdownV2")
        return

    q = target_title.strip()
    try:
        matches = search_upcoming_events(q)
    except Exception as e:
        await thinking_msg.edit_text(f"❌ Failed to search events: {str(e)}")
        return

    if not matches:
        await thinking_msg.edit_text(
            f"📭 Could not find any upcoming event matching *\"{_escape_md(q)}\"*\\.",
            parse_mode="MarkdownV2",
        )
        return

    event = matches[0]
    google_event_id = event["id"]
    title = event.get("summary", "Untitled Event")
    start_raw = event.get("start", {})
    start_str = start_raw.get("dateTime") or start_raw.get("date")
    if start_str and "T" in start_str:
        dt = datetime.fromisoformat(start_str)
        time_display = dt.strftime("%A, %b %d at %I:%M %p")
    elif start_str:
        time_display = start_str
    else:
        time_display = "Unknown time"

    card = (
        f"🗑️ *Delete Event*\n\n"
        f"📌 *{_escape_md(title)}*\n"
        f"📆 {_escape_md(time_display)}"
    )
    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🗑️ Delete", callback_data=f"deleteconf_{google_event_id}"),
            InlineKeyboardButton("❌ Cancel", callback_data=f"deletedis_{google_event_id}"),
        ]
    ])
    await thinking_msg.edit_text(card, reply_markup=keyboard, parse_mode="MarkdownV2")


@authorized_only
async def handle_confirm_delete(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle confirmed deletion of a calendar event."""
    query = update.callback_query
    await query.answer()

    google_event_id = query.data.replace("deleteconf_", "")

    try:
        await query.edit_message_text("⏳ Deleting event from Google Calendar\\.\\.\\.", parse_mode="MarkdownV2")
    except Exception:
        pass

    ev_summary = "Event"
    try:
        ev = get_calendar_event(google_event_id)
        if ev:
            ev_summary = ev.get("summary", "Event")
    except Exception:
        pass

    try:
        await asyncio.to_thread(delete_calendar_event, google_event_id)
    except CalendarAPIError as e:
        await _safe_edit_message_text(query, f"❌ {str(e)}", parse_mode=None)
        return

    cancel_reminders_for_google_event(google_event_id)
    delete_event_by_google_id(google_event_id)

    msg = (
        f"🗑️ *Deleted from Calendar*\n\n"
        f"📌 *{_escape_md(ev_summary)}*"
    )
    await _safe_edit_message_text(query, msg, parse_mode="MarkdownV2")

    await _safe_send_message(
        context.bot,
        chat_id=query.message.chat_id,
        text=f"🗑️ *Deleted from Calendar:* {_escape_md(ev_summary)}",
        parse_mode="MarkdownV2",
    )


@authorized_only
async def handle_cancel_delete(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle cancellation of event deletion."""
    query = update.callback_query
    await query.answer()
    await _safe_edit_message_text(query, "❌ Cancelled", parse_mode="MarkdownV2")


async def _prompt_event_reschedule(extracted, thinking_msg, user_id: int, context) -> None:
    """Find target event, calculate new date/time, and show reschedule confirmation."""
    target_title = extracted.target_event_title or extracted.title
    if not target_title or not target_title.strip():
        await thinking_msg.edit_text("❓ Please specify which event you'd like to reschedule\\.\n_Example: 'move AWS event to tomorrow 5 PM'_", parse_mode="MarkdownV2")
        return

    q = target_title.strip()
    try:
        matches = search_upcoming_events(q)
    except Exception as e:
        await thinking_msg.edit_text(f"❌ Failed to search events: {str(e)}")
        return

    if not matches:
        await thinking_msg.edit_text(
            f"📭 Could not find any upcoming event matching *\"{_escape_md(q)}\"* to reschedule\\.",
            parse_mode="MarkdownV2",
        )
        return

    target = matches[0]
    google_event_id = target["id"]
    event_title = target.get("summary", "Untitled Event")
    start_raw = target.get("start", {})
    end_raw = target.get("end", {})

    old_start_str = start_raw.get("dateTime") or start_raw.get("date")
    old_end_str = end_raw.get("dateTime") or end_raw.get("date")

    old_start_dt = None
    old_end_dt = None
    if old_start_str and "T" in old_start_str:
        old_start_dt = datetime.fromisoformat(old_start_str)
        if old_start_dt.tzinfo is None:
            old_start_dt = TIMEZONE.localize(old_start_dt)
    if old_end_str and "T" in old_end_str:
        old_end_dt = datetime.fromisoformat(old_end_str)
        if old_end_dt.tzinfo is None:
            old_end_dt = TIMEZONE.localize(old_end_dt)

    new_date = extracted.start_date
    new_time = extracted.start_time

    if not new_date and old_start_dt:
        new_date = old_start_dt.strftime("%Y-%m-%d")
    if not new_time and old_start_dt:
        new_time = old_start_dt.strftime("%H:%M")

    if not new_date or not new_time:
        await thinking_msg.edit_text(
            f"❓ What new date or time would you like to move *{_escape_md(event_title)}* to?\n"
            f"_Example: 'move {event_title} to Friday at 4 PM'_",
            parse_mode="MarkdownV2",
        )
        return

    duration = extracted.duration_minutes
    if not duration and old_start_dt and old_end_dt:
        duration = max(int((old_end_dt - old_start_dt).total_seconds() / 60), 15)
    if not duration:
        duration = 60

    try:
        new_event = validate_and_build_event(
            title=event_title,
            description=target.get("description"),
            start_date=new_date,
            start_time=new_time,
            duration_minutes=duration,
            reminder_minutes=DEFAULT_REMINDER_TIERS,
        )
    except EventValidationError as e:
        await thinking_msg.edit_text(f"❌ {str(e)}")
        return

    conf_id = create_pending_confirmation(
        telegram_user_id=user_id,
        title=new_event.title,
        start_time=new_event.start_datetime.isoformat(),
        end_time=new_event.end_datetime.isoformat(),
        timezone=new_event.timezone,
        description=new_event.description,
        reminder_minutes=DEFAULT_REMINDER_TIERS,
    )
    context.user_data[f"resched_target_{conf_id}"] = google_event_id

    old_display = old_start_dt.strftime("%A, %b %d at %I:%M %p") if old_start_dt else "Previous time"
    new_display = new_event.start_datetime.strftime("%A, %b %d at %I:%M %p")

    card = (
        f"🔄 *Reschedule Event*\n\n"
        f"📌 *{_escape_md(event_title)}*\n"
        f"📆 {_escape_md(old_display)} ➡️ {_escape_md(new_display)}\n"
        f"⏱️ {duration} min"
    )
    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🔄 Reschedule", callback_data=f"reschedconf_{conf_id}"),
            InlineKeyboardButton("❌ Cancel", callback_data=f"rescheddis_{conf_id}"),
        ]
    ])
    await thinking_msg.edit_text(card, reply_markup=keyboard, parse_mode="MarkdownV2")


@authorized_only
async def handle_confirm_reschedule(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle confirmed rescheduling of an event."""
    query = update.callback_query
    await query.answer()

    conf_id = query.data.replace("reschedconf_", "")
    google_event_id = context.user_data.get(f"resched_target_{conf_id}")

    conf = get_pending_confirmation(conf_id)
    if not conf or not resolve_confirmation(conf_id, "confirmed"):
        await _safe_edit_message_text(query, "⚠️ This reschedule request has already been processed\\.", parse_mode="MarkdownV2")
        return

    try:
        await query.edit_message_text("⏳ Updating event in Google Calendar\\.\\.\\.", parse_mode="MarkdownV2")
    except Exception:
        pass

    try:
        new_event = validate_and_build_event(
            title=conf["title"],
            description=conf["description"],
            start_date=datetime.fromisoformat(conf["start_time"]).strftime("%Y-%m-%d"),
            start_time=datetime.fromisoformat(conf["start_time"]).strftime("%H:%M"),
            end_date=datetime.fromisoformat(conf["end_time"]).strftime("%Y-%m-%d"),
            end_time=datetime.fromisoformat(conf["end_time"]).strftime("%H:%M"),
            reminder_minutes=DEFAULT_REMINDER_TIERS,
        )
    except EventValidationError as e:
        await _safe_edit_message_text(query, f"❌ {str(e)}", parse_mode=None)
        return

    try:
        updated_google = await asyncio.to_thread(update_calendar_event, google_event_id, new_event)
    except CalendarAPIError as e:
        await _safe_edit_message_text(query, f"❌ {str(e)}", parse_mode=None)
        return

    update_event_times(google_event_id, new_event.start_datetime.isoformat(), new_event.end_datetime.isoformat())
    cancel_reminders_for_google_event(google_event_id)

    local_event = get_event_by_google_id(google_event_id)
    local_event_id = local_event["id"] if local_event else save_event(
        google_event_id=google_event_id,
        title=new_event.title,
        start_time=new_event.start_datetime.isoformat(),
        end_time=new_event.end_datetime.isoformat(),
        telegram_user_id=AUTHORIZED_USER_ID,
        timezone=new_event.timezone,
    )

    schedule_reminders_for_event(
        event_id=local_event_id,
        google_event_id=google_event_id,
        telegram_user_id=AUTHORIZED_USER_ID,
        event_title=new_event.title,
        event_start_time=new_event.start_datetime,
        reminder_minutes_list=DEFAULT_REMINDER_TIERS,
    )

    new_time_str = new_event.start_datetime.strftime("%A, %B %d at %I:%M %p")
    event_link = updated_google.get("htmlLink", "")

    success_msg = (
        f"🔄 *Event Rescheduled*\n\n"
        f"📌 *{_escape_md(new_event.title)}*\n"
        f"📆 {_escape_md(new_time_str)}"
    )
    if event_link:
        success_msg += f"\n\n🔗 [Open in Google Calendar]({_escape_url(event_link)})"

    await _safe_edit_message_text(query, success_msg, parse_mode="MarkdownV2", disable_web_page_preview=True)

    await _safe_send_message(
        context.bot,
        chat_id=query.message.chat_id,
        text=(
            f"🔄 *Event Rescheduled*\n\n"
            f"📌 *{_escape_md(new_event.title)}*\n"
            f"📆 {_escape_md(new_time_str)}"
        ),
        parse_mode="MarkdownV2",
    )


@authorized_only
async def handle_cancel_reschedule(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle cancellation of reschedule."""
    query = update.callback_query
    await query.answer()
    conf_id = query.data.replace("rescheddis_", "")
    resolve_confirmation(conf_id, "cancelled")
    await _safe_edit_message_text(query, "❌ Cancelled", parse_mode="MarkdownV2")


# ────────────────────────────────────────────────────────────
# Conflict detection helpers & callbacks
# ────────────────────────────────────────────────────────────

def _build_conflict_card(
    event: ValidatedEvent,
    confirmation_id: str,
    conflicting_events: list[dict],
    suggested_start: Optional[datetime],
) -> tuple[str, InlineKeyboardMarkup]:
    """Build the conflict warning message and keyboard buttons."""
    # Describe each conflicting event
    conflict_lines = []
    for ce in conflicting_events[:3]:  # Show at most 3
        ce_title = ce.get("summary", "Untitled Event")
        ce_start_raw = ce.get("start", {}).get("dateTime") or ce.get("start", {}).get("date")
        ce_end_raw = ce.get("end", {}).get("dateTime") or ce.get("end", {}).get("date")
        if ce_start_raw and "T" in ce_start_raw:
            ce_s = datetime.fromisoformat(ce_start_raw)
            ce_e = datetime.fromisoformat(ce_end_raw) if ce_end_raw and "T" in ce_end_raw else ce_s
            time_range = f"{ce_s.strftime('%I:%M %p')} ➡️ {ce_e.strftime('%I:%M %p')}"
        else:
            time_range = "All day"
        conflict_lines.append(
            f"  📌 *{_escape_md(ce_title)}*\n     🕐 {_escape_md(time_range)}"
        )

    conflict_section = "\n\n".join(conflict_lines)

    card = (
        f"⚠️ *Scheduling Conflict Detected\\!*\n\n"
        f"📌 *{_escape_md(event.title)}*\n"
        f"📆 {_escape_md(event.start_datetime.strftime('%A, %b %d, %Y'))}\n"
        f"🕐 {_escape_md(event.start_datetime.strftime('%I:%M %p'))} ➡️ "
        f"{_escape_md(event.end_datetime.strftime('%I:%M %p'))}\n\n"
        f"⚡ *Conflicts:*\n{conflict_section}\n\n"
    )

    buttons = []

    if suggested_start:
        event_duration = event.end_datetime - event.start_datetime
        suggested_end = suggested_start + event_duration
        suggestion_str = suggested_start.strftime("%I:%M %p")
        card += (
            f"💡 *Suggested alternative:* {_escape_md(suggestion_str)} ➡️ "
            f"{_escape_md(suggested_end.strftime('%I:%M %p'))}\n"
        )
        buttons.append(
            InlineKeyboardButton(
                f"💡 Schedule at {suggestion_str}",
                callback_data=f"conflalt_{confirmation_id}",
            )
        )
    else:
        card += "💡 _No free alternative slots found in the next 8 hours\\._\n"

    buttons_row_1 = [
        InlineKeyboardButton("✅ Schedule Anyway", callback_data=f"conflovr_{confirmation_id}"),
        InlineKeyboardButton("❌ Cancel", callback_data=f"cancel_{confirmation_id}"),
    ]

    keyboard_rows = []
    if buttons:
        keyboard_rows.append(buttons)  # Suggested alt button on its own row
    keyboard_rows.append(buttons_row_1)

    return card, InlineKeyboardMarkup(keyboard_rows)


async def _check_conflicts_and_show(
    event: ValidatedEvent,
    confirmation_id: str,
    message_target,
    context: ContextTypes.DEFAULT_TYPE,
    is_edit: bool = False,
) -> bool:
    """
    Check for conflicts and show a conflict card if any are found.

    Args:
        event: The validated event to check.
        confirmation_id: The pending confirmation ID.
        message_target: A Telegram message or callback query to edit.
        context: Telegram context for user_data.
        is_edit: If True, call edit_text; otherwise reply_text.

    Returns:
        True if conflicts were found (conflict card was shown),
        False if no conflicts (caller should show normal confirmation).
    """
    try:
        busy = await asyncio.to_thread(check_conflicts, event.start_datetime, event.end_datetime)
    except CalendarAPIError:
        # If freebusy check fails, proceed without conflict detection
        logger.warning("Freebusy API check failed; skipping conflict detection.")
        return False

    if not busy:
        return False

    # Get event details for a richer conflict card
    conflicting_events = await asyncio.to_thread(
        get_conflicting_event_details,
        event.start_datetime,
        event.end_datetime,
    )
    if not conflicting_events:
        # freebusy says busy but no matching events found — likely owned by
        # another calendar; still warn the user.
        conflicting_events = []

    # Suggest an alternative time
    suggested = await asyncio.to_thread(
        suggest_alternative_time,
        event.start_datetime,
        event.end_datetime,
    )

    # Store the suggested time in user_data
    if suggested:
        event_duration = event.end_datetime - event.start_datetime
        context.user_data[f"conflict_alt_start_{confirmation_id}"] = suggested.isoformat()
        context.user_data[f"conflict_alt_end_{confirmation_id}"] = (suggested + event_duration).isoformat()

    card, keyboard = _build_conflict_card(event, confirmation_id, conflicting_events, suggested)

    if is_edit:
        await message_target.edit_text(card, reply_markup=keyboard, parse_mode="MarkdownV2")
    else:
        await message_target.reply_text(card, reply_markup=keyboard, parse_mode="MarkdownV2")

    return True


@authorized_only
async def handle_conflict_override(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle 'Schedule Anyway' — proceed with the original time despite conflict."""
    query = update.callback_query
    await query.answer()

    confirmation_id = query.data.replace("conflovr_", "")

    # Reuse the existing confirm logic
    was_resolved = resolve_confirmation(confirmation_id, "confirmed")
    if not was_resolved:
        await _safe_edit_message_text(query, "⚠️ This event has already been processed\\.", parse_mode="MarkdownV2")
        return

    try:
        await query.edit_message_text("⏳ Adding event to Google Calendar\\.\\.\\.", parse_mode="MarkdownV2")
    except Exception:
        pass

    conf = get_pending_confirmation(confirmation_id)
    if not conf:
        await _safe_edit_message_text(query, "❌ Confirmation not found\\.", parse_mode="MarkdownV2")
        return

    stored_rem = conf.get("reminder_minutes")
    reminder_val = None
    if stored_rem is not None:
        if isinstance(stored_rem, int):
            reminder_val = stored_rem
        elif isinstance(stored_rem, str):
            try:
                reminder_val = json.loads(stored_rem)
            except Exception:
                try:
                    reminder_val = int(stored_rem)
                except Exception:
                    reminder_val = None
        elif isinstance(stored_rem, list):
            reminder_val = stored_rem

    try:
        event = validate_and_build_event(
            title=conf["title"],
            description=conf["description"],
            start_date=datetime.fromisoformat(conf["start_time"]).strftime("%Y-%m-%d"),
            start_time=datetime.fromisoformat(conf["start_time"]).strftime("%H:%M"),
            end_date=datetime.fromisoformat(conf["end_time"]).strftime("%Y-%m-%d"),
            end_time=datetime.fromisoformat(conf["end_time"]).strftime("%H:%M"),
            reminder_minutes=reminder_val,
        )
    except EventValidationError as e:
        await _safe_edit_message_text(query, f"❌ Validation error: {str(e)}", parse_mode=None)
        return

    try:
        google_event = await asyncio.to_thread(create_calendar_event, event)
    except (CalendarAuthError, CalendarAPIError) as e:
        resolve_confirmation(confirmation_id, "pending")
        await _safe_edit_message_text(query, f"❌ {str(e)}", parse_mode=None)
        return

    google_event_id = google_event.get("id", "")
    event_link = google_event.get("htmlLink", "")

    local_event_id = save_event(
        google_event_id=google_event_id,
        title=event.title,
        start_time=event.start_datetime.isoformat(),
        end_time=event.end_datetime.isoformat(),
        telegram_user_id=AUTHORIZED_USER_ID,
        timezone=event.timezone,
        description=event.description,
    )

    reminder_info = ""
    reminder_tiers = event.get_reminder_minutes_list()
    if reminder_tiers:
        scheduled_ids = schedule_reminders_for_event(
            event_id=local_event_id,
            google_event_id=google_event_id,
            telegram_user_id=AUTHORIZED_USER_ID,
            event_title=event.title,
            event_start_time=event.start_datetime,
            reminder_minutes_list=reminder_tiers,
        )
        if scheduled_ids:
            from event_parser import _format_reminder_time
            if len(reminder_tiers) == 1:
                reminder_info = f"🔔 Reminder set: {_format_reminder_time(reminder_tiers[0])}"
            else:
                labels = [_format_reminder_time(m) for m in reminder_tiers]
                reminder_info = f"🔔 Reminders set: {', '.join(labels)}"

    success_msg = (
        f"✅ *Added to Calendar \\(Conflict Override\\)*\n\n"
        f"📌 *{_escape_md(event.title)}*\n"
        f"📆 {_escape_md(event.start_datetime.strftime('%A, %B %d, %Y'))}\n"
        f"🕐 {_escape_md(event.start_datetime.strftime('%I:%M %p'))} ➡️ "
        f"{_escape_md(event.end_datetime.strftime('%I:%M %p'))}"
    )
    if reminder_info:
        success_msg += f"\n{_escape_md(reminder_info)}"
    if event_link:
        success_msg += f"\n\n🔗 [Open in Google Calendar]({_escape_url(event_link)})"

    await _safe_edit_message_text(query, success_msg, parse_mode="MarkdownV2", disable_web_page_preview=True)

    notify_text = (
        f"✅ *Added to Calendar \\(Conflict Override\\)*\n\n"
        f"📌 *{_escape_md(event.title)}*\n"
        f"📆 {_escape_md(event.start_datetime.strftime('%A, %B %d, %Y'))}\n"
        f"🕐 {_escape_md(event.start_datetime.strftime('%I:%M %p'))} ➡️ "
        f"{_escape_md(event.end_datetime.strftime('%I:%M %p'))}"
    )
    if reminder_info:
        notify_text += f"\n{_escape_md(reminder_info)}"
    if event_link:
        notify_text += f"\n\n🔗 [Open in Google Calendar]({_escape_url(event_link)})"

    await _safe_send_message(
        context.bot,
        chat_id=query.message.chat_id,
        text=notify_text,
        parse_mode="MarkdownV2",
        disable_web_page_preview=True,
    )


@authorized_only
async def handle_conflict_alternative(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle 'Schedule at suggested time' — reschedule to the suggested alternative."""
    query = update.callback_query
    await query.answer()

    confirmation_id = query.data.replace("conflalt_", "")

    alt_start_iso = context.user_data.get(f"conflict_alt_start_{confirmation_id}")
    alt_end_iso = context.user_data.get(f"conflict_alt_end_{confirmation_id}")

    if not alt_start_iso or not alt_end_iso:
        await _safe_edit_message_text(query, "❌ Suggested time expired\\. Please try again\\.", parse_mode="MarkdownV2")
        return

    was_resolved = resolve_confirmation(confirmation_id, "confirmed")
    if not was_resolved:
        await _safe_edit_message_text(query, "⚠️ This event has already been processed\\.", parse_mode="MarkdownV2")
        return

    try:
        await query.edit_message_text("⏳ Rescheduling event in Google Calendar\\.\\.\\.", parse_mode="MarkdownV2")
    except Exception:
        pass

    conf = get_pending_confirmation(confirmation_id)
    if not conf:
        await _safe_edit_message_text(query, "❌ Confirmation not found\\.", parse_mode="MarkdownV2")
        return

    alt_start = datetime.fromisoformat(alt_start_iso)
    alt_end = datetime.fromisoformat(alt_end_iso)

    stored_rem = conf.get("reminder_minutes")
    reminder_val = None
    if stored_rem is not None:
        if isinstance(stored_rem, int):
            reminder_val = stored_rem
        elif isinstance(stored_rem, str):
            try:
                reminder_val = json.loads(stored_rem)
            except Exception:
                try:
                    reminder_val = int(stored_rem)
                except Exception:
                    reminder_val = None
        elif isinstance(stored_rem, list):
            reminder_val = stored_rem

    try:
        event = validate_and_build_event(
            title=conf["title"],
            description=conf["description"],
            start_date=alt_start.strftime("%Y-%m-%d"),
            start_time=alt_start.strftime("%H:%M"),
            end_date=alt_end.strftime("%Y-%m-%d"),
            end_time=alt_end.strftime("%H:%M"),
            reminder_minutes=reminder_val,
        )
    except EventValidationError as e:
        await _safe_edit_message_text(query, f"❌ Validation error: {str(e)}", parse_mode=None)
        return

    try:
        google_event = await asyncio.to_thread(create_calendar_event, event)
    except (CalendarAuthError, CalendarAPIError) as e:
        resolve_confirmation(confirmation_id, "pending")
        await _safe_edit_message_text(query, f"❌ {str(e)}", parse_mode=None)
        return

    google_event_id = google_event.get("id", "")
    event_link = google_event.get("htmlLink", "")

    local_event_id = save_event(
        google_event_id=google_event_id,
        title=event.title,
        start_time=event.start_datetime.isoformat(),
        end_time=event.end_datetime.isoformat(),
        telegram_user_id=AUTHORIZED_USER_ID,
        timezone=event.timezone,
        description=event.description,
    )

    reminder_info = ""
    reminder_tiers = event.get_reminder_minutes_list()
    if reminder_tiers:
        scheduled_ids = schedule_reminders_for_event(
            event_id=local_event_id,
            google_event_id=google_event_id,
            telegram_user_id=AUTHORIZED_USER_ID,
            event_title=event.title,
            event_start_time=event.start_datetime,
            reminder_minutes_list=reminder_tiers,
        )
        if scheduled_ids:
            from event_parser import _format_reminder_time
            if len(reminder_tiers) == 1:
                reminder_info = f"🔔 Reminder set: {_format_reminder_time(reminder_tiers[0])}"
            else:
                labels = [_format_reminder_time(m) for m in reminder_tiers]
                reminder_info = f"🔔 Reminders set: {', '.join(labels)}"

    success_msg = (
        f"✅ *Added to Calendar*\n\n"
        f"📌 *{_escape_md(event.title)}*\n"
        f"📆 {_escape_md(event.start_datetime.strftime('%A, %B %d, %Y'))}\n"
        f"🕐 {_escape_md(event.start_datetime.strftime('%I:%M %p'))} ➡️ "
        f"{_escape_md(event.end_datetime.strftime('%I:%M %p'))}"
    )
    if reminder_info:
        success_msg += f"\n{_escape_md(reminder_info)}"
    if event_link:
        success_msg += f"\n\n🔗 [Open in Google Calendar]({_escape_url(event_link)})"

    await _safe_edit_message_text(query, success_msg, parse_mode="MarkdownV2", disable_web_page_preview=True)

    notify_text = (
        f"✅ *Added to Calendar*\n\n"
        f"📌 *{_escape_md(event.title)}*\n"
        f"📆 {_escape_md(event.start_datetime.strftime('%A, %B %d, %Y'))}\n"
        f"🕐 {_escape_md(event.start_datetime.strftime('%I:%M %p'))} ➡️ "
        f"{_escape_md(event.end_datetime.strftime('%I:%M %p'))}"
    )
    if reminder_info:
        notify_text += f"\n{_escape_md(reminder_info)}"
    if event_link:
        notify_text += f"\n\n🔗 [Open in Google Calendar]({_escape_url(event_link)})"

    await _safe_send_message(
        context.bot,
        chat_id=query.message.chat_id,
        text=notify_text,
        parse_mode="MarkdownV2",
        disable_web_page_preview=True,
    )

    # Clean up user_data
    context.user_data.pop(f"conflict_alt_start_{confirmation_id}", None)
    context.user_data.pop(f"conflict_alt_end_{confirmation_id}", None)


# ────────────────────────────────────────────────────────────
# Utility functions
# ────────────────────────────────────────────────────────────

def _parse_time_input(text: str) -> Optional[str]:
    """
    Parse time input like '10:00', '3 PM', '14:30', '9:30 AM'
    into HH:MM (24-hour) format.
    """
    text = text.strip().upper()

    # Try HH:MM format directly
    for fmt in ("%H:%M", "%I:%M %p", "%I %p", "%I:%M%p", "%I%p"):
        try:
            dt = datetime.strptime(text, fmt)
            return dt.strftime("%H:%M")
        except ValueError:
            continue

    return None


def _parse_duration(text: str) -> Optional[int]:
    """
    Parse duration text like '1 hour', '30 minutes', '90 min', '1.5 hours'
    into minutes.
    """
    text = text.strip().lower()

    import re

    # Match patterns like "1 hour", "30 minutes", "1.5 hours", "90 min"
    patterns = [
        (r"(\d+(?:\.\d+)?)\s*(?:hours?|hrs?|h)", lambda m: int(float(m.group(1)) * 60)),
        (r"(\d+)\s*(?:minutes?|mins?|m(?:in)?)", lambda m: int(m.group(1))),
        (r"(\d+(?:\.\d+)?)\s*(?:hours?|hrs?|h)\s+(?:and\s+)?(\d+)\s*(?:minutes?|mins?|m)",
         lambda m: int(float(m.group(1)) * 60) + int(m.group(2))),
    ]

    # Try combined hours and minutes first
    combined = re.match(patterns[2][0], text)
    if combined:
        return patterns[2][1](combined)

    for pattern, extractor in patterns[:2]:
        match = re.match(pattern, text)
        if match:
            result = extractor(match)
            if result > 0:
                return result

    # Try plain number (assume minutes)
    try:
        val = int(text)
        if val > 0:
            return val
    except ValueError:
        pass

    return None


# ────────────────────────────────────────────────────────────
# Application setup and startup
# ────────────────────────────────────────────────────────────

async def post_init(application: Application) -> None:
    """Post-initialization hook: set up scheduler and reconcile reminders."""
    bot = application.bot

    # Initialize APScheduler
    scheduler = init_scheduler()
    set_bot(bot)

    # Reconcile pending reminders from DB
    await reconcile_reminders_on_startup()

    # Schedule daily morning briefing
    init_daily_briefing_job(AUTHORIZED_USER_ID)

    # Start the scheduler
    if not scheduler.running:
        scheduler.start()
        logger.info("APScheduler started.")


async def post_shutdown(application: Application) -> None:
    """Shutdown hook: stop the scheduler."""
    scheduler = get_scheduler()
    if scheduler.running:
        scheduler.shutdown(wait=False)
        logger.info("APScheduler shut down.")


async def global_error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Log unhandled exceptions and notify user gracefully."""
    logger.error("Unhandled exception processing update: %s", context.error, exc_info=context.error)
    if isinstance(update, Update) and update.effective_chat:
        try:
            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text="⚠️ An unexpected error occurred while processing your request. Please try again.",
            )
        except Exception:
            pass


def main() -> None:
    """Build and run the Telegram bot application."""
    # Initialize the database
    init_db()

    # Build the Telegram application
    app = (
        Application.builder()
        .token(TELEGRAM_BOT_TOKEN)
        .build()
    )

    app.add_error_handler(global_error_handler)

    # ── Guided event creation conversation handler ──
    guided_conv = ConversationHandler(
        entry_points=[CommandHandler("new", cmd_new)],
        states={
            STATE_TITLE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, guided_title),
            ],
            STATE_DATE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, guided_date),
            ],
            STATE_TIME: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, guided_time),
            ],
            STATE_DURATION: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, guided_duration),
            ],
            STATE_REMINDER: [
                CallbackQueryHandler(guided_reminder_callback, pattern=r"^guided_rem_"),
            ],
            STATE_CUSTOM_REMINDER: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, guided_custom_reminder),
            ],
        },
        fallbacks=[CommandHandler("cancel", cmd_cancel)],
        allow_reentry=True,
    )

    # ── Register handlers ──
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(guided_conv)
    app.add_handler(CommandHandler("briefing", cmd_briefing))
    app.add_handler(CommandHandler("upcoming", cmd_upcoming))
    app.add_handler(CommandHandler("today", cmd_today))
    app.add_handler(CommandHandler("reminders", cmd_reminders))
    app.add_handler(CommandHandler("tasks", cmd_tasks))
    app.add_handler(CommandHandler("testvoice", cmd_testvoice))
    app.add_handler(CommandHandler("delete", cmd_delete))
    app.add_handler(CommandHandler("cancel", cmd_cancel))

    # Inline button handlers for tasks and combined actions
    app.add_handler(CallbackQueryHandler(handle_confirm_task, pattern=r"^confirm_task_"))
    app.add_handler(CallbackQueryHandler(handle_cancel_task, pattern=r"^cancel_task_"))
    app.add_handler(CallbackQueryHandler(handle_confirm_task_done, pattern=r"^confirm_task_done_"))
    app.add_handler(CallbackQueryHandler(handle_confirm_task_delete, pattern=r"^confirm_task_del_"))
    app.add_handler(CallbackQueryHandler(handle_cancel_task_action, pattern=r"^cancel_task_action$"))
    app.add_handler(CallbackQueryHandler(handle_confirm_combined, pattern=r"^confirm_comb_"))
    app.add_handler(CallbackQueryHandler(handle_cancel_combined, pattern=r"^cancel_comb_"))

    # Inline button handlers for event confirmation/cancellation
    app.add_handler(CallbackQueryHandler(handle_confirm, pattern=r"^confirm_"))
    app.add_handler(CallbackQueryHandler(handle_cancel_event, pattern=r"^cancel_"))
    app.add_handler(CallbackQueryHandler(handle_confirm_delete, pattern=r"^deleteconf_"))
    app.add_handler(CallbackQueryHandler(handle_cancel_delete, pattern=r"^(deletedis_|cancel_del_menu)"))
    app.add_handler(CallbackQueryHandler(handle_confirm_reschedule, pattern=r"^reschedconf_"))
    app.add_handler(CallbackQueryHandler(handle_cancel_reschedule, pattern=r"^rescheddis_"))
    app.add_handler(CallbackQueryHandler(handle_conflict_override, pattern=r"^conflovr_"))
    app.add_handler(CallbackQueryHandler(handle_conflict_alternative, pattern=r"^conflalt_"))

    # Natural-language handler (catches all remaining text messages)
    app.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, handle_natural_language)
    )

    return app


async def async_main() -> None:
    """Async entry point — controls the full application lifecycle.

    Uses asyncio.run() instead of run_polling() for Python 3.14+
    compatibility (asyncio.get_event_loop() was removed).
    """
    app = main()

    logger.info("Starting T.I.M.E.S Calendar Assistant bot...")
    logger.info("Authorized user ID: %d", AUTHORIZED_USER_ID)

    async with app:
        await app.initialize()
        await post_init(app)
        await app.start()
        await app.updater.start_polling(allowed_updates=Update.ALL_TYPES)

        # Keep running until interrupted
        try:
            import asyncio
            stop_event = asyncio.Event()
            await stop_event.wait()
        except (KeyboardInterrupt, SystemExit):
            logger.info("Shutdown signal received.")
        finally:
            await app.updater.stop()
            await app.stop()
            await post_shutdown(app)
            await app.shutdown()


if __name__ == "__main__":
    import asyncio
    asyncio.run(async_main())
