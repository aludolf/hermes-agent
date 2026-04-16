"""Intelligence-layer gateway integration tests (021).

US1 Voice Note interception — verifies the extraction pipeline correctly
routes actions, persists records, and returns a reply string when the
caller should skip the LLM session. Exercises the extracted pipeline
function; the gateway method itself is pure plumbing over it.
"""

from __future__ import annotations

import asyncio
import json
from datetime import date
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from agent.orchestrator.extraction import ExtractedAction, ExtractionResult
from agent.orchestrator.voice_note_pipeline import (
    extract_voice_transcript,
    perform_extraction,
)
from hermes_state import SessionDB


# ---------------------------------------------------------------------------
# Voice-wrapper extraction
# ---------------------------------------------------------------------------

def test_extract_voice_transcript_strips_wrapper():
    enriched = (
        '[The user sent a voice message~ '
        'Here\'s what they said: "Comprar café e ligar pro João"]'
    )
    assert extract_voice_transcript(enriched) == "Comprar café e ligar pro João"


def test_extract_voice_transcript_returns_plain_text_when_no_wrapper():
    assert extract_voice_transcript("Hello world") == "Hello world"


def test_extract_voice_transcript_empty_returns_empty():
    assert extract_voice_transcript("") == ""


# ---------------------------------------------------------------------------
# perform_extraction — full pipeline with a real SessionDB + fake handlers
# ---------------------------------------------------------------------------

def _fake_extract_factory(result: ExtractionResult | None):
    """Return a synchronous callable matching extract_actions's signature."""
    def _fn(transcript, **kwargs):
        return result
    return _fn


def _fake_handlers():
    lm = MagicMock()
    lm.list_all.return_value = [{"name": "Compras"}, {"name": "Recados"}]
    lm.find_list_by_name.return_value = {"list_id": "list_1", "name": "Compras"}
    lm.add_item.return_value = {"item_id": "item_1"}
    cal = MagicMock()
    cal.create_event.return_value = {"id": "evt_1"}
    rem = MagicMock()
    rem.create_reminder.return_value = {"reminder_id": "rem_1"}
    return lm, cal, rem


def _task_result(confidence: float = 0.95) -> ExtractionResult:
    return ExtractionResult(
        actions=[ExtractedAction(
            action_type="task", content="café",
            confidence=confidence, metadata={"list": "Compras"},
        )],
        entities_mentioned=[],
        summary="1 item",
        transcript_hash="",  # pipeline re-hashes
    )


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def test_pipeline_happy_path_executes_and_persists(tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    lm, cal, rem = _fake_handlers()
    try:
        outcome = _run(perform_extraction(
            transcript="Comprar café",
            sender_id="s1",
            sender_role="owner",
            sender_capabilities={"all"},
            session_db=db,
            list_manager=lm,
            calendar_bridge=cal,
            reminder_service=rem,
            extract_fn=_fake_extract_factory(_task_result(0.95)),
            current_date=date(2026, 4, 16),
        ))
        assert outcome is not None
        assert outcome.execution_mode == "auto"
        assert "café" in outcome.reply_text
        lm.add_item.assert_called_once()

        # Extraction event persisted
        events = db.list_extraction_events(sender_id="s1")
        assert len(events) == 1
        assert events[0]["actions_count"] == 1
        assert events[0]["execution_mode"] == "auto"

        # Lineage link created
        link_jobs = db.get_extraction_job_ids(events[0]["extraction_id"])
        assert len(link_jobs) == 1
    finally:
        db.close()


def test_pipeline_dedup_hit_falls_through(tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    lm, cal, rem = _fake_handlers()
    try:
        # First call persists
        first = _run(perform_extraction(
            transcript="Comprar café",
            sender_id="s1",
            sender_role="owner",
            sender_capabilities={"all"},
            session_db=db,
            list_manager=lm, calendar_bridge=cal, reminder_service=rem,
            extract_fn=_fake_extract_factory(_task_result(0.95)),
        ))
        assert first is not None

        # Second call with the same transcript → dedup hit → None
        second = _run(perform_extraction(
            transcript="Comprar café",
            sender_id="s1",
            sender_role="owner",
            sender_capabilities={"all"},
            session_db=db,
            list_manager=lm, calendar_bridge=cal, reminder_service=rem,
            extract_fn=_fake_extract_factory(_task_result(0.95)),
        ))
        assert second is None
    finally:
        db.close()


def test_pipeline_falls_through_on_api_failure(tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    lm, cal, rem = _fake_handlers()
    try:
        outcome = _run(perform_extraction(
            transcript="anything",
            sender_id="s1",
            sender_role="owner",
            sender_capabilities={"all"},
            session_db=db,
            list_manager=lm, calendar_bridge=cal, reminder_service=rem,
            extract_fn=_fake_extract_factory(None),  # API error
        ))
        assert outcome is None
    finally:
        db.close()


def test_pipeline_falls_through_when_extraction_empty(tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    lm, cal, rem = _fake_handlers()
    empty = ExtractionResult(actions=[], entities_mentioned=[], summary="", transcript_hash="")
    try:
        outcome = _run(perform_extraction(
            transcript="small talk",
            sender_id="s1",
            sender_role="owner",
            sender_capabilities={"all"},
            session_db=db,
            list_manager=lm, calendar_bridge=cal, reminder_service=rem,
            extract_fn=_fake_extract_factory(empty),
        ))
        assert outcome is None
    finally:
        db.close()


def test_pipeline_falls_through_when_only_low_confidence(tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    lm, cal, rem = _fake_handlers()
    try:
        outcome = _run(perform_extraction(
            transcript="maybe I want something",
            sender_id="s1",
            sender_role="owner",
            sender_capabilities={"all"},
            session_db=db,
            list_manager=lm, calendar_bridge=cal, reminder_service=rem,
            extract_fn=_fake_extract_factory(_task_result(0.5)),
        ))
        # Everything below clarify threshold → silent fall-through
        assert outcome is None
    finally:
        db.close()


def test_pipeline_respects_rollback_flag(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_EXTRACTION_ENABLED", "0")
    db = SessionDB(db_path=tmp_path / "state.db")
    lm, cal, rem = _fake_handlers()
    try:
        outcome = _run(perform_extraction(
            transcript="Comprar café",
            sender_id="s1",
            sender_role="owner",
            sender_capabilities={"all"},
            session_db=db,
            list_manager=lm, calendar_bridge=cal, reminder_service=rem,
            extract_fn=_fake_extract_factory(_task_result(0.95)),
        ))
        assert outcome is None
    finally:
        db.close()


def test_pipeline_long_input_forces_preview(tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    lm, cal, rem = _fake_handlers()
    long_transcript = "a" * 2100  # > SHORT_INPUT_CHAR_LIMIT
    try:
        outcome = _run(perform_extraction(
            transcript=long_transcript,
            sender_id="s1",
            sender_role="owner",
            sender_capabilities={"all"},
            session_db=db,
            list_manager=lm, calendar_bridge=cal, reminder_service=rem,
            extract_fn=_fake_extract_factory(_task_result(0.95)),
        ))
        assert outcome is not None
        assert outcome.execution_mode == "preview"
        assert outcome.preview_id is not None
        lm.add_item.assert_not_called()  # preview never executes
    finally:
        db.close()


def test_pipeline_rejects_unknown_sender_role(tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    lm, cal, rem = _fake_handlers()
    try:
        outcome = _run(perform_extraction(
            transcript="Comprar café",
            sender_id="s1",
            sender_role="pending",  # not owner/contact
            sender_capabilities=set(),
            session_db=db,
            list_manager=lm, calendar_bridge=cal, reminder_service=rem,
            extract_fn=_fake_extract_factory(_task_result(0.95)),
        ))
        assert outcome is None
    finally:
        db.close()


def test_pipeline_filters_action_for_lists_only_contact(tmp_path):
    """Contact with lists-only capability: meeting action must be filtered."""
    db = SessionDB(db_path=tmp_path / "state.db")
    lm, cal, rem = _fake_handlers()
    result = ExtractionResult(
        actions=[
            ExtractedAction(
                action_type="task", content="café", confidence=0.95,
                metadata={"list": "Compras"},
            ),
            ExtractedAction(
                action_type="meeting", content="reunião", confidence=0.95,
                metadata={
                    "summary": "x", "date": "2026-04-20", "time": "10:00",
                },
            ),
        ],
        entities_mentioned=[], summary="2 itens", transcript_hash="",
    )
    try:
        outcome = _run(perform_extraction(
            transcript="Comprar café e reunião às 10",
            sender_id="s1",
            sender_role="contact",
            sender_capabilities={"lists"},
            session_db=db,
            list_manager=lm, calendar_bridge=cal, reminder_service=rem,
            extract_fn=_fake_extract_factory(result),
        ))
        assert outcome is not None
        statuses = [r.status for r in outcome.routed]
        assert "executed" in statuses  # task
        assert "filtered" in statuses  # meeting blocked by capability
        cal.create_event.assert_not_called()
    finally:
        db.close()
