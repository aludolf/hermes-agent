"""Tests for layer-aware status and reporting in gateway commands.

Spec: specs/002-knowledge-layer-split/spec.md — User Stories 1 & 2 (reporting)
Contract: specs/002-knowledge-layer-split/contracts/layer-routing-contract.md

Phase 1 scaffold — tests are marked xfail until Phase 3 (T017) and
Phase 4 (T023) add knowledge_tier and working artifact metadata to
the reporting payloads and gateway surfaces.
"""

import pytest


# ---------------------------------------------------------------------------
# US1: Layer state visible in /jobs and /status output
# ---------------------------------------------------------------------------

@pytest.mark.xfail(reason="T013/T017: layer-aware reporting not yet implemented")
def test_job_summary_includes_knowledge_tier(tmp_path):
    """format_job_summary must include knowledge_tier alongside route_class."""
    from agent.orchestrator.reporting import format_job_summary

    summary = format_job_summary({
        "job_id": "job_001",
        "job_type": "ingest_attachment",
        "status": "completed",
        "route_class": "raw_archive",
        "knowledge_tier": "raw_only",
    })
    assert "raw_only" in summary
    assert "knowledge_tier" in summary.lower() or "tier" in summary.lower()


@pytest.mark.xfail(reason="T013/T017: layer-aware list reporting not yet implemented")
def test_job_summary_list_shows_tier_per_job():
    """Multi-job overview must show knowledge_tier for each job."""
    from agent.orchestrator.reporting import format_job_summary_list

    summaries = [
        {"job_id": "j1", "job_type": "inspect_repo", "status": "completed",
         "route_class": "dev_workflow", "knowledge_tier": "working"},
        {"job_id": "j2", "job_type": "kb_publish", "status": "completed",
         "route_class": "kb_candidate", "knowledge_tier": "canonical_candidate"},
    ]
    output = format_job_summary_list(summaries)
    assert "working" in output
    assert "canonical_candidate" in output


# ---------------------------------------------------------------------------
# US2: Working artifact metadata in reporting
# ---------------------------------------------------------------------------

@pytest.mark.xfail(reason="T023: working artifact retrieval metadata not yet surfaced")
def test_job_summary_includes_working_artifact_metadata():
    """Jobs with working outputs should surface artifact kind and destination."""
    from agent.orchestrator.reporting import format_job_summary

    summary = format_job_summary({
        "job_id": "job_002",
        "job_type": "inspect_repo",
        "status": "completed",
        "route_class": "dev_workflow",
        "knowledge_tier": "working",
        "working_artifacts": [
            {"working_id": "wrk_001", "artifact_kind": "repo_inspection",
             "title": "hermes-agent repo summary", "status": "active"},
        ],
    })
    assert "repo_inspection" in summary or "hermes-agent" in summary


# ---------------------------------------------------------------------------
# Layer filtering in status queries
# ---------------------------------------------------------------------------

@pytest.mark.xfail(reason="T017: layer-filtered status not yet implemented in gateway")
def test_status_query_filters_by_knowledge_tier():
    """Status queries should support filtering by knowledge tier."""
    # This will test the gateway's ability to filter /jobs output by tier.
    # E.g., /jobs --tier=working should only show working-tier items.
    pytest.skip("Tier-filtered status queries not yet implemented (Phase 3, T017)")
