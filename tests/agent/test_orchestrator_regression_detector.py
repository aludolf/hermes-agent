"""Tests for the per-step regression detector (021 US5)."""

from __future__ import annotations

import pytest

from agent.orchestrator.regression_detector import detect_regressions


def _run(steps: list[dict]) -> dict:
    return {"run_id": "run_x", "steps": steps}


def test_no_baseline_returns_empty():
    current = _run([{"step": 1, "status": "fail"}])
    assert detect_regressions(current, None) == []


def test_step_that_regressed_is_flagged():
    current = _run([
        {"step": 1, "status": "pass"},
        {"step": 2, "status": "fail"},
    ])
    baseline = _run([
        {"step": 1, "status": "pass"},
        {"step": 2, "status": "pass"},
    ])
    assert detect_regressions(current, baseline) == [2]


def test_step_that_always_failed_is_not_flagged():
    current = _run([{"step": 1, "status": "fail"}])
    baseline = _run([{"step": 1, "status": "fail"}])
    assert detect_regressions(current, baseline) == []


def test_new_step_without_baseline_is_not_flagged():
    """Step 3 was added after baseline — no comparison possible."""
    current = _run([
        {"step": 1, "status": "pass"},
        {"step": 2, "status": "pass"},
        {"step": 3, "status": "fail"},  # new, no baseline
    ])
    baseline = _run([
        {"step": 1, "status": "pass"},
        {"step": 2, "status": "pass"},
    ])
    assert detect_regressions(current, baseline) == []


def test_multiple_regressions_sorted():
    current = _run([
        {"step": 1, "status": "fail"},
        {"step": 2, "status": "pass"},
        {"step": 3, "status": "timeout"},
    ])
    baseline = _run([
        {"step": 1, "status": "pass"},
        {"step": 2, "status": "pass"},
        {"step": 3, "status": "pass"},
    ])
    assert detect_regressions(current, baseline) == [1, 3]


def test_timeout_and_error_are_regressions():
    baseline = _run([
        {"step": 1, "status": "pass"},
        {"step": 2, "status": "pass"},
    ])
    current_timeout = _run([
        {"step": 1, "status": "pass"},
        {"step": 2, "status": "timeout"},
    ])
    current_error = _run([
        {"step": 1, "status": "error"},
        {"step": 2, "status": "pass"},
    ])
    assert detect_regressions(current_timeout, baseline) == [2]
    assert detect_regressions(current_error, baseline) == [1]


def test_skipped_current_is_not_regression():
    """Gated short-circuit → downstream skipped; not a regression."""
    current = _run([
        {"step": 1, "status": "fail", "gate": True},
        {"step": 2, "status": "skipped"},
    ])
    baseline = _run([
        {"step": 1, "status": "pass", "gate": True},
        {"step": 2, "status": "pass"},
    ])
    # Step 1 is the real regression; step 2 'skipped' should not be flagged.
    assert detect_regressions(current, baseline) == [1]


def test_handles_missing_steps_gracefully():
    """Malformed current/baseline shapes shouldn't crash."""
    assert detect_regressions({}, {}) == []
    assert detect_regressions({"steps": None}, {"steps": None}) == []
    assert detect_regressions({"steps": [{"step": None, "status": "fail"}]},
                               {"steps": [{"step": None, "status": "pass"}]}) == []
