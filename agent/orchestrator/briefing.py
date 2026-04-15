"""Proactive briefing builder for cron-driven notifications (003).

Composes the morning briefing from calendar events, pending reminders,
and active shared lists. Also generates individual meeting reminders
with dedup tracking.

All output in Portuguese (pt-BR).
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from hermes_state import SessionDB

from .calendar_bridge import CalendarBridge
from .lists import ListManager
from .reminders import ReminderService

logger = logging.getLogger(__name__)

BRT = timezone(timedelta(hours=-3))

# Portuguese weekday and month names for formatting
_PT_WEEKDAY = [
    "Segunda-feira", "Terça-feira", "Quarta-feira",
    "Quinta-feira", "Sexta-feira", "Sábado", "Domingo",
]
_PT_MONTH = [
    "", "janeiro", "fevereiro", "março", "abril", "maio", "junho",
    "julho", "agosto", "setembro", "outubro", "novembro", "dezembro",
]


def _format_pt_date(dt: datetime) -> str:
    """Format a datetime as 'Terça-feira, 15 de abril de 2026'."""
    return (
        f"{_PT_WEEKDAY[dt.weekday()]}, "
        f"{dt.day} de {_PT_MONTH[dt.month]} de {dt.year}"
    )


class BriefingBuilder:
    """Compose proactive briefings from calendar, reminders, and lists."""

    def __init__(
        self,
        db: SessionDB,
        calendar: CalendarBridge | None = None,
        list_manager: ListManager | None = None,
        reminder_service: ReminderService | None = None,
    ) -> None:
        self.db = db
        self.calendar = calendar
        self.list_manager = list_manager or ListManager(db)
        self.reminder_service = reminder_service or ReminderService(db, calendar=calendar)

    # ------------------------------------------------------------------
    # Morning briefing
    # ------------------------------------------------------------------

    def build_morning_briefing(self) -> str:
        """Build the full morning briefing in Portuguese.

        Sections:
        1. Greeting with date
        2. Today's calendar events
        3. Pending reminders
        4. Active lists summary (with recent additions)
        """
        now = datetime.now(BRT)
        sections = []

        # Greeting
        greeting = "Bom dia" if now.hour < 12 else "Boa tarde" if now.hour < 18 else "Boa noite"
        sections.append(f"{greeting}! ☀️ {_format_pt_date(now)}")

        # Calendar
        if self.calendar:
            try:
                cal_summary = self.calendar.format_today_summary()
                sections.append(cal_summary)
            except Exception as e:
                logger.warning("Briefing calendar failed: %s", e)
                sections.append("📅 _Agenda indisponível no momento._")
        else:
            sections.append("📅 _Calendário não configurado._")

        # Reminders
        try:
            rem_summary = self.reminder_service.format_pending_summary()
            sections.append(rem_summary)
        except Exception as e:
            logger.warning("Briefing reminders failed: %s", e)

        # Lists
        try:
            list_summary = self.list_manager.format_all_lists_summary()
            sections.append(list_summary)
        except Exception as e:
            logger.warning("Briefing lists failed: %s", e)

        return "\n\n".join(sections)

    # ------------------------------------------------------------------
    # Meeting reminders
    # ------------------------------------------------------------------

    def check_and_build_meeting_reminders(
        self, *, within_minutes: int = 45, remind_at_minutes: int = 30,
    ) -> list[str]:
        """Check upcoming calendar events and build reminders for unsent ones.

        Returns a list of formatted reminder messages (one per event).
        Only includes events starting between remind_at_minutes and
        within_minutes from now, and not yet reminded (dedup via DB).
        """
        if not self.calendar:
            return []

        try:
            events = self.calendar.get_upcoming(within_minutes=within_minutes)
        except Exception as e:
            logger.warning("Meeting reminder calendar check failed: %s", e)
            return []

        now = datetime.now(BRT)
        messages = []

        for event in events:
            event_id = event.get("id")
            if not event_id:
                continue

            # Skip all-day events
            start_str = str(event.get("start", ""))
            if "T" not in start_str:
                continue

            # Skip already reminded
            if self.db.was_meeting_reminded(event_id):
                continue

            # Parse start time
            try:
                start_dt = datetime.fromisoformat(start_str)
            except ValueError:
                continue

            # Only remind if event starts in the remind window
            minutes_until = (start_dt - now).total_seconds() / 60
            if minutes_until < 0 or minutes_until > remind_at_minutes:
                continue

            # Build reminder message
            msg = self._build_meeting_reminder(event, start_dt, int(minutes_until))

            # Record as sent
            self.db.record_meeting_reminder_sent(event_id)
            messages.append(msg)

        return messages

    def _build_meeting_reminder(
        self, event: dict[str, Any], start_dt: datetime, minutes_until: int,
    ) -> str:
        """Build a single meeting reminder notification."""
        summary = event.get("summary", "(sem título)")
        location = event.get("location", "")

        time_str = start_dt.strftime("%H:%M")
        loc_str = f"\n📍 {location}" if location else ""

        return (
            f"🔔 **Lembrete de reunião**\n\n"
            f"**{summary}** em {minutes_until} minutos ({time_str}){loc_str}"
        )
