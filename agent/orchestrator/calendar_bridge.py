"""Google Calendar bridge via the google-workspace skill (003).

Wraps the existing google_api.py script via subprocess to avoid
reimplementing the Google API client. All operations are synchronous
(subprocess.run) — suitable for the gateway's thread pool.

The bridge is stateless; authentication is handled by the skill's
OAuth token at ~/.hermes/google_token.json (or $HERMES_HOME).
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Default timezone for calendar operations (BRT)
DEFAULT_TZ = "America/Sao_Paulo"
DEFAULT_TZ_OFFSET = "-03:00"

# Days of the week in Portuguese (for date parsing)
_PT_WEEKDAYS = {
    "segunda": 0, "segunda-feira": 0,
    "terça": 1, "terca": 1, "terça-feira": 1, "terca-feira": 1,
    "quarta": 2, "quarta-feira": 2,
    "quinta": 3, "quinta-feira": 3,
    "sexta": 4, "sexta-feira": 4,
    "sábado": 5, "sabado": 5,
    "domingo": 6,
}

_PT_MONTHS = {
    "janeiro": 1, "fevereiro": 2, "março": 3, "marco": 3,
    "abril": 4, "maio": 5, "junho": 6,
    "julho": 7, "agosto": 8, "setembro": 9,
    "outubro": 10, "novembro": 11, "dezembro": 12,
}


def _find_google_api_script() -> str:
    """Locate the google_api.py script."""
    candidates = [
        Path(os.getenv("HERMES_HOME", "~/.hermes")).expanduser()
        / "skills/productivity/google-workspace/scripts/google_api.py",
        Path("/opt/hermes/skills/productivity/google-workspace/scripts/google_api.py"),
    ]
    for p in candidates:
        if p.exists():
            return str(p)
    raise FileNotFoundError("google_api.py not found in any expected location")


def _find_python() -> str:
    """Locate the Python interpreter (prefer venv)."""
    venv_python = Path("/opt/hermes/.venv/bin/python")
    if venv_python.exists():
        return str(venv_python)
    return "python3"


def parse_portuguese_datetime(text: str) -> datetime | None:
    """Parse Portuguese natural-language date/time into a datetime.

    Supports:
    - "hoje [às] HH:MM"  / "hoje [às] HHh"
    - "amanhã [às] HH:MM" / "amanhã [às] HHh"
    - "segunda [às] HH:MM" (next occurrence of that weekday)
    - "DD de MONTH [às] HH:MM"
    - "HH:MM" (assumes today)
    - "HHh" / "HHhMM" (e.g. "15h", "15h30")

    Returns timezone-aware datetime in America/Sao_Paulo, or None if unparsable.
    """
    import re

    text = text.strip().lower()
    text = text.replace("às ", "").replace("as ", "").replace("à ", "").replace("a ", "", 1) if text.startswith(("às", "as", "à")) else text

    now = datetime.now(timezone(timedelta(hours=-3)))
    target_date = now.date()
    target_time = None

    # Extract time component first
    time_match = re.search(r"(\d{1,2}):(\d{2})", text)
    if time_match:
        target_time = (int(time_match.group(1)), int(time_match.group(2)))
    else:
        h_match = re.search(r"(\d{1,2})h(\d{2})?", text)
        if h_match:
            hour = int(h_match.group(1))
            minute = int(h_match.group(2) or 0)
            target_time = (hour, minute)

    # Parse date component
    if "hoje" in text:
        target_date = now.date()
    elif "amanhã" in text or "amanha" in text:
        target_date = now.date() + timedelta(days=1)
    else:
        # Check weekday names
        for day_name, day_num in _PT_WEEKDAYS.items():
            if day_name in text:
                current_day = now.weekday()
                delta = (day_num - current_day) % 7
                if delta == 0:
                    delta = 7  # next week if today
                target_date = now.date() + timedelta(days=delta)
                break
        else:
            # Check "DD de MONTH"
            date_match = re.search(r"(\d{1,2})\s+de\s+(\w+)", text)
            if date_match:
                day = int(date_match.group(1))
                month_name = date_match.group(2).strip()
                month = _PT_MONTHS.get(month_name)
                if month:
                    year = now.year
                    candidate = now.replace(month=month, day=day).date()
                    if candidate < now.date():
                        year += 1
                    target_date = candidate.replace(year=year)

    if target_time is None:
        return None  # Can't create a reminder without a time

    hour, minute = target_time
    if hour > 23 or minute > 59:
        return None

    result = datetime(
        target_date.year, target_date.month, target_date.day,
        hour, minute, 0,
        tzinfo=timezone(timedelta(hours=-3)),
    )
    return result


class CalendarBridge:
    """Read/write Google Calendar via google_api.py subprocess."""

    def __init__(self, *, script_path: str | None = None, python_path: str | None = None) -> None:
        self._script = script_path or _find_google_api_script()
        self._python = python_path or _find_python()

    def _run(self, *args: str, timeout: int = 30) -> list[dict[str, Any]] | dict[str, Any]:
        """Run google_api.py with args, parse JSON output."""
        cmd = [self._python, self._script] + list(args)
        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            logger.warning("Calendar API timed out: %s", " ".join(cmd))
            return []
        except FileNotFoundError:
            logger.error("Calendar API script not found: %s", self._script)
            return []

        if result.returncode != 0:
            logger.warning("Calendar API error: %s", result.stderr.strip()[:200])
            return []

        try:
            return json.loads(result.stdout)
        except json.JSONDecodeError:
            logger.warning("Calendar API non-JSON output: %s", result.stdout[:200])
            return []

    # ------------------------------------------------------------------
    # Read
    # ------------------------------------------------------------------

    def get_events_today(self) -> list[dict[str, Any]]:
        """Get today's calendar events."""
        now = datetime.now(timezone(timedelta(hours=-3)))
        start = now.replace(hour=0, minute=0, second=0).isoformat()
        end = now.replace(hour=23, minute=59, second=59).isoformat()
        result = self._run("calendar", "list", "--start", start, "--end", end, "--max", "20")
        return result if isinstance(result, list) else []

    def get_events_range(self, start: str, end: str) -> list[dict[str, Any]]:
        """Get events in an ISO date range."""
        result = self._run("calendar", "list", "--start", start, "--end", end, "--max", "50")
        return result if isinstance(result, list) else []

    def get_upcoming(self, *, within_minutes: int = 45) -> list[dict[str, Any]]:
        """Get events starting within N minutes (for meeting reminders)."""
        now = datetime.now(timezone(timedelta(hours=-3)))
        end = now + timedelta(minutes=within_minutes)
        result = self._run(
            "calendar", "list",
            "--start", now.isoformat(),
            "--end", end.isoformat(),
            "--max", "10",
        )
        return result if isinstance(result, list) else []

    # ------------------------------------------------------------------
    # Write
    # ------------------------------------------------------------------

    def create_event(
        self, *, summary: str, start: str, end: str | None = None,
        location: str | None = None, description: str | None = None,
    ) -> dict[str, Any]:
        """Create a calendar event. Returns {status, id, summary, htmlLink}."""
        if end is None:
            # Default: 30 min event
            try:
                dt = datetime.fromisoformat(start)
                end = (dt + timedelta(minutes=30)).isoformat()
            except ValueError:
                end = start

        args = [
            "calendar", "create",
            "--summary", summary,
            "--start", start,
            "--end", end,
        ]
        if location:
            args.extend(["--location", location])
        if description:
            args.extend(["--description", description])

        result = self._run(*args)
        return result if isinstance(result, dict) else {}

    def delete_event(self, event_id: str) -> bool:
        """Delete a calendar event by ID."""
        result = self._run("calendar", "delete", event_id)
        return isinstance(result, dict) and result.get("status") == "deleted"

    # ------------------------------------------------------------------
    # Formatting
    # ------------------------------------------------------------------

    def format_today_summary(self) -> str:
        """Render today's calendar for briefing inclusion."""
        events = self.get_events_today()
        if not events:
            return "📅 Sem compromissos hoje."

        now = datetime.now(timezone(timedelta(hours=-3)))
        lines = [f"📅 **Agenda — {now.strftime('%A, %d de %B de %Y')}**", ""]

        for ev in events:
            start = ev.get("start", "")
            summary = ev.get("summary", "(sem título)")
            location = ev.get("location", "")

            # Format time — handle all-day events (date only, no T)
            if "T" in str(start):
                try:
                    dt = datetime.fromisoformat(str(start))
                    time_str = dt.strftime("%H:%M")
                except ValueError:
                    time_str = start
            else:
                time_str = "dia todo"

            loc_str = f" — {location}" if location else ""
            lines.append(f"• {time_str} — {summary}{loc_str}")

        return "\n".join(lines)

    def is_authenticated(self) -> bool:
        """Check if Google OAuth token exists."""
        home = Path(os.getenv("HERMES_HOME", "~/.hermes")).expanduser()
        return (home / "google_token.json").exists()
