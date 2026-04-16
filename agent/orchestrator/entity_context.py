"""Hermes-scoped entity context accumulator (021 US4).

Tracks people, projects, tools, and concepts mentioned across voice notes,
documents, and pasted transcripts. Accumulates per-entity snippets so
owners can query `/entity João` later and see everything Hermes has heard
about that person.

Explicitly segregated from HD Engine's `persons` table (FR-015). Hermes
entities are a private Hermes-only log; they don't sync to HD.

Storage model:
- One row per entity keyed by `name_lower` (lowercase disambiguation).
- `context_snippets_json` holds an array of `{text, source_extraction_id,
  added_at}` capped at 50 entries (oldest dropped on overflow).
- Collision detection (FR-018): when two entities share a normalized name
  and context is ambiguous, callers should ask a clarifying question
  instead of silently merging.
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

from hermes_state import SessionDB

logger = logging.getLogger(__name__)

MAX_CONTEXT_SNIPPETS = 50
BRT = timezone(timedelta(hours=-3))
ALLOWED_ENTITY_TYPES = {"person", "project", "tool", "concept"}


def _new_entity_id() -> str:
    return f"ent_{uuid4().hex[:12]}"


class EntityContextManager:
    """Manages the entity_context table from the Hermes perspective."""

    def __init__(self, db: SessionDB) -> None:
        self.db = db

    # ------------------------------------------------------------------
    # Write
    # ------------------------------------------------------------------

    def update_context(
        self,
        *,
        name: str,
        context_snippet: str | None = None,
        entity_type: str = "person",
        source_extraction_id: str | None = None,
    ) -> dict[str, Any] | None:
        """Upsert the entity and append the context snippet.

        Returns the entity record on success, None if `name` is empty or the
        entity_type is unknown.
        """
        clean_name = (name or "").strip()
        if not clean_name:
            return None
        if entity_type not in ALLOWED_ENTITY_TYPES:
            logger.debug("entity: unknown type %r, defaulting to 'person'", entity_type)
            entity_type = "person"

        existing = self.db.get_entity_by_name(clean_name)
        entity_id = existing["entity_id"] if existing else _new_entity_id()

        # Merge or initialize the snippets list.
        snippets = []
        if existing:
            raw = existing.get("context_snippets_json")
            if raw:
                try:
                    snippets = json.loads(raw) or []
                    if not isinstance(snippets, list):
                        snippets = []
                except (json.JSONDecodeError, TypeError):
                    snippets = []

        if context_snippet and context_snippet.strip():
            snippets.append({
                "text": context_snippet.strip()[:1000],  # cap per-snippet size
                "source_extraction_id": source_extraction_id,
                "added_at": time.time(),
            })
            # Keep only the most recent MAX_CONTEXT_SNIPPETS entries.
            if len(snippets) > MAX_CONTEXT_SNIPPETS:
                snippets = snippets[-MAX_CONTEXT_SNIPPETS:]

        # upsert_entity bumps mention_count + last_mentioned_at for existing
        # entities; pre-serialize snippets since we're also attaching them.
        snippets_json = json.dumps(snippets) if snippets else None
        self.db.upsert_entity(
            entity_id=entity_id,
            name=clean_name,
            entity_type=entity_type,
            context_snippets_json=snippets_json if not existing else None,
        )
        if existing and snippets_json:
            self.db.append_entity_context_snippet(entity_id, snippets_json)

        return self.db.get_entity_by_name(clean_name)

    # ------------------------------------------------------------------
    # Read
    # ------------------------------------------------------------------

    def get_entity(self, name: str) -> dict[str, Any] | None:
        """Case-insensitive lookup by display name."""
        return self.db.get_entity_by_name((name or "").strip())

    def search_entities(self, query: str) -> list[dict[str, Any]]:
        """Prefix-match search over known entities (case-insensitive)."""
        q = (query or "").strip().lower()
        if not q:
            return []
        rows = self.db.list_entities(limit=500)
        return [r for r in rows if (r.get("name_lower") or "").startswith(q)]

    def list_all_entities(
        self, *, entity_type: str | None = None, limit: int = 100,
    ) -> list[dict[str, Any]]:
        return self.db.list_entities(entity_type=entity_type, limit=limit)

    # ------------------------------------------------------------------
    # Collision detection (FR-018)
    # ------------------------------------------------------------------

    def detect_collision(self, name: str) -> bool:
        """True when the name matches an existing entity and we should ask
        a clarifying question before accumulating new context.

        For v1 we treat any EXACT-match on `name_lower` as a potential
        collision when the caller hasn't supplied additional qualifying
        context (surname, role, etc.) — callers decide whether to ask.
        """
        return self.get_entity(name) is not None

    # ------------------------------------------------------------------
    # Formatting
    # ------------------------------------------------------------------

    def format_entity_summary(self, name: str, *, max_lines: int = 10) -> str:
        """Render the accumulated context for an entity as pt-BR text.

        Shape:
            👤 João (person, 12 menções, última: 2026-04-16)
              • "reunião sexta 10h"  — 2026-04-14
              • "ligar depois de domingo"  — 2026-04-13
              ...
        """
        rec = self.get_entity(name)
        if rec is None:
            return f"Não encontrei nenhuma menção a \"{name}\"."

        snippets: list[dict[str, Any]] = []
        raw = rec.get("context_snippets_json")
        if raw:
            try:
                parsed = json.loads(raw) or []
                if isinstance(parsed, list):
                    snippets = parsed
            except (json.JSONDecodeError, TypeError):
                snippets = []

        # Sort newest first.
        snippets.sort(key=lambda s: s.get("added_at", 0), reverse=True)

        last_seen = datetime.fromtimestamp(
            rec.get("last_mentioned_at") or time.time(), tz=BRT,
        ).strftime("%Y-%m-%d %H:%M")

        lines = [
            f"👤 **{rec.get('name', name)}** "
            f"({rec.get('entity_type', 'person')}, "
            f"{rec.get('mention_count', 0)} menções, última: {last_seen})",
        ]
        for snip in snippets[:max_lines]:
            added = datetime.fromtimestamp(
                snip.get("added_at") or 0, tz=BRT,
            ).strftime("%Y-%m-%d")
            text = snip.get("text") or ""
            lines.append(f"  • \"{text}\"  — {added}")
        if not snippets:
            lines.append("  (sem contexto acumulado ainda)")
        return "\n".join(lines)

    def format_entity_list(
        self, *, entity_type: str | None = None, limit: int = 20,
    ) -> str:
        rows = self.list_all_entities(entity_type=entity_type, limit=limit)
        if not rows:
            return "Nenhuma entidade registrada ainda."
        header = f"📇 **Entidades** ({len(rows)} mostradas)"
        body = [header]
        for r in rows:
            last = datetime.fromtimestamp(
                r.get("last_mentioned_at") or 0, tz=BRT,
            ).strftime("%Y-%m-%d")
            body.append(
                f"  • {r.get('name')} "
                f"({r.get('entity_type', 'person')}, "
                f"{r.get('mention_count', 0)} menções, última: {last})"
            )
        return "\n".join(body)
