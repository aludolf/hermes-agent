"""Tests for productivity gateway commands (003).

Unit tests for command logic via direct ContactManager calls + DB state.
End-to-end gateway command dispatch is tested via the existing gateway
test harness (see test_jobs_command.py for the pattern).

These tests focus on the business logic inside the command handlers:
who can call what, what the output format looks like, and what state
changes occur.
"""

import pytest

from agent.orchestrator.contacts import ContactManager
from hermes_state import SessionDB


def _mgr(tmp_path) -> tuple[SessionDB, ContactManager]:
    db = SessionDB(db_path=tmp_path / "state.db")
    return db, ContactManager(db)


# ---------------------------------------------------------------------------
# Owner-only access control
# ---------------------------------------------------------------------------

def test_non_owner_cannot_approve_contacts(tmp_path):
    """Only the owner can approve contacts — a contact attempting approval should fail."""
    db, mgr = _mgr(tmp_path)
    try:
        mgr.ensure_owner("owner_id", "Alexandre")
        mgr.register_pending("maid_id", "Maria")
        mgr.approve_contact("maid_id", approved_by="owner_id", capabilities=["lists"])

        # Register another pending user
        mgr.register_pending("new_id", "New User")

        # The maid is NOT an owner
        assert mgr.is_owner("maid_id") is False
        assert mgr.is_owner("owner_id") is True
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Approval flow end-to-end
# ---------------------------------------------------------------------------

def test_full_pending_to_approved_flow(tmp_path):
    """Pending → approved → has capabilities."""
    db, mgr = _mgr(tmp_path)
    try:
        mgr.ensure_owner("owner_id", "Alexandre")

        # New user messages Hermes for the first time
        rec = mgr.register_pending("new_id", "Maria")
        assert rec["role"] == "pending"
        assert mgr.get_capabilities("new_id") == set()

        # Owner approves
        mgr.approve_contact(
            "new_id",
            approved_by="owner_id",
            capabilities=["lists", "requests"],
        )

        # Now has capabilities
        assert mgr.get_capabilities("new_id") == {"lists", "requests"}
    finally:
        db.close()


def test_approval_records_approver(tmp_path):
    """Approval must record who approved and when."""
    import time

    db, mgr = _mgr(tmp_path)
    try:
        mgr.ensure_owner("owner_id", "Alexandre")
        mgr.register_pending("new_id", "Maria")

        before = time.time()
        mgr.approve_contact("new_id", approved_by="owner_id")
        after = time.time()

        rec = mgr.get_role("new_id")
        assert rec["approved_by"] == "owner_id"
        assert before <= rec["approved_at"] <= after
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Blocking flow
# ---------------------------------------------------------------------------

def test_block_previously_approved_contact(tmp_path):
    """An approved contact can be blocked, losing all capabilities."""
    db, mgr = _mgr(tmp_path)
    try:
        mgr.ensure_owner("owner_id", "Alexandre")
        mgr.register_pending("maid_id", "Maria")
        mgr.approve_contact("maid_id", approved_by="owner_id")

        assert mgr.get_capabilities("maid_id") == {"lists"}

        mgr.block_contact("maid_id")

        assert mgr.get_capabilities("maid_id") == set()
        rec = mgr.get_role("maid_id")
        assert rec["role"] == "blocked"
    finally:
        db.close()


def test_blocked_contact_cannot_be_resolved_to_capabilities(tmp_path):
    """Blocked contacts must return empty capability set even if previously approved."""
    db, mgr = _mgr(tmp_path)
    try:
        mgr.register_pending("id1", "Spammer")
        mgr.block_contact("id1")
        assert mgr.get_capabilities("id1") == set()
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Listing
# ---------------------------------------------------------------------------

def test_list_contacts_groups_by_role(tmp_path):
    """list_contacts returns all contacts for command display."""
    db, mgr = _mgr(tmp_path)
    try:
        mgr.ensure_owner("owner_id", "Alexandre")
        mgr.register_pending("pending_id", "Pending User")
        mgr.register_pending("approved_id", "Approved User")
        mgr.approve_contact("approved_id", approved_by="owner_id")
        mgr.register_pending("blocked_id", "Blocked User")
        mgr.block_contact("blocked_id")

        all_contacts = mgr.list_contacts()
        assert len(all_contacts) == 4

        by_role = {c["role"]: c for c in all_contacts}
        assert "owner" in by_role
        assert "pending" in by_role
        assert "contact" in by_role
        assert "blocked" in by_role
    finally:
        db.close()


def test_list_contacts_filtered_by_role(tmp_path):
    """list_contacts(role=...) returns only that role."""
    db, mgr = _mgr(tmp_path)
    try:
        mgr.ensure_owner("owner_id", "Alexandre")
        mgr.register_pending("p1", "User 1")
        mgr.register_pending("p2", "User 2")
        mgr.approve_contact("p1", approved_by="owner_id")

        pending = mgr.list_contacts(role="pending")
        assert len(pending) == 1
        assert pending[0]["contact_id"] == "p2"

        contacts = mgr.list_contacts(role="contact")
        assert len(contacts) == 1
        assert contacts[0]["contact_id"] == "p1"
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Capability validation on approval
# ---------------------------------------------------------------------------

def test_valid_capabilities_accepted(tmp_path):
    """All valid capabilities from Capability enum are accepted."""
    db, mgr = _mgr(tmp_path)
    try:
        mgr.register_pending("id1", "User")
        rec = mgr.approve_contact(
            "id1",
            approved_by="owner",
            capabilities=["lists", "requests", "reminders"],
        )
        assert set(rec["approved_capabilities"]) == {"lists", "requests", "reminders"}
    finally:
        db.close()


def test_invalid_capability_rejected(tmp_path):
    """Invalid capabilities raise ValueError."""
    db, mgr = _mgr(tmp_path)
    try:
        mgr.register_pending("id1", "User")
        with pytest.raises(ValueError, match="Invalid capabilities"):
            mgr.approve_contact(
                "id1",
                approved_by="owner",
                capabilities=["lists", "admin_super_user"],
            )
    finally:
        db.close()
