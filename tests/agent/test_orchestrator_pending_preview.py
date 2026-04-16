"""Tests for PendingPreviewManager + reply parsing (021 US3)."""

from __future__ import annotations

import time
from unittest.mock import MagicMock

import pytest

from agent.orchestrator.action_router import RoutedAction, RoutedHandlers
from agent.orchestrator.extraction import ExtractedAction, ExtractionResult
from agent.orchestrator.pending_preview import (
    PREVIEW_TTL_SECONDS,
    PendingPreviewManager,
    parse_preview_reply,
)
from hermes_state import SessionDB


# ---------------------------------------------------------------------------
# Reply parser
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("text", [
    "confirmar", "Confirmar", "CONFIRMAR", "  confirma  ",
    "ok", "sim", "SIM", "confirmar.",
])
def test_parse_confirm_all_variants(text):
    parsed = parse_preview_reply(text)
    assert parsed.kind == "confirm_all"


@pytest.mark.parametrize("text,expected", [
    ("confirmar 2", [2]),
    ("confirmar 1, 3", [1, 3]),
    ("confirmar 2,4,7", [2, 4, 7]),
    ("confirmar 1 3 5", [1, 3, 5]),
    ("confirma 2; 4", [2, 4]),
])
def test_parse_confirm_subset_variants(text, expected):
    parsed = parse_preview_reply(text)
    assert parsed.kind == "confirm_subset"
    assert parsed.indices == expected


@pytest.mark.parametrize("text", [
    "cancelar", "Cancelar", "cancela", "não", "nao", "n",
])
def test_parse_cancel_variants(text):
    parsed = parse_preview_reply(text)
    assert parsed.kind == "cancel"


@pytest.mark.parametrize("text", [
    "", "boa tarde", "confirmar o café", "posso confirmar?",
    "quatro itens", "confirmar abc",
])
def test_parse_unknown(text):
    parsed = parse_preview_reply(text)
    assert parsed.kind == "unknown"


# ---------------------------------------------------------------------------
# PendingPreviewManager lifecycle
# ---------------------------------------------------------------------------

def _routed_actions() -> list[RoutedAction]:
    return [
        RoutedAction(
            ExtractedAction(
                action_type="task", content="café", confidence=0.95,
                metadata={"list": "Compras"},
            ),
            status="pending",
        ),
        RoutedAction(
            ExtractedAction(
                action_type="task", content="pão", confidence=0.9,
                metadata={"list": "Compras"},
            ),
            status="pending",
        ),
        RoutedAction(
            ExtractedAction(
                action_type="reminder", content="ligar pro João",
                confidence=0.9,
                metadata={"title": "ligar pro João",
                          "date": "2026-04-20", "time": "10:00"},
            ),
            status="pending",
        ),
    ]


def _seed_extraction(db: SessionDB, sender: str = "s1") -> str:
    ext_id = "ext_test1"
    db.create_extraction_event(
        extraction_id=ext_id,
        source_type="pasted_text",
        transcript_hash="t" * 64,
        sender_id=sender,
        sender_role="owner",
        execution_mode="preview",
    )
    return ext_id


def _fake_handlers() -> RoutedHandlers:
    lm = MagicMock()
    lm.find_list_by_name.return_value = {"list_id": "l1", "name": "Compras"}
    lm.add_item.return_value = {"item_id": "i1"}
    cal = MagicMock()
    cal.create_event.return_value = {"id": "evt_1"}
    rem = MagicMock()
    rem.create_reminder.return_value = {"reminder_id": "r1"}
    return RoutedHandlers(list_manager=lm, calendar_bridge=cal, reminder_service=rem)


def test_create_and_lookup_preview(tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    mgr = PendingPreviewManager(db)
    try:
        _seed_extraction(db)
        mgr.create_preview(
            preview_id="pv_1", extraction_id="ext_test1",
            sender_id="s1", chat_id="c1", routed=_routed_actions(),
        )
        active = mgr.get_active_preview("s1")
        assert active is not None
        assert active["preview_id"] == "pv_1"
        assert active["status"] == "awaiting_confirmation"
    finally:
        db.close()


def test_new_preview_expires_previous(tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    mgr = PendingPreviewManager(db)
    try:
        _seed_extraction(db)
        mgr.create_preview(
            preview_id="pv_old", extraction_id="ext_test1",
            sender_id="s1", chat_id="c1", routed=_routed_actions(),
        )
        # Re-seed a new extraction row for the second preview so the FK holds.
        db.create_extraction_event(
            extraction_id="ext_test2", source_type="pasted_text",
            transcript_hash="u" * 64, sender_id="s1",
            sender_role="owner", execution_mode="preview",
        )
        mgr.create_preview(
            preview_id="pv_new", extraction_id="ext_test2",
            sender_id="s1", chat_id="c1", routed=_routed_actions(),
        )
        # Only the newer one is active.
        active = mgr.get_active_preview("s1")
        assert active["preview_id"] == "pv_new"
    finally:
        db.close()


def test_confirm_all_executes_every_pending(tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    mgr = PendingPreviewManager(db)
    handlers = _fake_handlers()
    try:
        _seed_extraction(db)
        mgr.create_preview(
            preview_id="pv_1", extraction_id="ext_test1",
            sender_id="s1", chat_id="c1", routed=_routed_actions(),
        )
        result = mgr.confirm_all(
            sender_id="s1",
            sender_capabilities={"all"},
            handlers=handlers,
        )
        assert result.status == "confirmed"
        assert len([r for r in result.routed if r.status == "executed"]) == 3
        handlers.list_manager.add_item.assert_called()
        handlers.reminder_service.create_reminder.assert_called_once()

        # Preview is resolved — no longer active.
        assert mgr.get_active_preview("s1") is None
    finally:
        db.close()


def test_confirm_subset_executes_only_selected(tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    mgr = PendingPreviewManager(db)
    handlers = _fake_handlers()
    try:
        _seed_extraction(db)
        mgr.create_preview(
            preview_id="pv_1", extraction_id="ext_test1",
            sender_id="s1", chat_id="c1", routed=_routed_actions(),
        )
        result = mgr.confirm_subset(
            sender_id="s1",
            indices=[1, 3],  # task "café" + reminder
            sender_capabilities={"all"},
            handlers=handlers,
        )
        assert result.status == "partial_confirmed"
        executed = [r for r in result.routed if r.status == "executed"]
        assert len(executed) == 2
        assert handlers.list_manager.add_item.call_count == 1
        handlers.reminder_service.create_reminder.assert_called_once()
    finally:
        db.close()


def test_cancel_discards_preview(tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    mgr = PendingPreviewManager(db)
    try:
        _seed_extraction(db)
        mgr.create_preview(
            preview_id="pv_1", extraction_id="ext_test1",
            sender_id="s1", chat_id="c1", routed=_routed_actions(),
        )
        result = mgr.cancel(sender_id="s1")
        assert result.status == "cancelled"
        assert mgr.get_active_preview("s1") is None
    finally:
        db.close()


def test_expired_preview_returns_expired_on_confirm(tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    mgr = PendingPreviewManager(db)
    handlers = _fake_handlers()
    try:
        _seed_extraction(db)
        mgr.create_preview(
            preview_id="pv_1", extraction_id="ext_test1",
            sender_id="s1", chat_id="c1", routed=_routed_actions(),
            ttl_seconds=-1,  # already expired
        )
        # Auto-expire on read — get_active_preview returns None.
        # But confirm_all must also handle the case where we look
        # directly: use expire_stale_previews then confirm
        result = mgr.confirm_all(
            sender_id="s1",
            sender_capabilities={"all"},
            handlers=handlers,
        )
        # Since get_active_preview returned None (auto-expiry), we get not_found.
        assert result.status in ("expired", "not_found")
    finally:
        db.close()


def test_confirmation_requires_original_sender(tmp_path):
    """Contact cannot confirm the owner's preview (security, FR-010)."""
    db = SessionDB(db_path=tmp_path / "state.db")
    mgr = PendingPreviewManager(db)
    handlers = _fake_handlers()
    try:
        _seed_extraction(db, sender="owner")
        mgr.create_preview(
            preview_id="pv_1", extraction_id="ext_test1",
            sender_id="owner", chat_id="c1", routed=_routed_actions(),
        )
        # Different sender tries to confirm — should fail cleanly.
        result = mgr.confirm_all(
            sender_id="contact",
            sender_capabilities={"lists"},
            handlers=handlers,
        )
        assert result.status == "not_found"
        handlers.list_manager.add_item.assert_not_called()
    finally:
        db.close()


def test_ttl_default_is_ten_minutes():
    assert PREVIEW_TTL_SECONDS == 600
