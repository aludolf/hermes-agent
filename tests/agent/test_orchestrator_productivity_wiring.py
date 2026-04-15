"""Tests for productivity → orchestrator wiring (003 Phase 6).

Verifies that list updates, reminders, and briefings create working
artifacts queryable via the orchestrator.
"""

import time

from agent.orchestrator.jobs import OrchestratorJobService
from hermes_state import SessionDB


def test_track_list_update_creates_job_and_artifact(tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        svc = OrchestratorJobService(db)
        result = svc.track_productivity_event(
            event_type="list_update",
            title="Maria → Compras: alvejante",
            content="alvejante",
            triggered_by="111222333",
            metadata={"list_name": "Compras", "item": "alvejante"},
        )

        assert result["event_type"] == "list_update"
        assert result["job_id"].startswith("job_")
        assert result["working_id"].startswith("wrk_")

        # Job exists and is completed
        job = db.get_orchestrator_job(result["job_id"])
        assert job["status"] == "completed"
        assert job["job_type"] == "productivity_list_update"

        # Working artifact exists
        wa = db.get_working_artifact(result["working_id"])
        assert wa is not None
        assert wa["artifact_kind"] == "list_update"
        assert wa["title"] == "Maria → Compras: alvejante"

        # Layer assignment exists
        layer = db.get_layer_assignment(result["job_id"])
        assert layer is not None
        assert layer["knowledge_tier"] == "working"
    finally:
        db.close()


def test_track_reminder_creates_job_and_artifact(tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        svc = OrchestratorJobService(db)
        result = svc.track_productivity_event(
            event_type="reminder",
            title="Ligar para João",
            content="Ligar para João @ 15/04 às 15:00",
            triggered_by="289322060",
            metadata={"reminder_id": "rem_test"},
        )

        assert result["event_type"] == "reminder"
        job = db.get_orchestrator_job(result["job_id"])
        assert job["job_type"] == "productivity_reminder"
        assert job["status"] == "completed"

        wa = db.get_working_artifact(result["working_id"])
        assert wa["artifact_kind"] == "reminder"
    finally:
        db.close()


def test_track_briefing_creates_job_and_artifact(tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        svc = OrchestratorJobService(db)
        result = svc.track_productivity_event(
            event_type="briefing",
            title="Briefing diário",
            content="Bom dia! Agenda de hoje...",
            triggered_by="289322060",
        )

        assert result["event_type"] == "briefing"
        job = db.get_orchestrator_job(result["job_id"])
        assert job["job_type"] == "productivity_briefing"

        wa = db.get_working_artifact(result["working_id"])
        assert wa["artifact_kind"] == "briefing"
        assert wa["title"] == "Briefing diário"
    finally:
        db.close()
