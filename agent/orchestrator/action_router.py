"""Action router — dispatches extracted actions to their handlers (021).

Takes an `ExtractionResult` from `extraction.extract_actions()` and routes
each `ExtractedAction` to the appropriate handler (ListManager,
CalendarBridge, ReminderService, ...). Applies confidence thresholds and
capability filters before execution.

Confidence thresholds (per FR-005/006/007 for short inputs):
- >= 0.85  → auto-execute
- 0.70–0.84 → clarify (NOT executed yet; status = "clarified")
- < 0.70  → skip silently (status = "skipped")

For long inputs (`long_input=True`), execution is deferred — the caller is
expected to wrap the result in a PendingPreview. All actions come back with
`execution_status = "pending"` and no side effects occur.

Capability filter (per FR-011):
- Owner (capability `all`) → may execute every action type.
- Contact → only action types matching their approved capabilities. Filtered
  actions get `execution_status = "filtered"` with no side effects.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from typing import Any, Callable
from uuid import uuid4

from .extraction import ExtractedAction, ExtractionResult
from .models import Capability

logger = logging.getLogger(__name__)

CONFIDENCE_AUTO = 0.85
CONFIDENCE_CLARIFY = 0.70

BRT = timezone(timedelta(hours=-3))


# Action type → required capability to execute.
# Owners bypass this (they hold Capability.ALL).
_CAPABILITY_FOR_ACTION: dict[str, str] = {
    "task": Capability.LISTS.value,
    "meeting": Capability.REQUESTS.value,
    "reminder": Capability.REMINDERS.value,
    "kb_entry": Capability.REQUESTS.value,
    "entity_update": Capability.REQUESTS.value,
    "decision": Capability.REQUESTS.value,
    "info": Capability.REQUESTS.value,
}


@dataclass
class RoutedAction:
    """Result of routing a single extracted action."""
    action: ExtractedAction
    status: str  # executed | clarified | skipped | filtered | pending | failed
    handler_result: dict[str, Any] | None = None
    error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "action": self.action.as_dict(),
            "status": self.status,
            "handler_result": self.handler_result,
            "error": self.error,
        }


@dataclass
class RoutedHandlers:
    """Handlers available to the action router.

    Any handler may be None; actions of that type will be marked `skipped`
    with error `handler_unavailable`.
    """
    list_manager: Any | None = None
    calendar_bridge: Any | None = None
    reminder_service: Any | None = None


def _capability_allows(
    capabilities: set[str], action_type: str,
) -> bool:
    """True if `capabilities` permits executing this action_type."""
    if Capability.ALL.value in capabilities:
        return True
    needed = _CAPABILITY_FOR_ACTION.get(action_type)
    if needed is None:
        return False
    return needed in capabilities


def _parse_date_time(metadata: dict[str, Any]) -> datetime | None:
    """Combine metadata.date (YYYY-MM-DD) + metadata.time (HH:MM) as BRT.

    Returns None if either is missing or malformed.
    """
    d = metadata.get("date")
    t = metadata.get("time")
    if not d or not t:
        return None
    try:
        return datetime.fromisoformat(f"{d}T{t}").replace(tzinfo=BRT)
    except ValueError:
        logger.warning("router: bad date/time %r %r", d, t)
        return None


def _execute_task(
    action: ExtractedAction, sender_id: str, sender_name: str | None,
    handlers: RoutedHandlers,
) -> RoutedAction:
    if handlers.list_manager is None:
        return RoutedAction(action, "skipped", error="list_manager_unavailable")
    list_name = action.metadata.get("list") or "Compras"
    list_rec = handlers.list_manager.find_list_by_name(list_name)
    if list_rec is None:
        return RoutedAction(action, "failed", error=f"list_not_found:{list_name}")
    try:
        item = handlers.list_manager.add_item(
            list_id=list_rec["list_id"],
            content=action.content,
            added_by=sender_id,
            added_by_name=sender_name,
        )
    except ValueError as e:
        return RoutedAction(action, "failed", error=str(e))
    return RoutedAction(
        action, "executed",
        handler_result={
            "list_id": list_rec["list_id"],
            "list_name": list_rec.get("name"),
            "item_id": item.get("item_id"),
        },
    )


def _execute_meeting(
    action: ExtractedAction, handlers: RoutedHandlers,
) -> RoutedAction:
    if handlers.calendar_bridge is None:
        return RoutedAction(action, "skipped", error="calendar_unavailable")
    dt = _parse_date_time(action.metadata)
    if dt is None:
        return RoutedAction(action, "failed", error="missing_or_bad_datetime")
    summary = action.metadata.get("summary") or action.content or "Reunião"
    location = action.metadata.get("location")
    attendees = action.metadata.get("attendees") or []
    desc_parts = []
    if attendees:
        desc_parts.append("Participantes: " + ", ".join(str(a) for a in attendees))
    desc_parts.append("Criado pela extração automática do Hermes.")
    try:
        result = handlers.calendar_bridge.create_event(
            summary=summary,
            start=dt.isoformat(),
            location=location,
            description="\n".join(desc_parts),
        )
    except Exception as e:
        logger.warning("router: calendar create_event failed: %s", e)
        return RoutedAction(action, "failed", error=f"calendar_error:{e}")
    return RoutedAction(
        action, "executed",
        handler_result={
            "event_id": result.get("id"),
            "summary": summary,
            "start": dt.isoformat(),
        },
    )


def _execute_reminder(
    action: ExtractedAction, sender_id: str, handlers: RoutedHandlers,
) -> RoutedAction:
    if handlers.reminder_service is None:
        return RoutedAction(action, "skipped", error="reminder_service_unavailable")
    dt = _parse_date_time(action.metadata)
    if dt is None:
        return RoutedAction(action, "failed", error="missing_or_bad_datetime")
    title = action.metadata.get("title") or action.content or "Lembrete"
    try:
        rec = handlers.reminder_service.create_reminder(
            title=title,
            due_at=dt.timestamp(),
            owner_id=sender_id,
        )
    except Exception as e:
        logger.warning("router: create_reminder failed: %s", e)
        return RoutedAction(action, "failed", error=f"reminder_error:{e}")
    return RoutedAction(
        action, "executed",
        handler_result={
            "reminder_id": rec.get("reminder_id"),
            "title": title,
            "due_at": dt.isoformat(),
        },
    )


# Action types that have no persistent side-effect in v1 — they're noted on
# the summary but produce no DB mutation. Future work moves these into KB /
# entity context layers.
def _note_only(
    action: ExtractedAction, kind: str,
) -> RoutedAction:
    return RoutedAction(action, "executed", handler_result={"kind": kind, "noted": True})


_EXECUTORS: dict[str, Callable[..., RoutedAction]] = {}


def route_actions(
    result: ExtractionResult,
    *,
    sender_id: str,
    sender_capabilities: set[str],
    sender_name: str | None = None,
    handlers: RoutedHandlers | None = None,
    long_input: bool = False,
) -> list[RoutedAction]:
    """Route every action in `result` to its handler.

    For short inputs (`long_input=False`):
    - confidence >= 0.85 → execute
    - 0.70 <= confidence < 0.85 → clarified (not executed)
    - confidence < 0.70 → skipped

    For long inputs (`long_input=True`): every action returns with
    status='pending' and no side effects. Caller wraps in PendingPreview.
    """
    handlers = handlers or RoutedHandlers()
    capabilities = set(sender_capabilities or ())
    out: list[RoutedAction] = []

    for action in result.actions:
        # Capability filter first — a contact with lists-only should have
        # meetings silently filtered even if confidence is 0.99.
        if not _capability_allows(capabilities, action.action_type):
            out.append(RoutedAction(action, "filtered", error="capability_denied"))
            continue

        # Long input path: always preview, no execution.
        if long_input:
            out.append(RoutedAction(action, "pending"))
            continue

        # Confidence-based short-input routing.
        if action.confidence < CONFIDENCE_CLARIFY:
            out.append(RoutedAction(action, "skipped", error="low_confidence"))
            continue
        if action.confidence < CONFIDENCE_AUTO:
            out.append(RoutedAction(action, "clarified"))
            continue

        # Confidence >= 0.85 → execute per type.
        at = action.action_type
        if at == "task":
            out.append(_execute_task(action, sender_id, sender_name, handlers))
        elif at == "meeting":
            out.append(_execute_meeting(action, handlers))
        elif at == "reminder":
            out.append(_execute_reminder(action, sender_id, handlers))
        elif at in ("kb_entry", "entity_update", "decision", "info"):
            out.append(_note_only(action, at))
        else:
            out.append(RoutedAction(action, "skipped", error=f"unknown_type:{at}"))

    return out


# ---------------------------------------------------------------------------
# Portuguese summary formatting
# ---------------------------------------------------------------------------

def _format_executed(routed: RoutedAction) -> str:
    a = routed.action
    meta = a.metadata or {}
    if a.action_type == "task":
        list_name = (routed.handler_result or {}).get("list_name") or meta.get("list", "Compras")
        return f"✅ Adicionado em {list_name}: {a.content}"
    if a.action_type == "meeting":
        d = meta.get("date", "")
        t = meta.get("time", "")
        where = f" — {meta['location']}" if meta.get("location") else ""
        summary = meta.get("summary") or a.content
        return f"📅 Reunião: {summary} ({d} {t}{where})"
    if a.action_type == "reminder":
        d = meta.get("date", "")
        t = meta.get("time", "")
        title = meta.get("title") or a.content
        return f"⏰ Lembrete: {title} ({d} {t})"
    if a.action_type == "decision":
        return f"🧭 Decisão registrada: {a.content}"
    if a.action_type == "kb_entry":
        title = meta.get("title") or a.content
        return f"📘 Nota: {title}"
    if a.action_type == "entity_update":
        ent = meta.get("entity") or ""
        return f"👤 Atualizado: {ent} — {a.content}"
    if a.action_type == "info":
        return f"ℹ️ {a.content}"
    return f"• {a.content}"


def _format_clarify(routed: RoutedAction) -> str:
    return f"❓ Confirma? \"{routed.action.content}\" (confiança {routed.action.confidence:.2f})"


def format_extraction_response(
    result: ExtractionResult,
    routed_actions: list[RoutedAction],
) -> str:
    """Render a concise Portuguese summary for the Telegram reply.

    Shape:
        <summary from Sonnet, if any>
        ✅ <executed lines>
        ❓ <clarifications>
        (skipped/filtered are not shown — they're silent by design)
    """
    lines: list[str] = []
    executed = [r for r in routed_actions if r.status == "executed"]
    clarified = [r for r in routed_actions if r.status == "clarified"]
    failed = [r for r in routed_actions if r.status == "failed"]

    if result.summary and (executed or clarified or failed):
        lines.append(result.summary)
        lines.append("")

    for r in executed:
        lines.append(_format_executed(r))
    for r in clarified:
        lines.append(_format_clarify(r))
    for r in failed:
        lines.append(f"⚠️ Falhou: {r.action.content} ({r.error})")

    if not lines:
        # Nothing extracted / everything was filtered or skipped silently.
        return "Nenhuma ação clara para executar."
    return "\n".join(lines)


def format_preview_response(
    result: ExtractionResult,
    routed_actions: list[RoutedAction],
) -> str:
    """Render a numbered preview (used for long transcripts, US3).

    Only actions with status='pending' are listed. Caller may offer
    "confirmar", "confirmar N, M", "cancelar" replies.
    """
    pending = [r for r in routed_actions if r.status == "pending"]
    if not pending:
        return "Nenhuma ação extraída para confirmar."

    lines = [
        f"Encontrei {len(pending)} ação(ões) na transcrição. "
        "Responda `confirmar` para executar todas, "
        "`confirmar N, M` para um subconjunto, ou `cancelar`.",
        "",
    ]
    for i, r in enumerate(pending, start=1):
        a = r.action
        meta = a.metadata or {}
        if a.action_type == "task":
            lines.append(f"{i}. 📝 Tarefa em {meta.get('list', 'Compras')}: {a.content}")
        elif a.action_type == "meeting":
            lines.append(
                f"{i}. 📅 Reunião {meta.get('date', '')} {meta.get('time', '')}: "
                f"{meta.get('summary') or a.content}"
            )
        elif a.action_type == "reminder":
            lines.append(
                f"{i}. ⏰ Lembrete {meta.get('date', '')} {meta.get('time', '')}: "
                f"{meta.get('title') or a.content}"
            )
        else:
            lines.append(f"{i}. {a.action_type}: {a.content}")
    return "\n".join(lines)


def new_preview_id() -> str:
    return f"pv_{uuid4().hex[:12]}"


def new_extraction_id() -> str:
    return f"ext_{uuid4().hex[:12]}"
