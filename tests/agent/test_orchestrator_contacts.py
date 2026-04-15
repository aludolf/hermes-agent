"""Tests for the ContactManager (003 — productivity orchestrator).

Covers role CRUD, pending registration, approval flow, blocking,
and capability resolution.
"""

import pytest

from agent.orchestrator.contacts import (
    DEFAULT_CONTACT_CAPABILITIES,
    ContactManager,
)
from agent.orchestrator.models import Capability, ContactRole
from hermes_state import SessionDB


def _mgr(tmp_path) -> tuple[SessionDB, ContactManager]:
    db = SessionDB(db_path=tmp_path / "state.db")
    return db, ContactManager(db)


# ---------------------------------------------------------------------------
# Owner lifecycle
# ---------------------------------------------------------------------------

def test_ensure_owner_creates_record(tmp_path):
    db, mgr = _mgr(tmp_path)
    try:
        mgr.ensure_owner("289322060", "Alexandre")

        rec = mgr.get_role("289322060")
        assert rec is not None
        assert rec["role"] == "owner"
        assert rec["display_name"] == "Alexandre"
        assert rec["language"] == "pt-BR"
        assert rec["approved_capabilities"] == ["all"]
    finally:
        db.close()


def test_ensure_owner_is_idempotent(tmp_path):
    db, mgr = _mgr(tmp_path)
    try:
        mgr.ensure_owner("289322060", "Alexandre")
        mgr.ensure_owner("289322060", "Alexandre")
        mgr.ensure_owner("289322060", "Alexandre")

        contacts = mgr.list_contacts()
        assert len(contacts) == 1
    finally:
        db.close()


def test_is_owner_returns_true_for_owner(tmp_path):
    db, mgr = _mgr(tmp_path)
    try:
        mgr.ensure_owner("289322060", "Alexandre")
        assert mgr.is_owner("289322060") is True
        assert mgr.is_owner("999") is False
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Pending registration
# ---------------------------------------------------------------------------

def test_register_pending_new_contact(tmp_path):
    db, mgr = _mgr(tmp_path)
    try:
        rec = mgr.register_pending("111222333", "Maria")
        assert rec["role"] == "pending"
        assert rec["display_name"] == "Maria"
        assert rec["language"] == "pt-BR"
    finally:
        db.close()


def test_register_pending_is_idempotent(tmp_path):
    db, mgr = _mgr(tmp_path)
    try:
        mgr.register_pending("111222333", "Maria")
        mgr.register_pending("111222333", "Maria")

        pending = mgr.list_contacts(role="pending")
        assert len(pending) == 1
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Approval
# ---------------------------------------------------------------------------

def test_approve_contact_with_default_capabilities(tmp_path):
    db, mgr = _mgr(tmp_path)
    try:
        mgr.register_pending("111222333", "Maria")
        rec = mgr.approve_contact("111222333", approved_by="289322060")

        assert rec["role"] == "contact"
        assert rec["approved_capabilities"] == [c.value for c in DEFAULT_CONTACT_CAPABILITIES]
        assert rec["approved_by"] == "289322060"
        assert rec["approved_at"] is not None
    finally:
        db.close()


def test_approve_contact_with_custom_capabilities(tmp_path):
    db, mgr = _mgr(tmp_path)
    try:
        mgr.register_pending("111222333", "Maria")
        rec = mgr.approve_contact(
            "111222333",
            approved_by="289322060",
            capabilities=["lists", "requests"],
        )
        assert set(rec["approved_capabilities"]) == {"lists", "requests"}
    finally:
        db.close()


def test_approve_rejects_invalid_capability(tmp_path):
    db, mgr = _mgr(tmp_path)
    try:
        mgr.register_pending("111222333", "Maria")
        with pytest.raises(ValueError, match="Invalid capabilities"):
            mgr.approve_contact(
                "111222333",
                approved_by="289322060",
                capabilities=["lists", "admin_everything"],
            )
    finally:
        db.close()


def test_approve_unknown_contact_raises(tmp_path):
    db, mgr = _mgr(tmp_path)
    try:
        with pytest.raises(ValueError, match="not found"):
            mgr.approve_contact("999999", approved_by="289322060")
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Capabilities resolution
# ---------------------------------------------------------------------------

def test_owner_has_all_capability(tmp_path):
    db, mgr = _mgr(tmp_path)
    try:
        mgr.ensure_owner("289322060", "Alexandre")
        assert mgr.get_capabilities("289322060") == {"all"}
    finally:
        db.close()


def test_approved_contact_has_approved_capabilities(tmp_path):
    db, mgr = _mgr(tmp_path)
    try:
        mgr.register_pending("111", "Maria")
        mgr.approve_contact("111", approved_by="289", capabilities=["lists"])
        assert mgr.get_capabilities("111") == {"lists"}
    finally:
        db.close()


def test_pending_contact_has_no_capabilities(tmp_path):
    db, mgr = _mgr(tmp_path)
    try:
        mgr.register_pending("111", "Maria")
        assert mgr.get_capabilities("111") == set()
    finally:
        db.close()


def test_unknown_user_has_no_capabilities(tmp_path):
    db, mgr = _mgr(tmp_path)
    try:
        assert mgr.get_capabilities("999") == set()
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Blocking
# ---------------------------------------------------------------------------

def test_block_contact_removes_capabilities(tmp_path):
    db, mgr = _mgr(tmp_path)
    try:
        mgr.register_pending("111", "Maria")
        mgr.approve_contact("111", approved_by="289")
        assert mgr.get_capabilities("111") == {"lists"}

        mgr.block_contact("111")
        rec = mgr.get_role("111")
        assert rec["role"] == "blocked"
        assert mgr.get_capabilities("111") == set()
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Language
# ---------------------------------------------------------------------------

def test_update_language(tmp_path):
    db, mgr = _mgr(tmp_path)
    try:
        mgr.register_pending("111", "Johnny")
        mgr.update_language("111", "en-US")
        rec = mgr.get_role("111")
        assert rec["language"] == "en-US"
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Enums are accessible
# ---------------------------------------------------------------------------

def test_contact_role_enum_values():
    assert ContactRole.OWNER == "owner"
    assert ContactRole.CONTACT == "contact"
    assert ContactRole.PENDING == "pending"
    assert ContactRole.BLOCKED == "blocked"


def test_capability_enum_values():
    assert Capability.LISTS == "lists"
    assert Capability.ALL == "all"
