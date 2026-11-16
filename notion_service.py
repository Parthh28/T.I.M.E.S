"""
Notion service module for the T.I.M.E.S Assistant.

Provides asynchronous integration with Notion API (version 2022-06-28) to:
- Create new tasks with title, description, due date, priority, and category
- Query/list tasks by status, category, or priority
- Update task properties
- Complete tasks (mark as Done)
- Delete/archive tasks
- Search tasks by title
"""

import logging
from typing import Optional
import httpx

from config import NOTION_API_KEY, NOTION_DATABASE_ID, TIMEZONE
from task_parser import ValidatedTask

logger = logging.getLogger("calendar_bot.notion")

NOTION_API_BASE = "https://api.notion.com/v1"
NOTION_VERSION = "2022-06-28"

# Cached database properties schema mapping
_CACHED_SCHEMA: Optional[dict] = None


class NotionConfigError(Exception):
    """Raised when Notion API key or Database ID is missing or misconfigured."""
    pass


class NotionAPIError(Exception):
    """Raised when Notion API operation fails."""
    pass


def _get_headers() -> dict:
    """Return standard headers for Notion API requests."""
    if not NOTION_API_KEY or not NOTION_DATABASE_ID:
        raise NotionConfigError(
            "Notion integration is not configured. Please add NOTION_API_KEY and "
            "NOTION_DATABASE_ID to your .env file."
        )
    return {
        "Authorization": f"Bearer {NOTION_API_KEY.strip()}",
        "Notion-Version": NOTION_VERSION,
        "Content-Type": "application/json",
    }


async def _inspect_database_schema(client: httpx.AsyncClient) -> dict:
    """
    Fetch the Notion database schema and map known property names and types.
    Caches the schema in memory to avoid repeated lookups.
    """
    global _CACHED_SCHEMA
    if _CACHED_SCHEMA is not None:
        return _CACHED_SCHEMA

    headers = _get_headers()
    url = f"{NOTION_API_BASE}/databases/{NOTION_DATABASE_ID.strip()}"
    try:
        resp = await client.get(url, headers=headers)
        if resp.status_code == 401 or resp.status_code == 403:
            raise NotionAPIError(
                "Notion authentication failed. Make sure your NOTION_API_KEY is valid "
                "and that the integration is connected to your Notion database."
            )
        if resp.status_code == 404:
            raise NotionAPIError(
                f"Notion database '{NOTION_DATABASE_ID}' not found. "
                "Check that the ID is correct and shared with the integration."
            )
        if resp.status_code >= 400:
            raise NotionAPIError(f"Notion API error {resp.status_code}: {resp.text}")

        data = resp.json()
        properties = data.get("properties", {})

        mapping = {
            "title_prop": "Name",
            "status_prop": None,
            "status_type": "status",  # 'status' or 'select'
            "status_options": [],
            "priority_prop": None,
            "priority_options": [],
            "category_prop": None,
            "category_type": "select",  # 'select' or 'multi_select'
            "category_options": [],
            "due_prop": None,
            "notes_prop": None,
        }

        for prop_name, prop_data in properties.items():
            p_type = prop_data.get("type")
            name_lower = prop_name.lower()

            if p_type == "title":
                mapping["title_prop"] = prop_name
            elif p_type in ("status", "select") and ("status" in name_lower or "state" in name_lower):
                mapping["status_prop"] = prop_name
                mapping["status_type"] = p_type
                opts = prop_data.get(p_type, {}).get("options", [])
                mapping["status_options"] = [o.get("name") for o in opts if o.get("name")]
            elif p_type == "select" and ("priority" in name_lower or "urgency" in name_lower):
                mapping["priority_prop"] = prop_name
                opts = prop_data.get("select", {}).get("options", [])
                mapping["priority_options"] = [o.get("name") for o in opts if o.get("name")]
            elif p_type in ("select", "multi_select") and ("category" in name_lower or "tag" in name_lower):
                mapping["category_prop"] = prop_name
                mapping["category_type"] = p_type
                opts = prop_data.get(p_type, {}).get("options", [])
                mapping["category_options"] = [o.get("name") for o in opts if o.get("name")]
            elif p_type in ("select", "multi_select") and "type" in name_lower and not mapping["category_prop"]:
                # "Task type" only wins if no better category column was found
                mapping["category_prop"] = prop_name
                mapping["category_type"] = p_type
                opts = prop_data.get(p_type, {}).get("options", [])
                mapping["category_options"] = [o.get("name") for o in opts if o.get("name")]
            elif p_type == "date" and ("due" in name_lower or "date" in name_lower or "deadline" in name_lower):
                mapping["due_prop"] = prop_name
            elif p_type == "rich_text" and ("note" in name_lower or "desc" in name_lower or "detail" in name_lower):
                mapping["notes_prop"] = prop_name

        # Fallbacks if specific names weren't matched (verify property type before assigning)
        if not mapping["status_prop"] and "Status" in properties:
            st = properties["Status"].get("type")
            if st in ("status", "select"):
                mapping["status_prop"] = "Status"
                mapping["status_type"] = st
                opts = properties["Status"].get(st, {}).get("options", [])
                mapping["status_options"] = [o.get("name") for o in opts if o.get("name")]

        if not mapping["priority_prop"] and "Priority" in properties:
            if properties["Priority"].get("type") == "select":
                mapping["priority_prop"] = "Priority"
                opts = properties["Priority"].get("select", {}).get("options", [])
                mapping["priority_options"] = [o.get("name") for o in opts if o.get("name")]

        if not mapping["category_prop"] and "Category" in properties:
            ct = properties["Category"].get("type")
            if ct in ("select", "multi_select"):
                mapping["category_prop"] = "Category"
                mapping["category_type"] = ct
                opts = properties["Category"].get(ct, {}).get("options", [])
                mapping["category_options"] = [o.get("name") for o in opts if o.get("name")]

        if not mapping["due_prop"] and "Due Date" in properties:
            if properties["Due Date"].get("type") == "date":
                mapping["due_prop"] = "Due Date"

        if not mapping["notes_prop"] and "Notes" in properties:
            if properties["Notes"].get("type") == "rich_text":
                mapping["notes_prop"] = "Notes"

        _CACHED_SCHEMA = mapping
        logger.info("Discovered Notion database schema mapping: %s", mapping)
        return mapping

    except (NotionConfigError, NotionAPIError):
        raise
    except Exception as e:
        logger.error("Failed to fetch Notion database schema: %s", e)
        # Default fallback mapping
        return {
            "title_prop": "Name",
            "status_prop": "Status",
            "status_type": "status",
            "status_options": ["Not started", "In progress", "Done"],
            "priority_prop": "Priority",
            "priority_options": ["Low", "Medium", "High", "Urgent"],
            "category_prop": "Category",
            "category_type": "select",
            "category_options": [],
            "due_prop": "Due Date",
            "notes_prop": "Notes",
        }


def _match_option(value: str, allowed_options: list[str]) -> str:
    """Find a case-insensitive match for value in allowed_options, or return value."""
    if not allowed_options or not value:
        return value
    v_lower = value.strip().lower()
    for opt in allowed_options:
        if opt.lower() == v_lower:
            return opt
    # Match ignoring spaces, hyphens, underscores
    v_compact = v_lower.replace(" ", "").replace("-", "").replace("_", "")
    for opt in allowed_options:
        if opt.lower().replace(" ", "").replace("-", "").replace("_", "") == v_compact:
            return opt
    return value


def _build_page_properties(task: ValidatedTask, schema: dict) -> dict:
    """Build Notion page property payload based on discovered database schema."""
    title_key = schema.get("title_prop") or "Name"
    props = {
        title_key: {
            "title": [{"type": "text", "text": {"content": task.title}}]
        }
    }

    # Status - only set if status property exists in schema
    status_key = schema.get("status_prop")
    if status_key and task.status:
        status_val = _match_option(task.status, schema.get("status_options", []))
        status_type = schema.get("status_type", "status")
        if status_type == "status":
            props[status_key] = {"status": {"name": status_val}}
        else:
            props[status_key] = {"select": {"name": status_val}}

    # Priority - only set if priority property exists in schema
    priority_key = schema.get("priority_prop")
    if priority_key and task.priority:
        priority_val = _match_option(task.priority, schema.get("priority_options", []))
        props[priority_key] = {"select": {"name": priority_val}}

    # Category - only set if category property exists in schema
    category_key = schema.get("category_prop")
    if category_key and task.category:
        cat_type = schema.get("category_type", "select")
        cat_options = schema.get("category_options", [])
        cat_val = _match_option(task.category, cat_options)
        # If options are restricted and task.category is default 'General' not in options, don't force it
        if cat_options and cat_val not in cat_options and task.category.lower() == "general":
            pass
        else:
            if cat_type == "multi_select":
                props[category_key] = {"multi_select": [{"name": cat_val}]}
            else:
                props[category_key] = {"select": {"name": cat_val}}

    # Due Date - only set if due property exists in schema
    due_key = schema.get("due_prop")
    if due_key and task.due_date:
        if task.due_time:
            due_iso = f"{task.due_date}T{task.due_time}:00"
        else:
            due_iso = task.due_date
        props[due_key] = {"date": {"start": due_iso}}

    # Notes / Description - only set if notes property exists in schema
    notes_key = schema.get("notes_prop")
    if notes_key and task.description:
        props[notes_key] = {
            "rich_text": [{"type": "text", "text": {"content": task.description[:2000]}}]
        }

    return props


def _parse_notion_page(page: dict, schema: Optional[dict] = None) -> dict:
    """Convert raw Notion page object into a clean dictionary."""
    props = page.get("properties", {})
    page_id = page.get("id", "")
    page_url = page.get("url", "")

    # Extract title
    title = "Untitled Task"
    for p_name, p_val in props.items():
        if p_val.get("type") == "title":
            title_parts = p_val.get("title", [])
            if title_parts:
                title = "".join([t.get("plain_text", "") for t in title_parts])
            break

    # Extract status
    status = "Not started"
    for p_name, p_val in props.items():
        p_type = p_val.get("type")
        if p_type == "status" and p_val.get("status"):
            status = p_val["status"].get("name", "Not started")
            break
        elif p_type == "select" and "status" in p_name.lower() and p_val.get("select"):
            status = p_val["select"].get("name", "Not started")
            break

    # Extract priority
    priority = "Medium"
    for p_name, p_val in props.items():
        if p_val.get("type") == "select" and "priority" in p_name.lower():
            if p_val.get("select"):
                priority = p_val["select"].get("name", "Medium")
            break

    # Extract category
    category = "General"
    for p_name, p_val in props.items():
        if p_val.get("type") == "select" and ("category" in p_name.lower() or "tag" in p_name.lower()):
            if p_val.get("select"):
                category = p_val["select"].get("name", "General")
            break

    # Extract due date & time
    due_date = None
    due_time = None
    for p_name, p_val in props.items():
        if p_val.get("type") == "date" and p_val.get("date"):
            date_raw = p_val["date"].get("start", "")
            if "T" in date_raw:
                parts = date_raw.split("T")
                due_date = parts[0]
                due_time = parts[1][:5]
            else:
                due_date = date_raw
            break

    # Extract notes
    description = ""
    for p_name, p_val in props.items():
        if p_val.get("type") == "rich_text" and ("note" in p_name.lower() or "desc" in p_name.lower()):
            text_parts = p_val.get("rich_text", [])
            description = "".join([t.get("plain_text", "") for t in text_parts])
            break

    return {
        "id": page_id,
        "title": title,
        "status": status,
        "priority": priority,
        "category": category,
        "due_date": due_date,
        "due_time": due_time,
        "description": description,
        "url": page_url,
    }


# ────────────────────────────────────────────────────────────
# Public Async API Functions
# ────────────────────────────────────────────────────────────

async def check_notion_auth() -> bool:
    """Verify that Notion API key and database ID are valid and reachable."""
    if not NOTION_API_KEY or not NOTION_DATABASE_ID:
        return False
    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            schema = await _inspect_database_schema(client)
            return bool(schema)
    except Exception as e:
        logger.warning("Notion auth check failed: %s", e)
        return False


async def create_notion_task(task: ValidatedTask) -> dict:
    """
    Create a new task in Notion database.

    Returns the parsed task dict including Notion page ID and URL.
    """
    headers = _get_headers()
    async with httpx.AsyncClient(timeout=10.0) as client:
        schema = await _inspect_database_schema(client)
        props = _build_page_properties(task, schema)

        payload = {
            "parent": {"database_id": NOTION_DATABASE_ID.strip()},
            "properties": props,
        }

        url = f"{NOTION_API_BASE}/pages"
        resp = await client.post(url, headers=headers, json=payload)

        if resp.status_code >= 400:
            global _CACHED_SCHEMA
            _CACHED_SCHEMA = None
            logger.error("Failed to create Notion task: %s", resp.text)
            err_msg = ""
            try:
                err_data = resp.json()
                err_msg = err_data.get("message", "")
            except Exception:
                pass
            if err_msg:
                raise NotionAPIError(f"Failed to create task in Notion: {err_msg}")
            raise NotionAPIError(f"Failed to create task in Notion (HTTP {resp.status_code}): {resp.text[:200]}")

        page_data = resp.json()
        parsed = _parse_notion_page(page_data, schema)
        logger.info("Created Notion task '%s' (id=%s)", parsed["title"], parsed["id"])
        return parsed


async def list_notion_tasks(
    filter_status: Optional[str] = None,
    category: Optional[str] = None,
    priority: Optional[str] = None,
    max_results: int = 20,
) -> list[dict]:
    """
    Query tasks from the Notion database with optional filtering.
    """
    headers = _get_headers()
    async with httpx.AsyncClient(timeout=10.0) as client:
        schema = await _inspect_database_schema(client)

        filter_conditions = []

        if filter_status:
            status_key = schema.get("status_prop") or "Status"
            status_type = schema.get("status_type", "status")
            if status_type == "status":
                filter_conditions.append({
                    "property": status_key,
                    "status": {"equals": filter_status}
                })
            else:
                filter_conditions.append({
                    "property": status_key,
                    "select": {"equals": filter_status}
                })
        else:
            # Default: list pending/non-completed tasks (status != Done)
            status_key = schema.get("status_prop") or "Status"
            status_type = schema.get("status_type", "status")
            if status_type == "status":
                filter_conditions.append({
                    "property": status_key,
                    "status": {"does_not_equal": "Done"}
                })
            else:
                filter_conditions.append({
                    "property": status_key,
                    "select": {"does_not_equal": "Done"}
                })

        if category:
            cat_key = schema.get("category_prop") or "Category"
            filter_conditions.append({
                "property": cat_key,
                "select": {"equals": category}
            })

        if priority:
            p_key = schema.get("priority_prop") or "Priority"
            filter_conditions.append({
                "property": p_key,
                "select": {"equals": priority}
            })

        payload: dict = {
            "page_size": min(max_results, 50),
        }
        if len(filter_conditions) == 1:
            payload["filter"] = filter_conditions[0]
        elif len(filter_conditions) > 1:
            payload["filter"] = {"and": filter_conditions}

        url = f"{NOTION_API_BASE}/databases/{NOTION_DATABASE_ID.strip()}/query"
        resp = await client.post(url, headers=headers, json=payload)

        if resp.status_code >= 400:
            logger.error("Failed to query Notion tasks: %s", resp.text)
            err_msg = ""
            try:
                err_data = resp.json()
                err_msg = err_data.get("message", "")
            except Exception:
                pass
            if err_msg:
                raise NotionAPIError(f"Failed to fetch tasks from Notion: {err_msg}")
            raise NotionAPIError(f"Failed to fetch tasks from Notion (HTTP {resp.status_code}).")

        results = resp.json().get("results", [])
        return [_parse_notion_page(p, schema) for p in results]


async def get_notion_task(page_id: str) -> Optional[dict]:
    """Retrieve a single Notion task page by ID."""
    headers = _get_headers()
    async with httpx.AsyncClient(timeout=8.0) as client:
        url = f"{NOTION_API_BASE}/pages/{page_id.strip()}"
        resp = await client.get(url, headers=headers)
        if resp.status_code == 404:
            return None
        if resp.status_code >= 400:
            raise NotionAPIError(f"Failed to retrieve Notion task: HTTP {resp.status_code}")
        return _parse_notion_page(resp.json())


async def update_notion_task(page_id: str, updates: dict) -> dict:
    """
    Update properties of an existing Notion task page.

    `updates` can contain keys: `title`, `priority`, `category`, `due_date`, `due_time`, `status`, `description`.
    """
    headers = _get_headers()
    async with httpx.AsyncClient(timeout=10.0) as client:
        schema = await _inspect_database_schema(client)
        props: dict = {}

        if "title" in updates:
            title_key = schema.get("title_prop") or "Name"
            props[title_key] = {"title": [{"type": "text", "text": {"content": updates["title"]}}]}

        if "status" in updates:
            status_key = schema.get("status_prop")
            if status_key:
                status_val = _match_option(updates["status"], schema.get("status_options", []))
                status_type = schema.get("status_type", "status")
                if status_type == "status":
                    props[status_key] = {"status": {"name": status_val}}
                else:
                    props[status_key] = {"select": {"name": status_val}}

        if "priority" in updates:
            priority_key = schema.get("priority_prop")
            if priority_key:
                priority_val = _match_option(updates["priority"], schema.get("priority_options", []))
                props[priority_key] = {"select": {"name": priority_val}}

        if "category" in updates:
            category_key = schema.get("category_prop")
            if category_key:
                cat_type = schema.get("category_type", "select")
                cat_val = _match_option(updates["category"], schema.get("category_options", []))
                if cat_type == "multi_select":
                    props[category_key] = {"multi_select": [{"name": cat_val}]}
                else:
                    props[category_key] = {"select": {"name": cat_val}}

        if "due_date" in updates:
            due_key = schema.get("due_prop")
            if due_key:
                d_val = updates["due_date"]
                if d_val:
                    t_val = updates.get("due_time")
                    start_iso = f"{d_val}T{t_val}:00" if t_val else d_val
                    props[due_key] = {"date": {"start": start_iso}}
                else:
                    props[due_key] = {"date": None}

        if "description" in updates:
            notes_key = schema.get("notes_prop")
            if notes_key:
                props[notes_key] = {
                    "rich_text": [{"type": "text", "text": {"content": updates["description"][:2000]}}]
                }

        url = f"{NOTION_API_BASE}/pages/{page_id.strip()}"
        resp = await client.patch(url, headers=headers, json={"properties": props})

        if resp.status_code >= 400:
            logger.error("Failed to update Notion task: %s", resp.text)
            err_msg = ""
            try:
                err_data = resp.json()
                err_msg = err_data.get("message", "")
            except Exception:
                pass
            if err_msg:
                raise NotionAPIError(f"Failed to update task in Notion: {err_msg}")
            raise NotionAPIError(f"Failed to update task in Notion (HTTP {resp.status_code}).")

        return _parse_notion_page(resp.json(), schema)


async def complete_notion_task(page_id: str) -> dict:
    """Mark a Notion task as Done."""
    return await update_notion_task(page_id, {"status": "Done"})


async def delete_notion_task(page_id: str) -> bool:
    """
    Archive/delete a Notion page.

    In Notion API, deleting a page is done by setting archived=True.
    """
    headers = _get_headers()
    async with httpx.AsyncClient(timeout=8.0) as client:
        url = f"{NOTION_API_BASE}/pages/{page_id.strip()}"
        resp = await client.patch(url, headers=headers, json={"archived": True})
        if resp.status_code >= 400:
            logger.error("Failed to archive Notion task: %s", resp.text)
            raise NotionAPIError(f"Failed to delete task in Notion (HTTP {resp.status_code}).")
        logger.info("Archived Notion task page %s", page_id)
        return True


async def find_task_by_title(title_query: str) -> Optional[dict]:
    """Search for an existing task in Notion matching a title query string."""
    tasks = await list_notion_tasks(max_results=30)
    query_lower = title_query.strip().lower()

    # Exact or substring match
    for task in tasks:
        t_name = task["title"].lower()
        if query_lower == t_name or query_lower in t_name or t_name in query_lower:
            return task
    return None

