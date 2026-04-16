"""End-to-end voice-note / short-transcript extraction pipeline (021 US1).

A single entry point (`perform_extraction`) stitches together the extraction
engine, dedup, persistence, 002 lineage link, and action routing. Kept here
(not in `gateway/run.py`) so it can be unit-tested without standing up the
full gateway — the gateway wiring becomes pure plumbing.

Fall-through contract: returns a dict with the reply text on success,
or None when the caller should fall through to the normal LLM session
path (API error, zero actions, dedup hit, disabled flag, etc.).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable

from .action_router import (
    RoutedAction,
    RoutedHandlers,
    format_extraction_response,
    format_preview_response,
    new_extraction_id,
    new_preview_id,
    route_actions,
)
from .extraction import (
    ExtractionResult,
    compute_transcript_hash,
    extract_actions,
)
from .pending_preview import PendingPreviewManager

logger = logging.getLogger(__name__)

SHORT_INPUT_CHAR_LIMIT = 2000
CLARIFY_CONFIDENCE = 0.70
PREVIEW_TTL_SECONDS = 600
BRT = timezone(timedelta(hours=-3))


@dataclass
class PipelineOutcome:
    """Result of running the extraction pipeline once."""
    reply_text: str
    extraction_id: str
    execution_mode: str  # "auto" | "preview"
    result: ExtractionResult
    routed: list[RoutedAction]
    preview_id: str | None = None


_VOICE_WRAPPER_RE = re.compile(
    r'\[The user sent a voice message[^\]]*?Here\'s what they said:\s*"(?P<body>.+?)"\]',
    re.IGNORECASE | re.DOTALL,
)


def extract_voice_transcript(message_text: str) -> str:
    """Pull the raw transcript out of the gateway's STT wrapper text.

    The gateway currently wraps voice transcriptions as:
        [The user sent a voice message~ Here's what they said: "<body>"]

    If no wrapper is present, returns `message_text` unchanged (caller
    decides what to feed in — for `/transcript` commands the plain text
    flows through directly).
    """
    if not message_text:
        return ""
    match = _VOICE_WRAPPER_RE.search(message_text)
    if match:
        return match.group("body").strip()
    return message_text.strip()


def today_in_brt() -> date:
    """Today's date in America/Sao_Paulo (UTC-3, no DST since 2019)."""
    return datetime.now(BRT).date()


def is_extraction_enabled() -> bool:
    """Rollback flag — when set to '0', the pipeline short-circuits."""
    return os.getenv("HERMES_EXTRACTION_ENABLED", "1").strip() != "0"


def _active_list_names(list_manager: Any | None) -> list[str]:
    if list_manager is None:
        return []
    try:
        rows = list_manager.list_all()
    except Exception as e:  # pragma: no cover — defensive
        logger.debug("pipeline: list_all failed: %s", e)
        return []
    return [str(r.get("name") or "") for r in rows if r.get("name")]


def _persist_extraction(
    *,
    session_db: Any,
    extraction_id: str,
    source_type: str,
    source_format: str | None,
    transcript_hash: str,
    transcript_preview: str,
    sender_id: str,
    sender_role: str,
    execution_mode: str,
    routed: list[RoutedAction],
) -> None:
    actions_payload = [r.action.as_dict() | {"routed_status": r.status,
                                             "routed_error": r.error,
                                             "handler_result": r.handler_result}
                        for r in routed]
    session_db.create_extraction_event(
        extraction_id=extraction_id,
        source_type=source_type,
        source_format=source_format,
        transcript_hash=transcript_hash,
        transcript_preview=transcript_preview,
        sender_id=sender_id,
        sender_role=sender_role,
        execution_mode=execution_mode,
        actions_json=json.dumps(actions_payload),
        actions_count=len(routed),
    )


def _link_extraction_to_jobs(
    *,
    session_db: Any,
    extraction_id: str,
    source_type: str,
    sender_id: str,
    actions_count: int,
    transcript_preview: str,
) -> None:
    try:
        from .jobs import OrchestratorJobService
    except ImportError:  # pragma: no cover
        return
    try:
        svc = OrchestratorJobService(session_db)
        svc.track_extraction_event(
            extraction_id=extraction_id,
            source_type=source_type,
            triggered_by=sender_id,
            title=f"{source_type} extraction",
            actions_count=actions_count,
            transcript_preview=transcript_preview,
        )
    except Exception as e:
        logger.warning("pipeline: lineage link failed: %s", e)


async def perform_extraction(
    *,
    transcript: str,
    sender_id: str,
    sender_role: str,
    sender_capabilities: set[str],
    sender_name: str | None = None,
    source_type: str = "voice_note",
    source_format: str | None = "ogg",
    session_db: Any | None = None,
    list_manager: Any | None = None,
    calendar_bridge: Any | None = None,
    reminder_service: Any | None = None,
    chat_id: str | None = None,
    current_date: date | None = None,
    force_preview: bool = False,
    extract_fn: Callable[..., ExtractionResult | None] | None = None,
) -> PipelineOutcome | None:
    """Run the full extraction → route → persist pipeline.

    Returns a `PipelineOutcome` when a reply should be sent and the gateway
    should skip the agent session; returns None when the caller should fall
    through to the existing LLM path (disabled flag, empty extract, API
    error, dedup hit, etc.).
    """
    if not is_extraction_enabled():
        return None
    if not transcript.strip():
        return None
    if sender_role not in ("owner", "contact"):
        return None

    # Long input branch → always preview (US3).
    long_input = force_preview or len(transcript) > SHORT_INPUT_CHAR_LIMIT

    # Dedup — same transcript produces the same ExtractionEvent.
    transcript_hash = compute_transcript_hash(transcript)
    if session_db is not None:
        try:
            prior = session_db.get_extraction_by_hash(transcript_hash)
        except Exception as e:  # pragma: no cover
            logger.debug("pipeline: dedup lookup failed: %s", e)
            prior = None
        if prior is not None:
            logger.info(
                "pipeline: dedup hit ext=%s (skipping re-process)",
                prior.get("extraction_id"),
            )
            return None

    active_lists = _active_list_names(list_manager)
    today = current_date or today_in_brt()
    extractor = extract_fn or extract_actions

    try:
        result = await asyncio.to_thread(
            extractor, transcript,
            active_lists=active_lists,
            current_date=today,
        )
    except Exception as e:
        logger.warning("pipeline: extract_actions raised: %s", e)
        return None

    if result is None or not result.actions:
        return None

    # Short-input guard: if nothing clears the clarify threshold, fall back
    # to the LLM session so the user isn't silently ignored.
    if not long_input and all(a.confidence < CLARIFY_CONFIDENCE for a in result.actions):
        return None

    handlers = RoutedHandlers(
        list_manager=list_manager,
        calendar_bridge=calendar_bridge,
        reminder_service=reminder_service,
    )
    routed = route_actions(
        result,
        sender_id=sender_id,
        sender_capabilities=sender_capabilities,
        sender_name=sender_name,
        handlers=handlers,
        long_input=long_input,
    )

    execution_mode = "preview" if long_input else "auto"
    extraction_id = new_extraction_id()
    transcript_preview = transcript[:500]

    if session_db is not None:
        try:
            _persist_extraction(
                session_db=session_db,
                extraction_id=extraction_id,
                source_type=source_type,
                source_format=source_format,
                transcript_hash=transcript_hash,
                transcript_preview=transcript_preview,
                sender_id=sender_id,
                sender_role=sender_role,
                execution_mode=execution_mode,
                routed=routed,
            )
            _link_extraction_to_jobs(
                session_db=session_db,
                extraction_id=extraction_id,
                source_type=source_type,
                sender_id=sender_id,
                actions_count=len(routed),
                transcript_preview=transcript_preview,
            )
        except Exception as e:
            logger.warning("pipeline: persist failed: %s", e)

    if long_input:
        preview_id = new_preview_id()
        reply = format_preview_response(result, routed)
        # Persist the PendingPreview when we have a session_db + chat_id.
        # The gateway passes chat_id from the platform event; pure-function
        # callers (tests) may omit it and do their own persistence.
        if session_db is not None and chat_id is not None:
            try:
                mgr = PendingPreviewManager(session_db)
                mgr.create_preview(
                    preview_id=preview_id,
                    extraction_id=extraction_id,
                    sender_id=sender_id,
                    chat_id=chat_id,
                    routed=routed,
                )
            except Exception as e:
                logger.warning("pipeline: preview persist failed: %s", e)
        return PipelineOutcome(
            reply_text=reply,
            extraction_id=extraction_id,
            execution_mode=execution_mode,
            result=result,
            routed=routed,
            preview_id=preview_id,
        )

    return PipelineOutcome(
        reply_text=format_extraction_response(result, routed),
        extraction_id=extraction_id,
        execution_mode=execution_mode,
        result=result,
        routed=routed,
    )
