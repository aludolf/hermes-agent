"""Tests for the general orchestrator state primitives."""

from hermes_state import SCHEMA_VERSION, SessionDB


def test_new_database_uses_latest_schema_version(tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        with db._lock:
            row = db._conn.execute("SELECT version FROM schema_version").fetchone()
        assert row[0] == SCHEMA_VERSION
    finally:
        db.close()


def test_create_and_get_orchestrator_job(tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        db.create_orchestrator_job(
            job_id="job_1",
            job_type="parse_pdf",
            requested_by="agent:main:telegram:dm:289322060",
            source_type="telegram",
            source_uri="telegram://message/123",
            source_scope="telegram:289322060",
            metadata_json={"reply_target": "telegram"},
        )

        job = db.get_orchestrator_job("job_1")
        assert job is not None
        assert job["job_type"] == "parse_pdf"
        assert job["status"] == "queued"
        assert job["route_class"] == "pending_route"
        assert job["metadata_json"] == {"reply_target": "telegram"}
    finally:
        db.close()


def test_update_orchestrator_job_status_sets_timestamps(tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        db.create_orchestrator_job(
            job_id="job_1",
            job_type="inspect_repo",
            requested_by="cli",
            source_type="repo",
        )

        db.update_orchestrator_job_status("job_1", "running")
        running = db.get_orchestrator_job("job_1")
        assert running["started_at"] is not None
        assert running["finished_at"] is None

        db.update_orchestrator_job_status("job_1", "completed")
        completed = db.get_orchestrator_job("job_1")
        assert completed["finished_at"] is not None
    finally:
        db.close()


def test_create_artifact_and_routing_decision(tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        db.create_orchestrator_job(
            job_id="job_1",
            job_type="parse_pdf",
            requested_by="telegram",
            source_type="telegram",
        )
        db.create_orchestrator_artifact(
            artifact_id="artifact_1",
            job_id="job_1",
            artifact_type="raw_input",
            storage_backend="local_fs",
            storage_path="~/.hermes/raw/pdf/2026-04-12/statement.pdf",
            provenance_json={"source": "telegram://message/123"},
        )
        db.record_routing_decision(
            decision_id="decision_1",
            job_id="job_1",
            route_class="raw_archive",
            decision_reason="Document should be preserved but not published",
            confidence=0.92,
        )

        artifacts = db.list_orchestrator_artifacts("job_1")
        decision = db.get_routing_decision("job_1")
        job = db.get_orchestrator_job("job_1")

        assert len(artifacts) == 1
        assert artifacts[0]["provenance_json"] == {"source": "telegram://message/123"}
        assert decision["route_class"] == "raw_archive"
        assert decision["manual_override"] is False
        assert job["route_class"] == "raw_archive"
    finally:
        db.close()


def test_create_approval_request(tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        db.create_orchestrator_job(
            job_id="job_1",
            job_type="kb_publish",
            requested_by="telegram",
            source_type="telegram",
        )
        db.create_approval_request(
            approval_id="approval_1",
            job_id="job_1",
            policy_name="kb_publish_requires_approval",
        )

        approvals = db.list_approval_requests("job_1")
        assert len(approvals) == 1
        assert approvals[0]["policy_name"] == "kb_publish_requires_approval"
        assert approvals[0]["status"] == "pending"
    finally:
        db.close()
