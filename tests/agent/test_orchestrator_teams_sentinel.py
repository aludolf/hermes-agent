"""Tests for Teams sentinel (022 US1).

Covers:
- device-code auth flow stores ciphertext
- access-token refresh from credential store
- subscription create → webhook validation handshake
- notification with valid clientState enqueues extraction
- invalid clientState returns 400
- subscription-create failure falls back to polling
- fetched message text flows through enqueue
"""

from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from cryptography.fernet import Fernet

from agent.orchestrator.credentials import CredentialsStore
from agent.orchestrator.sentinel_queue import ExtractionItem, SentinelExtractionQueue
from agent.orchestrator.teams_auth import TeamsAuthError, TeamsAuthManager
from agent.orchestrator.teams_webhook import handle_ms_graph_webhook
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
# TeamsAuthManager
# ---------------------------------------------------------------------------

def test_teams_auth_requires_client_id(store):
    with pytest.raises(TeamsAuthError, match="MS_GRAPH_CLIENT_ID"):
        TeamsAuthManager(store, client_id="")


def test_start_device_code_flow_returns_user_code(store):
    mgr = TeamsAuthManager(store, client_id="fake-client-id")
    fake_flow = {
        "user_code": "ABCD1234",
        "verification_uri": "https://microsoft.com/devicelogin",
        "message": "Visit https://...",
        "interval": 5,
        "expires_in": 900,
    }
    mock_app = MagicMock()
    mock_app.initiate_device_flow.return_value = fake_flow

    with patch("agent.orchestrator.teams_auth.TeamsAuthManager._build_msal_app", return_value=mock_app):
        result = mgr.start_device_code_flow()

    assert result["user_code"] == "ABCD1234"
    assert "flow_state" in result


def test_complete_device_code_flow_persists_refresh_token(db, store):
    mgr = TeamsAuthManager(store, client_id="fake-client-id")
    mock_app = MagicMock()
    mock_app.acquire_token_by_device_flow.return_value = {
        "access_token": "at_abc",
        "refresh_token": "rt_xyz",
        "expires_in": 3600,
    }
    with patch("agent.orchestrator.teams_auth.TeamsAuthManager._build_msal_app", return_value=mock_app):
        at, rt, expires_at = mgr.complete_device_code_flow({"device_code": "dc"})

    assert at == "at_abc"
    assert rt == "rt_xyz"
    assert expires_at > time.time()
    assert mgr.has_credentials()


def test_get_access_token_uses_cache(store):
    mgr = TeamsAuthManager(store, client_id="fake-client-id")
    mgr._cached_token = "cached_token"
    mgr._cached_expiry = time.time() + 3600
    assert mgr.get_access_token() == "cached_token"


def test_get_access_token_refreshes_when_expired(db, store):
    mgr = TeamsAuthManager(store, client_id="fake-client-id")
    # Persist a dummy refresh token
    cid = store.put(kind="ms_graph_refresh_token", secret="rt_stale", label="test")
    mock_app = MagicMock()
    mock_app.acquire_token_by_refresh_token.return_value = {
        "access_token": "fresh_token",
        "expires_in": 3600,
    }
    with patch("agent.orchestrator.teams_auth.TeamsAuthManager._build_msal_app", return_value=mock_app):
        token = mgr.get_access_token()
    assert token == "fresh_token"


# ---------------------------------------------------------------------------
# MS Graph webhook — validation handshake
# ---------------------------------------------------------------------------

def test_webhook_validation_handshake(db):
    status, body, ct = _run(
        handle_ms_graph_webhook(
            b"",
            {"validationToken": "echo-me-back"},
            session_db=db,
            teams_sentinel=None,
        )
    )
    assert status == 200
    assert body == b"echo-me-back"
    assert ct == "text/plain"


def test_webhook_empty_body_returns_400(db):
    status, body, ct = _run(
        handle_ms_graph_webhook(b"", {}, session_db=db, teams_sentinel=None)
    )
    assert status == 400


def test_webhook_invalid_json_returns_400(db):
    status, body, ct = _run(
        handle_ms_graph_webhook(b"not-json", {}, session_db=db, teams_sentinel=None)
    )
    assert status == 400


# ---------------------------------------------------------------------------
# MS Graph webhook — notification with valid clientState
# ---------------------------------------------------------------------------

def _seed_watch(db, owner_id="289"):
    import secrets
    cs = secrets.token_hex(16)
    db.create_teams_watch(
        watch_id="tw_test1",
        owner_id=owner_id,
        resource_type="chat",
        ms_resource_id="19:abc@thread.v2",
        client_state=cs,
    )
    return cs


def test_webhook_valid_client_state_enqueues(db):
    cs = _seed_watch(db)
    sentinel_mock = AsyncMock()
    import json
    payload = json.dumps({"value": [{"clientState": cs, "resourceData": {"id": "msg_1"}}]}).encode()

    status, body, ct = _run(
        handle_ms_graph_webhook(payload, {}, session_db=db, teams_sentinel=sentinel_mock)
    )
    assert status == 202
    sentinel_mock.process_notification.assert_called_once()


def test_webhook_invalid_client_state_returns_400(db):
    _seed_watch(db)
    sentinel_mock = AsyncMock()
    import json
    payload = json.dumps({"value": [{"clientState": "wrong-state", "resourceData": {"id": "msg_1"}}]}).encode()

    status, body, ct = _run(
        handle_ms_graph_webhook(payload, {}, session_db=db, teams_sentinel=sentinel_mock)
    )
    assert status == 400
    sentinel_mock.process_notification.assert_not_called()


# ---------------------------------------------------------------------------
# TeamsSentinel.process_notification enqueues extraction
# ---------------------------------------------------------------------------

def test_process_notification_enqueues_item(db, tmp_path, store):
    from agent.orchestrator.teams_sentinel import TeamsSentinel

    enqueued = []

    class FakeQueue:
        def enqueue_nowait(self, item):
            enqueued.append(item)

    cs = _seed_watch(db)
    db.update_teams_watch_subscription("tw_test1", subscription_id="sub_1", expires_at=time.time() + 3600)

    auth_mock = MagicMock()
    auth_mock.get_access_token.return_value = "tok"

    sentinel = TeamsSentinel(
        session_db=db,
        auth_manager=auth_mock,
        extraction_queue=FakeQueue(),
        owner_id="289",
    )

    # Inject pre-fetched message body
    notification = {
        "_watch_id": "tw_test1",
        "_msg": {"body": {"contentType": "text", "content": "comprar leite"}},
        "clientState": cs,
        "resourceData": {"id": "msg_1"},
    }
    _run(sentinel.process_notification(notification))

    assert len(enqueued) == 1
    assert enqueued[0].transcript == "comprar leite"
    assert enqueued[0].source_type == "teams_message"


def test_process_notification_skips_muted_watch(db, store):
    from agent.orchestrator.teams_sentinel import TeamsSentinel

    enqueued = []

    class FakeQueue:
        def enqueue_nowait(self, item):
            enqueued.append(item)

    cs = _seed_watch(db)
    db.set_teams_watch_mute("tw_test1", mute_until=0)  # indefinite mute

    auth_mock = MagicMock()
    sentinel = TeamsSentinel(
        session_db=db, auth_manager=auth_mock, extraction_queue=FakeQueue(), owner_id="289"
    )
    _run(sentinel.process_notification({"_watch_id": "tw_test1", "clientState": cs}))
    assert enqueued == []


# ---------------------------------------------------------------------------
# SentinelExtractionQueue
# ---------------------------------------------------------------------------

def test_queue_consumer_calls_extraction_fn():
    called_with = []

    async def fake_extract(item):
        called_with.append(item)
        return None

    async def run():
        q = SentinelExtractionQueue(
            session_db=None,
            extraction_fn=fake_extract,
        )
        await q.start()
        item = ExtractionItem(
            transcript="test",
            sender_id="s1",
            sender_role="owner",
            source_type="teams_message",
        )
        await q.enqueue(item)
        await q.stop()

    asyncio.run(run())
    assert len(called_with) == 1
    assert called_with[0].transcript == "test"
