"""Tests for orchestrator job services."""

from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from agent.orchestrator.artifacts import LocalStorageAdapter
from agent.orchestrator.jobs import OrchestratorJobService
from agent.orchestrator.reporting import format_job_summary
from agent.orchestrator.models import SourceRef
from hermes_state import SessionDB


def test_ingest_bytes_creates_job_artifact_and_route(tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        service = OrchestratorJobService(db)
        summary = service.ingest_bytes(
            filename="statement.pdf",
            data=b"%PDF-1.4 fake pdf",
            requested_by="agent:main:telegram:dm:289322060",
            source=SourceRef(
                source_type="telegram",
                source_uri="telegram://message/123",
                source_scope="telegram:289322060",
            ),
        )

        assert summary["status"] == "queued"
        assert summary["route_class"] == "raw_archive"
        assert len(summary["artifacts"]) == 1
        assert summary["artifacts"][0]["display_name"] == "statement.pdf"
    finally:
        db.close()


def test_ingest_local_repo_file_routes_to_dev_workflow(tmp_path):
    source_dir = tmp_path / "repo"
    source_dir.mkdir()
    readme = source_dir / "README.md"
    readme.write_text("# hello", encoding="utf-8")

    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        service = OrchestratorJobService(
            db,
            local_storage=LocalStorageAdapter(allowed_roots=[source_dir]),
        )
        summary = service.ingest_local_path(
            readme,
            requested_by="cli",
            source_scope=str(source_dir),
            job_type="inspect_repo",
            intent="inspect_repo",
        )

        assert summary["route_class"] == "dev_workflow"
        assert summary["next_action"] == "inspect_repo"
    finally:
        db.close()


def test_request_approval_blocks_job_and_reporting_mentions_it(tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        service = OrchestratorJobService(db)
        summary = service.ingest_bytes(
            filename="plan.txt",
            data=b"meeting plan",
            requested_by="telegram-session",
            source=SourceRef(
                source_type="telegram",
                source_uri="telegram://message/5",
                source_scope="telegram:c1",
            ),
            job_type="meeting_pack",
            intent="meeting_pack",
        )

        approval = service.request_approval(summary["job_id"], "calendar_write_requires_approval")
        blocked = service.get_job_summary(summary["job_id"])
        rendered = format_job_summary(blocked)

        assert approval["status"] == "pending"
        assert blocked["status"] == "blocked_for_approval"
        assert blocked["next_action"] == "await_approval"
        assert "Approvals Pending" in rendered
    finally:
        db.close()


def test_parse_pdf_job_extracts_text_and_generates_summary(tmp_path):
    pytest.importorskip("pymupdf", reason="pymupdf not installed")
    import pymupdf

    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        pdf_path = tmp_path / "test.pdf"
        doc = pymupdf.open()
        page = doc.new_page()
        page.set_text("This is a test PDF document with sample content for parsing.")
        doc.save(str(pdf_path))
        doc.close()

        service = OrchestratorJobService(db)
        summary = service.ingest_local_path(
            pdf_path,
            requested_by="agent:main:telegram:dm:289322060",
            source_uri="file://test.pdf",
            source_scope=str(tmp_path),
            job_type="ingest_telegram_document",
        )

        assert summary["status"] == "queued"
        assert summary["route_class"] == "raw_archive"

        with patch("tools.web_tools._call_summarizer_llm") as mock_summary:
            mock_summary.return_value = "# Test Summary\n\nThis is the summarized content."

            result = service.parse_pdf_job(summary["job_id"])

            assert result["status"] == "completed"
            summary_artifacts = [
                a for a in result.get("artifacts", [])
                if a.get("artifact_type") == "generated_report"
            ]
            assert len(summary_artifacts) == 1
            assert "test.pdf" in summary_artifacts[0]["storage_path"]

            mock_summary.assert_called_once()
    finally:
        db.close()


def test_parse_pdf_job_handles_no_pdf_gracefully(tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        service = OrchestratorJobService(db)
        summary = service.ingest_bytes(
            filename="test.txt",
            data=b"not a pdf",
            requested_by="cli",
            source=SourceRef(
                source_type="local_fs",
                source_uri="file://test.txt",
                source_scope=str(tmp_path),
            ),
            job_type="ingest_local_file",
        )

        result = service.parse_pdf_job(summary["job_id"])

        assert result["status"] == "failed"
        assert "No PDF artifacts found" in result.get("error_message", "")
    finally:
        db.close()
