"""Tests for the Sonnet-powered extraction engine (021 US1)."""

from __future__ import annotations

import json
from datetime import date
from unittest.mock import MagicMock, patch

import pytest

from agent.orchestrator.extraction import (
    EXTRACTION_MODEL,
    ExtractedAction,
    ExtractionResult,
    _parse_action,
    _parse_response,
    compute_transcript_hash,
    extract_actions,
)
from agent.orchestrator.extraction_prompts import (
    build_extraction_system_prompt,
)


# ---------------------------------------------------------------------------
# Prompt builder
# ---------------------------------------------------------------------------

def test_prompt_includes_active_lists_and_dates():
    prompt = build_extraction_system_prompt(
        ["Compras", "Reparos"],
        current_date=date(2026, 4, 16),
    )
    assert "Compras, Reparos" in prompt
    assert "2026-04-16" in prompt
    assert "2026-04-17" in prompt  # tomorrow


def test_prompt_defaults_lists_when_empty():
    prompt = build_extraction_system_prompt([], current_date=date(2026, 4, 16))
    assert "Compras" in prompt


# ---------------------------------------------------------------------------
# Parse helpers
# ---------------------------------------------------------------------------

def test_parse_action_valid_task_defaults_to_compras():
    a = _parse_action({
        "action_type": "task",
        "content": "café",
        "confidence": 0.9,
        "metadata": {},
    })
    assert a is not None
    assert a.metadata["list"] == "Compras"
    assert a.confidence == 0.9


def test_parse_action_drops_unknown_type():
    assert _parse_action({"action_type": "bogus", "content": "x", "confidence": 0.9}) is None


def test_parse_action_clamps_bad_confidence():
    a = _parse_action({
        "action_type": "task",
        "content": "x",
        "confidence": "not-a-number",
    })
    assert a is not None
    assert a.confidence == 0.0  # invalid → 0.0 (skipped downstream)


def test_parse_action_meeting_missing_time_downgrades_confidence():
    a = _parse_action({
        "action_type": "meeting",
        "content": "Reunião",
        "confidence": 0.95,
        "metadata": {"date": "2026-04-20"},  # no time
    })
    assert a is not None
    assert a.confidence <= 0.65  # downgraded below clarify threshold


def test_parse_response_handles_valid_json():
    raw = json.dumps({
        "actions": [
            {"action_type": "task", "content": "café", "confidence": 0.9, "metadata": {}},
            {"action_type": "reminder", "content": "ligar", "confidence": 0.85,
             "metadata": {"title": "ligar", "date": "2026-04-18", "time": "15:00"}},
        ],
        "entities_mentioned": ["João"],
        "summary": "2 ações",
    })
    result = _parse_response(raw, "h" * 64)
    assert result is not None
    assert len(result.actions) == 2
    assert result.entities_mentioned == ["João"]
    assert result.summary == "2 ações"


def test_parse_response_strips_markdown_fences():
    raw = "```json\n" + json.dumps({"actions": [], "summary": ""}) + "\n```"
    result = _parse_response(raw, "h" * 64)
    assert result is not None
    assert result.actions == []


def test_parse_response_returns_none_on_malformed_json():
    assert _parse_response("not json at all", "h" * 64) is None


def test_parse_response_tolerates_missing_fields():
    raw = json.dumps({"actions": []})  # no entities_mentioned, no summary
    result = _parse_response(raw, "h" * 64)
    assert result is not None
    assert result.entities_mentioned == []
    assert result.summary == ""


def test_parse_response_ignores_non_list_actions():
    raw = json.dumps({"actions": "not a list"})
    result = _parse_response(raw, "h" * 64)
    assert result is not None
    assert result.actions == []


# ---------------------------------------------------------------------------
# Transcript hash dedup
# ---------------------------------------------------------------------------

def test_transcript_hash_stable_across_whitespace_and_case():
    h1 = compute_transcript_hash("Comprar café amanhã às 10h")
    h2 = compute_transcript_hash("  COMPRAR CAFÉ amanhã às 10h  ")
    h3 = compute_transcript_hash("comprar\n\tcafé amanhã  às 10h")
    assert h1 == h2 == h3


def test_transcript_hash_differs_for_different_content():
    h1 = compute_transcript_hash("comprar café")
    h2 = compute_transcript_hash("comprar chá")
    assert h1 != h2


# ---------------------------------------------------------------------------
# extract_actions end-to-end (mocked httpx)
# ---------------------------------------------------------------------------

def _fake_api_response(json_str: str) -> MagicMock:
    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    resp.json.return_value = {"content": [{"text": json_str}]}
    return resp


def test_extract_actions_happy_path(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    fake_json = json.dumps({
        "actions": [
            {"action_type": "task", "content": "café", "confidence": 0.9,
             "metadata": {"list": "Compras"}},
        ],
        "entities_mentioned": [],
        "summary": "1 ação extraída",
    })
    fake_httpx = MagicMock()
    fake_httpx.post.return_value = _fake_api_response(fake_json)
    with patch.dict("sys.modules", {"httpx": fake_httpx}):
        result = extract_actions(
            "Comprar café amanhã",
            active_lists=["Compras"],
            current_date=date(2026, 4, 16),
        )
    assert result is not None
    assert len(result.actions) == 1
    assert result.actions[0].content == "café"
    # Verify model + required params were sent
    call_kwargs = fake_httpx.post.call_args.kwargs
    assert call_kwargs["json"]["model"] == EXTRACTION_MODEL
    assert call_kwargs["json"]["temperature"] == 0.2
    assert "Compras" in call_kwargs["json"]["system"]


def test_extract_actions_returns_none_when_api_errors(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    fake_httpx = MagicMock()
    fake_httpx.post.side_effect = RuntimeError("network down")
    with patch.dict("sys.modules", {"httpx": fake_httpx}):
        result = extract_actions("anything", active_lists=["Compras"])
    assert result is None


def test_extract_actions_returns_none_on_malformed_response(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    fake_httpx = MagicMock()
    fake_httpx.post.return_value = _fake_api_response("garbage")
    with patch.dict("sys.modules", {"httpx": fake_httpx}):
        result = extract_actions("anything", active_lists=["Compras"])
    assert result is None


def test_extract_actions_returns_none_when_api_key_missing(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    result = extract_actions("anything", active_lists=["Compras"])
    assert result is None


def test_extract_actions_returns_none_on_empty_input(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    assert extract_actions("", active_lists=["Compras"]) is None
    assert extract_actions("   ", active_lists=["Compras"]) is None
