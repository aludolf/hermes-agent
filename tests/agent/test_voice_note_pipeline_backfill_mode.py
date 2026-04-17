"""Tests for voice_note_pipeline backfill-mode semantics (022 US3 gate).

`suppress_action_routing=True` and `execution_mode_override='skipped'`
must keep the pipeline's side-effect surface limited to:

  ✅ extraction_events row (execution_mode='skipped')
  ✅ entity_context updates
  ✅ orchestrator_jobs + working_artifact lineage (via track_extraction_event)
  ❌ list_items (task routing)
  ❌ calendar_bridge.create_event
  ❌ reminder_service.create_reminder
  ❌ pending_previews row
  ❌ reply_text (empty string — runner reports progress via its own channel)

This is the SC-005 guarantee: backfilling never produces retroactive
calendar events or reminders.
"""

from __future__ import annotations

import asyncio
from datetime import date
from unittest.mock import MagicMock

import pytest

from agent.orchestrator.extraction import ExtractedAction, ExtractionResult
from agent.orchestrator.voice_note_pipeline import perform_extraction
from hermes_state import SessionDB


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _seeded_db(tmp_path) -> SessionDB:
    db = SessionDB(db_path=tmp_path / "state.db")
    db.create_shared_list(
        list_id="list_compras", name="Compras",
        list_type="shopping", created_by="system",
    )
    return db


def _fake_handlers():
    lm = MagicMock()
    lm.list_all.return_value = [{"name": "Compras"}]
    lm.find_list_by_name.return_value = {"list_id": "list_compras", "name": "Compras"}
    lm.add_item.return_value = {"item_id": "item_1"}

    cal = MagicMock()
    cal.create_event.return_value = {"id": "evt_1"}

    rem = MagicMock()
    rem.create_reminder.return_value = {"reminder_id": "rem_1"}

    return lm, cal, rem


def _fake_extract(result: ExtractionResult | None):
    def _fn(transcript, **kwargs):
        return result
    return _fn


def _result_with_all_action_types() -> ExtractionResult:
    """3 actions one could expect to see in a historical email: task,
    meeting, reminder. All have confidence >= 0.85 — in normal mode this
    would fire ALL three handlers. Backfill mode must suppress everything."""
    return ExtractionResult(
        actions=[
            ExtractedAction(
                "task", "comprar presente", 0.92,
                {"list": "Compras"},
            ),
            ExtractedAction(
                "meeting", "reunião com Maria", 0.95,
                {"summary": "R", "date": "2024-05-01", "time": "10:00"},
            ),
            ExtractedAction(
                "reminder", "ligar pro dentista", 0.9,
                {"title": "ligar", "date": "2024-05-02", "time": "15:00"},
            ),
        ],
        entities_mentioned=["Maria", "dentista"],
        summary="3 ações históricas",
        transcript_hash="",
    )


# ---------------------------------------------------------------------------
# Positive case — backfill suppresses side effects
# ---------------------------------------------------------------------------

def test_backfill_mode_produces_extraction_event_with_skipped_mode(tmp_path):
    db = _seeded_db(tmp_path)
    lm, cal, rem = _fake_handlers()
    result = _result_with_all_action_types()
    try:
        outcome = _run(perform_extraction(
            transcript="Email antigo de 2024: reunião com Maria dia 01/05, "
                       "comprar presente, ligar pro dentista.",
            sender_id="owner",
            sender_role="owner",
            sender_capabilities={"all"},
            source_type="email_backfill",
            source_format="email-imap-backfill",
            session_db=db,
            list_manager=lm, calendar_bridge=cal, reminder_service=rem,
            current_date=date(2024, 5, 1),
            extract_fn=_fake_extract(result),
            suppress_action_routing=True,
            execution_mode_override="skipped",
        ))
        assert outcome is not None
        assert outcome.execution_mode == "skipped"
        assert outcome.reply_text == ""  # no user-facing reply

        # extraction_events row persisted with execution_mode='skipped'
        events = db.list_extraction_events(sender_id="owner")
        assert len(events) == 1
        assert events[0]["execution_mode"] == "skipped"
        assert events[0]["source_type"] == "email_backfill"

        # Entities still populated
        assert db.get_entity_by_name("Maria") is not None
        assert db.get_entity_by_name("dentista") is not None

        # Lineage job link created (002 raw_archive)
        job_ids = db.get_extraction_job_ids(events[0]["extraction_id"])
        assert len(job_ids) == 1

        # ZERO handler invocations — this is the SC-005 guarantee
        lm.add_item.assert_not_called()
        cal.create_event.assert_not_called()
        rem.create_reminder.assert_not_called()

        # No pending_previews row either
        assert db.get_active_preview_by_sender("owner") is None
    finally:
        db.close()


def test_backfill_mode_with_long_transcript_still_suppresses(tmp_path):
    """Long input normally forces preview; backfill mode must still suppress."""
    db = _seeded_db(tmp_path)
    lm, cal, rem = _fake_handlers()
    try:
        outcome = _run(perform_extraction(
            transcript="a" * 3000,
            sender_id="owner",
            sender_role="owner",
            sender_capabilities={"all"},
            source_type="email_backfill",
            chat_id="chat_would_get_preview",
            session_db=db,
            list_manager=lm, calendar_bridge=cal, reminder_service=rem,
            extract_fn=_fake_extract(_result_with_all_action_types()),
            suppress_action_routing=True,
            execution_mode_override="skipped",
        ))
        assert outcome.execution_mode == "skipped"
        # No pending_preview created even though chat_id was supplied + transcript is long
        assert db.get_active_preview_by_sender("owner") is None
        lm.add_item.assert_not_called()
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Regression: normal mode (defaults) must still work
# ---------------------------------------------------------------------------

def test_normal_mode_unchanged_executes_handlers(tmp_path):
    db = _seeded_db(tmp_path)
    lm, cal, rem = _fake_handlers()
    try:
        # Default kwargs — suppress_action_routing defaults False
        outcome = _run(perform_extraction(
            transcript="comprar leite",
            sender_id="owner",
            sender_role="owner",
            sender_capabilities={"all"},
            session_db=db,
            list_manager=lm, calendar_bridge=cal, reminder_service=rem,
            extract_fn=_fake_extract(ExtractionResult(
                actions=[ExtractedAction(
                    "task", "leite", 0.95, {"list": "Compras"},
                )],
                entities_mentioned=[], summary="", transcript_hash="",
            )),
        ))
        assert outcome is not None
        assert outcome.execution_mode == "auto"
        lm.add_item.assert_called_once()
        events = db.list_extraction_events(sender_id="owner")
        assert events[0]["execution_mode"] == "auto"
    finally:
        db.close()


# ---------------------------------------------------------------------------
# execution_mode_override works independently of suppress_action_routing
# ---------------------------------------------------------------------------

def test_execution_mode_override_persists_exact_value(tmp_path):
    db = _seeded_db(tmp_path)
    lm, cal, rem = _fake_handlers()
    try:
        _run(perform_extraction(
            transcript="X",
            sender_id="owner",
            sender_role="owner",
            sender_capabilities={"all"},
            session_db=db,
            list_manager=lm, calendar_bridge=cal, reminder_service=rem,
            extract_fn=_fake_extract(ExtractionResult(
                actions=[ExtractedAction("task", "x", 0.95, {"list": "Compras"})],
                entities_mentioned=[], summary="", transcript_hash="",
            )),
            suppress_action_routing=True,
            execution_mode_override="custom_audit",
        ))
        events = db.list_extraction_events(sender_id="owner")
        assert events[0]["execution_mode"] == "custom_audit"
    finally:
        db.close()
