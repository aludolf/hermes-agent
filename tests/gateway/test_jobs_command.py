"""Tests for gateway /jobs behavior."""

from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from agent.orchestrator.jobs import OrchestratorJobService
from agent.orchestrator.models import SourceRef
from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.platforms.base import BasePlatformAdapter, MessageEvent, MessageType
from gateway.session import SessionEntry, SessionSource, build_session_key
from hermes_state import SessionDB


def _make_source() -> SessionSource:
    return SessionSource(
        platform=Platform.TELEGRAM,
        user_id="u1",
        chat_id="c1",
        user_name="tester",
        chat_type="dm",
    )


def _make_event(text: str) -> MessageEvent:
    return MessageEvent(text=text, source=_make_source(), message_id="m1")


def _make_runner(session_entry: SessionEntry, db: SessionDB):
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(
        platforms={Platform.TELEGRAM: PlatformConfig(enabled=True, token="***")}
    )
    adapter = MagicMock()
    adapter.send = AsyncMock()
    runner.adapters = {Platform.TELEGRAM: adapter}
    runner._voice_mode = {}
    runner.hooks = SimpleNamespace(emit=AsyncMock(), loaded_hooks=False)
    runner.session_store = MagicMock()
    runner.session_store.get_or_create_session.return_value = session_entry
    runner._running_agents = {}
    runner._pending_messages = {}
    runner._pending_approvals = {}
    runner._session_db = db
    return runner


@pytest.mark.asyncio
async def test_jobs_command_lists_current_session_jobs(tmp_path):
    session_entry = SessionEntry(
        session_key=build_session_key(_make_source()),
        session_id="sess-1",
        created_at=datetime.now(),
        updated_at=datetime.now(),
        platform=Platform.TELEGRAM,
        chat_type="dm",
    )
    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        service = OrchestratorJobService(db)
        summary = service.ingest_bytes(
            filename="statement.pdf",
            data=b"%PDF-1.4 fake pdf",
            requested_by=session_entry.session_key,
            source=SourceRef(
                source_type="telegram",
                source_uri="telegram://message/1",
                source_scope="telegram:c1",
            ),
        )
        runner = _make_runner(session_entry, db)

        result = await runner._handle_jobs_command(_make_event("/jobs"))

        assert summary["job_id"] in result
        assert "Current Session Orchestrator Jobs" in result
    finally:
        db.close()


@pytest.mark.asyncio
async def test_jobs_command_detail_renders_artifacts(tmp_path):
    session_entry = SessionEntry(
        session_key=build_session_key(_make_source()),
        session_id="sess-1",
        created_at=datetime.now(),
        updated_at=datetime.now(),
        platform=Platform.TELEGRAM,
        chat_type="dm",
    )
    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        service = OrchestratorJobService(db)
        summary = service.ingest_bytes(
            filename="statement.pdf",
            data=b"%PDF-1.4 fake pdf",
            requested_by=session_entry.session_key,
            source=SourceRef(
                source_type="telegram",
                source_uri="telegram://message/1",
                source_scope="telegram:c1",
            ),
        )
        runner = _make_runner(session_entry, db)

        result = await runner._handle_jobs_command(_make_event(f"/jobs {summary['job_id']}"))

        assert "Hermes Orchestrator Job" in result
        assert "Artifacts:" in result
        assert summary["job_id"] in result
    finally:
        db.close()


@pytest.mark.asyncio
async def test_jobs_command_bypasses_active_session_guard():
    source = _make_source()
    session_key = build_session_key(source)
    handler_called_with = []

    async def fake_handler(event):
        handler_called_with.append(event)
        return "🧭 **Current Session Orchestrator Jobs**"

    class _ConcreteAdapter(BasePlatformAdapter):
        platform = Platform.TELEGRAM

        async def connect(self):
            return None

        async def disconnect(self):
            return None

        async def send(self, chat_id, content, **kwargs):
            return None

        async def get_chat_info(self, chat_id):
            return {}

    platform_config = PlatformConfig(enabled=True, token="***")
    adapter = _ConcreteAdapter(platform_config, Platform.TELEGRAM)
    adapter.set_message_handler(fake_handler)

    sent = []

    async def fake_send_with_retry(chat_id, content, reply_to=None, metadata=None):
        sent.append(content)

    adapter._send_with_retry = fake_send_with_retry
    adapter._active_sessions[session_key] = __import__("asyncio").Event()

    event = MessageEvent(
        text="/jobs",
        source=source,
        message_id="m1",
        message_type=MessageType.COMMAND,
    )
    await adapter.handle_message(event)

    assert handler_called_with
    assert sent
    assert session_key not in adapter._pending_messages
