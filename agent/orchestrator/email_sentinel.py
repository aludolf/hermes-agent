"""IMAP IDLE email sentinel with 6-state machine (022 US2).

Per-watch state machine (from imap-idle-contract.md):
  disconnected → connecting → authenticating → idling
                                              ↓ (EXISTS push)
                                         fetching → idling
              ← reconnecting_backoff ← (error)
              ← disabled (after max retries)

UIDVALIDITY is checked on every connect; cursor persisted every fetch.
Keepalive: DONE + re-IDLE every 25 minutes (RFC 2177 recommends < 29 min).
"""

from __future__ import annotations

import asyncio
import email as email_lib
import email.policy
import hashlib
import logging
import time
from typing import Callable, Coroutine

logger = logging.getLogger(__name__)

_IDLE_TIMEOUT = 25 * 60           # seconds before keepalive DONE
_BACKOFF_INITIAL = 1.0
_BACKOFF_MAX = 300.0
_BACKOFF_MULTIPLIER = 2.0
_MAX_RECONNECT_ATTEMPTS = 10
_FETCH_PEEK_LIMIT = 50            # max UIDs fetched per EXISTS trigger


class EmailSentinelError(Exception):
    pass


class EmailSentinel:
    """Manages all mail watches for one owner.

    One _WatcherTask per mail_watch row; tasks are stored in self._tasks.
    """

    def __init__(self, *, session_db, extraction_queue, owner_id: str) -> None:
        self._db = session_db
        self._queue = extraction_queue
        self._owner_id = owner_id
        self._tasks: dict[str, asyncio.Task] = {}

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        watches = self._db.list_all_mail_watches()
        for w in watches:
            acct = self._db.get_mail_account(w["account_id"])
            if acct and acct.get("owner_id") == self._owner_id:
                await self._start_watcher(w)
        logger.info(
            "EmailSentinel started: %d watcher(s) for owner=%s",
            len(self._tasks),
            self._owner_id,
        )

    async def stop(self) -> None:
        for t in list(self._tasks.values()):
            t.cancel()
        logger.info("EmailSentinel stopped")

    # ------------------------------------------------------------------
    # Account management
    # ------------------------------------------------------------------

    async def connect_account(
        self,
        *,
        alias: str,
        host: str,
        port: int = 993,
        username: str,
        auth_method: str,
        secret: str,
        credentials_store,
        owner_id: str,
    ) -> dict:
        """Store credential, create mail_account row, probe IMAP once."""
        from uuid import uuid4
        cred_kind = "imap_xoauth2_refresh_token" if auth_method == "xoauth2" else "imap_app_password"
        cred_id = credentials_store.put(kind=cred_kind, secret=secret, label=f"{alias}-{username}")
        account_id = f"ma_{uuid4().hex[:12]}"
        self._db.create_mail_account(
            account_id=account_id,
            alias=alias,
            host=host,
            port=port,
            username=username,
            auth_method=auth_method,
            credential_ref=cred_id,
            owner_id=owner_id,
        )
        # Probe the connection once to validate credentials
        try:
            await self._probe_imap(host, port, username, auth_method, secret)
            self._db.update_mail_account_state(account_id, "live")
        except Exception as exc:
            self._db.update_mail_account_state(account_id, "disconnected")
            raise EmailSentinelError(f"IMAP probe failed: {exc}") from exc
        return self._db.get_mail_account(account_id)

    async def disconnect_account(self, alias: str, owner_id: str) -> bool:
        acct = self._db.find_mail_account_by_alias(owner_id, alias)
        if not acct:
            return False
        account_id = acct["account_id"]
        # Cancel watcher tasks for this account
        watches = self._db.list_mail_watches_by_account(account_id)
        for w in watches:
            t = self._tasks.pop(w["watch_id"], None)
            if t:
                t.cancel()
        self._db.delete_mail_account(account_id)
        return True

    # ------------------------------------------------------------------
    # Watch management
    # ------------------------------------------------------------------

    async def watch_folder(self, account_id: str, folder: str = "INBOX") -> dict:
        from uuid import uuid4
        watch_id = f"mw_{uuid4().hex[:12]}"
        self._db.create_mail_watch(watch_id=watch_id, account_id=account_id, folder=folder)
        watch = self._db.list_mail_watches_by_account(account_id)
        # Find the one we just created
        w = next((x for x in watch if x["watch_id"] == watch_id), None)
        if w:
            await self._start_watcher(w)
        return w or {}

    async def unwatch(self, watch_id: str) -> None:
        t = self._tasks.pop(watch_id, None)
        if t:
            t.cancel()
        self._db.delete_mail_watch(watch_id)

    # ------------------------------------------------------------------
    # Internal watcher task
    # ------------------------------------------------------------------

    async def _start_watcher(self, watch: dict) -> None:
        watch_id = watch["watch_id"]
        if watch_id in self._tasks and not self._tasks[watch_id].done():
            return
        task = asyncio.create_task(
            self._watch_loop(watch_id), name=f"email-watch-{watch_id}"
        )
        self._tasks[watch_id] = task

    async def _watch_loop(self, watch_id: str) -> None:
        backoff = _BACKOFF_INITIAL
        attempts = 0
        while True:
            watch = self._db.list_all_mail_watches()
            watch = next((w for w in watch if w["watch_id"] == watch_id), None)
            if not watch:
                break
            acct = self._db.get_mail_account(watch["account_id"])
            if not acct:
                break
            self._db.update_mail_watch_state(watch_id, "connecting")
            try:
                secret = await self._load_secret(acct)
                await self._run_idle_session(watch, acct, secret)
                backoff = _BACKOFF_INITIAL
                attempts = 0
            except asyncio.CancelledError:
                break
            except Exception as exc:
                attempts += 1
                logger.warning(
                    "Watch %s error (attempt %d): %s", watch_id, attempts, exc
                )
                if attempts >= _MAX_RECONNECT_ATTEMPTS:
                    self._db.update_mail_watch_state(watch_id, "disabled")
                    break
                self._db.update_mail_watch_state(
                    watch_id, "reconnecting_backoff", increment_reconnect=True
                )
                await asyncio.sleep(backoff)
                backoff = min(backoff * _BACKOFF_MULTIPLIER, _BACKOFF_MAX)
        logger.info("WatcherTask exiting for watch_id=%s", watch_id)

    async def _run_idle_session(self, watch: dict, acct: dict, secret: str) -> None:
        """Full connect → authenticate → SELECT → IDLE loop."""
        try:
            import aioimaplib
        except ImportError as exc:  # pragma: no cover
            raise EmailSentinelError("aioimaplib not installed") from exc

        watch_id = watch["watch_id"]
        folder = watch.get("folder", "INBOX")
        host = acct["host"]
        port = int(acct.get("port", 993))
        username = acct["username"]
        auth_method = acct.get("auth_method", "app_password")

        self._db.update_mail_watch_state(watch_id, "connecting")
        client = aioimaplib.IMAP4_SSL(host=host, port=port)
        await client.wait_hello_from_server()

        self._db.update_mail_watch_state(watch_id, "authenticating")
        if auth_method == "app_password":
            resp = await client.login(username, secret)
        else:
            from agent.orchestrator.email_auth import xoauth2_build_sasl_string
            sasl = xoauth2_build_sasl_string(username, secret)
            resp = await client.authenticate("XOAUTH2", lambda _: sasl.encode())
        if resp.result != "OK":
            raise EmailSentinelError(f"AUTH failed: {resp.lines}")

        sel_resp = await client.select(folder)
        if sel_resp.result != "OK":
            raise EmailSentinelError(f"SELECT {folder!r} failed")

        # UIDVALIDITY check
        uidvalidity = _parse_uidvalidity(sel_resp.lines)
        stored_uv = watch.get("uidvalidity")
        if stored_uv and stored_uv != uidvalidity:
            logger.warning(
                "UIDVALIDITY changed for watch %s: %s → %s — resetting cursor",
                watch_id, stored_uv, uidvalidity,
            )
            self._db.update_mail_watch_cursor(watch_id, last_seen_uid=0, uidvalidity=uidvalidity)
        elif not stored_uv:
            self._db.update_mail_watch_cursor(
                watch_id,
                last_seen_uid=watch.get("last_seen_uid") or 0,
                uidvalidity=uidvalidity,
            )

        # Catch-up: fetch any messages since last_seen_uid
        watch = next(
            (w for w in self._db.list_all_mail_watches() if w["watch_id"] == watch_id), watch
        )
        last_uid = watch.get("last_seen_uid") or 0
        if last_uid:
            await self._fetch_since(client, watch_id, last_uid + 1, acct)

        self._db.update_mail_watch_state(watch_id, "idling")
        idle_start = time.monotonic()

        try:
            while True:
                idle_resp = await asyncio.wait_for(
                    client.idle(), timeout=_IDLE_TIMEOUT
                )
                elapsed = time.monotonic() - idle_start
                if elapsed >= _IDLE_TIMEOUT - 5:
                    # Keepalive: DONE + re-IDLE
                    await client.idle_done()
                    await client.idle()
                    idle_start = time.monotonic()
                    continue

                # Parse EXISTS push
                if _has_exists(idle_resp):
                    await client.idle_done()
                    watch = next(
                        (w for w in self._db.list_all_mail_watches() if w["watch_id"] == watch_id),
                        watch,
                    )
                    last_uid = watch.get("last_seen_uid") or 0
                    await self._fetch_since(client, watch_id, last_uid + 1, acct)
                    await client.idle()
                    idle_start = time.monotonic()
        finally:
            try:
                await client.idle_done()
            except Exception:
                pass
            try:
                await client.logout()
            except Exception:
                pass

    async def _fetch_since(
        self, client, watch_id: str, start_uid: int, acct: dict
    ) -> None:
        uid_str = f"{start_uid}:*" if start_uid > 1 else "1:*"
        resp = await client.uid("FETCH", uid_str, "(BODY.PEEK[])")
        if resp.result != "OK":
            return
        messages = _parse_fetch_response(resp.lines)
        if not messages:
            return
        max_uid = start_uid - 1
        for uid, raw in messages:
            if uid < start_uid:
                continue
            max_uid = max(max_uid, uid)
            body_text = _parse_email_body(raw)
            if not body_text:
                continue
            transcript_hash = hashlib.sha256(body_text.encode("utf-8")).hexdigest()
            from agent.orchestrator.sentinel_queue import ExtractionItem
            item = ExtractionItem(
                transcript=body_text,
                sender_id=acct.get("owner_id", "owner"),
                sender_role="owner",
                source_type="email",
                source_format="email-imap",
                chat_id=watch_id,
                extra_kwargs={"transcript_hash": transcript_hash},
            )
            self._queue.enqueue_nowait(item)
        if max_uid >= start_uid:
            watch_list = self._db.list_all_mail_watches()
            w = next((x for x in watch_list if x["watch_id"] == watch_id), None)
            uv = w.get("uidvalidity") if w else None
            self._db.update_mail_watch_cursor(
                watch_id, last_seen_uid=max_uid, uidvalidity=uv or 0
            )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    async def _load_secret(self, acct: dict) -> str:
        """Load the credential secret from the credentials store."""
        cred_ref = acct.get("credential_ref", "")
        row = self._db.get_credential_ciphertext(cred_ref)
        if not row:
            raise EmailSentinelError(f"Credential {cred_ref!r} not found")
        return bytes(row["ciphertext"]).decode("utf-8")  # raw secret (decrypted by CredentialsStore layer above)

    async def _probe_imap(
        self, host: str, port: int, username: str, auth_method: str, secret: str
    ) -> None:
        try:
            import aioimaplib
        except ImportError as exc:  # pragma: no cover
            raise EmailSentinelError("aioimaplib not installed") from exc
        client = aioimaplib.IMAP4_SSL(host=host, port=port)
        await client.wait_hello_from_server()
        if auth_method == "app_password":
            resp = await client.login(username, secret)
        else:
            from agent.orchestrator.email_auth import xoauth2_build_sasl_string
            sasl = xoauth2_build_sasl_string(username, secret)
            resp = await client.authenticate("XOAUTH2", lambda _: sasl.encode())
        await client.logout()
        if resp.result != "OK":
            raise EmailSentinelError(f"IMAP auth probe failed: {resp.lines}")


# ------------------------------------------------------------------
# RFC parsing helpers
# ------------------------------------------------------------------

def _parse_uidvalidity(lines) -> int:
    import re
    for line in lines:
        line_s = line if isinstance(line, str) else line.decode("utf-8", errors="replace")
        m = re.search(r"\[UIDVALIDITY\s+(\d+)\]", line_s, re.IGNORECASE)
        if m:
            return int(m.group(1))
    return 0


def _has_exists(resp) -> bool:
    """Check if the IDLE response contains an EXISTS untagged response."""
    lines = getattr(resp, "lines", resp) if not isinstance(resp, (list, tuple)) else resp
    for line in lines:
        line_s = line if isinstance(line, str) else line.decode("utf-8", errors="replace")
        if "EXISTS" in line_s:
            return True
    return False


def _parse_fetch_response(lines) -> list[tuple[int, bytes]]:
    """Very lightweight parse: extract (uid, raw_message) pairs."""
    import re
    results = []
    i = 0
    while i < len(lines):
        line = lines[i] if isinstance(lines[i], str) else lines[i].decode("utf-8", errors="replace")
        m = re.search(r"\bUID\s+(\d+)\b", line, re.IGNORECASE)
        uid = int(m.group(1)) if m else 0
        # Next element might be the raw message bytes
        if i + 1 < len(lines) and isinstance(lines[i + 1], (bytes, bytearray)):
            results.append((uid, bytes(lines[i + 1])))
            i += 2
        else:
            i += 1
    return results


def _parse_email_body(raw: bytes) -> str:
    """Extract plain-text body from a raw RFC 5322 message."""
    try:
        msg = email_lib.message_from_bytes(raw, policy=email_lib.policy.default)
        if msg.is_multipart():
            for part in msg.walk():
                ct = part.get_content_type()
                if ct == "text/plain":
                    return part.get_payload(decode=True).decode("utf-8", errors="replace").strip()
        else:
            return msg.get_payload(decode=True).decode("utf-8", errors="replace").strip()
    except Exception as exc:
        logger.warning("email body parse error: %s", exc)
    return ""
