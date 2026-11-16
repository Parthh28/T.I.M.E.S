"""
Gemini service module for the T.I.M.E.S Assistant.

Uses the google-genai SDK with structured (Pydantic) output to extract
structured Event and Task data and route user intents.
"""

import json
import logging
from datetime import datetime
from typing import Optional, Literal

from google import genai
from pydantic import BaseModel, Field

from config import GEMINI_API_KEY, GEMINI_MODEL, TIMEZONE, TIMEZONE_NAME

logger = logging.getLogger("calendar_bot.gemini")

# Initialize the Gemini client
client = genai.Client(api_key=GEMINI_API_KEY)


# ────────────────────────────────────────────────────────────
# Structured Pydantic Schemas
# ────────────────────────────────────────────────────────────

class ExtractedTaskData(BaseModel):
    """Structured task information for Notion."""
    title: Optional[str] = Field(None, description="Task title/name")
    description: Optional[str] = Field(None, description="Detailed notes, checklist, or context")
    due_date: Optional[str] = Field(None, description="Due date in YYYY-MM-DD format")
    due_time: Optional[str] = Field(None, description="Due time in HH:MM (24-hour format)")
    priority: Optional[str] = Field("Medium", description="Priority: 'High', 'Medium', or 'Low'")
    category: Optional[str] = Field("General", description="Category: 'Work', 'Personal', 'Study', 'Finance', 'Health', 'Errand', 'Urgent', or 'General'")
    status: Optional[str] = Field("To Do", description="Status: 'To Do', 'In Progress', or 'Done'")
    target_task_title: Optional[str] = Field(None, description="Existing task title to update, complete, or delete")
    reminder_minutes: Optional[int] = Field(None, description="Minutes before due date/time to send reminder")


class ExtractedEventData(BaseModel):
    """Structured calendar event information for Google Calendar."""
    title: Optional[str] = Field(None, description="Event title/summary")
    target_event_title: Optional[str] = Field(None, description="Title of existing event to delete or reschedule")
    description: Optional[str] = Field(None, description="Event description or notes")
    start_date: Optional[str] = Field(None, description="Start date in YYYY-MM-DD format (or new date for reschedule)")
    start_time: Optional[str] = Field(None, description="Start time in HH:MM (24-hour format, or new time for reschedule)")
    end_date: Optional[str] = Field(None, description="End date in YYYY-MM-DD format")
    end_time: Optional[str] = Field(None, description="End time in HH:MM (24-hour format)")
    duration_minutes: Optional[int] = Field(60, description="Duration in minutes if end time not specified")
    reminder_minutes: Optional[int] = Field(None, description="Reminder time in minutes before the event")


class UnifiedActionRequest(BaseModel):
    """Unified intent classification and structured extraction response."""
    intent: str = Field(
        "chat",
        description=(
            "Intent: 'create_event', 'delete_event', 'reschedule_event', 'list_upcoming', 'list_today', "
            "'create_task', 'list_tasks', 'update_task', 'complete_task', 'delete_task', "
            "'combined_action', 'daily_briefing', 'check_reminders', or 'chat'"
        ),
    )
    event: Optional[ExtractedEventData] = Field(None, description="Extracted event details if intent involves calendar event")
    task: Optional[ExtractedTaskData] = Field(None, description="Extracted task details if intent involves Notion task")

    needs_clarification: bool = Field(False, description="True if the request is ambiguous and needs clarification")
    clarification_message: Optional[str] = Field(None, description="Clarification question to ask the user")

    chat_response: Optional[str] = Field(None, description="Conversational reply if intent is 'chat'")
    requires_calendar_lookup: bool = Field(False, description="True if chat query needs inspecting calendar events/availability")
    requires_task_lookup: bool = Field(False, description="True if chat query needs inspecting Notion tasks")


class ExtractedEvent(BaseModel):
    """Legacy compatibility model for existing event code and tests."""
    intent: str = Field("create_event")
    title: Optional[str] = None
    target_event_title: Optional[str] = None
    description: Optional[str] = None
    start_date: Optional[str] = None
    start_time: Optional[str] = None
    end_date: Optional[str] = None
    end_time: Optional[str] = None
    duration_minutes: Optional[int] = None
    reminder_minutes: Optional[int] = None
    needs_clarification: bool = False
    clarification_message: Optional[str] = None
    is_event_request: bool = True
    chat_response: Optional[str] = None
    requires_calendar_lookup: bool = False


# ────────────────────────────────────────────────────────────
# Prompt Construction
# ────────────────────────────────────────────────────────────

def _build_unified_system_prompt(current_time: datetime) -> str:
    """Build the system prompt for classifying and extracting Events, Tasks, Combined actions, and Chat."""
    return f"""You are T.I.M.E.S, a personal AI operating system for Google Calendar (Events) and Notion (Tasks).

CURRENT CONTEXT:
- Date & Time: {current_time.strftime('%A, %B %d, %Y at %I:%M %p')}
- Day of week: {current_time.strftime('%A')}
- Timezone: {TIMEZONE_NAME}

INTENT CLASSIFICATION:
1. EVENT INTENTS (Google Calendar - time-bound meetings, appointments, events with specific start/end times):
   - 'create_event': Schedule an event (e.g. "Meeting with Bob tomorrow at 3 PM", "Dentist appointment next Monday 10 AM", "Webinar on Friday from 4 to 6 PM").
   - 'delete_event': Cancel/remove an event (e.g. "Cancel meeting tomorrow", "Delete dentist appointment").
   - 'reschedule_event': Move/postpone an event (e.g. "Move team sync to 4 PM", "Postpone AWS call to Friday 5 PM").
   - 'list_upcoming': Show upcoming calendar events (e.g. "What's coming up?", "Show upcoming meetings").
   - 'list_today': Show today's calendar schedule (e.g. "What do I have today?", "Today's schedule").

2. TASK INTENTS (Notion - to-dos, deliverables, assignments, items with deadlines, priorities, and categories):
   - 'create_task': Add a new to-do/task (e.g. "Add task submit tax report by Friday", "Remember to buy groceries tonight priority High", "Todo: write documentation category Work", "Task: prepare presentation").
   - 'list_tasks': Show or query tasks (e.g. "Show my tasks", "What are my high priority tasks?", "List pending work tasks", "What to-dos do I have?").
   - 'update_task': Change task properties (e.g. "Set tax report priority to High", "Change deadline of slides to tomorrow", "Update task buy milk to category Personal").
   - 'complete_task': Mark a task as completed/done (e.g. "Mark tax report as done", "Completed task buy groceries", "Finish presentation task").
   - 'delete_task': Remove a task from Notion (e.g. "Delete task grocery", "Remove presentation from my tasks").

3. COMBINED ACTION:
   - 'combined_action': The user request involves BOTH an event on Google Calendar AND a task in Notion in the same message (e.g., "Schedule client meeting tomorrow at 3 PM and add task to prepare proposal tonight", "Add dentist visit Friday at 10 AM and add a reminder to buy medicine to my tasks").
   In this case, populate BOTH the 'event' field AND the 'task' field!

4. GENERAL & SYSTEM INTENTS:
   - 'daily_briefing': Morning agenda & brief (e.g. "Give me my daily briefing", "Morning summary").
   - 'check_reminders': Check Telegram alerts (e.g. "When will you remind me?", "Show my reminders").
   - 'chat': Natural conversation, greetings, advice, or schedule/task availability questions (e.g. "Hi", "Who are you?", "Am I free tonight for gym?", "What should I work on today?").

RULES:
- Distinguish TASKS from EVENTS:
  - If it's a deliverable/to-do/checklist item (words like "todo", "task", "remember to", "buy", "finish", "write", "submit"), treat it as a TASK.
  - If it's a scheduled meeting, appointment, webinar, flight, call with someone at a specific time, treat it as an EVENT.
- For Tasks:
  - Extract 'title', 'due_date' (YYYY-MM-DD), 'due_time' (HH:MM 24-hr), 'priority' ('High', 'Medium', 'Low'), 'category' ('Work', 'Personal', 'Study', 'Finance', 'Health', 'Errand', 'Urgent', 'General'), and 'description'.
  - If priority is urgent, critical, or important -> 'High'. If minor, low -> 'Low'. Default -> 'Medium'.
  - If category is mentioned or implied -> extract it (e.g., taxes -> Finance, bug/code -> Work, gym/workout -> Health). Default -> 'General'.
  - For 'complete_task' and 'delete_task', extract the target task name into 'task.target_task_title' and 'task.title'.
- For Events:
  - Extract 'title', 'start_date' (YYYY-MM-DD), 'start_time' (HH:MM 24-hr), 'end_date', 'end_time', 'duration_minutes', 'description'.
  - Default duration: 60 minutes if end time not specified.
- For Ambiguous Requests:
  - If the user clearly wants to create an event or task but key info is missing/ambiguous (e.g. "Add a task" with no title, or "Schedule a meeting" with no date/time/title), set 'needs_clarification'=true and provide a short, direct question in 'clarification_message'. Do NOT use pleasantries, conversational filler, or AI slop.
- For 'chat':
  - If asking about free time or calendar availability ("am I free tonight?"), set 'requires_calendar_lookup'=true.
  - If asking about tasks/to-dos ("what should I work on?", "what tasks are due?"), set 'requires_task_lookup'=true.
  - Otherwise, provide a concise, direct response in 'chat_response'. No robotic filler, conversational pleasantries, or AI slop. Use emojis instead of plain symbols.
"""


# ────────────────────────────────────────────────────────────
# Core Extraction Functions
# ────────────────────────────────────────────────────────────

async def extract_action_from_text(text: str) -> UnifiedActionRequest:
    """
    Extract structured Event, Task, Combined action, or Chat intent using Gemini.
    """
    current_time = datetime.now(TIMEZONE)
    system_prompt = _build_unified_system_prompt(current_time)

    try:
        response = await client.aio.models.generate_content(
            model=GEMINI_MODEL,
            contents=f"{system_prompt}\n\nUser message: {text}",
            config={
                "response_mime_type": "application/json",
                "response_schema": UnifiedActionRequest,
                "thinking_config": {"thinking_budget": 0},
            },
        )

        if response.parsed:
            extracted = response.parsed
            logger.info("Gemini extracted unified action: intent=%s", extracted.intent)
            return extracted

        if response.text:
            data = json.loads(response.text)
            extracted = UnifiedActionRequest(**data)
            logger.info("Gemini extracted unified action (JSON fallback): intent=%s", extracted.intent)
            return extracted

        raise RuntimeError("Gemini returned an empty response.")

    except json.JSONDecodeError as e:
        logger.error("Failed to parse Gemini JSON response: %s", e)
        raise RuntimeError(
            "I couldn't understand that. Could you rephrase your request?"
        ) from e
    except Exception as e:
        logger.error("Gemini API error: %s", type(e).__name__)
        raise RuntimeError(
            "Sorry, I'm having trouble processing your request right now. "
            "Please try again in a moment."
        ) from e


async def extract_event_from_text(text: str) -> ExtractedEvent:
    """
    Backward-compatible wrapper returning ExtractedEvent for legacy handlers/tests.
    """
    action = await extract_action_from_text(text)

    # Convert UnifiedActionRequest to ExtractedEvent
    ev = ExtractedEvent(
        intent=action.intent,
        needs_clarification=action.needs_clarification,
        clarification_message=action.clarification_message,
        chat_response=action.chat_response,
        requires_calendar_lookup=action.requires_calendar_lookup,
        is_event_request=(action.intent == "create_event"),
    )

    if action.event:
        ev.title = action.event.title
        ev.target_event_title = action.event.target_event_title
        ev.description = action.event.description
        ev.start_date = action.event.start_date
        ev.start_time = action.event.start_time
        ev.end_date = action.event.end_date
        ev.end_time = action.event.end_time
        ev.duration_minutes = action.event.duration_minutes
        ev.reminder_minutes = action.event.reminder_minutes
    elif action.task:
        ev.title = action.task.title
        ev.target_event_title = action.task.target_task_title
        ev.description = action.task.description
        ev.start_date = action.task.due_date
        ev.start_time = action.task.due_time
        ev.reminder_minutes = action.task.reminder_minutes

    return ev


async def extract_event_from_url_content(
    url: str, page_content: str, user_text: str = ""
) -> ExtractedEvent:
    """
    Extract structured event data from scraped webpage content.
    """
    current_time = datetime.now(TIMEZONE)
    system_prompt = _build_unified_system_prompt(current_time)

    prompt = (
        f"{system_prompt}\n\n"
        f"The user shared an event link: {url}\n\n"
        f"Webpage content scraped from the event link:\n"
        f"\"\"\"\n{page_content}\n\"\"\"\n"
    )
    if user_text and user_text.strip() != url:
        prompt += f"\nUser's message accompanying the link: {user_text}\n"

    prompt += (
        "\nTASK: Extract the event title, start date (YYYY-MM-DD), start time (HH:MM 24-hr), "
        "end date, end time, duration, and description from the webpage content. "
        "Set intent='create_event'."
    )

    try:
        response = await client.aio.models.generate_content(
            model=GEMINI_MODEL,
            contents=prompt,
            config={
                "response_mime_type": "application/json",
                "response_schema": UnifiedActionRequest,
                "thinking_config": {"thinking_budget": 0},
            },
        )

        extracted = None
        if response.parsed:
            extracted = response.parsed
        elif response.text:
            data = json.loads(response.text)
            extracted = UnifiedActionRequest(**data)

        if not extracted or not extracted.event:
            raise RuntimeError("Gemini returned an empty event response from link.")

        # Convert to ExtractedEvent
        ev = ExtractedEvent(
            intent="create_event",
            title=extracted.event.title,
            description=extracted.event.description,
            start_date=extracted.event.start_date,
            start_time=extracted.event.start_time,
            end_date=extracted.event.end_date,
            end_time=extracted.event.end_time,
            duration_minutes=extracted.event.duration_minutes,
            is_event_request=True,
        )

        link_line = f"Event Link: {url}"
        if ev.description:
            if url not in ev.description:
                ev.description = f"{ev.description}\n\n{link_line}"
        else:
            ev.description = link_line

        return ev

    except Exception as e:
        logger.error("Failed to extract event from URL content: %s", e)
        raise RuntimeError(
            "I checked the link, but couldn't find event details like date and time on that webpage. "
            "Could you tell me the date and time manually?"
        ) from e


async def generate_schedule_chat_response(
    user_query: str, calendar_context: str, tasks_context: Optional[str] = None
) -> str:
    """
    Generate an intelligent conversational response synthesizing both calendar events
    and Notion tasks.
    """
    current_time = datetime.now(TIMEZONE)
    prompt = f"""You are T.I.M.E.S, a personal AI calendar and task assistant.
The user is asking a question or seeking advice about their schedule, free time, or to-dos.

CURRENT CONTEXT:
- Date & Time: {current_time.strftime('%A, %B %d, %Y at %I:%M %p')}
- Day of week: {current_time.strftime('%A')}
- Timezone: {TIMEZONE_NAME}

USER'S GOOGLE CALENDAR SCHEDULE:
{calendar_context if calendar_context.strip() else "No calendar events scheduled."}

USER'S NOTION TASKS:
{tasks_context if (tasks_context and tasks_context.strip()) else "No pending Notion tasks."}

USER QUERY:
"{user_query}"

INSTRUCTIONS:
- Analyze both the calendar schedule and Notion tasks to provide a direct, concise answer.
- Do NOT use robotic pleasantries, filler intros, or conversational AI slop (no "Sure!", "I would be happy to help", "Here is what I found", etc.).
- Use clean formatting with appropriate emojis instead of plain symbols (like bullet points or dashes).
- If asked about free time, state the exact free time blocks directly.
- If asked about tasks or priorities, highlight the urgent/high-priority tasks directly.
- Keep the response ultra-concise, clear, and straight to the point.
"""
    try:
        response = await client.aio.models.generate_content(
            model=GEMINI_MODEL,
            contents=prompt,
            config={
                "thinking_config": {"thinking_budget": 0},
            },
        )
        if response.text:
            return response.text.strip()
        return "I checked your schedule and tasks, but couldn't generate a detailed answer right now."
    except Exception as e:
        logger.error("Error generating schedule/tasks chat response: %s", e)
        return "I checked your schedule, but had trouble processing that question right now."
