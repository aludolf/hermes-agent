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

        # Create a working output linked to this job
        service.create_working_output(
            job_id=job_id,
            content=b"# Report on report-input.pdf",
            filename="report-input-summary.md",
            artifact_kind="report",
        )

        lineage = service.get_lineage(job_id)
        assert lineage is not None
        assert len(lineage["evidence_ids"]) >= 1
        assert len(lineage["working_ids"]) >= 1
    finally:
        db.close()


def test_superseded_working_artifact_preserves_lineage(tmp_path):
    """When a working artifact is superseded, history remains traceable."""
    from agent.orchestrator.jobs import OrchestratorJobService
    from agent.orchestrator.models import SourceRef
    from hermes_state import SessionDB

    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        service = OrchestratorJobService(db)

        s1 = service.ingest_bytes(
            filename="draft-v1.md",
            data=b"# v1",
            requested_by="test",
            source=SourceRef(source_type="telegram", source_uri="tg://20"),
        )
        job_id = s1["job_id"]

        # Create v1 working output
        w1 = service.create_working_output(
            job_id=job_id,
            content=b"# v1 summary",
            filename="draft-v1-summary.md",
            artifact_kind="report",
        )

        # Create v2 and supersede v1
        w2 = service.create_working_output(
            job_id=job_id,
            content=b"# v2 summary",
            filename="draft-v2-summary.md",
            artifact_kind="report",
        )
        service.supersede_working_artifact(w1["working_id"])

        # Both appear in lineage
        lineage = service.get_lineage(job_id)
        assert len(lineage["working_ids"]) == 2
        assert w1["working_id"] in lineage["working_ids"]
        assert w2["working_id"] in lineage["working_ids"]

        # v1 is superseded in DB
        v1_record = db.get_working_artifact(w1["working_id"])
        assert v1_record["status"] == "superseded"
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Lineage integrity across promotion
# ---------------------------------------------------------------------------

def test_canonical_publication_preserves_full_lineage(tmp_path):
    """After promotion, lineage traces from canonical back to raw evidence."""
    from agent.orchestrator.jobs import OrchestratorJobService
    from agent.orchestrator.models import SourceRef
    from agent.orchestrator.publish import PromotionService
    from hermes_state import SessionDB

    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        job_service = OrchestratorJobService(db)
        promo = PromotionService(db)

        # 1. Ingest → evidence
        summary = job_service.ingest_bytes(
            filename="kb-source.pdf",
            data=b"%PDF knowledge",
            requested_by="test",
            source=SourceRef(source_type="telegram", source_uri="tg://30"),
        )
        job_id = summary["job_id"]
        artifacts = db.list_orchestrator_artifacts(job_id)
        evidence_id = artifacts[0]["artifact_id"]

        # 2. Working output
        w = job_service.create_working_output(
            job_id=job_id,
            content=b"# Curated entry",
            filename="curated.md",
            artifact_kind="report",
            evidence_id=evidence_id,
        )

        # 3. Candidate → approve → publish
        candidate_id = promo.create_candidate(
            job_id=job_id,
            evidence_id=evidence_id,
            working_id=w["working_id"],
        )
        promo.approve(candidate_id=candidate_id, resolved_by="test")
        pub = promo.publish(candidate_id=candidate_id, destination_repo="Domains_KB")

        # 4. Verify full lineage chain
        lineage = job_service.get_lineage(job_id)
        assert lineage is not None
        assert evidence_id in lineage["evidence_ids"]
        assert w["working_id"] in lineage["working_ids"]
    finally:
        db.close()
