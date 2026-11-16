"""
Unit tests for the Unified Intent Router and Pydantic schemas in gemini_service.
"""

from datetime import datetime
from gemini_service import (
    UnifiedActionRequest,
    ExtractedTaskData,
    ExtractedEventData,
    ExtractedEvent,
    _build_unified_system_prompt,
)
from config import TIMEZONE


def test_unified_action_request_task_creation():
    """Verify UnifiedActionRequest with a task payload."""
    req = UnifiedActionRequest(
        intent="create_task",
        task=ExtractedTaskData(
            title="Prepare presentation",
            priority="High",
            category="Work",
            due_date="2026-11-20",
            due_time="15:00",
        ),
    )
    assert req.intent == "create_task"
    assert req.task is not None
    assert req.task.title == "Prepare presentation"
    assert req.task.priority == "High"
    assert req.task.category == "Work"
    assert req.task.due_date == "2026-11-20"


def test_unified_action_request_combined():
    """Verify UnifiedActionRequest with combined action."""
    req = UnifiedActionRequest(
        intent="combined_action",
        event=ExtractedEventData(
            title="Sprint Planning",
            start_date="2026-11-15",
            start_time="10:00",
        ),
        task=ExtractedTaskData(
            title="Write sprint agenda",
            priority="Medium",
            category="Work",
        ),
    )
    assert req.intent == "combined_action"
    assert req.event is not None
    assert req.event.title == "Sprint Planning"
    assert req.task is not None
    assert req.task.title == "Write sprint agenda"


def test_unified_action_request_clarification():
    """Verify ambiguity detection in UnifiedActionRequest."""
    req = UnifiedActionRequest(
        intent="create_event",
        needs_clarification=True,
        clarification_message="What time does this meeting start?",
    )
    assert req.needs_clarification is True
    assert req.clarification_message == "What time does this meeting start?"


def test_unified_action_request_chat():
    """Verify chat intent with schedule lookup flag."""
    req = UnifiedActionRequest(
        intent="chat",
        chat_response="You have 2 meetings today.",
        requires_calendar_lookup=True,
        requires_task_lookup=True,
    )
    assert req.intent == "chat"
    assert req.requires_calendar_lookup is True
    assert req.requires_task_lookup is True


def test_unified_system_prompt_contains_rules():
    """Verify system prompt includes task, event, and combined instructions."""
    now = datetime.now(TIMEZONE)
    prompt = _build_unified_system_prompt(now)
    assert "create_task" in prompt
    assert "list_tasks" in prompt
    assert "combined_action" in prompt
    assert "create_event" in prompt
    assert "Notion" in prompt
    assert "Google Calendar" in prompt

