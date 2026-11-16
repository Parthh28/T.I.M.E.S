"""
Unit tests for the Notion service module.
"""

from unittest.mock import patch, MagicMock
import pytest

from task_parser import ValidatedTask
from notion_service import (
    _build_page_properties,
    _parse_notion_page,
    _get_headers,
    NotionConfigError,
    NotionAPIError,
)


def test_build_page_properties():
    """Test building Notion JSON payload from a ValidatedTask."""
    schema = {
        "title_prop": "Task Name",
        "status_prop": "Status",
        "status_type": "status",
        "priority_prop": "Priority",
        "category_prop": "Category",
        "due_prop": "Due Date",
        "notes_prop": "Notes",
    }
    task = ValidatedTask(
        title="Submit tax return",
        description="Check deductible expenses",
        due_date="2026-11-30",
        due_time="18:00",
        priority="High",
        category="Finance",
        status="To Do",
    )
    props = _build_page_properties(task, schema)
    assert props["Task Name"]["title"][0]["text"]["content"] == "Submit tax return"
    assert props["Status"]["status"]["name"] == "To Do"
    assert props["Priority"]["select"]["name"] == "High"
    assert props["Category"]["select"]["name"] == "Finance"
    assert props["Due Date"]["date"]["start"] == "2026-11-30T18:00:00"
    assert "Check deductible expenses" in props["Notes"]["rich_text"][0]["text"]["content"]


def test_build_page_properties_select_status():
    """Test building payload when database uses select property for status."""
    schema = {
        "title_prop": "Name",
        "status_prop": "Status",
        "status_type": "select",
        "priority_prop": "Priority",
        "category_prop": "Category",
        "due_prop": "Due Date",
        "notes_prop": "Notes",
    }
    task = ValidatedTask(
        title="Grocery shopping",
        description=None,
        due_date=None,
        due_time=None,
        priority="Low",
        category="Personal",
        status="Done",
    )
    props = _build_page_properties(task, schema)
    assert props["Name"]["title"][0]["text"]["content"] == "Grocery shopping"
    assert props["Status"]["select"]["name"] == "Done"
    assert props["Priority"]["select"]["name"] == "Low"


def test_parse_notion_page():
    """Test parsing a raw Notion API page object into a clean dictionary."""
    raw_page = {
        "id": "page-12345",
        "url": "https://notion.so/page-12345",
        "properties": {
            "Task Name": {
                "type": "title",
                "title": [{"plain_text": "Buy groceries"}],
            },
            "Status": {
                "type": "status",
                "status": {"name": "In Progress"},
            },
            "Priority": {
                "type": "select",
                "select": {"name": "High"},
            },
            "Category": {
                "type": "select",
                "select": {"name": "Personal"},
            },
            "Due Date": {
                "type": "date",
                "date": {"start": "2026-11-15T09:30:00"},
            },
            "Notes": {
                "type": "rich_text",
                "rich_text": [{"plain_text": "Milk, eggs, and bread"}],
            },
        },
    }
    parsed = _parse_notion_page(raw_page)
    assert parsed["id"] == "page-12345"
    assert parsed["title"] == "Buy groceries"
    assert parsed["status"] == "In Progress"
    assert parsed["priority"] == "High"
    assert parsed["category"] == "Personal"
    assert parsed["due_date"] == "2026-11-15"
    assert parsed["due_time"] == "09:30"
    assert parsed["description"] == "Milk, eggs, and bread"


def test_get_headers_missing_config():
    """Test _get_headers raises NotionConfigError when API key or DB ID is missing."""
    with patch("notion_service.NOTION_API_KEY", ""), \
         patch("notion_service.NOTION_DATABASE_ID", ""):
        with pytest.raises(NotionConfigError, match="Notion integration is not configured"):
            _get_headers()


def test_get_headers_valid_config():
    """Test _get_headers returns correct Authorization and Notion-Version headers."""
    with patch("notion_service.NOTION_API_KEY", "secret_test_token_123"), \
         patch("notion_service.NOTION_DATABASE_ID", "db_test_id_456"):
        headers = _get_headers()
        assert headers["Authorization"] == "Bearer secret_test_token_123"
        assert headers["Notion-Version"] == "2022-06-28"
        assert headers["Content-Type"] == "application/json"
