"""MS Graph webhook handler — pure function, no framework coupling (022 US1).

handle_ms_graph_webhook(body_bytes, query_params, session_db, teams_sentinel)
  -> (http_status: int, body: bytes, content_type: str)

Handles two Graph scenarios:
1. Validation handshake: GET/POST with ?validationToken=... → echo token, 200
2. Notification delivery: POST with JSON body → validate clientState, process, 202
"""

from __future__ import annotations

import json
import logging

logger = logging.getLogger(__name__)


async def handle_ms_graph_webhook(
    body_bytes: bytes,
    query_params: dict,
    *,
    session_db,
    teams_sentinel=None,
) -> tuple[int, bytes, str]:
    """Entry point for POST /webhooks/ms-graph.

    Returns (status_code, body_bytes, content_type).
    """
    # --- Validation handshake -------------------------------------------
    validation_token = query_params.get("validationToken")
    if validation_token:
        return (200, validation_token.encode("utf-8"), "text/plain")

    # --- Notification delivery ------------------------------------------
    if not body_bytes:
        return (400, b'{"error":"empty body"}', "application/json")

    try:
        payload = json.loads(body_bytes)
    except (json.JSONDecodeError, ValueError):
        return (400, b'{"error":"invalid JSON"}', "application/json")

    notifications = payload.get("value", [])
    if not notifications:
        return (202, b'{"accepted":true}', "application/json")

    rejected = 0
    for notification in notifications:
        status = await _process_one(notification, session_db=session_db, teams_sentinel=teams_sentinel)
        if not status:
            rejected += 1

    if rejected == len(notifications):
        return (400, b'{"error":"all notifications rejected"}', "application/json")

    return (202, b'{"accepted":true}', "application/json")


async def _process_one(notification: dict, *, session_db, teams_sentinel) -> bool:
    """Validate clientState and enqueue the notification.  Returns True on success."""
    client_state = notification.get("clientState", "")
    if not client_state:
        logger.warning("MS Graph notification missing clientState — rejected")
        return False

    # Resolve which watch this notification belongs to
    watch = session_db.find_teams_watch_by_client_state(client_state)
    if watch is None:
        logger.warning("MS Graph notification clientState mismatch — rejected")
        return False

    notification["_watch_id"] = watch["watch_id"]

    if teams_sentinel is not None:
        try:
            await teams_sentinel.process_notification(notification)
        except Exception as exc:
            logger.exception("teams_sentinel.process_notification error: %s", exc)
            return False

    return True
