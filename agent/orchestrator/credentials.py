"""Encrypted credentials store for the 022 sentinels.

Fernet-symmetric encryption keyed by ``HERMES_MASTER_KEY`` (urlsafe-base64,
32 bytes). Stores OAuth refresh tokens and IMAP app-passwords so the
gateway can survive container restarts without the owner re-authing every
time. Fails closed when the key is missing or invalid — this is a
deliberate safety property: sentinels refuse to touch any protocol that
would leak plaintext credentials to logs or SQL dumps.
"""

from __future__ import annotations

import logging
import os
from uuid import uuid4

from hermes_state import SessionDB

from .models import CredentialKind

logger = logging.getLogger(__name__)

_ALLOWED_KINDS: set[str] = {k.value for k in CredentialKind}


class CredentialsStoreError(Exception):
    """Raised when encryption/decryption or key handling fails."""


class CredentialsStore:
    """Thin Fernet wrapper over ``credentials_store`` rows.

    A single instance is safe to share across the gateway — the Fernet
    instance is stateless and thread-safe.
    """

    _ENV_KEY = "HERMES_MASTER_KEY"

    def __init__(self, db: SessionDB, *, master_key: str | None = None) -> None:
        key = (master_key or os.getenv(self._ENV_KEY, "")).strip()
        if not key:
            raise CredentialsStoreError(
                f"{self._ENV_KEY} not set — sentinels cannot start without "
                "a master encryption key. Generate one with: "
                "python -c \"from cryptography.fernet import Fernet; "
                "print(Fernet.generate_key().decode())\""
            )
        try:
            from cryptography.fernet import Fernet
        except ImportError as e:  # pragma: no cover — dep is required
            raise CredentialsStoreError(
                f"cryptography package unavailable: {e}"
            ) from e
        try:
            self._fernet = Fernet(key.encode("utf-8") if isinstance(key, str) else key)
        except Exception as e:
            raise CredentialsStoreError(
                f"Invalid {self._ENV_KEY}: must be a 32-byte urlsafe-base64 string "
                f"(got an unreadable value): {e}"
            ) from e
        self.db = db

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def put(
        self,
        *,
        kind: str | CredentialKind,
        secret: str,
        label: str | None = None,
    ) -> str:
        """Encrypt and store a secret. Returns the credential_id."""
        kind_str = str(kind)
        if kind_str not in _ALLOWED_KINDS:
            raise CredentialsStoreError(f"unknown credential kind: {kind_str!r}")
        if not secret:
            raise CredentialsStoreError("secret cannot be empty")
        ciphertext = self._fernet.encrypt(secret.encode("utf-8"))
        credential_id = f"cred_{uuid4().hex[:12]}"
        self.db.put_credential_ciphertext(
            credential_id=credential_id,
            kind=kind_str,
            ciphertext=ciphertext,
            label=label,
        )
        logger.info(
            "credentials: stored new %s credential (id=%s)", kind_str, credential_id,
        )
        return credential_id

    def get(self, credential_id: str) -> str:
        """Decrypt and return the plaintext secret for the given id.

        Raises CredentialsStoreError on missing rows or decrypt failure.
        """
        row = self.db.get_credential_ciphertext(credential_id)
        if row is None:
            raise CredentialsStoreError(f"credential not found: {credential_id}")
        try:
            plaintext = self._fernet.decrypt(row["ciphertext"])
        except Exception as e:
            # Re-wrap cryptography's InvalidToken etc. into our domain exception.
            # Never log ciphertext or plaintext here.
            raise CredentialsStoreError(
                f"decrypt failed for credential {credential_id} "
                f"(kind={row.get('kind')}): {type(e).__name__}"
            ) from e
        return plaintext.decode("utf-8")

    def rotate(self, credential_id: str, new_secret: str) -> None:
        """Replace the ciphertext for an existing credential."""
        if not new_secret:
            raise CredentialsStoreError("new secret cannot be empty")
        # Ensure the row exists (and the new key decrypts the old one — a
        # sanity check that catches master-key mistakes BEFORE overwriting).
        _ = self.get(credential_id)
        ciphertext = self._fernet.encrypt(new_secret.encode("utf-8"))
        self.db.rotate_credential(credential_id, ciphertext)
        logger.info("credentials: rotated credential id=%s", credential_id)

    def delete(self, credential_id: str) -> None:
        self.db.delete_credential(credential_id)
        logger.info("credentials: deleted credential id=%s", credential_id)

    def list_metadata(self, kind: str | None = None) -> list[dict]:
        """List credentials WITHOUT ciphertext — id, kind, label, timestamps only."""
        return self.db.list_credentials_by_kind(kind=kind)
