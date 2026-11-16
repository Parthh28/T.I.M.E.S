"""
Unit tests for the Notion Task Parser and Validator module.
"""

from datetime import datetime, date, time
import pytest

from task_parser import (
    validate_and_build_task,
    format_task_summary,
    format_task_item,
    TaskValidationError,
    ValidatedTask,
)


def test_validate_task_minimal():
    """Test validating a task with only a title."""
    task = validate_and_build_task(title="Finish weekly report")
    assert task.title == "Finish weekly report"
    assert task.priority == "Medium"
    assert task.category == "General"
    assert task.status == "Not started"
    assert task.due_date is None
    assert task.due_time is None
    assert task.description is None


def test_validate_task_full_fields():
    """Test validating a task with all fields provided."""
    task = validate_and_build_task(
        title="File income taxes",
        description="Gather Form 16 and investment proofs",
        due_date="2026-11-20",
        due_time="17:00",
        priority="High",
        category="Finance",
        status="Not started",
        reminder_minutes=[60, 0],
    )
    assert task.title == "File income taxes"
    assert task.description == "Gather Form 16 and investment proofs"
    assert task.due_date == "2026-11-20"
    assert task.due_time == "17:00"
    assert task.priority == "High"
    assert task.category == "Finance"
    assert task.reminder_minutes == [60, 0]

    due_dt = task.get_due_datetime()
    assert due_dt is not None
    assert due_dt.year == 2026
    assert due_dt.month == 11
    assert due_dt.day == 20
    assert due_dt.hour == 17


def test_validate_task_empty_title_raises():
    """Test that empty or whitespace title raises TaskValidationError."""
    with pytest.raises(TaskValidationError, match="Task title is required"):
        validate_and_build_task(title="")

    with pytest.raises(TaskValidationError, match="Task title is required"):
        validate_and_build_task(title="   ")


def test_validate_task_title_too_long():
    """Test that title over 200 characters raises TaskValidationError."""
    long_title = "A" * 205
    with pytest.raises(TaskValidationError, match="too long"):
        validate_and_build_task(title=long_title)


def test_validate_task_invalid_date_format():
    """Test invalid date format raises error."""
    with pytest.raises(TaskValidationError, match="Invalid due date format"):
        validate_and_build_task(title="Submit project", due_date="20-11-2026")


def test_validate_task_invalid_time_format():
    """Test invalid time format raises error."""
    with pytest.raises(TaskValidationError, match="Invalid due time format"):
        validate_and_build_task(title="Submit project", due_time="5 PM")


def test_priority_normalization():
    """Test priority aliases map cleanly to High, Medium, Low."""
    t_urgent = validate_and_build_task(title="Fix prod bug", priority="urgent")
    assert t_urgent.priority == "High"

    t_p1 = validate_and_build_task(title="Security patch", priority="p1")
    assert t_p1.priority == "High"

    t_low = validate_and_build_task(title="Clean desktop", priority="low")
    assert t_low.priority == "Low"

    t_unknown = validate_and_build_task(title="Random note", priority="whenever")
    assert t_unknown.priority == "Medium"


def test_category_normalization():
    """Test category strings are capitalized and mapped."""
    t_work = validate_and_build_task(title="Code review", category="work")
    assert t_work.category == "Work"

    t_health = validate_and_build_task(title="Morning jog", category="health")
    assert t_health.category == "Health"

    t_none = validate_and_build_task(title="Something")
    assert t_none.category == "General"


def test_format_task_summary():
    """Test markdown summary output for task."""
    task = validate_and_build_task(
        title="Prepare presentation",
        description="Include Q3 revenue numbers",
        due_date="2026-11-15",
        due_time="14:00",
        priority="High",
        category="Work",
    )
    summary = format_task_summary(task)
    assert "Prepare presentation" in summary
    assert "High" in summary
    assert "Work" in summary
    assert "Include Q3 revenue numbers" in summary


def test_format_task_item():
    """Test formatting a task dict for list views."""
    task_dict = {
        "title": "Buy groceries",
        "priority": "Medium",
        "category": "Personal",
        "due_date": "2026-11-12",
        "status": "Not started",
    }
    item = format_task_item(task_dict, index=1)
    assert "1\\." in item
    assert "Buy groceries" in item
    assert "Medium" in item
    assert "Personal" in item

