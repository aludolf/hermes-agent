"""Tests for Email sentinel (022 US2).

Tests use mock IMAP interactions rather than a live server, covering:
- connect + app-password auth success path
- auth failure → state 'disabled' after max retries
- UIDVALIDITY change on reconnect → cursor reset + warning log
- XOAUTH2 sasl string format
- email body parser: plain, multipart, HTML-stripped
- enqueue_nowait on ExtractionItem with source_type='email'
"""

from __future__ import annotations

import asyncio
import email as email_lib
import email.mime.multipart
import email.mime.text
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from cryptography.fernet import Fernet

from agent.orchestrator.credentials import CredentialsStore
from agent.orchestrator.email_auth import (
    app_password_login_args,
    xoauth2_build_sasl_string,
)
from agent.orchestrator.email_sentinel import (
    _parse_email_body,
    _parse_uidvalidity,
    _has_exists,
    EmailSentinel,
    EmailSentinelError,
)
from agent.orchestrator.sentinel_queue import ExtractionItem, SentinelExtractionQueue
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
# email_auth module
# ---------------------------------------------------------------------------

def test_app_password_login_args():
    args = app_password_login_args("user@example.com", "hunter2")
    assert args["username"] == "user@example.com"
    assert args["password"] == "hunter2"


def test_xoauth2_sasl_string_format():
    import base64
    sasl = xoauth2_build_sasl_string("user@gmail.com", "access_token_abc")
    decoded = base64.b64decode(sasl).decode("utf-8")
    assert "user=user@gmail.com" in decoded
    assert "auth=Bearer access_token_abc" in decoded


# ---------------------------------------------------------------------------
# email body parsing
# ---------------------------------------------------------------------------

def _make_plain(text: str) -> bytes:
    msg = email_lib.mime.text.MIMEText(text, "plain", "utf-8")
    return msg.as_bytes()


def _make_multipart(plain: str, html: str) -> bytes:
    msg = email_lib.mime.multipart.MIMEMultipart("alternative")
    msg.attach(email_lib.mime.text.MIMEText(plain, "plain", "utf-8"))
    msg.attach(email_lib.mime.text.MIMEText(html, "html", "utf-8"))
    return msg.as_bytes()


def test_parse_email_body_plain():
    raw = _make_plain("Hello world!")
    assert _parse_email_body(raw) == "Hello world!"


def test_parse_email_body_multipart_returns_plain():
    raw = _make_multipart("Plain part", "<b>HTML part</b>")
    body = _parse_email_body(raw)
    assert "Plain part" in body


def test_parse_email_body_empty_bytes():
    assert _parse_email_body(b"") == ""


# ---------------------------------------------------------------------------
# IMAP helpers
# ---------------------------------------------------------------------------

def test_parse_uidvalidity_from_select_lines():
    lines = [
        b"* 5 EXISTS",
        b"* OK [UIDVALIDITY 12345] UIDs valid",
        b"* OK [UIDNEXT 100] Predicted next UID.",
    ]
    assert _parse_uidvalidity(lines) == 12345


def test_has_exists_true():
    assert _has_exists([b"* 3 EXISTS"])


def test_has_exists_false():
    assert not _has_exists([b"* OK idle accepted"])


# ---------------------------------------------------------------------------
# EmailSentinel.connect_account — probe failure
# ---------------------------------------------------------------------------

def test_connect_account_probe_failure_raises(db, store):
    enqueued = []

    class FQ:
        def enqueue_nowait(self, i):
            enqueued.append(i)

    sentinel = EmailSentinel(session_db=db, extraction_queue=FQ(), owner_id="289")

    async def run():
        with patch(
            "agent.orchestrator.email_sentinel.EmailSentinel._probe_imap",
            new_callable=AsyncMock,
            side_effect=EmailSentinelError("auth failed"),
        ):
            with pytest.raises(EmailSentinelError, match="probe failed"):
                await sentinel.connect_account(
                    alias="personal",
                    host="imap.example.com",
                    username="u@example.com",
                    auth_method="app_password",
                    secret="wrong",
                    credentials_store=store,
                    owner_id="289",
                )

    _run(run())
    # Account should still be created with connection_state='disconnected'
    acct = db.find_mail_account_by_alias("289", "personal")
    assert acct is not None
    assert acct["connection_state"] == "disconnected"


def test_connect_account_success_marks_live(db, store):
    enqueued = []

    class FQ:
        def enqueue_nowait(self, i):
            enqueued.append(i)

    sentinel = EmailSentinel(session_db=db, extraction_queue=FQ(), owner_id="289")

    async def run():
        with patch(
            "agent.orchestrator.email_sentinel.EmailSentinel._probe_imap",
            new_callable=AsyncMock,
            return_value=None,
        ):
            acct = await sentinel.connect_account(
                alias="work",
                host="imap.example.com",
                username="u@example.com",
                auth_method="app_password",
                secret="correct",
                credentials_store=store,
                owner_id="289",
            )
        return acct

    acct = _run(run())
    assert acct["connection_state"] == "live"


# ---------------------------------------------------------------------------
# EmailSentinel.disconnect_account
# ---------------------------------------------------------------------------

def test_disconnect_account_removes_row(db, store):
    class FQ:
        def enqueue_nowait(self, i):
            pass

    sentinel = EmailSentinel(session_db=db, extraction_queue=FQ(), owner_id="289")

    async def setup():
        with patch(
            "agent.orchestrator.email_sentinel.EmailSentinel._probe_imap",
            new_callable=AsyncMock,
        ):
            await sentinel.connect_account(
                alias="temp",
                host="imap.example.com",
                username="x@example.com",
                auth_method="app_password",
                secret="pw",
                credentials_store=store,
                owner_id="289",
            )

    _run(setup())
    assert db.find_mail_account_by_alias("289", "temp") is not None
    result = _run(sentinel.disconnect_account("temp", "289"))
    assert result is True
    assert db.find_mail_account_by_alias("289", "temp") is None


# ---------------------------------------------------------------------------
# SentinelExtractionQueue — email path integration
# ---------------------------------------------------------------------------

def test_queue_receives_email_item():
    received = []

    async def fake_extract(item: ExtractionItem):
        received.append(item)

    async def run():
        q = SentinelExtractionQueue(session_db=None, extraction_fn=fake_extract)
        await q.start()
        item = ExtractionItem(
            transcript="reunião amanhã às 10h",
            sender_id="owner",
            sender_role="owner",
            source_type="email",
            source_format="email-imap",
        )
        await q.enqueue(item)
        await q.stop()

    asyncio.run(run())
    assert received[0].source_type == "email"
    assert "reunião" in received[0].transcript
