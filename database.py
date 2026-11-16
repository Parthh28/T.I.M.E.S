"""
Database module for the Telegram Calendar Assistant.

Manages SQLite schema and CRUD operations for:
- Bot-created events
- Pending confirmations
- Scheduled reminders and their delivery status
"""

import sqlite3
import uuid
import json
import logging
from datetime import datetime
from contextlib import contextmanager
from typing import Optional

from config import DATABASE_FILE, TIMEZONE

logger = logging.getLogger("calendar_bot.database")


def _get_connection() -> sqlite3.Connection:
    """Create a new SQLite connection with WAL mode and foreign keys."""
    conn = sqlite3.connect(DATABASE_FILE)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    return conn


@contextmanager
def get_db():
    """Context manager for database connections."""
    conn = _get_connection()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db() -> None:
    """Initialize the database schema. Safe to call multiple times."""
    with get_db() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS events (
                id TEXT PRIMARY KEY,
                google_event_id TEXT UNIQUE,
                title TEXT NOT NULL,
                description TEXT,
                start_time TEXT NOT NULL,
                end_time TEXT NOT NULL,
                timezone TEXT NOT NULL DEFAULT 'Asia/Kolkata',
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                telegram_user_id INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS pending_confirmations (
                id TEXT PRIMARY KEY,
                telegram_user_id INTEGER NOT NULL,
                title TEXT NOT NULL,
                description TEXT,
                start_time TEXT NOT NULL,
                end_time TEXT NOT NULL,
                timezone TEXT NOT NULL DEFAULT 'Asia/Kolkata',
                reminder_minutes INTEGER,
                status TEXT NOT NULL DEFAULT 'pending'
                    CHECK(status IN ('pending', 'confirmed', 'cancelled', 'expired')),
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                resolved_at TEXT
            );

            CREATE TABLE IF NOT EXISTS reminders (
                id TEXT PRIMARY KEY,
                event_id TEXT NOT NULL,
                google_event_id TEXT,
                telegram_user_id INTEGER NOT NULL,
                event_title TEXT NOT NULL,
                event_start_time TEXT NOT NULL,
                reminder_time TEXT NOT NULL,
                reminder_minutes INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending'
                    CHECK(status IN ('pending', 'sent', 'failed', 'cancelled', 'overdue')),
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                sent_at TEXT,
                FOREIGN KEY (event_id) REFERENCES events(id)
            );

            CREATE INDEX IF NOT EXISTS idx_reminders_status
                ON reminders(status);
            CREATE INDEX IF NOT EXISTS idx_reminders_time
                ON reminders(reminder_time);
            CREATE INDEX IF NOT EXISTS idx_pending_status
                ON pending_confirmations(status);

            CREATE TABLE IF NOT EXISTS tasks (
                id TEXT PRIMARY KEY,
                notion_page_id TEXT UNIQUE,
                title TEXT NOT NULL,
                description TEXT,
                due_date TEXT,
                due_time TEXT,
                priority TEXT NOT NULL DEFAULT 'Medium',
                category TEXT NOT NULL DEFAULT 'General',
                status TEXT NOT NULL DEFAULT 'To Do',
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                updated_at TEXT NOT NULL DEFAULT (datetime('now')),
                telegram_user_id INTEGER NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_tasks_status
                ON tasks(status);
            CREATE INDEX IF NOT EXISTS idx_tasks_due
                ON tasks(due_date);
            CREATE INDEX IF NOT EXISTS idx_tasks_priority
                ON tasks(priority);
        """)

        # Schema migrations for existing SQLite databases
        for col, col_type in [
            ("target_type", "TEXT DEFAULT 'event'"),
            ("action_type", "TEXT DEFAULT 'create'"),
            ("payload_json", "TEXT"),
        ]:
            try:
                conn.execute(f"ALTER TABLE pending_confirmations ADD COLUMN {col} {col_type};")
            except Exception:
                pass

        for col, col_type in [
            ("target_type", "TEXT DEFAULT 'event'"),
            ("notion_page_id", "TEXT"),
        ]:
            try:
                conn.execute(f"ALTER TABLE reminders ADD COLUMN {col} {col_type};")
            except Exception:
                pass

        # Placeholder event for task reminders to satisfy foreign key constraint
        try:
            conn.execute("""
                INSERT OR IGNORE INTO events
                (id, google_event_id, title, start_time, end_time, telegram_user_id)
                VALUES ('task_placeholder_event', 'task_placeholder_event', 'Task Placeholder', '2000-01-01T00:00:00', '2000-01-01T00:00:00', 0);
            """)
        except Exception:
            pass

    logger.info("Database initialized successfully.")


# ────────────────────────────────────────────────────────────
# Pending Confirmations
# ────────────────────────────────────────────────────────────

def create_pending_confirmation(
    telegram_user_id: int,
    title: str,
    start_time: str,
    end_time: str,
    timezone: str = "Asia/Kolkata",
    description: Optional[str] = None,
    reminder_minutes: Optional[int | list[int]] = None,
) -> str:
    """Create a pending confirmation and return its ID."""
    confirmation_id = str(uuid.uuid4())
    stored_reminder = reminder_minutes
    if isinstance(reminder_minutes, list):
        stored_reminder = json.dumps(reminder_minutes)
    with get_db() as conn:
        conn.execute(
            """INSERT INTO pending_confirmations
               (id, telegram_user_id, title, description, start_time, end_time,
                timezone, reminder_minutes)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (confirmation_id, telegram_user_id, title, description,
             start_time, end_time, timezone, stored_reminder),
        )
    logger.info("Created pending confirmation %s", confirmation_id)
    return confirmation_id


def create_generic_pending_confirmation(
    telegram_user_id: int,
    target_type: str,
    action_type: str,
    title: str,
    payload: dict,
    start_time: str = "",
    end_time: str = "",
    timezone: str = "Asia/Kolkata",
    description: Optional[str] = None,
    reminder_minutes: Optional[int | list[int]] = None,
) -> str:
    """Create a generic pending confirmation for tasks, updates, or combined actions."""
    confirmation_id = str(uuid.uuid4())
    stored_reminder = reminder_minutes
    if isinstance(reminder_minutes, list):
        stored_reminder = json.dumps(reminder_minutes)
    payload_str = json.dumps(payload)
    with get_db() as conn:
        conn.execute(
            """INSERT INTO pending_confirmations
               (id, telegram_user_id, title, description, start_time, end_time,
                timezone, reminder_minutes, target_type, action_type, payload_json)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (confirmation_id, telegram_user_id, title, description,
             start_time, end_time, timezone, stored_reminder,
             target_type, action_type, payload_str),
        )
    logger.info("Created generic pending confirmation %s (type=%s, action=%s)",
                confirmation_id, target_type, action_type)
    return confirmation_id


def get_pending_confirmation(confirmation_id: str) -> Optional[dict]:
    """Get a pending confirmation by ID."""
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM pending_confirmations WHERE id = ?",
            (confirmation_id,),
        ).fetchone()
    return dict(row) if row else None


def resolve_confirmation(confirmation_id: str, status: str) -> bool:
    """
    Mark a confirmation as confirmed, cancelled, or expired.
    Returns True if the row was actually updated (prevents duplicate processing).
    """
    with get_db() as conn:
        cursor = conn.execute(
            """UPDATE pending_confirmations
               SET status = ?, resolved_at = datetime('now')
               WHERE id = ? AND status = 'pending'""",
            (status, confirmation_id),
        )
    updated = cursor.rowcount > 0
    if updated:
        logger.info("Resolved confirmation %s -> %s", confirmation_id, status)
    return updated


# ────────────────────────────────────────────────────────────
# Events
# ────────────────────────────────────────────────────────────

def save_event(
    google_event_id: str,
    title: str,
    start_time: str,
    end_time: str,
    telegram_user_id: int,
    timezone: str = "Asia/Kolkata",
    description: Optional[str] = None,
) -> str:
    """Save a created event record. Returns the local event ID."""
    event_id = str(uuid.uuid4())
    with get_db() as conn:
        conn.execute(
            """INSERT INTO events
               (id, google_event_id, title, description, start_time, end_time,
                timezone, telegram_user_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (event_id, google_event_id, title, description,
             start_time, end_time, timezone, telegram_user_id),
        )
    logger.info("Saved event %s (Google: %s)", event_id, google_event_id)
    return event_id


def get_event_by_google_id(google_event_id: str) -> Optional[dict]:
    """Look up a local event by its Google Calendar event ID."""
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM events WHERE google_event_id = ?",
            (google_event_id,),
        ).fetchone()
    return dict(row) if row else None


# ────────────────────────────────────────────────────────────
# Reminders
# ────────────────────────────────────────────────────────────

def create_reminder(
    event_id: str,
    google_event_id: str,
    telegram_user_id: int,
    event_title: str,
    event_start_time: str,
    reminder_time: str,
    reminder_minutes: int,
) -> str:
    """Create a reminder record. Returns the reminder ID."""
    reminder_id = str(uuid.uuid4())
    with get_db() as conn:
        # Check for duplicate (same event, same reminder time)
        existing = conn.execute(
            """SELECT id FROM reminders
               WHERE event_id = ? AND reminder_time = ? AND status = 'pending'""",
            (event_id, reminder_time),
        ).fetchone()
        if existing:
            logger.info("Duplicate reminder prevented for event %s at %s",
                         event_id, reminder_time)
            return existing["id"]

        conn.execute(
            """INSERT INTO reminders
               (id, event_id, google_event_id, telegram_user_id, event_title,
                event_start_time, reminder_time, reminder_minutes)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (reminder_id, event_id, google_event_id, telegram_user_id,
             event_title, event_start_time, reminder_time, reminder_minutes),
        )
    logger.info("Created reminder %s for event %s at %s",
                 reminder_id, event_id, reminder_time)
    return reminder_id


def get_pending_reminders() -> list[dict]:
    """Get all reminders with 'pending' status for startup reconciliation."""
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM reminders WHERE status = 'pending' ORDER BY reminder_time",
        ).fetchall()
    return [dict(r) for r in rows]


def get_pending_reminders_for_user(telegram_user_id: int) -> list[dict]:
    """Get all pending reminders for a specific user ordered by reminder time."""
    with get_db() as conn:
        rows = conn.execute(
            """SELECT * FROM reminders 
               WHERE telegram_user_id = ? AND status = 'pending'
               ORDER BY reminder_time ASC""",
            (telegram_user_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def get_pending_reminder_by_google_id(google_event_id: str) -> Optional[dict]:
    """Get any active pending reminder for a given Google Calendar event ID."""
    with get_db() as conn:
        row = conn.execute(
            """SELECT * FROM reminders 
               WHERE google_event_id = ? AND status = 'pending'
               ORDER BY reminder_time ASC LIMIT 1""",
            (google_event_id,),
        ).fetchone()
    return dict(row) if row else None


def get_pending_reminders_by_google_id(google_event_id: str) -> list[dict]:
    """Get all active pending reminders for a given Google Calendar event ID."""
    with get_db() as conn:
        rows = conn.execute(
            """SELECT * FROM reminders 
               WHERE google_event_id = ? AND status = 'pending'
               ORDER BY reminder_time ASC""",
            (google_event_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def mark_reminder_sent(reminder_id: str) -> bool:
    """Mark a reminder as sent. Returns True if actually updated."""
    with get_db() as conn:
        cursor = conn.execute(
            """UPDATE reminders SET status = 'sent', sent_at = datetime('now')
               WHERE id = ? AND status = 'pending'""",
            (reminder_id,),
        )
    return cursor.rowcount > 0


def mark_reminder_overdue(reminder_id: str) -> bool:
    """Mark a reminder as overdue (event already passed)."""
    with get_db() as conn:
        cursor = conn.execute(
            """UPDATE reminders SET status = 'overdue', sent_at = datetime('now')
               WHERE id = ? AND status = 'pending'""",
            (reminder_id,),
        )
    return cursor.rowcount > 0


def mark_reminder_failed(reminder_id: str) -> bool:
    """Mark a reminder as failed (could not send)."""
    with get_db() as conn:
        cursor = conn.execute(
            """UPDATE reminders SET status = 'failed', sent_at = datetime('now')
               WHERE id = ? AND status = 'pending'""",
            (reminder_id,),
        )
    return cursor.rowcount > 0


def cancel_reminders_for_event(event_id: str) -> int:
    """Cancel all pending reminders for a given event. Returns count cancelled."""
    with get_db() as conn:
        cursor = conn.execute(
            """UPDATE reminders SET status = 'cancelled'
               WHERE event_id = ? AND status = 'pending'""",
            (event_id,),
        )
    return cursor.rowcount


def cancel_reminders_by_google_id(google_event_id: str) -> list[str]:
    """Cancel all pending reminders for a given Google event ID and return their IDs."""
    with get_db() as conn:
        rows = conn.execute(
            """SELECT id FROM reminders
               WHERE google_event_id = ? AND status = 'pending'""",
            (google_event_id,),
        ).fetchall()
        cancelled_ids = [r["id"] for r in rows]
        if cancelled_ids:
            conn.execute(
                """UPDATE reminders SET status = 'cancelled'
                   WHERE google_event_id = ? AND status = 'pending'""",
                (google_event_id,),
            )
    return cancelled_ids


def update_event_times(google_event_id: str, start_time: str, end_time: str) -> bool:
    """Update start_time and end_time of a local event."""
    with get_db() as conn:
        cursor = conn.execute(
            """UPDATE events
               SET start_time = ?, end_time = ?
               WHERE google_event_id = ?""",
            (start_time, end_time, google_event_id),
        )
    return cursor.rowcount > 0


def delete_event_by_google_id(google_event_id: str) -> bool:
    """Delete an event and its associated reminders."""
    with get_db() as conn:
        conn.execute(
            "DELETE FROM reminders WHERE google_event_id = ?",
            (google_event_id,),
        )
        cursor = conn.execute(
            "DELETE FROM events WHERE google_event_id = ?",
            (google_event_id,),
        )
    return cursor.rowcount > 0


# ────────────────────────────────────────────────────────────
# Notion Tasks CRUD
# ────────────────────────────────────────────────────────────

def save_task(
    notion_page_id: str,
    title: str,
    telegram_user_id: int,
    description: Optional[str] = None,
    due_date: Optional[str] = None,
    due_time: Optional[str] = None,
    priority: str = "Medium",
    category: str = "General",
    status: str = "To Do",
) -> str:
    """Save or update a synced Notion task in local SQLite."""
    task_id = str(uuid.uuid4())
    with get_db() as conn:
        conn.execute(
            """INSERT INTO tasks
               (id, notion_page_id, title, description, due_date, due_time,
                priority, category, status, telegram_user_id, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
               ON CONFLICT(notion_page_id) DO UPDATE SET
                   title = excluded.title,
                   description = excluded.description,
                   due_date = excluded.due_date,
                   due_time = excluded.due_time,
                   priority = excluded.priority,
                   category = excluded.category,
                   status = excluded.status,
                   updated_at = datetime('now')""",
            (task_id, notion_page_id, title, description, due_date, due_time,
             priority, category, status, telegram_user_id),
        )
    logger.info("Saved local task %s (notion_page_id=%s)", task_id, notion_page_id)
    return task_id


def get_task_by_notion_id(notion_page_id: str) -> Optional[dict]:
    """Retrieve a local task by its Notion page ID."""
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM tasks WHERE notion_page_id = ?",
            (notion_page_id,),
        ).fetchone()
    return dict(row) if row else None


def update_task_status(notion_page_id: str, status: str) -> bool:
    """Update task status in local SQLite."""
    with get_db() as conn:
        cursor = conn.execute(
            """UPDATE tasks
               SET status = ?, updated_at = datetime('now')
               WHERE notion_page_id = ?""",
            (status, notion_page_id),
        )
    return cursor.rowcount > 0


def delete_task_by_notion_id(notion_page_id: str) -> bool:
    """Delete a task and its reminders from local SQLite."""
    with get_db() as conn:
        conn.execute(
            "DELETE FROM reminders WHERE notion_page_id = ?",
            (notion_page_id,),
        )
        cursor = conn.execute(
            "DELETE FROM tasks WHERE notion_page_id = ?",
            (notion_page_id,),
        )
    return cursor.rowcount > 0


def get_pending_tasks_for_user(telegram_user_id: int) -> list[dict]:
    """Get all non-completed tasks for a user."""
    with get_db() as conn:
        rows = conn.execute(
            """SELECT * FROM tasks
               WHERE telegram_user_id = ? AND status != 'Done'
               ORDER BY
                 CASE priority
                     WHEN 'High' THEN 1
                     WHEN 'Medium' THEN 2
                     WHEN 'Low' THEN 3
                     ELSE 4
                 END,
                 due_date ASC""",
            (telegram_user_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def create_task_reminder(
    notion_page_id: str,
    telegram_user_id: int,
    task_title: str,
    due_datetime_iso: str,
    reminder_time_iso: str,
    reminder_minutes: int,
) -> str:
    """Create a scheduled reminder for a Notion task."""
    reminder_id = str(uuid.uuid4())
    event_fk = "task_placeholder_event"
    with get_db() as conn:
        conn.execute(
            """INSERT INTO reminders
               (id, event_id, google_event_id, notion_page_id, target_type,
                telegram_user_id, event_title, event_start_time,
                reminder_time, reminder_minutes, status)
               VALUES (?, ?, NULL, ?, 'task', ?, ?, ?, ?, ?, 'pending')""",
            (reminder_id, event_fk, notion_page_id, telegram_user_id,
             task_title, due_datetime_iso, reminder_time_iso, reminder_minutes),
        )
    logger.info("Created task reminder %s for notion_page_id %s", reminder_id, notion_page_id)
    return reminder_id


def cancel_reminders_by_notion_id(notion_page_id: str) -> list[str]:
    """Cancel all pending reminders for a given Notion page ID and return their IDs."""
    with get_db() as conn:
        rows = conn.execute(
            """SELECT id FROM reminders
               WHERE notion_page_id = ? AND status = 'pending'""",
            (notion_page_id,),
        ).fetchall()
        cancelled_ids = [r["id"] for r in rows]
        if cancelled_ids:
            conn.execute(
                """UPDATE reminders SET status = 'cancelled'
                   WHERE notion_page_id = ? AND status = 'pending'""",
                (notion_page_id,),
            )
    return cancelled_ids

