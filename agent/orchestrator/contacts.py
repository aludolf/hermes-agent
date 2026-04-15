"""Role-based contact management for household access control (003).

Maps Telegram user IDs to roles and capabilities. The gateway uses this
to decide what an inbound user can do:

- owner: full access to all productivity features and commands
- contact: restricted to approved capabilities (e.g. lists only)
- pending: triggers an approval flow to the owner
- blocked: silent drop, no response

The owner is typically hardcoded via HERMES_OWNER_TELEGRAM_ID env var
and auto-created on first startup.
"""

from __future__ import annotations

from typing import Any

from hermes_state import SessionDB

from .models import Capability, ContactRole

DEFAULT_CONTACT_CAPABILITIES = [Capability.LISTS]
VALID_CAPABILITIES = {c.value for c in Capability}


class ContactManager:
    """Façade for contact role lookups and lifecycle."""

    def __init__(self, db: SessionDB) -> None:
        self.db = db

    # ------------------------------------------------------------------
    # Lookups
    # ------------------------------------------------------------------

    def get_role(self, telegram_id: str) -> dict[str, Any] | None:
        """Look up contact by telegram_id. Returns role record or None."""
        return self.db.get_contact_role(str(telegram_id))

    def is_owner(self, telegram_id: str) -> bool:
        """Check if this user is the owner."""
        rec = self.get_role(telegram_id)
        return rec is not None and rec.get("role") == ContactRole.OWNER

    def get_capabilities(self, telegram_id: str) -> set[str]:
        """Return the set of approved capabilities for a contact.

        Owner gets {'all'}.
        Approved contacts get their approved_capabilities set.
        Pending/blocked/unknown users get empty set.
        """
        rec = self.get_role(telegram_id)
        if rec is None:
            return set()

        role = rec.get("role")
        if role == ContactRole.OWNER:
            return {Capability.ALL.value}
        if role == ContactRole.CONTACT:
            caps = rec.get("approved_capabilities") or []
            return set(caps)
        return set()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def register_pending(self, telegram_id: str, display_name: str) -> dict[str, Any]:
        """Register a new pending contact. Idempotent.

        Called when an unknown user first messages Hermes. The gateway
        should then DM the owner with an approval request.
        Returns the contact record.
        """
        existing = self.get_role(telegram_id)
        if existing:
            return existing

        self.db.create_contact_role(
            contact_id=str(telegram_id),
            display_name=display_name or "",
            role=ContactRole.PENDING,
            language="pt-BR",
        )
        return self.get_role(telegram_id) or {}

    def ensure_owner(self, telegram_id: str, display_name: str = "Owner") -> None:
        """Ensure the owner record exists. Idempotent — no-op if already set.

        Called on gateway startup with HERMES_OWNER_TELEGRAM_ID.
        """
        existing = self.get_role(telegram_id)
        if existing and existing.get("role") == ContactRole.OWNER:
            return

        if existing:
            self.db.update_contact_role(
                str(telegram_id),
                role=ContactRole.OWNER,
                approved_capabilities=[Capability.ALL.value],
                approved_by="system",
            )
        else:
            self.db.create_contact_role(
                contact_id=str(telegram_id),
                display_name=display_name,
                role=ContactRole.OWNER,
                language="pt-BR",
                approved_capabilities=[Capability.ALL.value],
                approved_by="system",
            )

    def approve_contact(
        self,
        contact_id: str,
        *,
        approved_by: str,
        capabilities: list[str] | None = None,
        language: str = "pt-BR",
    ) -> dict[str, Any]:
        """Approve a pending contact with specific capabilities.

        Default capabilities: ['lists'].
        Validates that all capabilities are valid values.
        Raises ValueError on invalid capability or unknown contact.
        """
        rec = self.get_role(contact_id)
        if rec is None:
            raise ValueError(f"Contact {contact_id} not found")
        if rec.get("role") == "owner":
            raise ValueError("Cannot re-approve the owner")

        caps = [str(c) for c in (capabilities or DEFAULT_CONTACT_CAPABILITIES)]
        invalid = [c for c in caps if c not in VALID_CAPABILITIES]
        if invalid:
            raise ValueError(f"Invalid capabilities: {invalid}")

        self.db.update_contact_role(
            str(contact_id),
            role=ContactRole.CONTACT,
            language=language,
            approved_capabilities=caps,
            approved_by=approved_by,
        )
        return self.get_role(contact_id) or {}

    def block_contact(self, contact_id: str) -> None:
        """Block a contact. Blocked users get silent message drop."""
        self.db.block_contact_role(str(contact_id))

    def update_language(self, contact_id: str, language: str) -> None:
        """Change a contact's response language."""
        self.db.update_contact_role(str(contact_id), language=language)

    def list_contacts(self, *, role: str | None = None) -> list[dict[str, Any]]:
        """List all contacts, optionally filtered by role."""
        return self.db.list_contact_roles(role=role)
