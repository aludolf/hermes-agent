"""Tests for canonical promotion workflow.

Spec: specs/002-knowledge-layer-split/spec.md — User Story 3
Contract: specs/002-knowledge-layer-split/contracts/promotion-contract.md
Data model: CanonicalCandidate, PromotionDecision, CanonicalPublication entities
"""

import pytest


def _create_job_with_artifact(db, service):
    """Helper: ingest a file to get a real job_id and artifact_id."""
    from agent.orchestrator.models import SourceRef

    summary = service.ingest_bytes(
        filename="source-doc.pdf",
        data=b"%PDF-1.4 test",
        requested_by="test",
        source=SourceRef(source_type="telegram", source_uri="tg://promo"),
    )
    job_id = summary["job_id"]
    artifacts = db.list_orchestrator_artifacts(job_id)
    evidence_id = artifacts[0]["artifact_id"] if artifacts else None
    return job_id, evidence_id


# ---------------------------------------------------------------------------
# US3 Scenario 1: Approved candidate is published to Domains_KB
# ---------------------------------------------------------------------------

def test_approve_candidate_creates_publication(tmp_path):
    """Approving a candidate must create a CanonicalPublication record."""
    from agent.orchestrator.jobs import OrchestratorJobService
    from agent.orchestrator.publish import PromotionService
    from hermes_state import SessionDB

    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        job_service = OrchestratorJobService(db)
        promo = PromotionService(db)

        job_id, evidence_id = _create_job_with_artifact(db, job_service)

        candidate_id = promo.create_candidate(
            job_id=job_id,
            evidence_id=evidence_id,
            metadata={"title": "HD Gate 1 — The Creative"},
        )

        decision = promo.approve(
            candidate_id=candidate_id,
            resolved_by="agent:main:telegram:dm:289322060",
            note="Ready for canonical publication",
        )
        assert decision["status"] == "approved"

        result = promo.publish(
            candidate_id=candidate_id,
            destination_repo="Domains_KB",
        )
        assert result["validation_status"] == "passed"
        assert result["destination_repo"] == "Domains_KB"

        # Candidate status should be published
        candidate = promo.get_candidate(candidate_id)
        assert candidate["status"] == "published"
    finally:
        db.close()


# ---------------------------------------------------------------------------
# US3 Scenario 2: Unpromoted items invisible to canonical lookups
# ---------------------------------------------------------------------------

def test_unpromoted_working_artifact_not_canonical(tmp_path):
    """Working artifacts without promotion must not appear in canonical queries."""
    from agent.orchestrator.jobs import OrchestratorJobService
    from agent.orchestrator.publish import PromotionService
    from hermes_state import SessionDB

    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        job_service = OrchestratorJobService(db)
        promo = PromotionService(db)

        job_id, evidence_id = _create_job_with_artifact(db, job_service)

        # Create working artifact (no promotion)
        job_service.create_working_output(
            job_id=job_id,
            content=b"# Working draft",
            filename="draft.md",
            artifact_kind="draft_note",
        )

        # No candidates should exist
        candidates = promo.list_candidates(status="published")
        assert len(candidates) == 0
    finally:
        db.close()


# ---------------------------------------------------------------------------
# US3 Scenario 3: Rejection is durable and traceable
# ---------------------------------------------------------------------------

def test_rejected_candidate_remains_traceable(tmp_path):
    """Rejected candidates must retain the rejection rationale and not be treated as published."""
    from agent.orchestrator.jobs import OrchestratorJobService
    from agent.orchestrator.publish import PromotionService
    from hermes_state import SessionDB

    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        job_service = OrchestratorJobService(db)
        promo = PromotionService(db)

        job_id, evidence_id = _create_job_with_artifact(db, job_service)

        candidate_id = promo.create_candidate(
            job_id=job_id,
            evidence_id=evidence_id,
            metadata={"title": "Draft entry"},
        )

        decision = promo.reject(
            candidate_id=candidate_id,
            resolved_by="agent:main:telegram:dm:289322060",
            note="Still too provisional",
        )
        assert decision["status"] == "rejected"
        assert decision["resolution_note"] == "Still too provisional"

        candidate = promo.get_candidate(candidate_id)
        assert candidate["status"] == "rejected"

        # Must not appear in published list
        published = promo.list_candidates(status="published")
        assert len(published) == 0
    finally:
        db.close()


def test_publish_unapproved_candidate_raises(tmp_path):
    """Publishing a candidate that hasn't been approved must fail."""
    from agent.orchestrator.jobs import OrchestratorJobService
    from agent.orchestrator.publish import PromotionService
    from hermes_state import SessionDB

    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        job_service = OrchestratorJobService(db)
        promo = PromotionService(db)

        job_id, evidence_id = _create_job_with_artifact(db, job_service)

        candidate_id = promo.create_candidate(
            job_id=job_id,
            evidence_id=evidence_id,
        )

        with pytest.raises(ValueError, match="not approved_for_publish"):
            promo.publish(candidate_id=candidate_id, destination_repo="Domains_KB")
    finally:
        db.close()
