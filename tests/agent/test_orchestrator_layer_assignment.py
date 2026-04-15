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


def test_kb_publish_routes_to_canonical_candidate_tier():
    """KB publication intent → kb_candidate route + canonical_candidate tier."""
    from agent.orchestrator.router import classify_route

    decision = classify_route(
        {"job_type": "kb_publish", "source_type": "telegram", "intent": "kb_publish", "metadata_json": {}},
        [],
    )
    assert decision.route_class == RouteClass.KB_CANDIDATE
    assert decision.knowledge_tier == "canonical_candidate"


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

def test_personal_context_routes_to_working_tier():
    """Meeting packs and briefings → personal_context + working tier."""
    from agent.orchestrator.router import classify_route

    decision = classify_route(
        {"job_type": "meeting_pack", "source_type": "telegram", "intent": "meeting_pack", "metadata_json": {}},
        [],
    )
    assert decision.route_class == RouteClass.PERSONAL_CONTEXT
    assert decision.knowledge_tier == "working"


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


# ---------------------------------------------------------------------------
# T014: Integration tests — full ingest→classify→persist→summary chain
# ---------------------------------------------------------------------------

def test_ingest_repo_file_summary_includes_working_tier(tmp_path):
    """Repo ingest → job summary must include knowledge_tier=working."""
    from agent.orchestrator.jobs import OrchestratorJobService
    from agent.orchestrator.artifacts import LocalStorageAdapter
    from agent.orchestrator.models import SourceRef
    from hermes_state import SessionDB

    source_dir = tmp_path / "repo"
    source_dir.mkdir()
    (source_dir / "README.md").write_text("# hello", encoding="utf-8")

    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        service = OrchestratorJobService(
            db, local_storage=LocalStorageAdapter(allowed_roots=[source_dir]),
        )
        summary = service.ingest_local_path(
            source_dir / "README.md",
            requested_by="cli",
            source_scope=str(source_dir),
            job_type="inspect_repo",
            intent="inspect_repo",
        )
        assert summary["knowledge_tier"] == "working"
        assert summary["route_class"] == "dev_workflow"
    finally:
        db.close()


def test_ingest_personal_request_gets_working_tier(tmp_path):
    """Meeting pack ingest → knowledge_tier=working in job summary."""
    from agent.orchestrator.jobs import OrchestratorJobService
    from agent.orchestrator.models import SourceRef
    from hermes_state import SessionDB

    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        service = OrchestratorJobService(db)
        summary = service.create_job(
            job_type="meeting_pack",
            requested_by="test",
            source=SourceRef(source_type="telegram", source_uri="tg://5"),
            intent="meeting_pack",
        )
        assert summary["knowledge_tier"] == "working"
        assert summary["route_class"] == "personal_context"
    finally:
        db.close()


def test_layer_assignment_persisted_in_db(tmp_path):
    """Layer assignment must be stored in the DB, not just returned."""
    from agent.orchestrator.jobs import OrchestratorJobService
    from agent.orchestrator.models import SourceRef
    from hermes_state import SessionDB

    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        service = OrchestratorJobService(db)
        summary = service.ingest_bytes(
            filename="doc.pdf",
            data=b"%PDF",
            requested_by="test",
            source=SourceRef(source_type="telegram", source_uri="tg://3"),
        )
        # Verify DB has the layer assignment
        layer = db.get_layer_assignment(summary["job_id"])
        assert layer is not None
        assert layer["knowledge_tier"] == "raw_only"
        assert layer["route_class"] == "raw_archive"
    finally:
        db.close()
