"""Tests for working destination adapter.

Spec: specs/002-knowledge-layer-split/spec.md — User Story 2
Data model: WorkingArtifact entity

Phase 1 scaffold — tests are marked xfail until Phase 2 (T012) extends
artifacts.py with working_repo destination support, and Phase 4 (T020)
implements the actual write logic for reports, meeting packs, etc.
"""

import pytest


# ---------------------------------------------------------------------------
# US2: Working destination writes
# ---------------------------------------------------------------------------

def test_working_adapter_writes_report(tmp_path):
    """Working adapter must write a report to the working destination."""
    from agent.orchestrator.artifacts import WorkingDestinationAdapter

    adapter = WorkingDestinationAdapter(root=tmp_path / "working")
    result = adapter.write(
        content=b"# Repo Inspection Report\n\nSummary here.",
        filename="repo_inspection_2026-04-15.md",
        artifact_kind="repo_inspection",
        metadata={"source_job": "job_100"},
    )
    assert result["destination_path"].endswith(".md")
    assert (tmp_path / "working" / result["destination_path"]).exists()


def test_working_adapter_writes_meeting_pack(tmp_path):
    """Working adapter must handle meeting pack artifacts."""
    from agent.orchestrator.artifacts import WorkingDestinationAdapter

    adapter = WorkingDestinationAdapter(root=tmp_path / "working")
    result = adapter.write(
        content=b"Meeting pack content",
        filename="daily_briefing.md",
        artifact_kind="meeting_pack",
        metadata={"date": "2026-04-15"},
    )
    assert result["artifact_kind"] == "meeting_pack"


# ---------------------------------------------------------------------------
# Working artifacts never treated as canonical
# ---------------------------------------------------------------------------

def test_working_artifacts_not_in_canonical_destination(tmp_path):
    """Working destination must be separate from canonical (Domains_KB)."""
    from agent.orchestrator.artifacts import WorkingDestinationAdapter

    adapter = WorkingDestinationAdapter(root=tmp_path / "working")
    result = adapter.write(
        content=b"Draft note",
        filename="draft.md",
        artifact_kind="draft_note",
        metadata={},
    )
    # The destination must NOT be under Domains_KB
    assert "Domains_KB" not in result.get("destination_path", "")
    assert result.get("destination_backend") != "kb_repo"


# ---------------------------------------------------------------------------
# Versioning and supersession
# ---------------------------------------------------------------------------

def test_working_adapter_supersedes_previous_version(tmp_path):
    """Writing a new version should mark the old one as superseded."""
    from agent.orchestrator.artifacts import WorkingDestinationAdapter

    adapter = WorkingDestinationAdapter(root=tmp_path / "working")

    v1 = adapter.write(content=b"v1", filename="doc.md", artifact_kind="report", metadata={})
    v2 = adapter.write(
        content=b"v2", filename="doc.md", artifact_kind="report",
        metadata={"supersedes": v1.get("working_id")},
    )
    assert v2.get("status") == "active"
    # v1 should now be superseded (checked via service, not adapter alone)
