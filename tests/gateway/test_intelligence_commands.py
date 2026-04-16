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


def test_scenario_loader_populates_cache_used_by_db_queries(tmp_path):
    """US6: loader → DB cache → list_scenarios / get_scenario_by_id reads."""
    import json as _json
    from agent.orchestrator.scenario_loader import ScenarioLoader

    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        scen_dir = tmp_path / "scenarios"
        scen_dir.mkdir()
        (scen_dir / "s.yaml").write_text(
            "id: probe_a\n"
            "name: \"Probe A\"\n"
            "category: smoke\n"
            "target_bot: b\n"
            "target_chat_id: \"-1\"\n"
            "steps:\n"
            "  - step: 1\n    sent: hi\n    expected_pattern: hi\n",
            encoding="utf-8",
        )
        stats = ScenarioLoader(db).load_all_from_dir(scen_dir)
        assert stats.loaded == 1

        rows = db.list_scenarios()
        assert len(rows) == 1
        assert rows[0]["scenario_id"] == "probe_a"

        row = db.get_scenario_by_id("probe_a")
        assert row is not None
        assert _json.loads(row["steps_json"])[0]["sent"] == "hi"
    finally:
        db.close()


def test_test_history_db_query_returns_recent_runs(tmp_path):
    """US6 /test_history backing: list_harness_runs returns by started_at DESC."""
    import json as _json
    import time

    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        for i in range(3):
            rid = f"run_{i}"
            db.create_harness_run(
                run_id=rid, scenario_id="s1",
                triggered_by="t", trigger_source="slash_command",
            )
            db.complete_harness_run(rid, status="pass", steps_json=_json.dumps([]))
            time.sleep(0.01)

        runs = db.list_harness_runs(limit=20)
        assert len(runs) == 3
        assert runs[0]["run_id"] == "run_2"  # newest first

        filtered = db.list_harness_runs_by_scenario("s1", limit=20)
        assert len(filtered) == 3
    finally:
        db.close()


def test_harness_runner_executes_scenario_from_db_cache(tmp_path):
    """US5: scenario loaded from DB cache → HarnessRunner → JSONL + DB row."""
    import asyncio
    import json as _json
    from agent.orchestrator.harness_runner import HarnessRunner

    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        steps = [
            {"step": 1, "sent": "/start", "expected_pattern": "Welcome",
             "match_type": "substring", "timeout_ms": 500, "gate": True},
        ]
        db.upsert_scenario_from_yaml(
            scenario_id="rauru_smoke",
            name="Rauru smoke",
            category="smoke",
            steps_json=_json.dumps(steps),
            target_bot="Rauru_HD_bot",
            target_chat_id="-1001",
            yaml_path="scenarios/rauru_smoke.yaml",
            yaml_hash="abc123",
        )

        # Simulate the gateway's hydration path
        row = db.get_scenario_by_id("rauru_smoke")
        assert row is not None
        scenario = {
            "scenario_id": row["scenario_id"],
            "name": row["name"],
            "category": row["category"],
            "target_bot": row["target_bot"],
            "target_chat_id": row["target_chat_id"],
            "steps": _json.loads(row["steps_json"]),
        }

        inbox_records: list = []

        async def _send(chat_id: str, text: str):
            inbox_records.append({
                "user_name": "Rauru_HD_bot",
                "user_id": "r1",
                "text": "Welcome to Rauru!",
            })

        runner = HarnessRunner(
            db, send_message=_send,
            inbox_reader=lambda: list(inbox_records),
            results_path=tmp_path / "harness_results.jsonl",
        )

        loop = asyncio.new_event_loop()
        run = loop.run_until_complete(runner.run_scenario(
            scenario, triggered_by="hermes:owner:1",
            trigger_source="slash_command",
        ))
        assert run.status == "pass"
        assert run.steps[0].status == "pass"
        assert (tmp_path / "harness_results.jsonl").exists()
    finally:
        db.close()


def test_pipeline_accumulates_entities_from_extraction(tmp_path):
    """US4: entities_mentioned in ExtractionResult → entity_context rows."""
    db = SessionDB(db_path=tmp_path / "state.db")
    lm, cal, rem = _fake_handlers()
    result = ExtractionResult(
        actions=[ExtractedAction(
            action_type="task", content="café", confidence=0.95,
            metadata={"list": "Compras"},
        )],
        entities_mentioned=["João", "contador"],
        summary="1 item, 2 pessoas mencionadas",
        transcript_hash="",
    )
    try:
        _run(perform_extraction(
            transcript="Comprar café, ligar pro João ou o contador",
            sender_id="owner",
            sender_role="owner",
            sender_capabilities={"all"},
            session_db=db,
            list_manager=lm, calendar_bridge=cal, reminder_service=rem,
            extract_fn=_fake_extract_factory(result),
        ))
        joao = db.get_entity_by_name("João")
        contador = db.get_entity_by_name("contador")
        assert joao is not None
        assert contador is not None
        assert joao["mention_count"] == 1
        assert contador["mention_count"] == 1
    finally:
        db.close()


def test_pipeline_long_input_persists_pending_preview(tmp_path):
    """US3: long transcript + chat_id → pending_previews row created."""
    db = SessionDB(db_path=tmp_path / "state.db")
    lm, cal, rem = _fake_handlers()
    try:
        outcome = _run(perform_extraction(
            transcript="a" * 2100,
            sender_id="owner",
            sender_role="owner",
            sender_capabilities={"all"},
            chat_id="chat_42",
            session_db=db,
            list_manager=lm, calendar_bridge=cal, reminder_service=rem,
            extract_fn=_fake_extract_factory(_task_result(0.95)),
        ))
        assert outcome is not None
        assert outcome.preview_id is not None

        # An active preview row should exist for the sender.
        active = db.get_active_preview_by_sender("owner")
        assert active is not None
        assert active["preview_id"] == outcome.preview_id
        assert active["chat_id"] == "chat_42"
    finally:
        db.close()


def test_pipeline_force_preview_without_chat_id_skips_persist(tmp_path):
    """Tests can call pipeline without chat_id; no DB row but preview_id set."""
    db = SessionDB(db_path=tmp_path / "state.db")
    lm, cal, rem = _fake_handlers()
    try:
        outcome = _run(perform_extraction(
            transcript="whatever",
            sender_id="owner",
            sender_role="owner",
            sender_capabilities={"all"},
            session_db=db,
            list_manager=lm, calendar_bridge=cal, reminder_service=rem,
            force_preview=True,
            extract_fn=_fake_extract_factory(_task_result(0.95)),
        ))
        assert outcome is not None
        assert outcome.execution_mode == "preview"
        # No preview row because chat_id wasn't provided.
        assert db.get_active_preview_by_sender("owner") is None
    finally:
        db.close()


def test_pipeline_document_source_type_is_recorded(tmp_path):
    """US2: a document_upload pipeline run persists source_type correctly."""
    db = SessionDB(db_path=tmp_path / "state.db")
    lm, cal, rem = _fake_handlers()
    try:
        outcome = _run(perform_extraction(
            transcript="# Ações\n- comprar pão\n- reunião sexta",
            sender_id="s1",
            sender_role="owner",
            sender_capabilities={"all"},
            source_type="document_upload",
            source_format="docx",
            session_db=db,
            list_manager=lm, calendar_bridge=cal, reminder_service=rem,
            extract_fn=_fake_extract_factory(_task_result(0.95)),
        ))
        assert outcome is not None
        events = db.list_extraction_events(sender_id="s1")
        assert events[0]["source_type"] == "document_upload"
        assert events[0]["source_format"] == "docx"
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
