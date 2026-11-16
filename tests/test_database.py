"""
Tests for the database module.

Tests cover:
- Schema initialization
- Pending confirmation CRUD
- Duplicate confirmation prevention
- Event storage
- Reminder CRUD
- Duplicate reminder prevention
- Reminder status transitions
"""

import unittest
import os
import tempfile
from datetime import datetime, timedelta
from unittest.mock import patch

import pytz


class TestDatabase(unittest.TestCase):
    """Tests for the database module using a temporary database file."""

    def setUp(self):
        """Create a temporary database for each test."""
        self.temp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.temp_db.close()
        self.db_path = self.temp_db.name

        # Patch the DATABASE_FILE before importing database functions
        self.patcher = patch("database.DATABASE_FILE", self.db_path)
        self.patcher.start()

        # Import after patching
        import database
        self.db = database
        self.db.init_db()

    def tearDown(self):
        """Clean up the temporary database."""
        self.patcher.stop()
        try:
            os.unlink(self.db_path)
        except OSError:
            pass

    # ── Schema ──

    def test_init_db_idempotent(self):
        """init_db should be safe to call multiple times."""
        self.db.init_db()
        self.db.init_db()
        # Should not raise

    # ── Pending Confirmations ──

    def test_create_and_get_confirmation(self):
        """Create a pending confirmation and retrieve it."""
        conf_id = self.db.create_pending_confirmation(
            telegram_user_id=12345,
            title="Test Event",
            start_time="2026-12-01T10:00:00+05:30",
            end_time="2026-12-01T11:00:00+05:30",
            description="A test event",
            reminder_minutes=30,
        )
        self.assertIsNotNone(conf_id)

        conf = self.db.get_pending_confirmation(conf_id)
        self.assertIsNotNone(conf)
        self.assertEqual(conf["title"], "Test Event")
        self.assertEqual(conf["status"], "pending")
        self.assertEqual(conf["telegram_user_id"], 12345)
        self.assertEqual(conf["reminder_minutes"], 30)

    def test_resolve_confirmation_confirm(self):
        """Resolving a pending confirmation as confirmed should work once."""
        conf_id = self.db.create_pending_confirmation(
            telegram_user_id=12345,
            title="Test Event",
            start_time="2026-12-01T10:00:00+05:30",
            end_time="2026-12-01T11:00:00+05:30",
        )

        result = self.db.resolve_confirmation(conf_id, "confirmed")
        self.assertTrue(result)

        # Second resolve should return False (already processed)
        result2 = self.db.resolve_confirmation(conf_id, "confirmed")
        self.assertFalse(result2)

    def test_resolve_confirmation_cancel(self):
        """Resolving as cancelled should work."""
        conf_id = self.db.create_pending_confirmation(
            telegram_user_id=12345,
            title="Test Event",
            start_time="2026-12-01T10:00:00+05:30",
            end_time="2026-12-01T11:00:00+05:30",
        )

        result = self.db.resolve_confirmation(conf_id, "cancelled")
        self.assertTrue(result)

        conf = self.db.get_pending_confirmation(conf_id)
        self.assertEqual(conf["status"], "cancelled")

    def test_duplicate_confirmation_prevention(self):
        """Confirming an already-confirmed event should fail gracefully."""
        conf_id = self.db.create_pending_confirmation(
            telegram_user_id=12345,
            title="Test Event",
            start_time="2026-12-01T10:00:00+05:30",
            end_time="2026-12-01T11:00:00+05:30",
        )

        # First confirm
        self.assertTrue(self.db.resolve_confirmation(conf_id, "confirmed"))
        # Second confirm - should return False
        self.assertFalse(self.db.resolve_confirmation(conf_id, "confirmed"))
        # Try cancelling confirmed - should return False
        self.assertFalse(self.db.resolve_confirmation(conf_id, "cancelled"))

    def test_get_nonexistent_confirmation(self):
        """Getting a non-existent confirmation should return None."""
        result = self.db.get_pending_confirmation("nonexistent-id")
        self.assertIsNone(result)

    # ── Events ──

    def test_save_and_get_event(self):
        """Save an event and retrieve it by Google ID."""
        event_id = self.db.save_event(
            google_event_id="google_123",
            title="Created Event",
            start_time="2026-12-01T10:00:00+05:30",
            end_time="2026-12-01T11:00:00+05:30",
            telegram_user_id=12345,
            description="Test description",
        )
        self.assertIsNotNone(event_id)

        event = self.db.get_event_by_google_id("google_123")
        self.assertIsNotNone(event)
        self.assertEqual(event["title"], "Created Event")
        self.assertEqual(event["google_event_id"], "google_123")

    def test_get_nonexistent_event(self):
        """Getting a non-existent event by Google ID should return None."""
        result = self.db.get_event_by_google_id("nonexistent")
        self.assertIsNone(result)

    # ── Reminders ──

    def test_create_and_get_reminder(self):
        """Create a reminder and verify it appears in pending list."""
        # First create an event
        event_id = self.db.save_event(
            google_event_id="google_456",
            title="Reminder Event",
            start_time="2026-12-01T10:00:00+05:30",
            end_time="2026-12-01T11:00:00+05:30",
            telegram_user_id=12345,
        )

        reminder_id = self.db.create_reminder(
            event_id=event_id,
            google_event_id="google_456",
            telegram_user_id=12345,
            event_title="Reminder Event",
            event_start_time="2026-12-01T10:00:00+05:30",
            reminder_time="2026-12-01T09:30:00+05:30",
            reminder_minutes=30,
        )
        self.assertIsNotNone(reminder_id)

        pending = self.db.get_pending_reminders()
        self.assertTrue(len(pending) >= 1)
        reminder = next(r for r in pending if r["id"] == reminder_id)
        self.assertEqual(reminder["status"], "pending")
        self.assertEqual(reminder["event_title"], "Reminder Event")

    def test_duplicate_reminder_prevention(self):
        """Creating the same reminder twice should return the existing ID."""
        event_id = self.db.save_event(
            google_event_id="google_789",
            title="Dup Reminder Event",
            start_time="2026-12-01T10:00:00+05:30",
            end_time="2026-12-01T11:00:00+05:30",
            telegram_user_id=12345,
        )

        id1 = self.db.create_reminder(
            event_id=event_id,
            google_event_id="google_789",
            telegram_user_id=12345,
            event_title="Dup Reminder Event",
            event_start_time="2026-12-01T10:00:00+05:30",
            reminder_time="2026-12-01T09:30:00+05:30",
            reminder_minutes=30,
        )

        id2 = self.db.create_reminder(
            event_id=event_id,
            google_event_id="google_789",
            telegram_user_id=12345,
            event_title="Dup Reminder Event",
            event_start_time="2026-12-01T10:00:00+05:30",
            reminder_time="2026-12-01T09:30:00+05:30",
            reminder_minutes=30,
        )

        self.assertEqual(id1, id2)

    def test_multiple_reminders_for_same_event(self):
        """An event can have multiple reminders at different offsets (1440, 60, 15)."""
        event_id = self.db.save_event(
            google_event_id="google_multi_rem",
            title="Multi Reminder Event",
            start_time="2026-12-05T18:00:00+05:30",
            end_time="2026-12-05T19:00:00+05:30",
            telegram_user_id=12345,
        )

        id_1d = self.db.create_reminder(
            event_id=event_id,
            google_event_id="google_multi_rem",
            telegram_user_id=12345,
            event_title="Multi Reminder Event",
            event_start_time="2026-12-05T18:00:00+05:30",
            reminder_time="2026-12-04T18:00:00+05:30",
            reminder_minutes=1440,
        )
        id_1h = self.db.create_reminder(
            event_id=event_id,
            google_event_id="google_multi_rem",
            telegram_user_id=12345,
            event_title="Multi Reminder Event",
            event_start_time="2026-12-05T18:00:00+05:30",
            reminder_time="2026-12-05T17:00:00+05:30",
            reminder_minutes=60,
        )
        id_15m = self.db.create_reminder(
            event_id=event_id,
            google_event_id="google_multi_rem",
            telegram_user_id=12345,
            event_title="Multi Reminder Event",
            event_start_time="2026-12-05T18:00:00+05:30",
            reminder_time="2026-12-05T17:45:00+05:30",
            reminder_minutes=15,
        )

        # All IDs are distinct
        self.assertNotEqual(id_1d, id_1h)
        self.assertNotEqual(id_1h, id_15m)

        all_rems = self.db.get_pending_reminders_by_google_id("google_multi_rem")
        self.assertEqual(len(all_rems), 3)
        self.assertEqual([r["reminder_minutes"] for r in all_rems], [1440, 60, 15])

    def test_mark_reminder_sent(self):
        """Marking a reminder as sent should work once."""
        event_id = self.db.save_event(
            google_event_id="google_sent",
            title="Sent Test",
            start_time="2026-12-01T10:00:00+05:30",
            end_time="2026-12-01T11:00:00+05:30",
            telegram_user_id=12345,
        )
        reminder_id = self.db.create_reminder(
            event_id=event_id,
            google_event_id="google_sent",
            telegram_user_id=12345,
            event_title="Sent Test",
            event_start_time="2026-12-01T10:00:00+05:30",
            reminder_time="2026-12-01T09:30:00+05:30",
            reminder_minutes=30,
        )

        # First mark
        self.assertTrue(self.db.mark_reminder_sent(reminder_id))
        # Second mark - should return False (already sent)
        self.assertFalse(self.db.mark_reminder_sent(reminder_id))

    def test_mark_reminder_overdue(self):
        """Mark a reminder as overdue."""
        event_id = self.db.save_event(
            google_event_id="google_overdue",
            title="Overdue Test",
            start_time="2026-12-01T10:00:00+05:30",
            end_time="2026-12-01T11:00:00+05:30",
            telegram_user_id=12345,
        )
        reminder_id = self.db.create_reminder(
            event_id=event_id,
            google_event_id="google_overdue",
            telegram_user_id=12345,
            event_title="Overdue Test",
            event_start_time="2026-12-01T10:00:00+05:30",
            reminder_time="2026-12-01T09:30:00+05:30",
            reminder_minutes=30,
        )

        self.assertTrue(self.db.mark_reminder_overdue(reminder_id))
        # Check it's no longer pending
        pending = self.db.get_pending_reminders()
        ids = [r["id"] for r in pending]
        self.assertNotIn(reminder_id, ids)

    def test_cancel_reminders_for_event(self):
        """Cancel all pending reminders for a given event."""
        event_id = self.db.save_event(
            google_event_id="google_cancel",
            title="Cancel Test",
            start_time="2026-12-01T10:00:00+05:30",
            end_time="2026-12-01T11:00:00+05:30",
            telegram_user_id=12345,
        )

        # Create two reminders
        self.db.create_reminder(
            event_id=event_id,
            google_event_id="google_cancel",
            telegram_user_id=12345,
            event_title="Cancel Test",
            event_start_time="2026-12-01T10:00:00+05:30",
            reminder_time="2026-12-01T09:30:00+05:30",
            reminder_minutes=30,
        )
        self.db.create_reminder(
            event_id=event_id,
            google_event_id="google_cancel",
            telegram_user_id=12345,
            event_title="Cancel Test",
            event_start_time="2026-12-01T10:00:00+05:30",
            reminder_time="2026-12-01T09:00:00+05:30",
            reminder_minutes=60,
        )

        cancelled = self.db.cancel_reminders_for_event(event_id)
        self.assertEqual(cancelled, 2)

    def test_mark_reminder_failed(self):
        """Mark a reminder as failed."""
        event_id = self.db.save_event(
            google_event_id="google_fail",
            title="Fail Test",
            start_time="2026-12-01T10:00:00+05:30",
            end_time="2026-12-01T11:00:00+05:30",
            telegram_user_id=12345,
        )
        reminder_id = self.db.create_reminder(
            event_id=event_id,
            google_event_id="google_fail",
            telegram_user_id=12345,
            event_title="Fail Test",
            event_start_time="2026-12-01T10:00:00+05:30",
            reminder_time="2026-12-01T09:30:00+05:30",
            reminder_minutes=30,
        )

        self.assertTrue(self.db.mark_reminder_failed(reminder_id))

    def test_update_event_times(self):
        """Update event start and end time."""
        self.db.save_event(
            google_event_id="google_update_time",
            title="Update Time Test",
            start_time="2026-12-01T10:00:00+05:30",
            end_time="2026-12-01T11:00:00+05:30",
            telegram_user_id=12345,
        )
        updated = self.db.update_event_times(
            google_event_id="google_update_time",
            start_time="2026-12-01T14:00:00+05:30",
            end_time="2026-12-01T15:00:00+05:30",
        )
        self.assertTrue(updated)
        ev = self.db.get_event_by_google_id("google_update_time")
        self.assertEqual(ev["start_time"], "2026-12-01T14:00:00+05:30")

    def test_delete_event_by_google_id(self):
        """Delete an event and verify it is removed along with cancelled reminders."""
        event_id = self.db.save_event(
            google_event_id="google_del_test",
            title="Delete Test",
            start_time="2026-12-01T10:00:00+05:30",
            end_time="2026-12-01T11:00:00+05:30",
            telegram_user_id=12345,
        )
        self.db.create_reminder(
            event_id=event_id,
            google_event_id="google_del_test",
            telegram_user_id=12345,
            event_title="Delete Test",
            event_start_time="2026-12-01T10:00:00+05:30",
            reminder_time="2026-12-01T09:45:00+05:30",
            reminder_minutes=15,
        )
        self.assertTrue(self.db.delete_event_by_google_id("google_del_test"))
        self.assertIsNone(self.db.get_event_by_google_id("google_del_test"))
        self.assertEqual(len(self.db.get_pending_reminders_by_google_id("google_del_test")), 0)

    # ── Notion Tasks CRUD ──

    def test_task_crud(self):
        """Test creating, reading, updating status, and deleting local Notion task records."""
        task_id = self.db.save_task(
            notion_page_id="notion_page_101",
            title="Submit Quarterly Report",
            telegram_user_id=12345,
            description="Q3 numbers",
            due_date="2026-11-20",
            due_time="17:00",
            priority="High",
            category="Finance",
            status="To Do",
        )
        self.assertIsNotNone(task_id)

        task = self.db.get_task_by_notion_id("notion_page_101")
        self.assertIsNotNone(task)
        self.assertEqual(task["title"], "Submit Quarterly Report")
        self.assertEqual(task["priority"], "High")
        self.assertEqual(task["status"], "To Do")

        # Update status
        self.assertTrue(self.db.update_task_status("notion_page_101", "Done"))
        updated_task = self.db.get_task_by_notion_id("notion_page_101")
        self.assertEqual(updated_task["status"], "Done")

        # Delete
        self.assertTrue(self.db.delete_task_by_notion_id("notion_page_101"))
        self.assertIsNone(self.db.get_task_by_notion_id("notion_page_101"))

    def test_generic_pending_confirmation(self):
        """Test generic pending confirmations for tasks and combined actions."""
        payload = {"title": "Task A", "priority": "High"}
        cid = self.db.create_generic_pending_confirmation(
            telegram_user_id=12345,
            target_type="task",
            action_type="create",
            title="Task A",
            payload=payload,
            description="Some desc",
            reminder_minutes=60,
        )
        self.assertIsNotNone(cid)
        conf = self.db.get_pending_confirmation(cid)
        self.assertEqual(conf["target_type"], "task")
        self.assertEqual(conf["action_type"], "create")
        self.assertIn("Task A", conf["payload_json"])

    def test_task_reminders(self):
        """Test creating and cancelling reminders for Notion tasks."""
        rid = self.db.create_task_reminder(
            notion_page_id="notion_task_xyz",
            telegram_user_id=12345,
            task_title="File Taxes",
            due_datetime_iso="2026-11-20T17:00:00+05:30",
            reminder_time_iso="2026-11-20T16:00:00+05:30",
            reminder_minutes=60,
        )
        self.assertIsNotNone(rid)

        # Cancel
        cancelled = self.db.cancel_reminders_by_notion_id("notion_task_xyz")
        self.assertEqual(len(cancelled), 1)
        self.assertEqual(cancelled[0], rid)



if __name__ == "__main__":
    unittest.main()
