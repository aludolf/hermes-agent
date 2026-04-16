"""Tests for the YAML ScenarioLoader (021 US6)."""

from __future__ import annotations

import json

import pytest

from agent.orchestrator.scenario_loader import (
    ScenarioLoader,
    ScenarioValidationError,
)
from hermes_state import SessionDB


def _write(path, body: str) -> None:
    path.write_text(body, encoding="utf-8")


def _valid_yaml(**overrides: str) -> str:
    base = {
        "id": "rauru_smoke",
        "name": "Rauru smoke",
        "category": "smoke",
        "target_bot": "Rauru_HD_bot",
        "target_chat_id": '"-1001"',
    }
    base.update(overrides)
    return (
        f"id: {base['id']}\n"
        f"name: \"{base['name']}\"\n"
        f"category: {base['category']}\n"
        f"target_bot: {base['target_bot']}\n"
        f"target_chat_id: {base['target_chat_id']}\n"
        "timeout_per_step_ms: 15000\n"
        "steps:\n"
        "  - step: 1\n"
        "    sent: \"/start\"\n"
        "    expected_pattern: \"Welcome\"\n"
        "    gate: true\n"
        "  - step: 2\n"
        "    sent: \"English\"\n"
        "    expected_pattern: \"date of birth\"\n"
    )


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------

def test_load_all_from_dir_happy_path(tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        scen_dir = tmp_path / "scenarios"
        scen_dir.mkdir()
        _write(scen_dir / "a.yaml", _valid_yaml(id="rauru_smoke"))
        _write(scen_dir / "b.yaml", _valid_yaml(
            id="rauru_full_onboarding", name="Full onboarding",
        ))

        stats = ScenarioLoader(db).load_all_from_dir(scen_dir)
        assert stats.loaded == 2
        assert stats.skipped == 0

        rows = db.list_scenarios()
        ids = {r["scenario_id"] for r in rows}
        assert ids == {"rauru_smoke", "rauru_full_onboarding"}

        # Steps serialized as JSON in DB
        row = db.get_scenario_by_id("rauru_smoke")
        assert row is not None
        steps = json.loads(row["steps_json"])
        assert len(steps) == 2
        assert steps[0]["expected_pattern"] == "Welcome"
    finally:
        db.close()


def test_load_clears_cache_between_runs(tmp_path):
    """A deleted YAML should not linger in the cache after reload."""
    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        scen_dir = tmp_path / "scenarios"
        scen_dir.mkdir()
        _write(scen_dir / "a.yaml", _valid_yaml(id="rauru_smoke"))
        _write(scen_dir / "b.yaml", _valid_yaml(id="to_be_deleted"))

        loader = ScenarioLoader(db)
        loader.load_all_from_dir(scen_dir)
        assert {r["scenario_id"] for r in db.list_scenarios()} == {
            "rauru_smoke", "to_be_deleted",
        }

        (scen_dir / "b.yaml").unlink()
        loader.load_all_from_dir(scen_dir)
        assert {r["scenario_id"] for r in db.list_scenarios()} == {"rauru_smoke"}
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Validation errors — invalid scenarios should skip without aborting the batch
# ---------------------------------------------------------------------------

def test_invalid_yaml_skipped_but_others_still_loaded(tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        scen_dir = tmp_path / "scenarios"
        scen_dir.mkdir()
        _write(scen_dir / "good.yaml", _valid_yaml(id="rauru_smoke"))
        _write(scen_dir / "bad.yaml", "not: [unterminated")

        stats = ScenarioLoader(db).load_all_from_dir(scen_dir)
        assert stats.loaded == 1
        assert stats.skipped == 1
        assert any("bad.yaml" in e for e in stats.errors)
    finally:
        db.close()


def test_missing_required_field_is_validation_error(tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        scen_dir = tmp_path / "scenarios"
        scen_dir.mkdir()
        _write(scen_dir / "x.yaml", _valid_yaml().replace(
            "target_bot: Rauru_HD_bot\n", "",
        ))
        stats = ScenarioLoader(db).load_all_from_dir(scen_dir)
        assert stats.loaded == 0
        assert stats.skipped == 1
        assert "target_bot" in stats.errors[0]
    finally:
        db.close()


def test_bad_category_rejected(tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        scen_dir = tmp_path / "scenarios"
        scen_dir.mkdir()
        _write(scen_dir / "x.yaml", _valid_yaml(category="smog"))
        stats = ScenarioLoader(db).load_all_from_dir(scen_dir)
        assert stats.skipped == 1
        assert "category" in stats.errors[0]
    finally:
        db.close()


def test_non_sequential_step_numbers_rejected(tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        scen_dir = tmp_path / "scenarios"
        scen_dir.mkdir()
        body = (
            "id: skip_step\n"
            "name: \"skip\"\n"
            "category: smoke\n"
            "target_bot: b\n"
            "target_chat_id: \"-1\"\n"
            "steps:\n"
            "  - step: 1\n    sent: a\n    expected_pattern: a\n"
            "  - step: 3\n    sent: b\n    expected_pattern: b\n"  # skips 2
        )
        _write(scen_dir / "x.yaml", body)
        stats = ScenarioLoader(db).load_all_from_dir(scen_dir)
        assert stats.skipped == 1
        assert "sequential" in stats.errors[0].lower()
    finally:
        db.close()


def test_bad_match_type_rejected(tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        scen_dir = tmp_path / "scenarios"
        scen_dir.mkdir()
        body = (
            "id: bad_match\n"
            "name: \"x\"\n"
            "category: smoke\n"
            "target_bot: b\n"
            "target_chat_id: \"-1\"\n"
            "steps:\n"
            "  - step: 1\n    sent: a\n    expected_pattern: a\n"
            "    match_type: fuzzy\n"
        )
        _write(scen_dir / "x.yaml", body)
        stats = ScenarioLoader(db).load_all_from_dir(scen_dir)
        assert stats.skipped == 1
        assert "match_type" in stats.errors[0]
    finally:
        db.close()


def test_timeout_out_of_range_rejected(tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        scen_dir = tmp_path / "scenarios"
        scen_dir.mkdir()
        body = _valid_yaml().replace(
            "timeout_per_step_ms: 15000", "timeout_per_step_ms: 999999",
        )
        _write(scen_dir / "x.yaml", body)
        stats = ScenarioLoader(db).load_all_from_dir(scen_dir)
        assert stats.skipped == 1
    finally:
        db.close()


def test_uppercase_id_rejected(tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        scen_dir = tmp_path / "scenarios"
        scen_dir.mkdir()
        _write(scen_dir / "x.yaml", _valid_yaml(id="Rauru_Smoke"))
        stats = ScenarioLoader(db).load_all_from_dir(scen_dir)
        assert stats.skipped == 1
        assert "lowercase" in stats.errors[0].lower()
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Missing directory tolerated
# ---------------------------------------------------------------------------

def test_missing_directory_returns_empty_stats(tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        stats = ScenarioLoader(db).load_all_from_dir(tmp_path / "does-not-exist")
        assert stats.loaded == 0
        assert stats.skipped == 0
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Estimated duration is computed
# ---------------------------------------------------------------------------

def test_estimated_duration_sums_per_step_timeouts(tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        scen_dir = tmp_path / "scenarios"
        scen_dir.mkdir()
        body = (
            "id: timings\n"
            "name: \"t\"\n"
            "category: smoke\n"
            "target_bot: b\n"
            "target_chat_id: \"-1\"\n"
            "timeout_per_step_ms: 5000\n"
            "steps:\n"
            "  - step: 1\n    sent: a\n    expected_pattern: a\n"
            "    timeout_ms: 3000\n"
            "  - step: 2\n    sent: b\n    expected_pattern: b\n"
            # no per-step override → uses 5000
        )
        _write(scen_dir / "x.yaml", body)
        ScenarioLoader(db).load_all_from_dir(scen_dir)
        row = db.get_scenario_by_id("timings")
        assert row["estimated_duration_ms"] == 3000 + 5000
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Actual shipped YAMLs parse cleanly
# ---------------------------------------------------------------------------

def test_all_shipped_yaml_files_load(tmp_path):
    """Load the real `scenarios/*.yaml` files from the repo and verify they pass."""
    from pathlib import Path
    repo_scenarios = Path(__file__).resolve().parents[2] / "scenarios"
    yaml_files = list(repo_scenarios.glob("*.yaml"))
    assert len(yaml_files) >= 8, f"expected >= 8 YAML scenarios, got {len(yaml_files)}"

    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        stats = ScenarioLoader(db).load_all_from_dir(repo_scenarios)
        assert stats.skipped == 0, f"shipped YAMLs failed validation: {stats.errors}"
        assert stats.loaded >= 8
    finally:
        db.close()
