"""Tests for Telegram attachment -> orchestrator ingest wiring."""

from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.platforms.base import MessageEvent, MessageType
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
    runner._running_agents_ts = {}
    runner._pending_messages = {}
    runner._pending_approvals = {}
    runner._session_db = db
    runner._update_prompt_pending = {}
    runner._is_user_authorized = lambda _source: True
    runner._run_agent = AsyncMock(
        side_effect=AssertionError("attachment ingest should bypass the agent loop")
    )
    return runner


@pytest.mark.asyncio
async def test_handle_message_ingests_telegram_document(tmp_path):
    session_entry = SessionEntry(
        session_key=build_session_key(_make_source()),
        session_id="sess-1",
        created_at=datetime.now(),
        updated_at=datetime.now(),
        platform=Platform.TELEGRAM,
        chat_type="dm",
    )
    db = SessionDB(db_path=tmp_path / "state.db")
    cached_doc = tmp_path / "cached-report.pdf"
    cached_doc.write_bytes(b"%PDF-1.4 fake pdf")
    try:
        runner = _make_runner(session_entry, db)
        raw_message = MagicMock()
        raw_message.caption = "Please summarize this later"
        raw_message.document = SimpleNamespace(file_name="report.pdf")

        event = MessageEvent(
            text="Please summarize this later",
            source=_make_source(),
            message_id="m42",
            message_type=MessageType.DOCUMENT,
            raw_message=raw_message,
            media_urls=[str(cached_doc)],
            media_types=["application/pdf"],
        )

        result = await runner._handle_message(event)
        jobs = db.list_orchestrator_jobs(requested_by=session_entry.session_key)
        assert len(jobs) == 1

        job = jobs[0]
        artifacts = db.list_orchestrator_artifacts(job["job_id"])
        assert job["source_type"] == "telegram"
        assert job["route_class"] == "raw_archive"
        assert job["metadata_json"]["caption_text"] == "Please summarize this later"
        assert len(artifacts) == 1
        assert Path(artifacts[0]["storage_path"]).name == "report.pdf"
        assert "Hermes Orchestrator Job" in result
        assert "Use `/jobs" in result
    finally:
        db.close()


@pytest.mark.asyncio
async def test_handle_message_ingests_telegram_photo_batch(tmp_path):
    session_entry = SessionEntry(
        session_key=build_session_key(_make_source()),
        session_id="sess-1",
        created_at=datetime.now(),
        updated_at=datetime.now(),
        platform=Platform.TELEGRAM,
        chat_type="dm",
    )
    db = SessionDB(db_path=tmp_path / "state.db")
    image_a = tmp_path / "img-a.jpg"
    image_b = tmp_path / "img-b.jpg"
    image_a.write_bytes(b"image-a")
    image_b.write_bytes(b"image-b")
    try:
        runner = _make_runner(session_entry, db)
        raw_message = MagicMock()
        raw_message.caption = None
        raw_message.document = None

        event = MessageEvent(
            text="",
            source=_make_source(),
            message_id="m77",
            message_type=MessageType.PHOTO,
            raw_message=raw_message,
            media_urls=[str(image_a), str(image_b)],
            media_types=["image/jpeg", "image/jpeg"],
        )

        result = await runner._handle_message(event)
        jobs = db.list_orchestrator_jobs(requested_by=session_entry.session_key)
        assert len(jobs) == 1

        artifacts = db.list_orchestrator_artifacts(jobs[0]["job_id"])
        assert jobs[0]["route_class"] == "raw_archive"
        assert len(artifacts) == 2
        assert Path(artifacts[0]["storage_path"]).name.startswith("telegram-photo-m77-1")
        assert Path(artifacts[1]["storage_path"]).name.startswith("telegram-photo-m77-2")
        assert "Artifacts:" in result
    finally:
        db.close()
