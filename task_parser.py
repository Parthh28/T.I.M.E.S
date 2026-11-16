"""
Task parser and validator module for Notion tasks.

Validates and normalizes task data extracted by Gemini before any Notion API operation.
Treats Gemini output as untrusted input.
"""

import logging
from datetime import datetime, date, time
from dataclasses import dataclass
from typing import Optional

from config import TIMEZONE, TIMEZONE_NAME
from event_parser import _escape_md

logger = logging.getLogger("calendar_bot.task_parser")

PRIORITY_MAP = {
    "high": "High",
    "p1": "High",
    "urgent": "High",
    "critical": "High",
    "medium": "Medium",
    "med": "Medium",
    "p2": "Medium",
    "normal": "Medium",
    "standard": "Medium",
    "low": "Low",
    "p3": "Low",
    "minor": "Low",
}

PRIORITY_ICONS = {
    "High": "🔴",
    "Medium": "🟡",
    "Low": "🟢",
}

CATEGORY_ICONS = {
    "Work": "💼",
    "Personal": "👤",
    "Study": "📚",
    "Health": "💪",
    "Fitness": "🏃",
    "Finance": "💰",
    "Errand": "🛒",
    "Urgent": "⚡",
    "General": "🏷️",
}


@dataclass
class ValidatedTask:
    """A fully validated and normalized task ready for Notion creation or update."""
    title: str
    description: Optional[str]
    due_date: Optional[str]        # YYYY-MM-DD
    due_time: Optional[str]        # HH:MM
    priority: str                  # High, Medium, Low
    category: str                  # Work, Personal, etc.
    status: str = "Not started"    # Not started, In progress, Done
    reminder_minutes: Optional[int | list[int]] = None

    def get_due_datetime(self) -> Optional[datetime]:
        """Return timezone-aware datetime if due date is present."""
        if not self.due_date:
            return None
        parsed_date = datetime.strptime(self.due_date, "%Y-%m-%d").date()
        if self.due_time:
            parsed_time = datetime.strptime(self.due_time, "%H:%M").time()
        else:
            # Default to 09:00 AM on due date if time not specified
            parsed_time = time(9, 0)
        dt = datetime.combine(parsed_date, parsed_time)
        return TIMEZONE.localize(dt)


class TaskValidationError(Exception):
    """Raised when task data fails validation with a user-friendly message."""
    pass


def validate_and_build_task(
    title: Optional[str] = None,
    description: Optional[str] = None,
    due_date: Optional[str] = None,
    due_time: Optional[str] = None,
    priority: Optional[str] = None,
    category: Optional[str] = None,
    status: Optional[str] = None,
    reminder_minutes: Optional[int | list[int]] = None,
) -> ValidatedTask:
    """
    Validate all task fields and produce a ValidatedTask.

    Raises TaskValidationError with a user-friendly message for any problem.
    """
    # --- Title ---
    if not title or not title.strip():
        raise TaskValidationError("Task title is required. What is the task?")
    title = title.strip()
    if len(title) > 200:
        raise TaskValidationError("Task title is too long. Please keep it under 200 characters.")

    # --- Description ---
    if description:
        description = description.strip()
        if len(description) > 2000:
            description = description[:2000]

    # --- Due Date ---
    normalized_due_date = None
    if due_date and due_date.strip():
        due_date_clean = due_date.strip()
        try:
            parsed_date = datetime.strptime(due_date_clean, "%Y-%m-%d").date()
            normalized_due_date = parsed_date.strftime("%Y-%m-%d")
        except ValueError:
            raise TaskValidationError(
                f"Invalid due date format: '{due_date}'. Expected YYYY-MM-DD."
            )

    # --- Due Time ---
    normalized_due_time = None
    if due_time and due_time.strip():
        due_time_clean = due_time.strip()
        try:
            parsed_time = datetime.strptime(due_time_clean, "%H:%M").time()
            normalized_due_time = parsed_time.strftime("%H:%M")
        except ValueError:
            raise TaskValidationError(
                f"Invalid due time format: '{due_time}'. Expected HH:MM (24-hour)."
            )

    # --- Priority ---
    normalized_priority = "Medium"
    if priority and priority.strip():
        p_lower = priority.strip().lower()
        normalized_priority = PRIORITY_MAP.get(p_lower, "Medium")

    # --- Category ---
    normalized_category = "General"
    if category and category.strip():
        cat_clean = category.strip().capitalize()
        # Common canonical tags
        for canonical in ["Work", "Personal", "Study", "Health", "Fitness", "Finance", "Errand", "Urgent"]:
            if canonical.lower() == cat_clean.lower():
                cat_clean = canonical
                break
        normalized_category = cat_clean[:50]

    # --- Status ---
    normalized_status = "Not started"
    if status and status.strip():
        s_lower = status.strip().lower()
        if "done" in s_lower or "complete" in s_lower:
            normalized_status = "Done"
        elif "progress" in s_lower:
            normalized_status = "In progress"
        else:
            normalized_status = "Not started"

    return ValidatedTask(
        title=title,
        description=description,
        due_date=normalized_due_date,
        due_time=normalized_due_time,
        priority=normalized_priority,
        category=normalized_category,
        status=normalized_status,
        reminder_minutes=reminder_minutes,
    )


def format_task_summary(task: ValidatedTask) -> str:
    """Format a validated task into a clean, human-friendly summary with icons."""
    p_icon = PRIORITY_ICONS.get(task.priority, "🟡")
    c_icon = CATEGORY_ICONS.get(task.category, "📌")

    lines = [
        f"📋 *{_escape_md(task.title)}*",
        f"{p_icon} *Priority:* {_escape_md(task.priority)}",
        f"{c_icon} *Category:* {_escape_md(task.category)}",
    ]

    if task.due_date:
        try:
            dt = datetime.strptime(task.due_date, "%Y-%m-%d").date()
            date_str = dt.strftime("%A, %B %d, %Y")
            if task.due_time:
                tm = datetime.strptime(task.due_time, "%H:%M").time()
                time_str = tm.strftime("%I:%M %p").lstrip("0")
                lines.append(f"⏰ *Due:* {_escape_md(date_str)} at {_escape_md(time_str)}")
            else:
                lines.append(f"📅 *Due:* {_escape_md(date_str)}")
        except Exception:
            lines.append(f"📅 *Due:* {_escape_md(task.due_date)}")
    else:
        lines.append("📅 *Due:* No deadline set")

    if task.description:
        lines.append(f"📝 *Notes:* {_escape_md(task.description)}")

    return "\n".join(lines)


def format_task_item(task: dict, index: Optional[int] = None) -> str:
    """Format an existing Notion task for listing."""
    title = task.get("title", "Untitled Task")
    priority = task.get("priority", "Medium")
    category = task.get("category", "General")
    due_date = task.get("due_date")
    due_time = task.get("due_time")
    status = task.get("status", "To Do")

    p_icon = PRIORITY_ICONS.get(priority, "🟡")
    c_icon = CATEGORY_ICONS.get(category, "🏷️")
    prefix = f"{index}\\. " if index is not None else ""

    status_icon = "✅" if status == "Done" else ("⏳" if status == "In progress" else "⭕")

    text = f"{prefix}{status_icon} *{_escape_md(title)}*  {p_icon} `{_escape_md(priority)}`  {c_icon} `{_escape_md(category)}`"
    if due_date:
        if due_time:
            text += f"\n    📅 Due: {_escape_md(due_date)} {_escape_md(due_time)}"
        else:
            text += f"\n    📅 Due: {_escape_md(due_date)}"
    return text

