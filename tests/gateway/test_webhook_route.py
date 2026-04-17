"""Tests for the POST /webhooks/ms-graph route (022 US1 T052).

Uses the pure handler function rather than a live HTTP server
since the gateway uses aiohttp, not a test-client-friendly framework.
All tests exercise handle_ms_graph_webhook directly.
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock

import pytest

from agent.orchestrator.teams_webhook import handle_ms_graph_webhook
from hermes_state import SessionDB


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


@pytest.fixture
def db(tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    yield db
    db.close()


def _seed_watch(db, client_state: str = "nonce_abc") -> None:
    db.create_teams_watch(
        watch_id="tw_1",
        owner_id="289",
        resource_type="chat",
        ms_resource_id="19:chat@thread.v2",
        client_state=client_state,
    )


# ---------------------------------------------------------------------------
# Validation handshake
# ---------------------------------------------------------------------------

def test_validation_handshake_echo():
    status, body, ct = _run(
        handle_ms_graph_webhook(
            b"",
            {"validationToken": "test-token-xyz"},
            session_db=None,
            teams_sentinel=None,
        )
    )
    assert status == 200
    assert body == b"test-token-xyz"
    assert "text/plain" in ct


# ---------------------------------------------------------------------------
# Valid notification → 202
# ---------------------------------------------------------------------------

def test_valid_notification_returns_202(db):
    _seed_watch(db)
    sentinel = AsyncMock()
    payload = json.dumps({
        "value": [{"clientState": "nonce_abc", "resourceData": {"id": "msg1"}}]
    }).encode()
    status, body, ct = _run(
        handle_ms_graph_webhook(payload, {}, session_db=db, teams_sentinel=sentinel)
    )
    assert status == 202
    sentinel.process_notification.assert_called_once()


# ---------------------------------------------------------------------------
# Invalid clientState → 400
# ---------------------------------------------------------------------------

def test_invalid_client_state_returns_400(db):
    _seed_watch(db)
    payload = json.dumps({
        "value": [{"clientState": "wrong", "resourceData": {"id": "msg1"}}]
    }).encode()
    status, _, _ = _run(
        handle_ms_graph_webhook(payload, {}, session_db=db, teams_sentinel=None)
    )
    assert status == 400


# ---------------------------------------------------------------------------
# Malformed body → 400
# ---------------------------------------------------------------------------

def test_malformed_json_returns_400(db):
    status, _, _ = _run(
        handle_ms_graph_webhook(b"[not valid json", {}, session_db=db, teams_sentinel=None)
    )
    assert status == 400


# ---------------------------------------------------------------------------
# Empty body → 400
# ---------------------------------------------------------------------------

def test_empty_body_returns_400(db):
    status, _, _ = _run(
        handle_ms_graph_webhook(b"", {}, session_db=db, teams_sentinel=None)
    )
    assert status == 400
