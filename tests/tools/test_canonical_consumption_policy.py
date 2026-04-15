"""Tests for canonical consumption policy enforcement.

Spec: specs/002-knowledge-layer-split/spec.md — User Story 4
Contract: specs/002-knowledge-layer-split/contracts/bot-consumption-contract.md
Data model: ConsumptionPolicy entity
"""

from agent.orchestrator.policy import (
    LayerQueryResult,
    query_knowledge,
    resolve_consumption_policy,
)


# ---------------------------------------------------------------------------
# US4 Scenario 1: Canonical-only consumers never see working content
# ---------------------------------------------------------------------------

def test_canonical_only_policy_rejects_working_content(tmp_path):
    """A canonical_only consumer must not receive working-layer results."""
    from hermes_state import SessionDB

    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        policy = resolve_consumption_policy(db, consumer_name="rauru-hd-bot")
        assert policy["mode"] == "canonical_only"
        assert policy["fallback_allowed"] is False
        assert policy["working_visibility"] == "none"
    finally:
        db.close()


def test_canonical_only_no_silent_fallback(tmp_path):
    """canonical_only queries must return empty rather than falling back to working."""
    from agent.orchestrator.jobs import OrchestratorJobService
    from agent.orchestrator.models import SourceRef
    from hermes_state import SessionDB

    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        # Create a working artifact (no canonical publications)
        service = OrchestratorJobService(db)
        summary = service.ingest_bytes(
            filename="draft.md",
            data=b"# Draft content",
            requested_by="test",
            source=SourceRef(source_type="telegram", source_uri="tg://40"),
        )
        service.create_working_output(
            job_id=summary["job_id"],
            content=b"Working output",
            filename="output.md",
            artifact_kind="report",
        )

        # Query as canonical_only — must get empty, NOT the working artifact
        result = query_knowledge(db, consumer_name="rauru-hd-bot")
        assert isinstance(result, LayerQueryResult)
        assert result.source_layer == "canonical"
        assert len(result.items) == 0
        assert result.fallback_used is False
    finally:
        db.close()


# ---------------------------------------------------------------------------
# US4 Scenario 2: Working-capable consumers can use working content
# ---------------------------------------------------------------------------

def test_working_plus_canonical_sees_both_layers(tmp_path):
    """A working_plus_canonical consumer can retrieve from both layers."""
    from hermes_state import SessionDB

    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        policy = resolve_consumption_policy(db, consumer_name="repo-briefing-workflow")
        assert policy["mode"] == "working_plus_canonical"
        assert policy["fallback_allowed"] is True
        assert policy["working_visibility"] in {"own_outputs", "shared_outputs"}
    finally:
        db.close()


def test_working_content_labeled_as_non_canonical(tmp_path):
    """Working content returned to consumers must be labeled as non-canonical."""
    from agent.orchestrator.jobs import OrchestratorJobService
    from agent.orchestrator.models import SourceRef
    from hermes_state import SessionDB

    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        service = OrchestratorJobService(db)
        summary = service.ingest_bytes(
            filename="notes.md",
            data=b"# Notes",
            requested_by="test",
            source=SourceRef(source_type="telegram", source_uri="tg://41"),
        )
        service.create_working_output(
            job_id=summary["job_id"],
            content=b"Working notes",
            filename="notes-output.md",
            artifact_kind="draft_note",
        )

        result = query_knowledge(db, consumer_name="repo-briefing-workflow")
        working_items = [i for i in result.items if i.get("_source_layer") == "working"]
        assert len(working_items) >= 1
        # Every item must have a _source_layer label
        for item in result.items:
            assert "_source_layer" in item
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Policy storage and audit
# ---------------------------------------------------------------------------

def test_policy_persisted_after_first_resolve(tmp_path):
    """Resolving a default policy should persist it for future lookups."""
    from hermes_state import SessionDB

    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        # First resolve creates the record
        resolve_consumption_policy(db, consumer_name="rauru-hd-bot")

        # Direct DB read should find it
        stored = db.get_consumption_policy("rauru-hd-bot")
        assert stored is not None
        assert stored["mode"] == "canonical_only"
    finally:
        db.close()


def test_policy_change_is_auditable(tmp_path):
    """Updating a consumption policy must be reflected in the DB."""
    from hermes_state import SessionDB

    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        # Create initial policy
        resolve_consumption_policy(db, consumer_name="rauru-hd-bot")

        # Update it
        db.create_consumption_policy(
            policy_id="pol_updated",
            consumer_name="rauru-hd-bot",
            mode="canonical_first",
            fallback_allowed=True,
            working_visibility="shared_outputs",
        )

        # Re-resolve picks up the update
        policy = resolve_consumption_policy(db, consumer_name="rauru-hd-bot")
        assert policy["mode"] == "canonical_first"
        assert policy["fallback_allowed"] is True
    finally:
        db.close()
