"""Tests for cross-layer lineage tracking.

Spec: specs/002-knowledge-layer-split/spec.md — User Story 2 (lineage)
Data model: LineageRecord, WorkingArtifact, RawEvidence entities

Phase 1 scaffold — tests are marked xfail until Phase 2/4 implement
lineage persistence and working artifact versioning.
"""

import pytest


# ---------------------------------------------------------------------------
# US2: Working artifacts traceable to raw evidence
# ---------------------------------------------------------------------------

@pytest.mark.xfail(reason="T010: lineage schema not yet added to hermes_state.py")
def test_working_artifact_links_to_source_evidence(tmp_path):
    """A working artifact must reference at least one upstream evidence record."""
    from agent.orchestrator.jobs import OrchestratorJobService
    from agent.orchestrator.models import SourceRef
    from hermes_state import SessionDB

    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        service = OrchestratorJobService(db)
        summary = service.ingest_bytes(
            filename="report-input.pdf",
            data=b"%PDF",
            requested_by="test",
            source=SourceRef(source_type="telegram", source_uri="tg://10"),
        )
        job_id = summary["job_id"]

        # After working output is produced, lineage must link back
        lineage = service.get_lineage(job_id)
        assert lineage is not None
        assert len(lineage["evidence_ids"]) >= 1
    finally:
        db.close()


@pytest.mark.xfail(reason="T022: working artifact versioning not yet implemented")
def test_superseded_working_artifact_preserves_lineage(tmp_path):
    """When a working artifact is superseded, history remains traceable."""
    from agent.orchestrator.jobs import OrchestratorJobService
    from agent.orchestrator.models import SourceRef
    from hermes_state import SessionDB

    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        service = OrchestratorJobService(db)

        # Create initial working artifact
        s1 = service.ingest_bytes(
            filename="draft-v1.md",
            data=b"# v1",
            requested_by="test",
            source=SourceRef(source_type="telegram", source_uri="tg://20"),
        )

        # Supersede with updated version
        s2 = service.ingest_bytes(
            filename="draft-v2.md",
            data=b"# v2",
            requested_by="test",
            source=SourceRef(source_type="telegram", source_uri="tg://20"),
        )

        lineage = service.get_lineage(s1["job_id"])
        assert len(lineage.get("working_ids", [])) >= 1
        # Superseded artifact still appears in lineage
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Lineage integrity across promotion
# ---------------------------------------------------------------------------

@pytest.mark.xfail(reason="T028: cross-layer lineage updates not yet implemented")
def test_canonical_publication_preserves_full_lineage(tmp_path):
    """After promotion, lineage traces from canonical back to raw evidence."""
    from hermes_state import SessionDB

    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        # This test will create evidence → working → candidate → publication
        # and verify the lineage record links all four.
        # Implementation deferred to Phase 5 (T028).
        pytest.skip("Full lineage chain requires promotion implementation (Phase 5)")
    finally:
        db.close()
