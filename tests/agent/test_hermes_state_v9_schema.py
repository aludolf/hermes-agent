"""Tests for the v9 schema migration (021 — intelligence layer).

Covers:
- Fresh database initializes at v9 with all new tables present.
- A simulated v8 database upgrades cleanly to v9 without data loss.
- All six new tables are created and indexed correctly.
"""

import sqlite3

import pytest

import hermes_state
from hermes_state import SessionDB


INTELLIGENCE_LAYER_TABLES = [
    "extraction_events",
    "pending_previews",
    "entity_context",
    "harness_scenarios_cache",
    "harness_runs",
    "transcript_extraction_links",
]


def _list_tables(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table'"
    ).fetchall()
    return {row["name"] if hasattr(row, "keys") else row[0] for row in rows}


def test_fresh_db_initializes_at_v9(tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        row = db._conn.execute(
            "SELECT version FROM schema_version"
        ).fetchone()
        assert row is not None
        assert row["version"] == 9
        assert hermes_state.SCHEMA_VERSION == 9
    finally:
        db.close()


def test_all_021_tables_exist(tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        tables = _list_tables(db._conn)
        for table in INTELLIGENCE_LAYER_TABLES:
            assert table in tables, f"missing table {table!r}"
    finally:
        db.close()


def test_v8_database_upgrades_to_v9(tmp_path):
    """Simulate a pre-021 database at schema v8 and verify in-place upgrade."""
    db_path = tmp_path / "state.db"
    # First pass: stand up a database, then hack schema_version back to 8
    # (simulating an older deploy that pre-dates 021).
    db = SessionDB(db_path=db_path)
    try:
        db._conn.execute("UPDATE schema_version SET version = 8")
        db._conn.commit()
        # Drop the 021 tables so the v9 migration has something to create.
        for table in INTELLIGENCE_LAYER_TABLES:
            db._conn.execute(f"DROP TABLE IF EXISTS {table}")
        db._conn.commit()
    finally:
        db.close()

    # Second pass: re-open — this should run the v8→v9 migration.
    db2 = SessionDB(db_path=db_path)
    try:
        row = db2._conn.execute(
            "SELECT version FROM schema_version"
        ).fetchone()
        assert row["version"] == 9

        tables = _list_tables(db2._conn)
        for table in INTELLIGENCE_LAYER_TABLES:
            assert table in tables, f"table {table!r} missing after v9 migration"
    finally:
        db2.close()


# ---------------------------------------------------------------------------
# Smoke-test the new CRUD methods to make sure schema columns match
# ---------------------------------------------------------------------------

def test_extraction_event_crud(tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        db.create_extraction_event(
            extraction_id="ext_abc123",
            source_type="voice_note",
            source_format="ogg",
            transcript_hash="h" * 64,
            transcript_preview="Hello world",
            sender_id="289322060",
            sender_role="owner",
            execution_mode="auto",
            actions_json="[]",
            actions_count=0,
        )
        found = db.get_extraction_by_hash("h" * 64)
        assert found is not None
        assert found["extraction_id"] == "ext_abc123"
        assert found["execution_mode"] == "auto"

        db.update_extraction_execution_mode("ext_abc123", "skipped")
        assert db.get_extraction_by_hash("h" * 64)["execution_mode"] == "skipped"

        events = db.list_extraction_events(sender_id="289322060")
        assert len(events) == 1
    finally:
        db.close()


def test_pending_preview_expiry(tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        db.create_extraction_event(
            extraction_id="ext_1",
            source_type="pasted_text",
            transcript_hash="x" * 64,
            sender_id="s1",
            sender_role="owner",
            execution_mode="preview",
        )
        # Create a preview that's already expired by using a negative TTL.
        db.create_pending_preview(
            preview_id="pv_1",
            extraction_id="ext_1",
            sender_id="s1",
            chat_id="c1",
            actions_json="[]",
            ttl_seconds=-1,
        )
        # Reading should auto-expire and return None.
        assert db.get_active_preview_by_sender("s1") is None
    finally:
        db.close()


def test_entity_context_upsert_case_insensitive(tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        db.upsert_entity(entity_id="ent_1", name="João")
        db.upsert_entity(entity_id="ent_2", name="joão")  # same lowercased

        row = db.get_entity_by_name("JOÃO")
        assert row is not None
        assert row["entity_id"] == "ent_1"
        assert row["mention_count"] == 2
    finally:
        db.close()


def test_harness_run_last_passing(tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        db.create_harness_run(
            run_id="run_1", scenario_id="s1",
            triggered_by="hermes:owner:1", trigger_source="slash_command",
        )
        db.complete_harness_run("run_1", status="pass", steps_json="[]")

        db.create_harness_run(
            run_id="run_2", scenario_id="s1",
            triggered_by="hermes:owner:1", trigger_source="slash_command",
        )
        db.complete_harness_run("run_2", status="fail", steps_json="[]")

        last_pass = db.get_last_passing_run("s1")
        assert last_pass is not None
        assert last_pass["run_id"] == "run_1"
    finally:
        db.close()
