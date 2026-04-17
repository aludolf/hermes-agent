"""Teams OAuth authentication via MSAL device-code flow (022 US1).

TeamsAuthManager wraps msal.PublicClientApplication with:
- device-code flow for first-time authorization
- in-memory token cache with expiry-based refresh
- credential persistence via CredentialsStore (Fernet-encrypted)
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any

logger = logging.getLogger(__name__)

MS_GRAPH_AUTHORITY = "https://login.microsoftonline.com/consumers"
MS_GRAPH_SCOPES = [
    "Chat.Read",
    "ChannelMessage.Read.All",
    "User.Read",
    "offline_access",
]

_CRED_KIND_REFRESH = "ms_graph_refresh_token"


class TeamsAuthError(Exception):
    pass


class TeamsAuthManager:
    """Manages Microsoft Graph authentication for the Hermes Teams sentinel.

    Auth tokens are stored encrypted via CredentialsStore.  A single
    in-memory cache avoids re-reading the DB on every API call.
    """

    def __init__(
        self,
        credentials_store,
        *,
        client_id: str | None = None,
        authority: str = MS_GRAPH_AUTHORITY,
    ) -> None:
        self._store = credentials_store
        self._client_id = client_id or os.getenv("MS_GRAPH_CLIENT_ID", "")
        if not self._client_id:
            raise TeamsAuthError(
                "MS_GRAPH_CLIENT_ID is not set — cannot initialize Teams auth"
            )
        self._authority = authority
        self._cached_token: str | None = None
        self._cached_expiry: float = 0.0
        self._credential_id: str | None = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def start_device_code_flow(self) -> dict[str, Any]:
        """Begin the device-code flow.  Returns the user-facing dict."""
        app = self._build_msal_app()
        flow = app.initiate_device_flow(scopes=MS_GRAPH_SCOPES)
        if "error" in flow:
            raise TeamsAuthError(
                f"Could not start device-code flow: {flow.get('error_description', flow)}"
            )
        return {
            "user_code": flow["user_code"],
            "verification_uri": flow["verification_uri"],
            "message": flow.get("message", ""),
            "flow_state": flow,
        }

    def complete_device_code_flow(self, flow_state: dict) -> tuple[str, str, float]:
        """Block until the user completes auth.

        Returns (access_token, refresh_token, expires_at_epoch).
        Stores the refresh token encrypted.
        """
        app = self._build_msal_app()
        result = app.acquire_token_by_device_flow(flow_state)
        if "error" in result:
            raise TeamsAuthError(
                f"Device-code flow failed: {result.get('error_description', result)}"
            )
        access_token = result["access_token"]
        refresh_token = result.get("refresh_token", "")
        expires_in = int(result.get("expires_in", 3600))
        expires_at = time.time() + expires_in - 60

        self._cache_token(access_token, expires_at)
        if refresh_token:
            self._persist_refresh_token(refresh_token)
        return access_token, refresh_token, expires_at

    def get_access_token(self) -> str:
        """Return a live access token, refreshing silently if needed."""
        if self._cached_token and time.time() < self._cached_expiry:
            return self._cached_token
        return self._refresh_from_store()

    def has_credentials(self) -> bool:
        """Return True if a refresh token has been persisted."""
        return self._find_credential_id() is not None

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _build_msal_app(self):
        try:
            import msal
        except ImportError as exc:  # pragma: no cover
            raise TeamsAuthError(
                "msal is not installed — run: pip install 'msal>=1.31'"
            ) from exc
        return msal.PublicClientApplication(
            client_id=self._client_id,
            authority=self._authority,
        )

    def _cache_token(self, token: str, expires_at: float) -> None:
        self._cached_token = token
        self._cached_expiry = expires_at

    def _persist_refresh_token(self, refresh_token: str) -> None:
        existing = self._find_credential_id()
        if existing:
            self._store.rotate(existing, refresh_token)
            self._credential_id = existing
        else:
            cid = self._store.put(
                kind=_CRED_KIND_REFRESH,
                secret=refresh_token,
                label="ms_graph_refresh_token",
            )
            self._credential_id = cid
        logger.info("Teams refresh token persisted (credential_id=%s)", self._credential_id)

    def _find_credential_id(self) -> str | None:
        if self._credential_id:
            return self._credential_id
        rows = self._store.list_metadata(kind=_CRED_KIND_REFRESH)
        if rows:
            self._credential_id = rows[0]["credential_id"]
            return self._credential_id
        return None

    def _refresh_from_store(self) -> str:
        cid = self._find_credential_id()
        if not cid:
            raise TeamsAuthError(
                "No refresh token found — run /teams_connect first"
            )
        refresh_token = self._store.get(cid)
        app = self._build_msal_app()
        result = app.acquire_token_by_refresh_token(
            refresh_token=refresh_token,
            scopes=MS_GRAPH_SCOPES,
        )
        if "error" in result:
            raise TeamsAuthError(
                f"Token refresh failed: {result.get('error_description', result)}"
            )
        access_token = result["access_token"]
        new_refresh = result.get("refresh_token", refresh_token)
        expires_in = int(result.get("expires_in", 3600))
        expires_at = time.time() + expires_in - 60
        self._cache_token(access_token, expires_at)
        if new_refresh != refresh_token:
            self._persist_refresh_token(new_refresh)
        return access_token
