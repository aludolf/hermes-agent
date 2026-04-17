"""Async mailbox backfill runner (022 US3).

Token-bucket rate limiter + resumable per-folder UID cursor.
Backfilled items flow into the extraction queue with
suppress_action_routing=True and execution_mode_override='skipped'
so they populate KB/entity-context only, never fire calendar events.
"""

from __future__ import annotations

import asyncio
import datetime
import email as email_lib
import email.policy
import hashlib
import json
import logging
import re
import time
from typing import Any, Callable, Coroutine, Optional

logger = logging.getLogger(__name__)

_DEFAULT_RATE = 60          # messages per minute
_PROGRESS_THRESHOLDS = [0.25, 0.50, 0.75, 1.0]


class TokenBucketRateLimiter:
    """Token bucket refilling at `rate_per_minute` tokens per minute."""

    def __init__(self, rate_per_minute: float = _DEFAULT_RATE) -> None:
        self._rate = max(1.0, rate_per_minute)
        self._tokens = self._rate
        self._last_refill = time.monotonic()

    async def acquire(self) -> None:
        interval = 60.0 / self._rate
        while True:
            now = time.monotonic()
            elapsed = now - self._last_refill
            self._tokens = min(self._rate, self._tokens + elapsed * (self._rate / 60.0))
            self._last_refill = now
            if self._tokens >= 1.0:
                self._tokens -= 1.0
                return
            await asyncio.sleep(interval * 0.5)


class BackfillRunner:
    """Runs one backfill_job to completion (or until cancelled).

    Parameters
    ----------
    session_db:
        Open SessionDB instance.
    extraction_queue:
        SentinelExtractionQueue — must implement ``enqueue(item)`` coroutine.
    credentials_store:
        CredentialsStore for loading the IMAP credential secret.
    notify_fn:
        Optional async callable(msg: str) — sends progress updates to owner.
    """

    def __init__(
        self,
        *,
        session_db,
        extraction_queue,
        credentials_store,
        notify_fn: Optional[Callable[[str], Coroutine]] = None,
    ) -> None:
        self._db = session_db
        self._queue = extraction_queue
        self._store = credentials_store
        self._notify = notify_fn

    async def run(self, job_id: str) -> None:
        """Execute the backfill job identified by *job_id*."""
        job = self._db.get_backfill_job(job_id)
        if not job:
            logger.error("BackfillRunner: job %s not found", job_id)
            return

        acct = self._db.get_mail_account(job["account_id"])
        if not acct:
            self._db.fail_backfill_job(job_id, "mail_account not found")
            return

        rate = job.get("rate_limit_msgs_per_min") or _DEFAULT_RATE
        limiter = TokenBucketRateLimiter(rate)
        since_ts: Optional[float] = job.get("since_ts")

        try:
            secret = await self._load_secret(acct)
        except Exception as exc:
            self._db.fail_backfill_job(job_id, f"credential load failed: {exc}")
            return

        # Restore resumable cursor {folder: last_uid_processed}
        cursor: dict[str, int] = {}
        raw_cursor = job.get("resumption_cursor_json")
        if raw_cursor:
            try:
                cursor = json.loads(raw_cursor)
            except Exception:
                cursor = {}

        folders: list[str] = ["INBOX"]
        try:
            folders = json.loads(job.get("folders_json") or '["INBOX"]')
        except Exception:
            pass

        self._db.start_backfill_job(job_id, total_estimated=0)
        await self._notify_progress(
            f"🔄 Backfill `{job_id}` iniciado — {len(folders)} pasta(s), {int(rate)} msgs/min"
        )

        processed = 0
        failed = 0
        last_notified = -1.0

        try:
            import aioimaplib
        except ImportError:
            self._db.fail_backfill_job(job_id, "aioimaplib not installed")
            return

        try:
            client = aioimaplib.IMAP4_SSL(host=acct["host"], port=int(acct.get("port", 993)))
            await client.wait_hello_from_server()

            auth_method = acct.get("auth_method", "app_password")
            if auth_method == "app_password":
                resp = await client.login(acct["username"], secret)
            else:
                from agent.orchestrator.email_auth import xoauth2_build_sasl_string
                sasl = xoauth2_build_sasl_string(acct["username"], secret)
                resp = await client.authenticate("XOAUTH2", lambda _: sasl.encode())
            if resp.result != "OK":
                self._db.fail_backfill_job(job_id, f"AUTH failed: {resp.lines}")
                return

            for folder in folders:
                if asyncio.current_task().cancelled():
                    break

                start_uid = cursor.get(folder, 1)
                sel = await client.select(folder)
                if sel.result != "OK":
                    logger.warning("BackfillRunner: SELECT %r failed, skipping", folder)
                    continue

                uids = await self._search_uids(client, start_uid, since_ts)
                total = len(uids)
                logger.info("BackfillRunner job %s folder %r: %d UIDs", job_id, folder, total)

                for i, uid in enumerate(uids):
                    if asyncio.current_task().cancelled():
                        raise asyncio.CancelledError()

                    await limiter.acquire()
                    try:
                        fetch_resp = await client.uid("FETCH", str(uid), "(BODY.PEEK[])")
                        if fetch_resp.result != "OK":
                            failed += 1
                            continue
                        messages = _parse_fetch_response(fetch_resp.lines)
                        for _uid, raw in messages:
                            body = _parse_email_body(raw)
                            if not body:
                                continue
                            from agent.orchestrator.sentinel_queue import ExtractionItem
                            item = ExtractionItem(
                                transcript=body,
                                sender_id=acct.get("owner_id", "owner"),
                                sender_role="owner",
                                source_type="email",
                                source_format="email-backfill",
                                chat_id=job_id,
                                suppress_action_routing=True,
                                execution_mode_override="skipped",
                                extra_kwargs={
                                    "transcript_hash": hashlib.sha256(body.encode()).hexdigest(),
                                    "backfill_job_id": job_id,
                                    "folder": folder,
                                },
                            )
                            await self._queue.enqueue(item)
                        processed += 1
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        logger.warning("BackfillRunner uid %s error: %s", uid, exc)
                        failed += 1

                    # Persist resumable cursor after each message
                    cursor[folder] = uid
                    self._db.update_backfill_cursor(job_id, json.dumps(cursor))
                    self._db.update_backfill_progress(
                        job_id, processed_count=processed, failed_count=failed
                    )

                    # Milestone notifications
                    if total > 0:
                        fraction = (i + 1) / total
                        for threshold in _PROGRESS_THRESHOLDS:
                            if last_notified < threshold <= fraction:
                                pct = int(threshold * 100)
                                await self._notify_progress(
                                    f"📬 Backfill `{job_id}` — {pct}% "
                                    f"({processed} msgs, {failed} erros)"
                                )
                                last_notified = threshold
                                break

            try:
                await client.logout()
            except Exception:
                pass

            self._db.complete_backfill_job(job_id)
            await self._notify_progress(
                f"✅ Backfill `{job_id}` completo — "
                f"{processed} msgs processadas, {failed} falhas."
            )

        except asyncio.CancelledError:
            # Preserve cursor so job can be resumed
            self._db.update_backfill_cursor(job_id, json.dumps(cursor))
            self._db.update_backfill_progress(
                job_id, processed_count=processed, failed_count=failed
            )
            logger.info("BackfillRunner job %s cancelled (cursor saved)", job_id)
            raise
        except Exception as exc:
            logger.exception("BackfillRunner job %s fatal error: %s", job_id, exc)
            self._db.fail_backfill_job(job_id, str(exc))
            await self._notify_progress(f"❌ Backfill `{job_id}` falhou: {exc}")

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _search_uids(self, client, start_uid: int, since_ts: Optional[float]) -> list[int]:
        uid_range = f"{start_uid}:*" if start_uid > 1 else "1:*"
        if since_ts:
            since_date = datetime.datetime.utcfromtimestamp(since_ts).strftime("%d-%b-%Y")
            resp = await client.uid("SEARCH", f"UID {uid_range} SINCE {since_date}")
        else:
            resp = await client.uid("SEARCH", f"UID {uid_range}")
        if resp.result != "OK":
            return []
        return _parse_uid_search(resp.lines)

    async def _load_secret(self, acct: dict) -> str:
        cred_ref = acct.get("credential_ref", "")
        row = self._db.get_credential_ciphertext(cred_ref)
        if not row:
            raise RuntimeError(f"Credential {cred_ref!r} not found")
        return bytes(row["ciphertext"]).decode("utf-8")

    async def _notify_progress(self, msg: str) -> None:
        if self._notify:
            try:
                await self._notify(msg)
            except Exception as exc:
                logger.debug("BackfillRunner notify_fn failed: %s", exc)


# ---------------------------------------------------------------------------
# RFC parsing helpers
# ---------------------------------------------------------------------------

def _parse_uid_search(lines) -> list[int]:
    """Parse ``* SEARCH uid uid ...`` lines into a list of integer UIDs."""
    uids: list[int] = []
    for line in lines:
        s = line if isinstance(line, str) else line.decode("utf-8", errors="replace")
        if "SEARCH" in s:
            for token in s.split():
                if token.isdigit():
                    uids.append(int(token))
    return uids


def _parse_fetch_response(lines) -> list[tuple[int, bytes]]:
    results = []
    i = 0
    while i < len(lines):
        line = lines[i] if isinstance(lines[i], str) else lines[i].decode("utf-8", errors="replace")
        m = re.search(r"\bUID\s+(\d+)\b", line, re.IGNORECASE)
        uid = int(m.group(1)) if m else 0
        if i + 1 < len(lines) and isinstance(lines[i + 1], (bytes, bytearray)):
            results.append((uid, bytes(lines[i + 1])))
            i += 2
        else:
            i += 1
    return results


def _parse_email_body(raw: bytes) -> str:
    try:
        msg = email_lib.message_from_bytes(raw, policy=email_lib.policy.default)
        if msg.is_multipart():
            for part in msg.walk():
                if part.get_content_type() == "text/plain":
                    payload = part.get_payload(decode=True)
                    if payload:
                        return payload.decode("utf-8", errors="replace").strip()
        else:
            payload = msg.get_payload(decode=True)
            if payload:
                return payload.decode("utf-8", errors="replace").strip()
    except Exception as exc:
        logger.warning("backfill body parse error: %s", exc)
    return ""
