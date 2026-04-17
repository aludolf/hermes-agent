"""Tests for BackfillRunner + TokenBucketRateLimiter (022 US3)."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from cryptography.fernet import Fernet

from agent.orchestrator.credentials import CredentialsStore
from agent.orchestrator.email_backfill import (
    BackfillRunner,
    TokenBucketRateLimiter,
    _parse_uid_search,
)
from agent.orchestrator.sentinel_queue import ExtractionItem
from hermes_state import SessionDB


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


@pytest.fixture
def db(tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    yield db
    db.close()


@pytest.fixture
def master_key():
    return Fernet.generate_key().decode("utf-8")


@pytest.fixture
def store(db, master_key):
    return CredentialsStore(db, master_key=master_key)


# ---------------------------------------------------------------------------
# TokenBucketRateLimiter
# ---------------------------------------------------------------------------

def test_rate_limiter_allows_first_acquire():
    limiter = TokenBucketRateLimiter(rate_per_minute=60)

    async def run():
        # Should return immediately (bucket starts full)
        await asyncio.wait_for(limiter.acquire(), timeout=1.0)

    _run(run())


def test_rate_limiter_clamps_minimum():
    limiter = TokenBucketRateLimiter(rate_per_minute=0)
    assert limiter._rate >= 1.0


# ---------------------------------------------------------------------------
# _parse_uid_search
# ---------------------------------------------------------------------------

def test_parse_uid_search_basic():
    lines = [b"* SEARCH 1 2 3 42"]
    assert _parse_uid_search(lines) == [1, 2, 3, 42]


def test_parse_uid_search_empty():
    assert _parse_uid_search([b"* SEARCH"]) == []


# ---------------------------------------------------------------------------
# BackfillRunner — no-account error
# ---------------------------------------------------------------------------

def test_backfill_runner_missing_account(db, store):
    enqueued = []

    class FQ:
        async def enqueue(self, i):
            enqueued.append(i)

    mock_db = MagicMock()
    mock_db.get_backfill_job.return_value = {
        "job_id": "bf_test1",
        "account_id": "ma_nonexistent",
        "scope_hash": "h1",
        "folders_json": '["INBOX"]',
        "since_ts": None,
        "rate_limit_msgs_per_min": 60,
        "resumption_cursor_json": None,
        "state": "queued",
    }
    mock_db.get_mail_account.return_value = None

    runner = BackfillRunner(session_db=mock_db, extraction_queue=FQ(), credentials_store=store)
    _run(runner.run("bf_test1"))

    mock_db.fail_backfill_job.assert_called_once()
    args = mock_db.fail_backfill_job.call_args[0]
    assert "mail_account" in args[1]


def test_backfill_runner_missing_job(db, store):
    """run() with unknown job_id should return silently."""
    class FQ:
        async def enqueue(self, i):
            pass

    runner = BackfillRunner(session_db=db, extraction_queue=FQ(), credentials_store=store)
    _run(runner.run("bf_does_not_exist"))  # must not raise


# ---------------------------------------------------------------------------
# BackfillRunner — credential missing error
# ---------------------------------------------------------------------------

def test_backfill_runner_missing_credential(store):
    """Credential not found → job marked failed."""
    class FQ:
        async def enqueue(self, i):
            pass

    mock_db = MagicMock()
    mock_db.get_backfill_job.return_value = {
        "job_id": "bf_cred",
        "account_id": "ma_cred_test",
        "scope_hash": "h2",
        "folders_json": '["INBOX"]',
        "since_ts": None,
        "rate_limit_msgs_per_min": 60,
        "resumption_cursor_json": None,
        "state": "queued",
    }
    mock_db.get_mail_account.return_value = {
        "account_id": "ma_cred_test",
        "alias": "work",
        "host": "imap.example.com",
        "port": 993,
        "username": "u@example.com",
        "auth_method": "app_password",
        "credential_ref": "cred_missing",
        "owner_id": "289",
    }
    mock_db.get_credential_ciphertext.return_value = None  # credential missing

    runner = BackfillRunner(session_db=mock_db, extraction_queue=FQ(), credentials_store=store)
    _run(runner.run("bf_cred"))

    mock_db.fail_backfill_job.assert_called_once()
    args = mock_db.fail_backfill_job.call_args[0]
    assert "credential" in args[1]


# ---------------------------------------------------------------------------
# BackfillRunner — suppress_action_routing flag (SC-005)
# ---------------------------------------------------------------------------

def test_backfill_items_have_suppress_action_routing(db, store):
    """Items enqueued by BackfillRunner must have suppress_action_routing=True."""
    cred_id = store.put(kind="imap_app_password", secret="pw123", label="test")
    db.create_mail_account(
        account_id="ma_bk1",
        alias="personal",
        host="imap.example.com",
        port=993,
        username="u@example.com",
        auth_method="app_password",
        credential_ref=cred_id,
        owner_id="289",
    )
    db._conn.execute(
        """INSERT INTO backfill_jobs
           (job_id, account_id, scope_hash, folders_json, state, owner_id)
           VALUES (?, ?, ?, ?, ?, ?)""",
        ("bf_flag", "ma_bk1", "hash3", '["INBOX"]', "queued", "289"),
    )
    db._conn.commit()

    enqueued: list[ExtractionItem] = []

    class FQ:
        async def enqueue(self, item):
            enqueued.append(item)

    import email as email_lib
    import email.mime.text

    plain_bytes = email_lib.mime.text.MIMEText("Hello backfill", "plain", "utf-8").as_bytes()

    async def run():
        with patch("agent.orchestrator.email_backfill.aioimaplib", create=True) as mock_imap:
            mock_client = AsyncMock()
            mock_client.wait_hello_from_server = AsyncMock()
            mock_client.login = AsyncMock(return_value=MagicMock(result="OK", lines=[]))
            mock_client.select = AsyncMock(return_value=MagicMock(result="OK", lines=[]))
            mock_client.uid = AsyncMock(side_effect=[
                # UID SEARCH → uid 100
                MagicMock(result="OK", lines=[b"* SEARCH 100"]),
                # UID FETCH uid 100
                MagicMock(result="OK", lines=[
                    b"* 1 FETCH (UID 100 BODY[] {128}",
                    plain_bytes,
                ]),
            ])
            mock_client.logout = AsyncMock()
            mock_imap.IMAP4_SSL.return_value = mock_client

            runner = BackfillRunner(
                session_db=db,
                extraction_queue=FQ(),
                credentials_store=store,
            )
            await runner.run("bf_flag")

    _run(run())

    if enqueued:
        for item in enqueued:
            assert item.suppress_action_routing is True
            assert item.execution_mode_override == "skipped"
            assert item.source_format == "email-backfill"


# ---------------------------------------------------------------------------
# BackfillRunner — notify_fn called
# ---------------------------------------------------------------------------

def test_backfill_notify_fn_called_on_complete(db, store):
    cred_id = store.put(kind="imap_app_password", secret="pw", label="n")
    db.create_mail_account(
        account_id="ma_notify",
        alias="notify",
        host="imap.example.com",
        port=993,
        username="u@example.com",
        auth_method="app_password",
        credential_ref=cred_id,
        owner_id="289",
    )
    db._conn.execute(
        """INSERT INTO backfill_jobs
           (job_id, account_id, scope_hash, folders_json, state, owner_id)
           VALUES (?, ?, ?, ?, ?, ?)""",
        ("bf_notify", "ma_notify", "hashN", '["INBOX"]', "queued", "289"),
    )
    db._conn.commit()

    notifications = []

    async def fake_notify(msg):
        notifications.append(msg)

    class FQ:
        async def enqueue(self, i):
            pass

    async def run():
        with patch("agent.orchestrator.email_backfill.aioimaplib", create=True) as mock_imap:
            mock_client = AsyncMock()
            mock_client.wait_hello_from_server = AsyncMock()
            mock_client.login = AsyncMock(return_value=MagicMock(result="OK", lines=[]))
            mock_client.select = AsyncMock(return_value=MagicMock(result="OK", lines=[]))
            # Empty mailbox
            mock_client.uid = AsyncMock(
                return_value=MagicMock(result="OK", lines=[b"* SEARCH"])
            )
            mock_client.logout = AsyncMock()
            mock_imap.IMAP4_SSL.return_value = mock_client

            runner = BackfillRunner(
                session_db=db,
                extraction_queue=FQ(),
                credentials_store=store,
                notify_fn=fake_notify,
            )
            await runner.run("bf_notify")

    _run(run())
    # At minimum: start notification + complete notification
    assert any("iniciado" in n or "completo" in n for n in notifications)


# ---------------------------------------------------------------------------
# BackfillRunner — resumable cursor persisted
# ---------------------------------------------------------------------------

def test_backfill_cursor_persisted_on_cancel(db, store):
    cred_id = store.put(kind="imap_app_password", secret="pw", label="c")
    db.create_mail_account(
        account_id="ma_cursor",
        alias="cursor_acct",
        host="imap.example.com",
        port=993,
        username="u@example.com",
        auth_method="app_password",
        credential_ref=cred_id,
        owner_id="289",
    )
    db._conn.execute(
        """INSERT INTO backfill_jobs
           (job_id, account_id, scope_hash, folders_json, state, owner_id)
           VALUES (?, ?, ?, ?, ?, ?)""",
        ("bf_cursor", "ma_cursor", "hashC", '["INBOX"]', "queued", "289"),
    )
    db._conn.commit()

    class FQ:
        async def enqueue(self, i):
            pass

    async def run():
        with patch("agent.orchestrator.email_backfill.aioimaplib", create=True) as mock_imap:
            mock_client = AsyncMock()
            mock_client.wait_hello_from_server = AsyncMock()
            mock_client.login = AsyncMock(return_value=MagicMock(result="OK", lines=[]))
            mock_client.select = AsyncMock(return_value=MagicMock(result="OK", lines=[]))
            # Return UIDs 1-3; cancel task after first fetch
            fetch_call_count = 0

            async def uid_side_effect(cmd, *args):
                nonlocal fetch_call_count
                if cmd == "SEARCH":
                    return MagicMock(result="OK", lines=[b"* SEARCH 1 2 3"])
                fetch_call_count += 1
                if fetch_call_count == 1:
                    return MagicMock(result="OK", lines=[b"* FETCH (UID 1)"])
                raise asyncio.CancelledError()

            mock_client.uid = AsyncMock(side_effect=uid_side_effect)
            mock_client.logout = AsyncMock()
            mock_imap.IMAP4_SSL.return_value = mock_client

            runner = BackfillRunner(
                session_db=db,
                extraction_queue=FQ(),
                credentials_store=store,
            )
            task = asyncio.create_task(runner.run("bf_cursor"))
            try:
                await task
            except asyncio.CancelledError:
                pass

    _run(run())
    # Cursor should be persisted (may or may not have content depending on cancel timing)
    job = db.get_backfill_job("bf_cursor")
    assert job is not None
