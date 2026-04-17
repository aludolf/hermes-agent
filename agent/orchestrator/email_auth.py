"""Unified email authentication helpers for IMAP sentinel (022 US2).

Supports:
- App-password LOGIN (any IMAP server)
- XOAUTH2 SASL for Gmail (Google OAuth2) and Outlook (MSAL)

All functions are pure/async and have no side effects on the DB;
credential persistence is handled by the caller (EmailSentinel).
"""

from __future__ import annotations

import base64
import logging
import time
from typing import Literal

logger = logging.getLogger(__name__)

Provider = Literal["gmail", "outlook", "generic"]

_GMAIL_TOKEN_URL = "https://oauth2.googleapis.com/token"
_OUTLOOK_AUTHORITY = "https://login.microsoftonline.com/consumers"
_OUTLOOK_SCOPES = ["https://outlook.office.com/IMAP.AccessAsUser.All", "offline_access"]


def app_password_login_args(username: str, app_password: str) -> dict:
    """Return LOGIN args dict for aioimaplib plain LOGIN."""
    return {"username": username, "password": app_password}


def xoauth2_build_sasl_string(username: str, access_token: str) -> str:
    """Build the base64 SASL XOAUTH2 string for AUTHENTICATE XOAUTH2."""
    sasl = f"user={username}\x01auth=Bearer {access_token}\x01\x01"
    return base64.b64encode(sasl.encode("utf-8")).decode("ascii")


async def refresh_xoauth2_access_token(
    provider: Provider,
    refresh_token: str,
    *,
    client_id: str = "",
    client_secret: str = "",
) -> tuple[str, float]:
    """Refresh an XOAUTH2 access token.  Returns (access_token, expires_at)."""
    if provider == "gmail":
        return await _gmail_refresh(refresh_token, client_id=client_id, client_secret=client_secret)
    elif provider == "outlook":
        return await _outlook_refresh(refresh_token, client_id=client_id)
    else:
        raise ValueError(f"Unknown provider for XOAUTH2 refresh: {provider!r}")


# ------------------------------------------------------------------
# Gmail
# ------------------------------------------------------------------

async def _gmail_refresh(
    refresh_token: str, *, client_id: str, client_secret: str
) -> tuple[str, float]:
    import httpx
    data = {
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": client_id,
        "client_secret": client_secret,
    }
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(_GMAIL_TOKEN_URL, data=data)
    body = resp.json()
    if "error" in body:
        raise RuntimeError(f"Gmail token refresh failed: {body.get('error_description', body)}")
    expires_at = time.time() + int(body.get("expires_in", 3600)) - 60
    return body["access_token"], expires_at


# ------------------------------------------------------------------
# Outlook (MSAL)
# ------------------------------------------------------------------

async def _outlook_refresh(
    refresh_token: str, *, client_id: str
) -> tuple[str, float]:
    try:
        import msal
    except ImportError as exc:
        raise RuntimeError("msal not installed") from exc
    app = msal.PublicClientApplication(
        client_id=client_id or __import__("os").getenv("MS_GRAPH_CLIENT_ID", ""),
        authority=_OUTLOOK_AUTHORITY,
    )
    result = app.acquire_token_by_refresh_token(
        refresh_token=refresh_token,
        scopes=_OUTLOOK_SCOPES,
    )
    if "error" in result:
        raise RuntimeError(
            f"Outlook token refresh failed: {result.get('error_description', result)}"
        )
    expires_at = time.time() + int(result.get("expires_in", 3600)) - 60
    return result["access_token"], expires_at
