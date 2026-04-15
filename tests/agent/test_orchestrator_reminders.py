"""Tests for ReminderService + CalendarBridge + date parsing (003)."""

import time
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest

from agent.orchestrator.calendar_bridge import parse_portuguese_datetime
from agent.orchestrator.models import ReminderStatus
from agent.orchestrator.reminders import ReminderService
from hermes_state import SessionDB


BRT = timezone(timedelta(hours=-3))


def _service(tmp_path, *, with_calendar=False):
    db = SessionDB(db_path=tmp_path / "state.db")
    calendar = None
    if with_calendar:
        calendar = MagicMock()
        calendar.create_event.return_value = {"id": "gcal_test_123", "status": "created"}
        calendar.delete_event.return_value = True
    return db, ReminderService(db, calendar=calendar), calendar


# ---------------------------------------------------------------------------
# Reminder CRUD
# ---------------------------------------------------------------------------

def test_create_reminder_without_calendar(tmp_path):
    db, svc, _ = _service(tmp_path)
    try:
        due = time.time() + 3600
        rec = svc.create_reminder(title="Ligar para João", due_at=due, owner_id="owner")
        assert rec["title"] == "Ligar para João"
        assert rec["status"] == "pending"
        assert rec["google_event_id"] is None
    finally:
        db.close()


def test_create_reminder_with_calendar_sync(tmp_path):
    db, svc, cal_mock = _service(tmp_path, with_calendar=True)
    try:
        due = time.time() + 3600
        rec = svc.create_reminder(title="Enviar relatório", due_at=due, owner_id="owner")
        assert rec["google_event_id"] == "gcal_test_123"
        cal_mock.create_event.assert_called_once()
    finally:
        db.close()


def test_get_pending_returns_only_pending(tmp_path):
    db, svc, _ = _service(tmp_path)
    try:
        svc.create_reminder(title="A", due_at=time.time() + 3600, owner_id="owner")
        rec2 = svc.create_reminder(title="B", due_at=time.time() + 7200, owner_id="owner")
        svc.complete_reminder(rec2["reminder_id"])

        pending = svc.get_pending(owner_id="owner")
        assert len(pending) == 1
        assert pending[0]["title"] == "A"
    finally:
        db.close()


def test_get_due_soon_filters_by_time(tmp_path):
    db, svc, _ = _service(tmp_path)
    try:
        # Due in 10 minutes — should appear
        svc.create_reminder(
            title="Soon", due_at=time.time() + 600, owner_id="owner",
            sync_to_calendar=False,
        )
        # Due in 2 hours — should NOT appear
        svc.create_reminder(
            title="Later", due_at=time.time() + 7200, owner_id="owner",
            sync_to_calendar=False,
        )

        due_soon = svc.get_due_soon(within_minutes=30)
        assert len(due_soon) == 1
        assert due_soon[0]["title"] == "Soon"
    finally:
        db.close()


def test_complete_reminder(tmp_path):
    db, svc, _ = _service(tmp_path)
    try:
        rec = svc.create_reminder(title="X", due_at=time.time() + 3600, owner_id="owner")
        svc.complete_reminder(rec["reminder_id"])
        updated = db.get_reminder(rec["reminder_id"])
        assert updated["status"] == "completed"
        assert updated["completed_at"] is not None
    finally:
        db.close()


def test_cancel_reminder_deletes_calendar_event(tmp_path):
    db, svc, cal_mock = _service(tmp_path, with_calendar=True)
    try:
        rec = svc.create_reminder(title="Y", due_at=time.time() + 3600, owner_id="owner")
        svc.cancel_reminder(rec["reminder_id"])

        updated = db.get_reminder(rec["reminder_id"])
        assert updated["status"] == "cancelled"
        cal_mock.delete_event.assert_called_once_with("gcal_test_123")
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------

def test_format_pending_summary_empty(tmp_path):
    db, svc, _ = _service(tmp_path)
    try:
        out = svc.format_pending_summary()
        assert "Nenhum lembrete pendente" in out
    finally:
        db.close()


def test_format_pending_summary_with_items(tmp_path):
    db, svc, _ = _service(tmp_path)
    try:
        svc.create_reminder(
            title="Ligar para João", due_at=time.time() + 3600, owner_id="owner",
            sync_to_calendar=False,
        )
        out = svc.format_pending_summary()
        assert "Ligar para João" in out
        assert "Lembretes pendentes" in out
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Portuguese date parsing
# ---------------------------------------------------------------------------

def test_parse_hoje_com_hora():
    dt = parse_portuguese_datetime("hoje 15:30")
    assert dt is not None
    assert dt.hour == 15
    assert dt.minute == 30
    assert dt.date() == datetime.now(BRT).date()


def test_parse_amanha_com_hora():
    dt = parse_portuguese_datetime("amanhã às 10:00")
    assert dt is not None
    assert dt.hour == 10
    assert dt.date() == (datetime.now(BRT) + timedelta(days=1)).date()


def test_parse_hora_formato_h():
    dt = parse_portuguese_datetime("hoje 15h")
    assert dt is not None
    assert dt.hour == 15
    assert dt.minute == 0


def test_parse_hora_formato_h_minutos():
    dt = parse_portuguese_datetime("amanhã 14h30")
    assert dt is not None
    assert dt.hour == 14
    assert dt.minute == 30


def test_parse_weekday():
    dt = parse_portuguese_datetime("segunda 09:00")
    assert dt is not None
    assert dt.weekday() == 0  # Monday
    assert dt.hour == 9


def test_parse_date_with_month():
    dt = parse_portuguese_datetime("25 de abril 14:00")
    assert dt is not None
    assert dt.month == 4
    assert dt.day == 25
    assert dt.hour == 14


def test_parse_no_time_returns_none():
    dt = parse_portuguese_datetime("amanhã")
    assert dt is None  # time required


def test_parse_bare_time_assumes_today():
    dt = parse_portuguese_datetime("17:00")
    assert dt is not None
    assert dt.hour == 17
    assert dt.date() == datetime.now(BRT).date()


def test_reminder_status_enum():
    assert ReminderStatus.PENDING == "pending"
    assert ReminderStatus.COMPLETED == "completed"
    assert ReminderStatus.CANCELLED == "cancelled"
