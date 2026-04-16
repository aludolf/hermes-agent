"""Shared list management for household tasks (003).

Lists are append-only item collections shared between the owner and
approved contacts. Each item tracks who added it and when. Items can
be checked off (for shopping) or deleted.

Default lists seeded on first run:
- Compras (shopping)
- Material Escolar (school)
- Reparos (repairs)
- Recados (errands)

The owner manages lists via slash commands. Contacts add items via
natural Portuguese intercepted before the LLM, or via LLM fallback.
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

from hermes_state import SessionDB

from .models import ListType

# Default lists created on first run (Portuguese names, standard types)
DEFAULT_LISTS: list[tuple[str, str]] = [
    ("Compras", ListType.SHOPPING.value),
    ("Material Escolar", ListType.SCHOOL.value),
    ("Reparos", ListType.REPAIRS.value),
    ("Recados", ListType.ERRANDS.value),
]


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex[:12]}"


class ListManager:
    """Façade for shared list CRUD and summary formatting."""

    def __init__(self, db: SessionDB) -> None:
        self.db = db

    # ------------------------------------------------------------------
    # Lists
    # ------------------------------------------------------------------

    def create_list(
        self,
        name: str,
        list_type: str = "shopping",
        created_by: str = "system",
    ) -> dict[str, Any]:
        """Create a new shared list. Returns the list record."""
        list_id = _new_id("list")
        self.db.create_shared_list(
            list_id=list_id,
            name=name,
            list_type=str(list_type),
            created_by=str(created_by),
        )
        return self.db.get_shared_list(list_id) or {}

    def get_list(self, list_id: str) -> dict[str, Any] | None:
        """Get a list record by ID (without items)."""
        return self.db.get_shared_list(list_id)

    def find_list_by_name(self, name: str) -> dict[str, Any] | None:
        """Find an active list by exact (case-insensitive) name match."""
        return self.db.find_shared_list_by_name(name)

    def list_all(self, *, status: str = "active") -> list[dict[str, Any]]:
        """List all lists with their status."""
        return self.db.list_shared_lists(status=status)

    def archive_list(self, list_id: str) -> None:
        """Archive a list (soft delete)."""
        self.db.archive_shared_list(list_id)

    def seed_default_lists(self, created_by: str = "system") -> list[dict[str, Any]]:
        """Create the default household lists if they don't already exist.

        Idempotent — returns the list of newly created lists (empty if all
        already existed).
        """
        created = []
        for name, list_type in DEFAULT_LISTS:
            existing = self.db.find_shared_list_by_name(name)
            if existing is None:
                rec = self.create_list(name=name, list_type=list_type, created_by=created_by)
                created.append(rec)
        return created

    # ------------------------------------------------------------------
    # Items
    # ------------------------------------------------------------------

    def add_item(
        self,
        list_id: str,
        content: str,
        added_by: str,
        added_by_name: str | None = None,
    ) -> dict[str, Any]:
        """Add an item to a list. Returns the item record.

        Raises ValueError on empty content or archived list.
        """
        content = content.strip()
        if not content:
            raise ValueError("Item content cannot be empty")
        if len(content) > 500:
            raise ValueError("Item content too long (max 500 characters)")

        list_rec = self.db.get_shared_list(list_id)
        if list_rec is None:
            raise ValueError(f"List {list_id} not found")
        if list_rec.get("status") != "active":
            raise ValueError(f"List '{list_rec.get('name')}' is archived")

        item_id = _new_id("item")
        self.db.create_list_item(
            item_id=item_id,
            list_id=list_id,
            content=content,
            added_by=str(added_by),
            added_by_name=added_by_name,
        )
        return self.db.get_list_item(item_id) or {}

    def remove_item(self, item_id: str) -> None:
        """Delete an item from a list."""
        self.db.delete_list_item(item_id)

    def check_item(self, item_id: str, checked_by: str) -> None:
        """Mark an item as checked (purchased / done)."""
        self.db.update_list_item_checked(item_id, checked=True, checked_by=str(checked_by))

    def uncheck_item(self, item_id: str) -> None:
        """Uncheck a previously checked item."""
        self.db.update_list_item_checked(item_id, checked=False)

    def get_items(self, list_id: str, *, include_checked: bool = False) -> list[dict[str, Any]]:
        """Get items for a list. Unchecked only by default."""
        return self.db.get_list_items(list_id, include_checked=include_checked)

    # ------------------------------------------------------------------
    # Formatting
    # ------------------------------------------------------------------

    def find_item_fuzzy(self, text: str, *, list_id: str | None = None) -> dict[str, Any] | None:
        """Fuzzy-match an item by content text across one or all active lists.

        Searches unchecked items. Returns the best match (case-insensitive
        substring), or None if no match.
        """
        text_lower = text.strip().lower()
        if not text_lower:
            return None

        if list_id:
            lists_to_search = [{"list_id": list_id}]
        else:
            lists_to_search = self.list_all(status="active")

        best = None
        for lr in lists_to_search:
            items = self.get_items(lr["list_id"], include_checked=False)
            for item in items:
                content_lower = item["content"].lower()
                # Exact match takes priority
                if content_lower == text_lower:
                    return item
                # Substring match
                if text_lower in content_lower or content_lower in text_lower:
                    if best is None or len(item["content"]) < len(best["content"]):
                        best = item
        return best

    def check_item_by_text(
        self, text: str, checked_by: str, *, list_id: str | None = None,
    ) -> dict[str, Any] | None:
        """Find and check off an item by fuzzy text match.

        Returns the checked item dict with list_name, or None if not found.
        """
        item = self.find_item_fuzzy(text, list_id=list_id)
        if item is None:
            return None

        self.check_item(item["item_id"], checked_by)
        list_rec = self.db.get_shared_list(item["list_id"])
        return {
            **item,
            "list_name": list_rec["name"] if list_rec else "?",
            "checked_by": checked_by,
        }

    def format_list_summary(self, list_id: str) -> str:
        """Render a Telegram-friendly list summary for a single list."""
        list_rec = self.db.get_shared_list(list_id)
        if list_rec is None:
            return "Lista não encontrada."

        items = self.get_items(list_id, include_checked=False)
        lines = [f"📋 **{list_rec['name']}** ({len(items)} itens)"]
        if not items:
            lines.append("")
            lines.append("_Lista vazia._")
            return "\n".join(lines)

        lines.append("")
        for item in items:
            added_by = item.get("added_by_name") or "?"
            lines.append(f"• {item['content']} _(por {added_by})_")

        return "\n".join(lines)

    def format_all_lists_summary(self) -> str:
        """Render a compact overview of all active lists for briefings."""
        lists = self.list_all(status="active")
        if not lists:
            return "Nenhuma lista ativa."

        lines = ["🛒 **Listas ativas**", ""]
        for list_rec in lists:
            items = self.get_items(list_rec["list_id"], include_checked=False)
            count = len(items)
            last_line = ""
            if items:
                last = items[-1]
                added_by = last.get("added_by_name") or "?"
                last_line = f' — último: "{last["content"]}" por {added_by}'
            lines.append(f"• **{list_rec['name']}** ({count} itens){last_line}")

        return "\n".join(lines)
