"""End-to-end integration tests for the 021 Hermes Intelligence Layer.

These exercise the full stack — extraction → routing → persistence →
summary — with mocks only at the external boundary (Sonnet API, Telegram
adapter, bot-peer inbox). Everything in between is the real module graph
talking to a real (tmp) SQLite database.

Scope:
- T070: voice note → extraction → execution → Portuguese summary
- T071: document upload → MarkItDown → extraction → execution
- T072: long transcript → preview → partial confirmation → execution
- T073: harness run → Rauru mock → JSONL → regression detection
- T074: Sonnet API failure → graceful fallback (None returned)
- T075: contact capability filter — lists-only contact cannot create meetings
"""

from __future__ import annotations

import asyncio
import json
import time
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from agent.orchestrator.extraction import ExtractedAction, ExtractionResult
from agent.orchestrator.harness_runner import HarnessRunner
from agent.orchestrator.pending_preview import PendingPreviewManager
from agent.orchestrator.voice_note_pipeline import perform_extraction
from hermes_state import SessionDB


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _seeded_db(tmp_path) -> SessionDB:
    """DB with the Compras list already created (emulates the seeded default)."""
    db = SessionDB(db_path=tmp_path / "state.db")
    db.create_shared_list(
        list_id="list_compras", name="Compras",
        list_type="shopping", created_by="system",
    )
    return db


def _handlers():
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


# ---------------------------------------------------------------------------
# T070: Voice note → extraction → execution
# ---------------------------------------------------------------------------

def test_e2e_voice_note_extraction_executes_and_persists(tmp_path):
    db = _seeded_db(tmp_path)
    lm, cal, rem = _handlers()
    result = ExtractionResult(
        actions=[
            ExtractedAction("task", "café", 0.95, {"list": "Compras"}),
            ExtractedAction("meeting", "reunião com João", 0.92, {
                "summary": "Reunião com João", "date": "2026-04-20",
                "time": "10:00",
            }),
            ExtractedAction("reminder", "ligar pro contador", 0.90, {
                "title": "Ligar pro contador",
                "date": "2026-04-17", "time": "15:00",
            }),
        ],
        entities_mentioned=["João", "contador"],
        summary="3 ações: 1 compra, 1 reunião, 1 lembrete",
        transcript_hash="",
    )
    try:
        outcome = _run(perform_extraction(
            transcript="Comprar café. Reunião com João sexta às 10. "
                       "Lembrar de ligar pro contador amanhã às 15h.",
            sender_id="owner",
            sender_role="owner",
            sender_capabilities={"all"},
            sender_name="Alexandre",
            session_db=db,
            list_manager=lm, calendar_bridge=cal, reminder_service=rem,
            current_date=date(2026, 4, 16),
            extract_fn=_fake_extract(result),
        ))
        assert outcome is not None
        assert outcome.execution_mode == "auto"
        # All three handlers invoked
        lm.add_item.assert_called_once()
        cal.create_event.assert_called_once()
        rem.create_reminder.assert_called_once()
        # Entities accumulated
        assert db.get_entity_by_name("João") is not None
        assert db.get_entity_by_name("contador") is not None
        # Extraction + lineage persisted
        events = db.list_extraction_events(sender_id="owner")
        assert len(events) == 1
        assert events[0]["actions_count"] == 3
        assert db.get_extraction_job_ids(events[0]["extraction_id"])
        # Portuguese summary mentions all three actions
        assert "Compras" in outcome.reply_text
        assert "Reunião" in outcome.reply_text or "reunião" in outcome.reply_text.lower()
        assert "Lembrete" in outcome.reply_text or "ligar" in outcome.reply_text.lower()
    finally:
        db.close()


# ---------------------------------------------------------------------------
# T071: Document upload → MarkItDown → extraction → execution
# ---------------------------------------------------------------------------

def test_e2e_document_upload_pipeline(tmp_path):
    """Simulates a .docx being converted to markdown then extracted."""
    from agent.orchestrator.document_converter import DocumentConverter

    db = _seeded_db(tmp_path)
    lm, cal, rem = _handlers()
    # Use a plain-text fixture — DocumentConverter passes .md through without MarkItDown.
    doc = tmp_path / "action_items.md"
    doc.write_text(
        "# Reunião 2026-04-16\n\n"
        "- Comprar post-its\n- Comprar café\n- Ligar pro cliente na sexta\n",
        encoding="utf-8",
    )
    markdown = DocumentConverter().convert(doc)
    assert markdown is not None and "post-its" in markdown

    result = ExtractionResult(
        actions=[
            ExtractedAction("task", "post-its", 0.9, {"list": "Compras"}),
            ExtractedAction("task", "café", 0.9, {"list": "Compras"}),
        ],
        entities_mentioned=[], summary="2 itens em Compras",
        transcript_hash="",
    )
    try:
        outcome = _run(perform_extraction(
            transcript=markdown,
            sender_id="owner",
            sender_role="owner",
            sender_capabilities={"all"},
            source_type="document_upload",
            source_format="md",
            session_db=db,
            list_manager=lm, calendar_bridge=cal, reminder_service=rem,
            extract_fn=_fake_extract(result),
        ))
        assert outcome is not None
        assert lm.add_item.call_count == 2
        events = db.list_extraction_events(sender_id="owner")
        assert events[0]["source_type"] == "document_upload"
        assert events[0]["source_format"] == "md"
    finally:
        db.close()


# ---------------------------------------------------------------------------
# T072: Long transcript → preview → partial confirmation → execution
# ---------------------------------------------------------------------------

def test_e2e_long_transcript_preview_partial_confirm(tmp_path):
    db = _seeded_db(tmp_path)
    lm, cal, rem = _handlers()
    from agent.orchestrator.action_router import RoutedHandlers

    # 3 actions; we'll confirm only #1 and #3
    result = ExtractionResult(
        actions=[
            ExtractedAction("task", "café", 0.95, {"list": "Compras"}),
            ExtractedAction("task", "pão", 0.9, {"list": "Compras"}),
            ExtractedAction("reminder", "ligar domingo", 0.9, {
                "title": "Ligar", "date": "2026-04-19", "time": "14:00",
            }),
        ],
        entities_mentioned=[], summary="3 ações", transcript_hash="",
    )
    try:
        # Long transcript → preview, no side effects yet.
        outcome = _run(perform_extraction(
            transcript="a" * 2100,  # forces long_input
            sender_id="owner",
            sender_role="owner",
            sender_capabilities={"all"},
            chat_id="chat_1",
            session_db=db,
            list_manager=lm, calendar_bridge=cal, reminder_service=rem,
            extract_fn=_fake_extract(result),
        ))
        assert outcome is not None
        assert outcome.execution_mode == "preview"
        lm.add_item.assert_not_called()
        rem.create_reminder.assert_not_called()

        # Owner replies "confirmar 1, 3" — partial confirmation
        mgr = PendingPreviewManager(db)
        confirm = mgr.confirm_subset(
            sender_id="owner", indices=[1, 3],
            sender_capabilities={"all"},
            handlers=RoutedHandlers(
                list_manager=lm, calendar_bridge=cal, reminder_service=rem,
            ),
        )
        assert confirm.status == "partial_confirmed"
        # One task (café) + one reminder executed; second task skipped.
        assert lm.add_item.call_count == 1
        rem.create_reminder.assert_called_once()
        # Preview no longer active
        assert mgr.get_active_preview("owner") is None
    finally:
        db.close()


# ---------------------------------------------------------------------------
# T073: Harness run → Rauru mock → JSONL → regression detection
# ---------------------------------------------------------------------------

def test_e2e_harness_run_with_regression_detection(tmp_path):
    db = _seeded_db(tmp_path)
    # Seed a passing baseline for scenario s1, then re-run and break step 2.
    db.create_harness_run(
        run_id="run_baseline", scenario_id="s1",
        triggered_by="t", trigger_source="slash_command",
    )
    db.complete_harness_run(
        "run_baseline", status="pass",
        steps_json=json.dumps([
            {"step": 1, "status": "pass"},
            {"step": 2, "status": "pass"},
        ]),
    )

    inbox: list = []

    async def _send(chat_id, text):
        # Step 1 gets a proper reply; step 2 gets nothing → timeout → regression.
        if text == "hello":
            inbox.append({
                "user_name": "Rauru_HD_bot", "user_id": "r",
                "text": "hi there",
            })

    runner = HarnessRunner(
        db, send_message=_send,
        inbox_reader=lambda: list(inbox),
        results_path=tmp_path / "harness_results.jsonl",
    )
    scenario = {
        "scenario_id": "s1", "name": "Test", "category": "smoke",
        "target_bot": "Rauru_HD_bot", "target_chat_id": "-1001",
        "steps": [
            {"step": 1, "sent": "hello", "expected_pattern": "hi",
             "match_type": "substring", "timeout_ms": 500},
            {"step": 2, "sent": "silent", "expected_pattern": "whatever",
             "match_type": "substring", "timeout_ms": 300},
        ],
    }
    try:
        run = _run(runner.run_scenario(
            scenario, triggered_by="hermes:owner:1",
            trigger_source="slash_command",
        ))
        assert run.status == "timeout"
        assert run.regression_flags == [2]
        assert run.last_passing_run_id == "run_baseline"

        # JSONL contract-shape row written
        records = [
            json.loads(line)
            for line in (tmp_path / "harness_results.jsonl").read_text("utf-8").splitlines()
            if line.strip()
        ]
        assert len(records) == 1
        assert records[0]["regression_flags"] == [2]
        assert records[0]["status"] == "timeout"
    finally:
        db.close()


# ---------------------------------------------------------------------------
# T074: Extraction API failure → fallback (no side effects)
# ---------------------------------------------------------------------------

def test_e2e_extraction_api_failure_falls_back_cleanly(tmp_path):
    """When extract_fn returns None (API failure), pipeline must return None
    and leave ZERO DB side effects so the caller can fall back to the LLM."""
    db = _seeded_db(tmp_path)
    lm, cal, rem = _handlers()
    try:
        outcome = _run(perform_extraction(
            transcript="anything",
            sender_id="owner",
            sender_role="owner",
            sender_capabilities={"all"},
            session_db=db,
            list_manager=lm, calendar_bridge=cal, reminder_service=rem,
            extract_fn=_fake_extract(None),
        ))
        assert outcome is None
        # No extraction events, no handler calls.
        assert db.list_extraction_events(sender_id="owner") == []
        lm.add_item.assert_not_called()
        cal.create_event.assert_not_called()
        rem.create_reminder.assert_not_called()
    finally:
        db.close()


# ---------------------------------------------------------------------------
# T075: Contact capability filter — lists-only contact cannot create meetings
# ---------------------------------------------------------------------------

def test_e2e_contact_capability_filter_blocks_meetings(tmp_path):
    db = _seeded_db(tmp_path)
    lm, cal, rem = _handlers()
    result = ExtractionResult(
        actions=[
            ExtractedAction("task", "café", 0.95, {"list": "Compras"}),
            ExtractedAction("meeting", "reunião", 0.95, {
                "summary": "R", "date": "2026-04-20", "time": "10:00",
            }),
        ],
        entities_mentioned=[], summary="2 ações", transcript_hash="",
    )
    try:
        outcome = _run(perform_extraction(
            transcript="Comprar café e reunião às 10",
            sender_id="maid",
            sender_role="contact",
            sender_capabilities={"lists"},  # cannot create meetings
            session_db=db,
            list_manager=lm, calendar_bridge=cal, reminder_service=rem,
            extract_fn=_fake_extract(result),
        ))
        assert outcome is not None
        # Task executed, meeting filtered silently
        lm.add_item.assert_called_once()
        cal.create_event.assert_not_called()
        statuses = [r.status for r in outcome.routed]
        assert "executed" in statuses
        assert "filtered" in statuses
    finally:
        db.close()
