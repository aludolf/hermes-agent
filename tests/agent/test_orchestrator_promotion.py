"""Tests for canonical promotion workflow.

Spec: specs/002-knowledge-layer-split/spec.md — User Story 3
Contract: specs/002-knowledge-layer-split/contracts/promotion-contract.md
Data model: CanonicalCandidate, PromotionDecision, CanonicalPublication entities

Phase 1 scaffold — tests are marked xfail until Phase 5 implements
candidate review, approval, and publication into Domains_KB.
"""

import pytest


# ---------------------------------------------------------------------------
# US3 Scenario 1: Approved candidate is published to Domains_KB
# ---------------------------------------------------------------------------

@pytest.mark.xfail(reason="T011: publish.py not yet created")
def test_approve_candidate_creates_publication(tmp_path):
    """Approving a candidate must create a CanonicalPublication record."""
    from agent.orchestrator.publish import PromotionService
    from hermes_state import SessionDB

    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        service = PromotionService(db)

        # Create a candidate (requires Phase 2 schema)
        candidate_id = service.create_candidate(
            job_id="job_test_1",
            evidence_id="ev_test_1",
            metadata={"title": "HD Gate 1 — The Creative"},
        )

        # Approve it
        decision = service.approve(
            candidate_id=candidate_id,
            resolved_by="agent:main:telegram:dm:289322060",
            note="Ready for canonical publication",
        )
        assert decision["status"] == "approved"

        # Publish
        result = service.publish(
            candidate_id=candidate_id,
            destination_repo="Domains_KB",
        )
        assert result["validation_status"] == "passed"
        assert result["destination_repo"] == "Domains_KB"
    finally:
        db.close()


# ---------------------------------------------------------------------------
# US3 Scenario 2: Unpromoted items invisible to canonical lookups
# ---------------------------------------------------------------------------

@pytest.mark.xfail(reason="T025: candidate state handling not yet implemented")
def test_unpromoted_working_artifact_not_canonical(tmp_path):
    """Working artifacts without promotion must not appear in canonical queries."""
    from hermes_state import SessionDB

    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        # Create a working artifact (not promoted)
        # Query canonical publications — must return empty
        # Implementation deferred to Phase 5
        pytest.skip("Requires promotion + consumption query implementation")
    finally:
        db.close()


# ---------------------------------------------------------------------------
# US3 Scenario 3: Rejection is durable and traceable
# ---------------------------------------------------------------------------

@pytest.mark.xfail(reason="T025: rejection state handling not yet implemented")
def test_rejected_candidate_remains_traceable(tmp_path):
    """Rejected candidates must retain the rejection rationale and not be treated as published."""
    from agent.orchestrator.publish import PromotionService
    from hermes_state import SessionDB

    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        service = PromotionService(db)

        candidate_id = service.create_candidate(
            job_id="job_test_2",
            evidence_id="ev_test_2",
            metadata={"title": "Draft entry"},
        )

        decision = service.reject(
            candidate_id=candidate_id,
            resolved_by="agent:main:telegram:dm:289322060",
            note="Still too provisional",
        )
        assert decision["status"] == "rejected"
        assert decision["resolution_note"] == "Still too provisional"

        # Candidate must not appear in published queries
        candidate = service.get_candidate(candidate_id)
        assert candidate["status"] == "rejected"
    finally:
        db.close()
