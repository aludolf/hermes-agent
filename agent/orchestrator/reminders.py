"""Structured reminders with Google Calendar phone sync (003).

Reminders are stored in our SQLite DB AND optionally synced to Google
Calendar as events with 15-minute alarms. This gives:
- Phone notification via Google Calendar alarm
- Hermes tracking via DB for briefings and /reminders command
- Completion tracking when the user acknowledges

The CalendarBridge is optional — reminders work without it (just no phone sync).
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

from hermes_state import SessionDB

from .calendar_bridge import CalendarBridge, DEFAULT_TZ_OFFSET
from .models import ReminderStatus

logger = logging.getLogger(__name__)


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex[:12]}"


class ReminderService:
    """Façade for structured reminder lifecycle."""

    def __init__(
        self,
        db: SessionDB,
        calendar: CalendarBridge | None = None,
    ) -> None:
        self.db = db
        self.calendar = calendar

    # ------------------------------------------------------------------
    # Create
    # ------------------------------------------------------------------

    def create_reminder(
        self,
        *,
        title: str,
        due_at: float,
        owner_id: str,
        sync_to_calendar: bool = True,
    ) -> dict[str, Any]:
        """Create a reminder. Optionally syncs to Google Calendar with alarm.

        Args:
            title: human-readable reminder text
            due_at: Unix timestamp when the reminder is due
            owner_id: Telegram user ID
            sync_to_calendar: if True AND CalendarBridge available, create event

        Returns the reminder record with google_event_id if synced.
        """
        reminder_id = _new_id("rem")
        google_event_id = None

        # Sync to Google Calendar
        if sync_to_calendar and self.calendar is not None:
            try:
                dt = datetime.fromtimestamp(due_at, tz=timezone(timedelta(hours=-3)))
                start_iso = dt.isoformat()
                result = self.calendar.create_event(
                    summary=f"⏰ {title}",
                    start=start_iso,
                    description=f"Lembrete criado pelo Hermes (ID: {reminder_id})",
                )
                google_event_id = result.get("id")
                if google_event_id:
                    logger.info("Reminder synced to Calendar: %s → %s", reminder_id, google_event_id)
            except Exception as e:
                logger.warning("Calendar sync failed for reminder %s: %s", reminder_id, e)

        self.db.create_reminder(
            reminder_id=reminder_id,
            owner_id=str(owner_id),
            title=title,
            due_at=due_at,
            google_event_id=google_event_id,
            status=ReminderStatus.PENDING,
        )

        return self.db.get_reminder(reminder_id) or {}

    # ------------------------------------------------------------------
    # Read
    # ------------------------------------------------------------------

    def get_pending(self, owner_id: str | None = None) -> list[dict[str, Any]]:
        """Get all pending reminders, optionally filtered by owner."""
        return self.db.list_reminders(owner_id=owner_id, status="pending")

    def get_due_soon(self, *, within_minutes: int = 30) -> list[dict[str, Any]]:
        """Get reminders due within N minutes (for proactive notification)."""
        return self.db.get_reminders_due_soon(within_minutes=within_minutes)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def complete_reminder(self, reminder_id: str) -> None:
        """Mark a reminder as completed."""
        self.db.update_reminder_status(reminder_id, ReminderStatus.COMPLETED)

    def cancel_reminder(self, reminder_id: str) -> None:
        """Cancel a reminder. Also deletes the Calendar event if synced."""
        rec = self.db.get_reminder(reminder_id)
        if rec and rec.get("google_event_id") and self.calendar:
            try:
                self.calendar.delete_event(rec["google_event_id"])
            except Exception as e:
                logger.warning("Calendar event delete failed: %s", e)

        self.db.update_reminder_status(reminder_id, ReminderStatus.CANCELLED)

    # ------------------------------------------------------------------
    # Formatting
    # ------------------------------------------------------------------

    def format_pending_summary(self, owner_id: str | None = None) -> str:
        """Render pending reminders for briefing or /reminders command."""
        reminders = self.get_pending(owner_id=owner_id)
        if not reminders:
            return "⏰ Nenhum lembrete pendente."

        lines = [f"⏰ **Lembretes pendentes** ({len(reminders)})", ""]
        for r in reminders:
            try:
                dt = datetime.fromtimestamp(
                    r["due_at"], tz=timezone(timedelta(hours=-3))
                )
                when = dt.strftime("%d/%m às %H:%M")
            except (ValueError, OSError):
                when = "?"
            synced = " 📱" if r.get("google_event_id") else ""
            lines.append(f"• {r['title']} — {when}{synced}")

        return "\n".join(lines)
