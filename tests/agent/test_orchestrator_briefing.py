"""Tests for BriefingBuilder (003 — Phase 5)."""

import time
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

from agent.orchestrator.briefing import BriefingBuilder, _format_pt_date
from agent.orchestrator.lists import ListManager
from agent.orchestrator.reminders import ReminderService
from hermes_state import SessionDB


BRT = timezone(timedelta(hours=-3))


def _builder(tmp_path, *, with_calendar=True):
    db = SessionDB(db_path=tmp_path / "state.db")
    cal = None
    if with_calendar:
        cal = MagicMock()
        cal.format_today_summary.return_value = (
            "📅 **Agenda — Terça-feira, 15 de abril de 2026**\n\n"
            "• 09:00 — Reunião com equipe Transfero\n"
            "• 14:00 — Call com contador"
        )
        cal.get_upcoming.return_value = []
    list_mgr = ListManager(db)
    rem_svc = ReminderService(db, calendar=None)
    builder = BriefingBuilder(db, calendar=cal, list_manager=list_mgr, reminder_service=rem_svc)
    return db, builder, list_mgr, rem_svc


# ---------------------------------------------------------------------------
# Morning briefing
# ---------------------------------------------------------------------------

def test_morning_briefing_includes_all_sections(tmp_path):
    db, builder, list_mgr, rem_svc = _builder(tmp_path)
    try:
        # Seed some data
        list_mgr.seed_default_lists()
        compras = list_mgr.find_list_by_name("Compras")
        list_mgr.add_item(compras["list_id"], "café", added_by="owner", added_by_name="Alexandre")

        rem_svc.create_reminder(
            title="Ligar para João",
            due_at=time.time() + 3600,
            owner_id="owner",
            sync_to_calendar=False,
        )

        briefing = builder.build_morning_briefing()

        # Should have greeting
        assert any(g in briefing for g in ("Bom dia", "Boa tarde", "Boa noite"))
        # Should have calendar
        assert "Reunião com equipe Transfero" in briefing
        # Should have reminders
        assert "Ligar para João" in briefing
        # Should have lists
        assert "Compras" in briefing
        assert "café" in briefing
    finally:
        db.close()


def test_morning_briefing_without_calendar(tmp_path):
    db, builder, _, _ = _builder(tmp_path, with_calendar=False)
    try:
        briefing = builder.build_morning_briefing()
        assert "não configurado" in briefing.lower() or "indisponível" in briefing.lower() or "Calendário" in briefing
    finally:
        db.close()


def test_morning_briefing_empty_state(tmp_path):
    """Briefing should still produce output even with no data."""
    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        builder = BriefingBuilder(db, calendar=None)
        briefing = builder.build_morning_briefing()
        assert len(briefing) > 0
        assert any(g in briefing for g in ("Bom dia", "Boa tarde", "Boa noite"))
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Meeting reminders
# ---------------------------------------------------------------------------

def test_meeting_reminder_dedup(tmp_path):
    """Same event should not generate two reminders."""
    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        now = datetime.now(BRT)
        event_start = (now + timedelta(minutes=20)).isoformat()
        events = [
            {"id": "evt_123", "summary": "Standup", "start": event_start, "location": ""},
        ]

        cal = MagicMock()
        cal.get_upcoming.return_value = events
        builder = BriefingBuilder(db, calendar=cal)

        # First call — should produce a reminder
        msgs1 = builder.check_and_build_meeting_reminders()
        assert len(msgs1) == 1
        assert "Standup" in msgs1[0]

        # Second call — dedup should prevent duplicate
        msgs2 = builder.check_and_build_meeting_reminders()
        assert len(msgs2) == 0
    finally:
        db.close()


def test_meeting_reminder_skips_all_day_events(tmp_path):
    """All-day events (date only, no time) should not trigger reminders."""
    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        events = [
            {"id": "evt_bday", "summary": "Birthday", "start": "2026-04-16", "location": ""},
        ]
        cal = MagicMock()
        cal.get_upcoming.return_value = events
        builder = BriefingBuilder(db, calendar=cal)

        msgs = builder.check_and_build_meeting_reminders()
        assert len(msgs) == 0
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------

def test_format_pt_date():
    dt = datetime(2026, 4, 15, 8, 0, tzinfo=BRT)
    result = _format_pt_date(dt)
    assert "Quarta-feira" in result or "15 de abril" in result
    assert "2026" in result
