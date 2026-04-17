"""Tests for the v10 schema migration (022 — Teams & Email sentinels).

Covers:
- Fresh database initializes at v10 with all 5 new tables present.
- A simulated v9 database upgrades cleanly to v10.
- Each new table's CRUD methods round-trip.
"""

import sqlite3

import pytest

import hermes_state
from hermes_state import SessionDB


SENTINELS_TABLES = [
    "credentials_store",
    "teams_watches",
    "mail_accounts",
    "mail_watches",
    "backfill_jobs",
]


def _list_tables(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table'"
    ).fetchall()
    return {row["name"] if hasattr(row, "keys") else row[0] for row in rows}


def test_fresh_db_initializes_at_v10(tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        row = db._conn.execute(
            "SELECT version FROM schema_version"
        ).fetchone()
        assert row["version"] == 10
        assert hermes_state.SCHEMA_VERSION == 10
    finally:
        db.close()


def test_all_022_tables_exist(tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        tables = _list_tables(db._conn)
        for t in SENTINELS_TABLES:
            assert t in tables, f"missing table {t!r}"
    finally:
        db.close()


def test_v9_database_upgrades_to_v10(tmp_path):
    """Simulate a pre-022 database at schema v9 and verify in-place upgrade."""
    db_path = tmp_path / "state.db"
    db = SessionDB(db_path=db_path)
    try:
        db._conn.execute("UPDATE schema_version SET version = 9")
        db._conn.commit()
        for t in SENTINELS_TABLES:
            db._conn.execute(f"DROP TABLE IF EXISTS {t}")
        db._conn.commit()
    finally:
        db.close()

    db2 = SessionDB(db_path=db_path)
    try:
        row = db2._conn.execute(
            "SELECT version FROM schema_version"
        ).fetchone()
        assert row["version"] == 10
        tables = _list_tables(db2._conn)
        for t in SENTINELS_TABLES:
            assert t in tables
    finally:
        db2.close()


# ---------------------------------------------------------------------------
# CRUD round-trips
# ---------------------------------------------------------------------------

def test_credentials_store_crud(tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        db.put_credential_ciphertext(
            credential_id="cred_1",
            kind="imap_app_password",
            ciphertext=b"gAAAAAB_fakeciphertext",
            label="personal gmail",
        )
        row = db.get_credential_ciphertext("cred_1")
        assert row is not None
        assert row["kind"] == "imap_app_password"
        assert bytes(row["ciphertext"]) == b"gAAAAAB_fakeciphertext"
        assert row["label"] == "personal gmail"

        db.rotate_credential("cred_1", b"gAAAAAB_new")
        row = db.get_credential_ciphertext("cred_1")
        assert bytes(row["ciphertext"]) == b"gAAAAAB_new"
        assert row["rotated_at"] is not None

        # list_credentials_by_kind must NOT leak ciphertext
        meta = db.list_credentials_by_kind(kind="imap_app_password")
        assert len(meta) == 1
        assert "ciphertext" not in meta[0]
        assert meta[0]["credential_id"] == "cred_1"

        db.delete_credential("cred_1")
        assert db.get_credential_ciphertext("cred_1") is None
    finally:
        db.close()


def test_teams_watches_crud(tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        db.create_teams_watch(
            watch_id="tw_1", owner_id="289",
            resource_type="chat", ms_resource_id="19:abc@thread.v2",
            alias="family", client_state="nonce1",
        )
        rec = db.get_teams_watch("tw_1")
        assert rec["alias"] == "family"
        assert rec["delivery_mode"] == "realtime"
        assert rec["owner_id"] == "289"

        # Unique (owner_id, ms_resource_id)
        with pytest.raises(sqlite3.IntegrityError):
            db.create_teams_watch(
                watch_id="tw_dup", owner_id="289",
                resource_type="chat", ms_resource_id="19:abc@thread.v2",
            )

        db.update_teams_watch_subscription(
            "tw_1", subscription_id="sub_abc", expires_at=9999999999.0,
        )
        assert db.get_teams_watch("tw_1")["subscription_id"] == "sub_abc"

        db.update_teams_watch_mode("tw_1", "polling", polling_cursor="msg_42")
        rec = db.get_teams_watch("tw_1")
        assert rec["delivery_mode"] == "polling"
        assert rec["polling_cursor"] == "msg_42"

        db.set_teams_watch_mute("tw_1", mute_until=0)  # indefinite mute
        assert db.get_teams_watch("tw_1")["mute_until"] == 0

        watches = db.list_teams_watches(owner_id="289")
        assert len(watches) == 1

        db.delete_teams_watch("tw_1")
        assert db.get_teams_watch("tw_1") is None
    finally:
        db.close()


def test_mail_accounts_and_watches_crud(tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        db.put_credential_ciphertext(
            credential_id="cred_x", kind="imap_app_password",
            ciphertext=b"x",
        )
        db.create_mail_account(
            account_id="ma_1", alias="personal",
            host="imap.gmail.com", username="me@gmail.com",
            auth_method="app_password", credential_ref="cred_x",
            owner_id="289",
        )
        rec = db.get_mail_account("ma_1")
        assert rec["connection_state"] == "disconnected"

        # Unique (owner_id, alias)
        with pytest.raises(sqlite3.IntegrityError):
            db.create_mail_account(
                account_id="ma_dup", alias="personal",
                host="x", username="y",
                auth_method="app_password", credential_ref="cred_x",
                owner_id="289",
            )

        db.update_mail_account_state("ma_1", "live")
        rec = db.get_mail_account("ma_1")
        assert rec["connection_state"] == "live"
        assert rec["last_successful_sync_at"] is not None

        db.create_mail_watch(watch_id="mw_1", account_id="ma_1", folder="INBOX")
        db.create_mail_watch(watch_id="mw_2", account_id="ma_1", folder="Sent")
        watches = db.list_mail_watches_by_account("ma_1")
        assert len(watches) == 2

        db.update_mail_watch_cursor("mw_1", last_seen_uid=100, uidvalidity=12345)
        db.update_mail_watch_state("mw_1", "idling")
        rec = db.list_mail_watches_by_account("ma_1")[0]
        assert rec["last_seen_uid"] == 100
        assert rec["idle_state"] == "idling"

        db.update_mail_watch_state("mw_1", "reconnecting_backoff", increment_reconnect=True)
        rec = db.list_mail_watches_by_account("ma_1")[0]
        assert rec["reconnect_attempts"] == 1

        # Delete account cascades watches
        db.delete_mail_account("ma_1")
        assert db.get_mail_account("ma_1") is None
        assert db.list_mail_watches_by_account("ma_1") == []
    finally:
        db.close()


def test_backfill_jobs_crud(tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        db.put_credential_ciphertext(
            credential_id="cred_b", kind="imap_app_password", ciphertext=b"x",
        )
        db.create_mail_account(
            account_id="ma_b", alias="work", host="h", username="u",
            auth_method="app_password", credential_ref="cred_b", owner_id="289",
        )

        db.create_backfill_job(
            job_id="bf_1", account_id="ma_b",
            scope_hash="hash1", folders_json='["INBOX"]',
            owner_id="289",
        )
        assert db.get_backfill_job("bf_1")["state"] == "queued"
        assert db.get_active_backfill_job("ma_b", "hash1")["job_id"] == "bf_1"

        db.start_backfill_job("bf_1", total_estimated=1000)
        assert db.get_backfill_job("bf_1")["state"] == "running"
        assert db.get_backfill_job("bf_1")["total_estimated"] == 1000

        db.update_backfill_progress("bf_1", processed_count=500, failed_count=3)
        rec = db.get_backfill_job("bf_1")
        assert rec["processed_count"] == 500
        assert rec["failed_count"] == 3

        db.update_backfill_cursor("bf_1", '{"INBOX": {"uidvalidity": 1, "last_processed_uid": 500}}')
        assert "last_processed_uid" in db.get_backfill_job("bf_1")["resumption_cursor_json"]

        # Resumable list picks this up
        resumable = db.list_resumable_backfill_jobs()
        assert any(r["job_id"] == "bf_1" for r in resumable)

        db.pause_backfill_job("bf_1")
        assert db.get_backfill_job("bf_1")["state"] == "paused"

        db.complete_backfill_job("bf_1")
        rec = db.get_backfill_job("bf_1")
        assert rec["state"] == "completed"
        assert rec["finished_at"] is not None

        # After completion no longer considered "active" for the dedup gate
        assert db.get_active_backfill_job("ma_b", "hash1") is None
    finally:
        db.close()
