"""Tests for orchestrator route classification."""

from agent.orchestrator.router import classify_route


def test_repo_jobs_route_to_dev_workflow():
    decision = classify_route(
        {
            "job_type": "inspect_repo",
            "source_type": "repo",
            "intent": "inspect_repo",
            "metadata_json": {},
        },
        [],
    )

    assert decision.route_class == "dev_workflow"
    assert decision.confidence >= 0.9


def test_task_named_artifacts_route_to_actionable_task():
    decision = classify_route(
        {
            "job_type": "ingest_attachment",
            "source_type": "telegram",
            "intent": "",
            "metadata_json": {},
        },
        [
            {
                "storage_path": "/tmp/action-items.txt",
                "metadata_json": {"display_name": "action-items.txt"},
            }
        ],
    )

    assert decision.route_class == "actionable_task"


def test_pdf_defaults_to_raw_archive():
    decision = classify_route(
        {
            "job_type": "ingest_attachment",
            "source_type": "telegram",
            "intent": "",
            "metadata_json": {},
        },
        [
            {
                "storage_path": "/tmp/statement.pdf",
                "metadata_json": {"display_name": "statement.pdf"},
            }
        ],
    )

    assert decision.route_class == "raw_archive"
