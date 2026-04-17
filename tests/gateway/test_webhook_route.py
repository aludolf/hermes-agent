"""Tests for the /webhooks/ms-graph aiohttp route (022 Phase 7)."""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch


def _make_adapter():
    from gateway.platforms.webhook import WebhookAdapter
    adapter = WebhookAdapter.__new__(WebhookAdapter)
    adapter._routes = {}
    adapter._rate_counts = {}
    adapter._rate_limit = 30
    adapter._max_body_bytes = 1_048_576
    adapter._seen_deliveries = {}
    adapter._idempotency_ttl = 3600
    adapter._delivery_info = {}
    adapter._delivery_info_created = {}
    adapter.gateway_runner = None
    adapter._reload_dynamic_routes = lambda: None
    return adapter


def test_ms_graph_route_not_in_generic_routes():
    """ms-graph is handled before the generic _routes lookup."""
    adapter = _make_adapter()
    assert "ms-graph" not in adapter._routes
    assert hasattr(adapter, "_handle_ms_graph_webhook")


def test_handle_ms_graph_webhook_validation():
    adapter = _make_adapter()
    mock_request = MagicMock()
    mock_request.read = AsyncMock(return_value=b"")
    mock_request.rel_url.query = {"validationToken": "abc123"}

    async def run():
        with patch(
            "agent.orchestrator.teams_webhook.handle_ms_graph_webhook",
            new_callable=AsyncMock,
            return_value=(200, b"abc123", "text/plain"),
        ):
            return await adapter._handle_ms_graph_webhook(mock_request)

    response = asyncio.run(run())
    assert response.status == 200


def test_handle_ms_graph_webhook_notification():
    adapter = _make_adapter()
    adapter.gateway_runner = MagicMock()
    adapter.gateway_runner._session_db = MagicMock()
    adapter.gateway_runner._teams_sentinel = None

    body = json.dumps({"value": [{"clientState": "cs_x"}]}).encode()
    mock_request = MagicMock()
    mock_request.read = AsyncMock(return_value=body)
    mock_request.rel_url.query = {}

    async def run():
        with patch(
            "agent.orchestrator.teams_webhook.handle_ms_graph_webhook",
            new_callable=AsyncMock,
            return_value=(202, b"", "application/json"),
        ):
            return await adapter._handle_ms_graph_webhook(mock_request)

    response = asyncio.run(run())
    assert response.status == 202


def test_handle_ms_graph_webhook_exception_returns_500():
    adapter = _make_adapter()
    mock_request = MagicMock()
    mock_request.read = AsyncMock(return_value=b"bad")
    mock_request.rel_url.query = {}

    async def run():
        with patch(
            "agent.orchestrator.teams_webhook.handle_ms_graph_webhook",
            new_callable=AsyncMock,
            side_effect=RuntimeError("boom"),
        ):
            return await adapter._handle_ms_graph_webhook(mock_request)

    response = asyncio.run(run())
    assert response.status == 500
