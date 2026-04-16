"""Pending preview manager for long-transcript extractions (021 US3).

Long-form inputs (>2000 chars text, >3min voice, `/transcript` command)
never auto-execute. Hermes renders a numbered preview and waits up to
10 minutes for the sender to reply:
- "confirmar"         → execute all pending actions
- "confirmar 2, 4"    → execute only selected indices
- "cancelar"          → discard all

Security: only the original sender may confirm their own preview
(a contact cannot confirm the owner's preview).

This module is a thin façade over the `pending_previews` + `extraction_events`
tables plus the action_router — it doesn't know how to talk to Telegram.
The gateway owns all adapter I/O; this module owns the state machine.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from typing import Any

from hermes_state import SessionDB

from .action_router import (
    RoutedAction,
    RoutedHandlers,
    format_extraction_response,
    route_actions,
)
from .extraction import ExtractedAction, ExtractionResult

logger = logging.getLogger(__name__)

PREVIEW_TTL_SECONDS = 600  # 10 minutes


@dataclass
class ConfirmationResult:
    """Outcome of confirming (all or subset) a preview."""
    status: str  # "confirmed" | "partial_confirmed" | "cancelled" | "expired" | "not_found"
    reply_text: str
    routed: list[RoutedAction]
    preview_id: str | None = None


class PendingPreviewManager:
    """Owns the lifecycle of long-transcript preview confirmations."""

    def __init__(self, db: SessionDB) -> None:
        self.db = db

    # ------------------------------------------------------------------
    # Create
    # ------------------------------------------------------------------

    def create_preview(
        self,
        *,
        preview_id: str,
        extraction_id: str,
        sender_id: str,
        chat_id: str,
        routed: list[RoutedAction],
        ttl_seconds: int = PREVIEW_TTL_SECONDS,
    ) -> None:
        """Persist a PendingPreview with a snapshot of the actions to execute.

        Any prior active preview for `sender_id` is auto-expired (one active
        preview per sender at any time — enforced in the DB layer).
        """
        payload = [
            r.action.as_dict() | {
                "routed_status": r.status,
                "routed_error": r.error,
            }
            for r in routed if r.status == "pending"
        ]
        self.db.create_pending_preview(
            preview_id=preview_id,
            extraction_id=extraction_id,
            sender_id=str(sender_id),
            chat_id=str(chat_id),
            actions_json=json.dumps(payload),
            ttl_seconds=ttl_seconds,
        )

    # ------------------------------------------------------------------
    # Lookup
    # ------------------------------------------------------------------

    def get_active_preview(self, sender_id: str) -> dict[str, Any] | None:
        """Return the sender's active preview, auto-expiring past-TTL rows.

        Returns None if there's no active preview (or the prior one expired
        silently on read).
        """
        return self.db.get_active_preview_by_sender(str(sender_id))

    def is_expired(self, preview: dict[str, Any]) -> bool:
        return float(preview.get("expires_at", 0.0)) < time.time()

    # ------------------------------------------------------------------
    # Resolve
    # ------------------------------------------------------------------

    def _load_actions(self, preview: dict[str, Any]) -> list[ExtractedAction]:
        raw = preview.get("actions_json")
        if not raw:
            return []
        try:
            items = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return []
        out: list[ExtractedAction] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            out.append(ExtractedAction(
                action_type=item.get("action_type", ""),
                content=item.get("content", ""),
                confidence=float(item.get("confidence", 0.0)),
                metadata=item.get("metadata") or {},
            ))
        return out

    def _execute(
        self,
        *,
        preview: dict[str, Any],
        indices: list[int] | None,
        sender_capabilities: set[str],
        sender_name: str | None,
        handlers: RoutedHandlers,
    ) -> list[RoutedAction]:
        """Run route_actions on the preview's snapshot (short-input path).

        `indices` is 1-based to match the numbered preview; None means execute
        every action in the snapshot.
        """
        actions = self._load_actions(preview)
        if indices is not None:
            chosen = set(i for i in indices if 1 <= i <= len(actions))
            actions = [a for i, a in enumerate(actions, start=1) if i in chosen]

        result = ExtractionResult(
            actions=actions,
            entities_mentioned=[],
            summary="",
            transcript_hash="",
        )
        return route_actions(
            result,
            sender_id=str(preview.get("sender_id")),
            sender_capabilities=sender_capabilities,
            sender_name=sender_name,
            handlers=handlers,
            long_input=False,  # now we execute
        )

    def confirm_all(
        self,
        *,
        sender_id: str,
        sender_capabilities: set[str],
        handlers: RoutedHandlers,
        sender_name: str | None = None,
    ) -> ConfirmationResult:
        return self._confirm(
            sender_id=sender_id, indices=None,
            sender_capabilities=sender_capabilities,
            handlers=handlers, sender_name=sender_name,
        )

    def confirm_subset(
        self,
        *,
        sender_id: str,
        indices: list[int],
        sender_capabilities: set[str],
        handlers: RoutedHandlers,
        sender_name: str | None = None,
    ) -> ConfirmationResult:
        return self._confirm(
            sender_id=sender_id, indices=indices,
            sender_capabilities=sender_capabilities,
            handlers=handlers, sender_name=sender_name,
        )

    def cancel(self, *, sender_id: str) -> ConfirmationResult:
        preview = self.get_active_preview(sender_id)
        if preview is None:
            return ConfirmationResult(
                status="not_found",
                reply_text="Não há pré-visualização ativa.",
                routed=[],
            )
        self.db.update_preview_status(preview["preview_id"], "cancelled")
        return ConfirmationResult(
            status="cancelled",
            reply_text="Cancelado. Nenhuma ação foi executada.",
            routed=[],
            preview_id=preview["preview_id"],
        )

    def _confirm(
        self,
        *,
        sender_id: str,
        indices: list[int] | None,
        sender_capabilities: set[str],
        handlers: RoutedHandlers,
        sender_name: str | None,
    ) -> ConfirmationResult:
        preview = self.get_active_preview(sender_id)
        if preview is None:
            return ConfirmationResult(
                status="not_found",
                reply_text="Não há pré-visualização ativa para confirmar.",
                routed=[],
            )
        # Security: only the original sender can confirm.
        if str(preview.get("sender_id")) != str(sender_id):
            return ConfirmationResult(
                status="not_found",
                reply_text="Somente quem criou a pré-visualização pode confirmar.",
                routed=[],
            )
        if self.is_expired(preview):
            self.db.update_preview_status(preview["preview_id"], "expired")
            return ConfirmationResult(
                status="expired",
                reply_text="Pré-visualização expirou. Envie a transcrição novamente.",
                routed=[],
                preview_id=preview["preview_id"],
            )

        routed = self._execute(
            preview=preview,
            indices=indices,
            sender_capabilities=sender_capabilities,
            sender_name=sender_name,
            handlers=handlers,
        )

        new_status = "confirmed" if indices is None else "partial_confirmed"
        self.db.update_preview_status(preview["preview_id"], new_status)

        # Also record on the ExtractionEvent that we moved past preview.
        self.db.update_extraction_execution_mode(
            preview["extraction_id"], "auto",
        )

        # Craft the post-execution summary in pt-BR.
        result_proxy = ExtractionResult(
            actions=[r.action for r in routed],
            entities_mentioned=[],
            summary="",
            transcript_hash="",
        )
        reply = format_extraction_response(result_proxy, routed)
        return ConfirmationResult(
            status=new_status,
            reply_text=reply,
            routed=routed,
            preview_id=preview["preview_id"],
        )


# ---------------------------------------------------------------------------
# Reply parsing (Portuguese) — "confirmar", "confirmar 2, 4", "cancelar"
# ---------------------------------------------------------------------------

import re as _re

_CONFIRM_ALL_RE = _re.compile(r"^\s*(confirmar|confirma|ok|sim)\s*\.?\s*$", _re.IGNORECASE)
_CONFIRM_SUBSET_RE = _re.compile(
    # Require at least one digit so "confirma   " (trailing whitespace only)
    # doesn't match subset with empty indices.
    r"^\s*(confirmar|confirma)\s+(\d[\d\s,.;]*?)\s*\.?\s*$", _re.IGNORECASE,
)
_CANCEL_RE = _re.compile(r"^\s*(cancelar|cancela|não|nao|n)\s*\.?\s*$", _re.IGNORECASE)


@dataclass
class ParsedReply:
    kind: str  # "confirm_all" | "confirm_subset" | "cancel" | "unknown"
    indices: list[int] | None = None


def parse_preview_reply(text: str) -> ParsedReply:
    """Classify a short Portuguese reply against a pending preview.

    Only matches explicit short responses so we don't swallow unrelated
    messages. Anything we don't understand returns kind="unknown" and the
    caller should fall through to the normal flow.
    """
    if not text:
        return ParsedReply(kind="unknown")

    subset_match = _CONFIRM_SUBSET_RE.match(text)
    if subset_match:
        raw_indices = subset_match.group(2)
        # Accept commas, semicolons, or bare spaces as separators.
        tokens = _re.split(r"[,\s;]+", raw_indices.strip())
        indices: list[int] = []
        for t in tokens:
            if not t:
                continue
            try:
                indices.append(int(t))
            except ValueError:
                return ParsedReply(kind="unknown")
        if indices:
            return ParsedReply(kind="confirm_subset", indices=indices)
        return ParsedReply(kind="unknown")

    if _CONFIRM_ALL_RE.match(text):
        return ParsedReply(kind="confirm_all")
    if _CANCEL_RE.match(text):
        return ParsedReply(kind="cancel")
    return ParsedReply(kind="unknown")
