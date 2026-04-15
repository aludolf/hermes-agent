"""Edge case tests for the 003 productivity orchestrator.

Tests hardening: empty inputs, archived lists, past-due reminders,
unicode/emoji handling, boundary conditions.
"""

import time

import pytest

from agent.orchestrator.contacts import ContactManager
from agent.orchestrator.lists import ListManager
from agent.orchestrator.reminders import ReminderService
from agent.orchestrator.calendar_bridge import parse_portuguese_datetime
from hermes_state import SessionDB


# ---------------------------------------------------------------------------
# List edge cases
# ---------------------------------------------------------------------------

def test_add_item_to_archived_list_raises(tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        mgr = ListManager(db)
        rec = mgr.create_list(name="Old", list_type="custom", created_by="owner")
        mgr.archive_list(rec["list_id"])

        with pytest.raises(ValueError, match="archived"):
            mgr.add_item(rec["list_id"], "should fail", added_by="owner")
    finally:
        db.close()


def test_add_empty_item_raises(tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        mgr = ListManager(db)
        rec = mgr.create_list(name="Test", list_type="custom", created_by="owner")

        with pytest.raises(ValueError, match="empty"):
            mgr.add_item(rec["list_id"], "", added_by="owner")

        with pytest.raises(ValueError, match="empty"):
            mgr.add_item(rec["list_id"], "   ", added_by="owner")
    finally:
        db.close()


def test_add_very_long_item_raises(tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        mgr = ListManager(db)
        rec = mgr.create_list(name="Test", list_type="custom", created_by="owner")

        with pytest.raises(ValueError, match="too long"):
            mgr.add_item(rec["list_id"], "x" * 501, added_by="owner")
    finally:
        db.close()


def test_add_item_with_unicode_and_emoji(tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        mgr = ListManager(db)
        rec = mgr.create_list(name="Test", list_type="custom", created_by="owner")

        item = mgr.add_item(rec["list_id"], "café ☕ pão 🍞", added_by="owner")
        assert item["content"] == "café ☕ pão 🍞"
    finally:
        db.close()


def test_add_item_to_nonexistent_list_raises(tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        mgr = ListManager(db)
        with pytest.raises(ValueError, match="not found"):
            mgr.add_item("list_nonexistent", "test", added_by="owner")
    finally:
        db.close()


def test_find_list_by_name_returns_none_for_archived(tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        mgr = ListManager(db)
        rec = mgr.create_list(name="Archived", list_type="custom", created_by="owner")
        mgr.archive_list(rec["list_id"])
        assert mgr.find_list_by_name("Archived") is None
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Contact edge cases
# ---------------------------------------------------------------------------

def test_approve_owner_raises(tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        mgr = ContactManager(db)
        mgr.ensure_owner("owner_id", "Alexandre")

        with pytest.raises(ValueError, match="owner"):
            mgr.approve_contact("owner_id", approved_by="owner_id")
    finally:
        db.close()


def test_block_nonexistent_contact_is_noop(tmp_path):
    """Blocking a nonexistent contact shouldn't crash."""
    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        mgr = ContactManager(db)
        mgr.block_contact("nonexistent_999")  # should not raise
        assert mgr.get_role("nonexistent_999") is None  # still doesn't exist
    finally:
        db.close()


def test_register_pending_preserves_existing_approved(tmp_path):
    """Re-registering an already-approved contact should NOT reset their role."""
    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        mgr = ContactManager(db)
        mgr.ensure_owner("owner_id", "Owner")
        mgr.register_pending("maid_id", "Maria")
        mgr.approve_contact("maid_id", approved_by="owner_id")

        # Try to re-register — should return existing record, not reset to pending
        rec = mgr.register_pending("maid_id", "Maria")
        assert rec["role"] == "contact"  # not "pending"
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Reminder edge cases
# ---------------------------------------------------------------------------

def test_reminder_due_in_past_still_creates(tmp_path):
    """A reminder due in the past is still valid (for catch-up scenarios)."""
    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        svc = ReminderService(db)
        rec = svc.create_reminder(
            title="Overdue", due_at=time.time() - 3600,
            owner_id="owner", sync_to_calendar=False,
        )
        assert rec["status"] == "pending"
    finally:
        db.close()


def test_complete_already_completed_is_idempotent(tmp_path):
    """Completing an already-completed reminder shouldn't crash."""
    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        svc = ReminderService(db)
        rec = svc.create_reminder(
            title="X", due_at=time.time() + 3600,
            owner_id="owner", sync_to_calendar=False,
        )
        svc.complete_reminder(rec["reminder_id"])
        svc.complete_reminder(rec["reminder_id"])  # second call — no crash
        assert db.get_reminder(rec["reminder_id"])["status"] == "completed"
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Date parsing edge cases
# ---------------------------------------------------------------------------

def test_parse_invalid_time_returns_none():
    assert parse_portuguese_datetime("hoje 25:00") is None


def test_parse_empty_string_returns_none():
    assert parse_portuguese_datetime("") is None


def test_parse_gibberish_returns_none():
    assert parse_portuguese_datetime("xyzzy foo bar") is None


def test_parse_just_date_no_time_returns_none():
    assert parse_portuguese_datetime("amanhã") is None
    assert parse_portuguese_datetime("segunda") is None
