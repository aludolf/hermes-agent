"""Tests for the action router (021 US1)."""

from __future__ import annotations

from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock

import pytest

from agent.orchestrator.action_router import (
    CONFIDENCE_AUTO,
    CONFIDENCE_CLARIFY,
    RoutedHandlers,
    format_extraction_response,
    format_preview_response,
    route_actions,
)
from agent.orchestrator.extraction import ExtractedAction, ExtractionResult


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _result_with(*actions: ExtractedAction, summary: str = "teste") -> ExtractionResult:
    return ExtractionResult(
        actions=list(actions),
        entities_mentioned=[],
        summary=summary,
        transcript_hash="h" * 64,
    )


def _task(confidence: float = 0.9, content: str = "café", list_name: str = "Compras") -> ExtractedAction:
    return ExtractedAction(
        action_type="task",
        content=content,
        confidence=confidence,
        metadata={"list": list_name},
    )


def _meeting(confidence: float = 0.9) -> ExtractedAction:
    return ExtractedAction(
        action_type="meeting",
        content="Reunião com João",
        confidence=confidence,
        metadata={
            "summary": "Reunião com João",
            "date": "2026-04-20",
            "time": "10:00",
            "location": "escritório",
            "attendees": ["João"],
        },
    )


def _reminder(confidence: float = 0.9) -> ExtractedAction:
    return ExtractedAction(
        action_type="reminder",
        content="Ligar pro contador",
        confidence=confidence,
        metadata={
            "title": "Ligar pro contador",
            "date": "2026-04-17",
            "time": "15:00",
        },
    )


def _fake_handlers():
    lm = MagicMock()
    lm.find_list_by_name.return_value = {"list_id": "list_1", "name": "Compras"}
    lm.add_item.return_value = {"item_id": "item_1"}

    cal = MagicMock()
    cal.create_event.return_value = {"id": "evt_123"}

    rem = MagicMock()
    rem.create_reminder.return_value = {"reminder_id": "rem_1"}

    return RoutedHandlers(list_manager=lm, calendar_bridge=cal, reminder_service=rem)


OWNER_CAPS = {"all"}


# ---------------------------------------------------------------------------
# Confidence thresholds (FR-005/006/007)
# ---------------------------------------------------------------------------

def test_high_confidence_task_auto_executes():
    handlers = _fake_handlers()
    result = _result_with(_task(confidence=0.95))
    routed = route_actions(result, sender_id="s1", sender_capabilities=OWNER_CAPS, handlers=handlers)
    assert len(routed) == 1
    assert routed[0].status == "executed"
    assert routed[0].handler_result["item_id"] == "item_1"
    handlers.list_manager.add_item.assert_called_once()


def test_mid_confidence_action_is_clarified_not_executed():
    handlers = _fake_handlers()
    result = _result_with(_task(confidence=0.75))
    routed = route_actions(result, sender_id="s1", sender_capabilities=OWNER_CAPS, handlers=handlers)
    assert routed[0].status == "clarified"
    handlers.list_manager.add_item.assert_not_called()


def test_low_confidence_action_is_skipped_silently():
    handlers = _fake_handlers()
    result = _result_with(_task(confidence=0.5))
    routed = route_actions(result, sender_id="s1", sender_capabilities=OWNER_CAPS, handlers=handlers)
    assert routed[0].status == "skipped"
    handlers.list_manager.add_item.assert_not_called()


def test_confidence_exactly_at_auto_threshold_executes():
    handlers = _fake_handlers()
    result = _result_with(_task(confidence=CONFIDENCE_AUTO))
    routed = route_actions(result, sender_id="s1", sender_capabilities=OWNER_CAPS, handlers=handlers)
    assert routed[0].status == "executed"


def test_confidence_below_clarify_threshold_is_skipped():
    handlers = _fake_handlers()
    result = _result_with(_task(confidence=CONFIDENCE_CLARIFY - 0.01))
    routed = route_actions(result, sender_id="s1", sender_capabilities=OWNER_CAPS, handlers=handlers)
    assert routed[0].status == "skipped"


# ---------------------------------------------------------------------------
# Per-type routing
# ---------------------------------------------------------------------------

def test_meeting_routes_to_calendar_bridge():
    handlers = _fake_handlers()
    result = _result_with(_meeting(confidence=0.95))
    routed = route_actions(result, sender_id="s1", sender_capabilities=OWNER_CAPS, handlers=handlers)
    assert routed[0].status == "executed"
    assert routed[0].handler_result["event_id"] == "evt_123"
    args = handlers.calendar_bridge.create_event.call_args.kwargs
    assert args["summary"] == "Reunião com João"
    assert args["location"] == "escritório"
    assert "João" in args["description"]


def test_reminder_routes_to_reminder_service():
    handlers = _fake_handlers()
    result = _result_with(_reminder(confidence=0.95))
    routed = route_actions(result, sender_id="s1", sender_capabilities=OWNER_CAPS, handlers=handlers)
    assert routed[0].status == "executed"
    assert routed[0].handler_result["reminder_id"] == "rem_1"
    kwargs = handlers.reminder_service.create_reminder.call_args.kwargs
    assert kwargs["title"] == "Ligar pro contador"
    assert kwargs["owner_id"] == "s1"


def test_kb_entry_is_noted_without_handler_call():
    handlers = _fake_handlers()
    action = ExtractedAction(
        action_type="kb_entry", content="algo útil", confidence=0.95,
        metadata={"title": "Dica"},
    )
    result = _result_with(action)
    routed = route_actions(result, sender_id="s1", sender_capabilities=OWNER_CAPS, handlers=handlers)
    assert routed[0].status == "executed"
    assert routed[0].handler_result["noted"] is True


def test_missing_list_manager_marks_task_skipped():
    handlers = RoutedHandlers()  # no handlers wired
    result = _result_with(_task(confidence=0.95))
    routed = route_actions(result, sender_id="s1", sender_capabilities=OWNER_CAPS, handlers=handlers)
    assert routed[0].status == "skipped"
    assert routed[0].error == "list_manager_unavailable"


def test_meeting_with_bad_date_fails():
    handlers = _fake_handlers()
    a = ExtractedAction(
        action_type="meeting", content="x", confidence=0.95,
        metadata={"date": "tomorrow", "time": "bad"},
    )
    routed = route_actions(
        _result_with(a), sender_id="s1",
        sender_capabilities=OWNER_CAPS, handlers=handlers,
    )
    assert routed[0].status == "failed"
    handlers.calendar_bridge.create_event.assert_not_called()


# ---------------------------------------------------------------------------
# Capability filter (FR-011)
# ---------------------------------------------------------------------------

def test_contact_with_lists_only_gets_meeting_filtered():
    handlers = _fake_handlers()
    result = _result_with(_task(confidence=0.95), _meeting(confidence=0.95))
    routed = route_actions(
        result, sender_id="s1",
        sender_capabilities={"lists"},  # contact, lists only
        handlers=handlers,
    )
    statuses = [r.status for r in routed]
    assert "executed" in statuses  # task
    assert "filtered" in statuses  # meeting
    handlers.calendar_bridge.create_event.assert_not_called()


def test_contact_with_no_caps_gets_everything_filtered():
    handlers = _fake_handlers()
    result = _result_with(_task(confidence=0.95), _meeting(confidence=0.95))
    routed = route_actions(
        result, sender_id="s1",
        sender_capabilities=set(),
        handlers=handlers,
    )
    assert all(r.status == "filtered" for r in routed)


# ---------------------------------------------------------------------------
# Long input path
# ---------------------------------------------------------------------------

def test_long_input_skips_execution_returns_pending():
    handlers = _fake_handlers()
    result = _result_with(_task(confidence=0.95), _meeting(confidence=0.95))
    routed = route_actions(
        result, sender_id="s1",
        sender_capabilities=OWNER_CAPS,
        handlers=handlers,
        long_input=True,
    )
    assert all(r.status == "pending" for r in routed)
    handlers.list_manager.add_item.assert_not_called()
    handlers.calendar_bridge.create_event.assert_not_called()


# ---------------------------------------------------------------------------
# Portuguese summary formatting
# ---------------------------------------------------------------------------

def test_format_extraction_response_executed_task():
    handlers = _fake_handlers()
    result = _result_with(_task(confidence=0.95), summary="1 item em Compras")
    routed = route_actions(
        result, sender_id="s1", sender_capabilities=OWNER_CAPS, handlers=handlers,
    )
    out = format_extraction_response(result, routed)
    assert "Compras" in out
    assert "café" in out
    assert "✅" in out
    assert "1 item em Compras" in out


def test_format_extraction_response_shows_clarify_question():
    result = _result_with(_task(confidence=0.75))
    routed = route_actions(
        result, sender_id="s1", sender_capabilities=OWNER_CAPS,
        handlers=_fake_handlers(),
    )
    out = format_extraction_response(result, routed)
    assert "❓" in out
    assert "café" in out


def test_format_extraction_response_silent_on_all_skipped():
    result = _result_with(_task(confidence=0.5))
    routed = route_actions(
        result, sender_id="s1", sender_capabilities=OWNER_CAPS,
        handlers=_fake_handlers(),
    )
    out = format_extraction_response(result, routed)
    assert "Nenhuma ação clara" in out


def test_format_preview_response_numbers_pending_actions():
    result = _result_with(_task(confidence=0.95), _meeting(confidence=0.95))
    routed = route_actions(
        result, sender_id="s1", sender_capabilities=OWNER_CAPS,
        handlers=_fake_handlers(), long_input=True,
    )
    out = format_preview_response(result, routed)
    assert "confirmar" in out
    assert "cancelar" in out
    assert "1." in out
    assert "2." in out
