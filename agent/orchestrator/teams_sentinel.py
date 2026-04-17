"""Microsoft Teams real-time sentinel via MS Graph change notifications (022 US1).

TeamsSentinel manages:
- Graph subscription lifecycle (create, renew every 45 min, delete on unwatch)
- Polling fallback when webhook registration fails
- Notification dispatch: validate clientState → fetch message → enqueue extraction

Polling is done via a per-watch asyncio loop calling the Graph messages API
every 60 s, tracking the latest message timestamp as a cursor.
"""

from __future__ import annotations

import asyncio
import logging
import time

logger = logging.getLogger(__name__)

# MS Graph base
_GRAPH_BASE = "https://graph.microsoft.com/v1.0"
_SUBSCRIPTION_RENEWAL_INTERVAL = 45 * 60  # seconds
_SUBSCRIPTION_EXPIRY_SECONDS = 3600  # 1 h per Graph limits for personal accts
_POLLING_INTERVAL = 60  # seconds
_FETCH_TIMEOUT = 15  # seconds for individual Graph calls


class TeamsSentinelError(Exception):
    pass


class TeamsSentinel:
    """Manages all Teams watches for one owner.

    Designed to run as a single background service per GatewayRunner.
    Multiple watches are handled as parallel asyncio tasks.
    """

    def __init__(
        self,
        *,
        session_db,
        auth_manager,
        extraction_queue,
        owner_id: str,
    ) -> None:
        self._db = session_db
        self._auth = auth_manager
        self._queue = extraction_queue
        self._owner_id = owner_id
        self._watch_tasks: dict[str, asyncio.Task] = {}
        self._renewal_task: asyncio.Task | None = None
        self._running = False

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Load existing watches from DB and start tasks for each."""
        self._running = True
        watches = self._db.list_teams_watches(owner_id=self._owner_id)
        for w in watches:
            await self._start_watch_task(w)
        self._renewal_task = asyncio.create_task(
            self._renewal_loop(), name="teams-sentinel-renewal"
        )
        logger.info(
            "TeamsSentinel started: %d watch(es) for owner=%s",
            len(watches),
            self._owner_id,
        )

    async def stop(self) -> None:
        self._running = False
        for task in list(self._watch_tasks.values()):
            task.cancel()
        if self._renewal_task:
            self._renewal_task.cancel()
        logger.info("TeamsSentinel stopped")

    # ------------------------------------------------------------------
    # Watch management
    # ------------------------------------------------------------------

    async def watch_resource(
        self,
        resource_type: str,
        ms_resource_id: str,
        alias: str = "",
    ) -> dict:
        """Create a new watch row and start its task."""
        from uuid import uuid4
        import secrets
        watch_id = f"tw_{uuid4().hex[:12]}"
        client_state = secrets.token_hex(16)
        self._db.create_teams_watch(
            watch_id=watch_id,
            owner_id=self._owner_id,
            resource_type=resource_type,
            ms_resource_id=ms_resource_id,
            alias=alias,
            client_state=client_state,
        )
        watch = self._db.get_teams_watch(watch_id)
        await self._start_watch_task(watch)
        return watch

    async def unwatch(self, watch_id: str) -> None:
        """Delete a watch and cancel its task."""
        task = self._watch_tasks.pop(watch_id, None)
        if task:
            task.cancel()
        watch = self._db.get_teams_watch(watch_id)
        if watch and watch.get("subscription_id"):
            try:
                await self._delete_subscription(watch["subscription_id"])
            except Exception as exc:
                logger.warning("Failed to delete Graph subscription: %s", exc)
        self._db.delete_teams_watch(watch_id)

    # ------------------------------------------------------------------
    # Webhook notification processing
    # ------------------------------------------------------------------

    async def process_notification(self, payload: dict) -> None:
        """Validate + process a single notification from MS Graph."""
        resource_id = payload.get("resourceData", {}).get("id") or payload.get("resource", "")
        watch_id = payload.get("_watch_id")  # pre-resolved by webhook handler
        if not watch_id:
            logger.debug("process_notification: no watch_id, skipping")
            return
        watch = self._db.get_teams_watch(watch_id)
        if not watch:
            return
        if watch.get("mute_until") is not None:
            mute = watch["mute_until"]
            if mute == 0 or time.time() < mute:
                logger.debug("Muted watch %s — skipping notification", watch_id)
                return
        # Quiet hours: suppress during configured UTC hour range
        qh_start = watch.get("quiet_hours_start")
        qh_end = watch.get("quiet_hours_end")
        if qh_start is not None and qh_end is not None:
            import datetime as _dt
            current_hour = _dt.datetime.now(_dt.timezone.utc).hour
            if qh_start <= qh_end:
                in_quiet = qh_start <= current_hour < qh_end
            else:  # spans midnight
                in_quiet = current_hour >= qh_start or current_hour < qh_end
            if in_quiet:
                logger.debug("Quiet hours active for watch %s — skipping notification", watch_id)
                return
        message_text = await self._fetch_message_text(watch, resource_id, payload)
        if not message_text:
            return
        self._db.touch_teams_watch_event(watch_id)
        from agent.orchestrator.sentinel_queue import ExtractionItem
        item = ExtractionItem(
            transcript=message_text,
            sender_id=self._owner_id,
            sender_role="owner",
            source_type="teams_message",
            source_format="teams-message",
            chat_id=watch.get("alias") or watch_id,
        )
        self._queue.enqueue_nowait(item)

    # ------------------------------------------------------------------
    # Internal task management
    # ------------------------------------------------------------------

    async def _start_watch_task(self, watch: dict) -> None:
        watch_id = watch["watch_id"]
        if watch_id in self._watch_tasks and not self._watch_tasks[watch_id].done():
            return
        # Try to create/renew Graph subscription; fall back to polling
        try:
            sub_id, expires_at = await self._create_or_renew_subscription(watch)
            self._db.update_teams_watch_subscription(watch_id, sub_id, expires_at)
            self._db.update_teams_watch_mode(watch_id, "realtime")
            # In realtime mode no active poll loop; webhook drives delivery
            logger.info("Teams watch %s: realtime subscription %s", watch_id, sub_id)
        except Exception as exc:
            logger.warning(
                "Teams watch %s: subscription failed (%s) — falling back to polling",
                watch_id,
                exc,
            )
            self._db.update_teams_watch_mode(watch_id, "polling", polling_cursor="")
            task = asyncio.create_task(
                self._polling_loop(watch_id), name=f"teams-poll-{watch_id}"
            )
            self._watch_tasks[watch_id] = task

    async def _renewal_loop(self) -> None:
        while self._running:
            await asyncio.sleep(_SUBSCRIPTION_RENEWAL_INTERVAL)
            watches = self._db.list_teams_watches(owner_id=self._owner_id)
            for w in watches:
                if w.get("delivery_mode") != "realtime":
                    continue
                sub_id = w.get("subscription_id")
                expires_at = w.get("expires_at") or 0
                if not sub_id or time.time() + 300 < expires_at:
                    continue
                try:
                    new_expiry = await self._renew_subscription(sub_id)
                    self._db.update_teams_watch_subscription(
                        w["watch_id"], sub_id, new_expiry
                    )
                    logger.info("Renewed Graph subscription %s", sub_id)
                except Exception as exc:
                    logger.warning("Renewal failed for %s: %s", sub_id, exc)

    async def _polling_loop(self, watch_id: str) -> None:
        while self._running:
            watch = self._db.get_teams_watch(watch_id)
            if not watch:
                break
            try:
                messages = await self._poll_messages(watch)
                for msg in messages:
                    await self.process_notification(
                        {"_watch_id": watch_id, "resourceData": {"id": msg.get("id", "")}, "_msg": msg}
                    )
                if messages:
                    cursor = messages[-1].get("createdDateTime", "")
                    self._db.update_teams_watch_mode(
                        watch_id, "polling", polling_cursor=cursor
                    )
            except Exception as exc:
                logger.warning("Polling error for watch %s: %s", watch_id, exc)
            await asyncio.sleep(_POLLING_INTERVAL)

    # ------------------------------------------------------------------
    # Graph API helpers
    # ------------------------------------------------------------------

    async def _build_headers(self) -> dict:
        token = self._auth.get_access_token()
        return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    async def _create_or_renew_subscription(self, watch: dict) -> tuple[str, float]:
        import httpx
        resource_path = self._resource_path(watch)
        payload = {
            "changeType": "created",
            "notificationUrl": self._notification_url(),
            "resource": resource_path,
            "expirationDateTime": _iso_expiry(_SUBSCRIPTION_EXPIRY_SECONDS),
            "clientState": watch["client_state"],
        }
        headers = await self._build_headers()
        async with httpx.AsyncClient(timeout=_FETCH_TIMEOUT) as client:
            resp = await client.post(
                f"{_GRAPH_BASE}/subscriptions", json=payload, headers=headers
            )
        if resp.status_code not in (200, 201):
            raise TeamsSentinelError(
                f"Graph subscription failed {resp.status_code}: {resp.text[:200]}"
            )
        data = resp.json()
        expiry = _parse_expiry(data.get("expirationDateTime", ""))
        return data["id"], expiry

    async def _renew_subscription(self, sub_id: str) -> float:
        import httpx
        payload = {"expirationDateTime": _iso_expiry(_SUBSCRIPTION_EXPIRY_SECONDS)}
        headers = await self._build_headers()
        async with httpx.AsyncClient(timeout=_FETCH_TIMEOUT) as client:
            resp = await client.patch(
                f"{_GRAPH_BASE}/subscriptions/{sub_id}",
                json=payload,
                headers=headers,
            )
        if resp.status_code != 200:
            raise TeamsSentinelError(f"Renewal failed {resp.status_code}")
        return _parse_expiry(resp.json().get("expirationDateTime", ""))

    async def _delete_subscription(self, sub_id: str) -> None:
        import httpx
        headers = await self._build_headers()
        async with httpx.AsyncClient(timeout=_FETCH_TIMEOUT) as client:
            await client.delete(
                f"{_GRAPH_BASE}/subscriptions/{sub_id}", headers=headers
            )

    async def _fetch_message_text(
        self, watch: dict, resource_id: str, payload: dict
    ) -> str:
        # Use pre-fetched message if injected (polling path)
        if "_msg" in payload:
            return _extract_body(payload["_msg"])
        if not resource_id:
            return ""
        try:
            import httpx
            headers = await self._build_headers()
            url = self._message_url(watch, resource_id)
            async with httpx.AsyncClient(timeout=_FETCH_TIMEOUT) as client:
                resp = await client.get(url, headers=headers)
            if resp.status_code == 200:
                return _extract_body(resp.json())
        except Exception as exc:
            logger.warning("fetch_message_text error: %s", exc)
        return ""

    async def _poll_messages(self, watch: dict) -> list[dict]:
        import httpx
        resource_path = self._resource_path(watch)
        headers = await self._build_headers()
        cursor = watch.get("polling_cursor") or ""
        filter_param = f"&$filter=createdDateTime gt '{cursor}'" if cursor else ""
        url = f"{_GRAPH_BASE}/{resource_path}/messages?$top=50&$orderby=createdDateTime asc{filter_param}"
        async with httpx.AsyncClient(timeout=_FETCH_TIMEOUT) as client:
            resp = await client.get(url, headers=headers)
        if resp.status_code != 200:
            return []
        return resp.json().get("value", [])

    def _resource_path(self, watch: dict) -> str:
        rt = watch.get("resource_type", "chat")
        rid = watch["ms_resource_id"]
        if rt == "chat":
            return f"chats/{rid}"
        return f"teams/{rid}/channels/messages"

    def _message_url(self, watch: dict, message_id: str) -> str:
        rt = watch.get("resource_type", "chat")
        rid = watch["ms_resource_id"]
        if rt == "chat":
            return f"{_GRAPH_BASE}/chats/{rid}/messages/{message_id}"
        return f"{_GRAPH_BASE}/{rid}/messages/{message_id}"

    def _notification_url(self) -> str:
        base = os.getenv("HERMES_WEBHOOK_BASE_URL", "").rstrip("/")
        return f"{base}/webhooks/ms-graph"



# ------------------------------------------------------------------
# Module-level helpers
# ------------------------------------------------------------------

def _iso_expiry(seconds: int) -> str:
    import datetime
    expiry = datetime.datetime.utcnow() + datetime.timedelta(seconds=seconds)
    return expiry.strftime("%Y-%m-%dT%H:%M:%S.0000000Z")


def _parse_expiry(iso: str) -> float:
    import datetime
    if not iso:
        return time.time() + _SUBSCRIPTION_EXPIRY_SECONDS
    try:
        dt = datetime.datetime.strptime(iso[:19], "%Y-%m-%dT%H:%M:%S")
        return dt.replace(tzinfo=datetime.timezone.utc).timestamp()
    except ValueError:
        return time.time() + _SUBSCRIPTION_EXPIRY_SECONDS


def _extract_body(msg: dict) -> str:
    body = msg.get("body", {})
    content = body.get("content", "")
    content_type = body.get("contentType", "text")
    if content_type == "html":
        import re
        content = re.sub(r"<[^>]+>", " ", content)
    return content.strip()
