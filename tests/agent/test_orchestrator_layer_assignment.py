"""Tests for knowledge-layer assignment during orchestrator routing.

Spec: specs/002-knowledge-layer-split/spec.md — User Story 1
Contract: specs/002-knowledge-layer-split/contracts/layer-routing-contract.md
Data model: LayerAssignment entity

Phase 1 scaffold — tests are marked xfail until Phase 2/3 implement the
underlying enums, schema extensions, and classification logic.
"""

import pytest

from agent.orchestrator.models import RouteClass


# ---------------------------------------------------------------------------
# US1 Scenario 1: Inbound items get a knowledge tier alongside route class
# ---------------------------------------------------------------------------

@pytest.mark.xfail(reason="T009: KnowledgeTier enum not yet added to models.py")
def test_ingest_attachment_assigns_knowledge_tier(tmp_path):
    """Every completed ingest must expose both route_class AND knowledge_tier."""
    from agent.orchestrator.models import KnowledgeTier  # noqa: F811
    from agent.orchestrator.jobs import OrchestratorJobService
    from agent.orchestrator.models import SourceRef
    from hermes_state import SessionDB

    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        service = OrchestratorJobService(db)
        summary = service.ingest_bytes(
            filename="report.pdf",
            data=b"%PDF-1.4 fake",
            requested_by="test",
            source=SourceRef(source_type="telegram", source_uri="tg://1"),
        )
        assert "knowledge_tier" in summary
        assert summary["knowledge_tier"] in {t.value for t in KnowledgeTier}
    finally:
        db.close()


@pytest.mark.xfail(reason="T015: classify_route does not yet return knowledge_tier")
def test_repo_inspection_routes_to_working_tier():
    """Repo-oriented input → dev_workflow route + working knowledge tier."""
    from agent.orchestrator.router import classify_route

    decision = classify_route(
        {"job_type": "inspect_repo", "source_type": "repo", "intent": "inspect_repo", "metadata_json": {}},
        [],
    )
    assert decision.route_class == RouteClass.DEV_WORKFLOW
    assert hasattr(decision, "knowledge_tier")
    assert decision.knowledge_tier == "working"


@pytest.mark.xfail(reason="T015: classify_route does not yet return knowledge_tier")
def test_kb_publish_routes_to_canonical_candidate_tier():
    """KB publication intent → kb_candidate route + canonical_candidate tier."""
    from agent.orchestrator.router import classify_route

    decision = classify_route(
        {"job_type": "kb_publish", "source_type": "telegram", "intent": "kb_publish", "metadata_json": {}},
        [],
    )
    assert decision.route_class == RouteClass.KB_CANDIDATE
    assert decision.knowledge_tier == "canonical_candidate"


@pytest.mark.xfail(reason="T015: classify_route does not yet return knowledge_tier")
def test_binary_attachment_routes_to_raw_only_tier():
    """Binary evidence (PDF, image) → raw_archive route + raw_only tier."""
    from agent.orchestrator.router import classify_route

    decision = classify_route(
        {"job_type": "ingest_attachment", "source_type": "telegram", "intent": "", "metadata_json": {}},
        [{"storage_path": "/tmp/scan.pdf", "metadata_json": {}}],
    )
    assert decision.route_class == RouteClass.RAW_ARCHIVE
    assert decision.knowledge_tier == "raw_only"


# ---------------------------------------------------------------------------
# US1 Scenario 2: Operational items preserved outside canonical KB
# ---------------------------------------------------------------------------

@pytest.mark.xfail(reason="T015: tier assignment for personal/task routes not yet implemented")
def test_personal_context_routes_to_working_tier():
    """Meeting packs and briefings → personal_context + working tier."""
    from agent.orchestrator.router import classify_route

    decision = classify_route(
        {"job_type": "meeting_pack", "source_type": "telegram", "intent": "meeting_pack", "metadata_json": {}},
        [],
    )
    assert decision.route_class == RouteClass.PERSONAL_CONTEXT
    assert decision.knowledge_tier == "working"


@pytest.mark.xfail(reason="T015: tier assignment for generated output not yet implemented")
def test_generated_output_routes_to_working_tier():
    """Generated deliverables → generated_output + working tier."""
    from agent.orchestrator.router import classify_route

    decision = classify_route(
        {"job_type": "deliver_report", "source_type": "generated", "intent": "", "metadata_json": {}},
        [],
    )
    assert decision.route_class == RouteClass.GENERATED_OUTPUT
    assert decision.knowledge_tier == "working"


# ---------------------------------------------------------------------------
# US1 Scenario 3: Manual override preserves audit trail
# ---------------------------------------------------------------------------

@pytest.mark.xfail(reason="T016: manual layer override not yet implemented in job lifecycle")
def test_manual_override_from_raw_to_canonical_candidate(tmp_path):
    """Operator can reclassify an item; previous tier is preserved for audit."""
    from agent.orchestrator.jobs import OrchestratorJobService
    from agent.orchestrator.models import SourceRef
    from hermes_state import SessionDB

    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        service = OrchestratorJobService(db)
        summary = service.ingest_bytes(
            filename="notes.pdf",
            data=b"%PDF",
            requested_by="test",
            source=SourceRef(source_type="telegram", source_uri="tg://2"),
        )
        job_id = summary["job_id"]

        # Override: raw_only → canonical_candidate
        updated = service.override_layer_assignment(
            job_id=job_id,
            new_tier="canonical_candidate",
            new_route_class="kb_candidate",
            changed_by="agent:main:telegram:dm:289322060",
            note="Operator marked as promotion-eligible",
        )
        assert updated["knowledge_tier"] == "canonical_candidate"
        assert updated["manual_override"] is True
        assert updated["overridden_from_tier"] == "raw_only"
    finally:
        db.close()
