"""Tests for canonical consumption policy enforcement.

Spec: specs/002-knowledge-layer-split/spec.md — User Story 4
Contract: specs/002-knowledge-layer-split/contracts/bot-consumption-contract.md
Data model: ConsumptionPolicy entity

Phase 1 scaffold — tests are marked xfail until Phase 6 (T030-T033)
implements policy storage, resolution, and no-fallback enforcement.
"""

import pytest


# ---------------------------------------------------------------------------
# US4 Scenario 1: Canonical-only consumers never see working content
# ---------------------------------------------------------------------------

@pytest.mark.xfail(reason="T030: consumption policy storage not yet implemented")
def test_canonical_only_policy_rejects_working_content(tmp_path):
    """A canonical_only consumer must not receive working-layer results."""
    from agent.orchestrator.policy import resolve_consumption_policy
    from hermes_state import SessionDB

    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        policy = resolve_consumption_policy(db, consumer_name="rauru-hd-bot")
        assert policy["mode"] == "canonical_only"
        assert policy["fallback_allowed"] is False
        assert policy["working_visibility"] == "none"
    finally:
        db.close()


@pytest.mark.xfail(reason="T031: no-fallback enforcement not yet implemented")
def test_canonical_only_no_silent_fallback(tmp_path):
    """canonical_only queries must fail explicitly rather than falling back to working."""
    from hermes_state import SessionDB

    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        # Set up: one working artifact, zero canonical publications
        # Query as canonical_only consumer
        # Expect: empty result or explicit "no canonical content" response
        # Must NOT silently return the working artifact
        pytest.skip("No-fallback enforcement requires Phase 6 (T031)")
    finally:
        db.close()


# ---------------------------------------------------------------------------
# US4 Scenario 2: Working-capable consumers can use working content
# ---------------------------------------------------------------------------

@pytest.mark.xfail(reason="T030: consumption policy storage not yet implemented")
def test_working_plus_canonical_sees_both_layers(tmp_path):
    """A working_plus_canonical consumer can retrieve from both layers."""
    from agent.orchestrator.policy import resolve_consumption_policy
    from hermes_state import SessionDB

    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        policy = resolve_consumption_policy(db, consumer_name="repo-briefing-workflow")
        assert policy["mode"] == "working_plus_canonical"
        assert policy["fallback_allowed"] is True
        assert policy["working_visibility"] in {"own_outputs", "shared_outputs"}
    finally:
        db.close()


@pytest.mark.xfail(reason="T031: source labeling not yet implemented")
def test_working_content_labeled_as_non_canonical():
    """Working content returned to consumers must be labeled as non-canonical."""
    # When a working-capable consumer receives a working artifact,
    # the response must indicate the source layer so the consumer
    # does not mistake it for canonical truth.
    pytest.skip("Source labeling requires Phase 6 (T031)")


# ---------------------------------------------------------------------------
# Policy registration and audit
# ---------------------------------------------------------------------------

@pytest.mark.xfail(reason="T032: layer-aware lookup not yet registered in tools/registry.py")
def test_consumption_policy_registered_in_tool_registry():
    """Layer-aware lookup entry points must be discoverable via the tool registry."""
    pytest.skip("Tool registry integration requires Phase 6 (T032)")


@pytest.mark.xfail(reason="T030: policy persistence not yet implemented")
def test_policy_change_is_auditable(tmp_path):
    """Updating a consumption policy must create an auditable record."""
    from hermes_state import SessionDB

    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        # Create policy → update policy → verify audit trail
        pytest.skip("Policy audit trail requires Phase 6 (T030)")
    finally:
        db.close()
