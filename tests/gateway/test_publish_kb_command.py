"""Tests for /publish_kb gateway command.

Spec: specs/002-knowledge-layer-split/spec.md — User Story 3 (command surface)
Contract: specs/002-knowledge-layer-split/contracts/promotion-contract.md

Phase 1 scaffold — tests are marked xfail until Phase 5 (T027) wires
the /publish_kb command into gateway/run.py and hermes_cli/commands.py.
"""

import pytest


# ---------------------------------------------------------------------------
# Command parsing and dispatch
# ---------------------------------------------------------------------------

@pytest.mark.xfail(reason="T027: /publish_kb command not yet wired")
def test_publish_kb_command_dispatches_promotion():
    """/publish_kb <job_id> should trigger the promotion workflow."""
    # This test will simulate a Telegram message with /publish_kb job_123
    # and verify the gateway dispatches to the PromotionService.
    pytest.skip("/publish_kb command not yet implemented (Phase 5, T027)")


@pytest.mark.xfail(reason="T027: /publish_kb command not yet wired")
def test_publish_kb_requires_job_id():
    """/publish_kb without a job_id should return a usage hint."""
    pytest.skip("/publish_kb command not yet implemented (Phase 5, T027)")


@pytest.mark.xfail(reason="T027: /publish_kb command not yet wired")
def test_publish_kb_rejects_non_candidate():
    """/publish_kb on a job that isn't a canonical_candidate should fail gracefully."""
    pytest.skip("/publish_kb on non-candidate not yet implemented (Phase 5, T027)")


# ---------------------------------------------------------------------------
# Telegram-visible feedback
# ---------------------------------------------------------------------------

@pytest.mark.xfail(reason="T027: /publish_kb response formatting not yet implemented")
def test_publish_kb_success_shows_publication_summary():
    """Successful promotion should return a formatted publication summary."""
    pytest.skip("Publication summary formatting not yet implemented (Phase 5, T027)")


@pytest.mark.xfail(reason="T027: /publish_kb response formatting not yet implemented")
def test_publish_kb_rejection_shows_rationale():
    """If the candidate was previously rejected, /publish_kb should explain why."""
    pytest.skip("Rejection display not yet implemented (Phase 5, T027)")
