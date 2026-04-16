"""Sonnet-powered extraction engine (021).

Takes a transcript (voice note, document markdown, pasted text), sends it to
Anthropic Claude Sonnet with the Portuguese extraction prompt, and returns a
structured `ExtractionResult` of `ExtractedAction`s.

Design decisions:
- Sonnet 4.6 (not Opus) — extraction is a structured-output task, not a
  reasoning-heavy one. Good latency/cost tradeoff.
- temperature=0.2 — reduce drift, since output is parsed as JSON.
- max_tokens=2000 — enough for ~30 actions with metadata.
- timeout=30s — reject slow responses; caller falls back to LLM session.
- SHA256 transcript hash — dedup key; callers skip re-processing when the
  same transcript (normalized) has already produced an extraction event.

See specs/021-hermes-intelligence-layer/contracts/extraction-contract.md for
the stable input/output contract.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from .extraction_prompts import build_extraction_system_prompt
from .models import ActionType

logger = logging.getLogger(__name__)

EXTRACTION_MODEL = "claude-sonnet-4-6"
EXTRACTION_MAX_TOKENS = 2000
EXTRACTION_TEMPERATURE = 0.2
EXTRACTION_TIMEOUT_S = 30.0

_ALLOWED_ACTION_TYPES = {a.value for a in ActionType}


@dataclass
class ExtractedAction:
    """A single extracted action (task, meeting, reminder, etc.)."""
    action_type: str
    content: str
    confidence: float
    metadata: dict[str, Any] = field(default_factory=dict)
    execution_status: str = "pending"
    handler_result: dict[str, Any] | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "action_type": self.action_type,
            "content": self.content,
            "confidence": self.confidence,
            "metadata": dict(self.metadata),
            "execution_status": self.execution_status,
            "handler_result": self.handler_result,
        }


@dataclass
class ExtractionResult:
    """Structured result from a single extraction call."""
    actions: list[ExtractedAction]
    entities_mentioned: list[str]
    summary: str
    transcript_hash: str
    raw_response: dict[str, Any] | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "actions": [a.as_dict() for a in self.actions],
            "entities_mentioned": list(self.entities_mentioned),
            "summary": self.summary,
            "transcript_hash": self.transcript_hash,
        }


def compute_transcript_hash(text: str) -> str:
    """SHA256 of the normalized transcript for dedup."""
    normalized = re.sub(r"\s+", " ", (text or "").strip()).lower()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _parse_action(raw: Any) -> ExtractedAction | None:
    if not isinstance(raw, dict):
        return None
    action_type = raw.get("action_type")
    if action_type not in _ALLOWED_ACTION_TYPES:
        logger.debug("extraction: dropping unknown action_type=%r", action_type)
        return None
    content = raw.get("content") or ""
    try:
        confidence = float(raw.get("confidence", 0.0))
    except (TypeError, ValueError):
        confidence = 0.0
    if not (0.0 <= confidence <= 1.0):
        confidence = 0.0

    metadata = raw.get("metadata")
    if not isinstance(metadata, dict):
        metadata = {}

    # Per contract: meeting/reminder MUST have date+time; missing → downgrade
    if action_type in ("meeting", "reminder"):
        if not metadata.get("date") or not metadata.get("time"):
            confidence = min(confidence, 0.65)

    # Per contract: task defaults to "Compras" list if missing
    if action_type == "task" and not metadata.get("list"):
        metadata["list"] = "Compras"

    return ExtractedAction(
        action_type=action_type,
        content=str(content),
        confidence=confidence,
        metadata=metadata,
    )


def _parse_response(raw_text: str, transcript_hash: str) -> ExtractionResult | None:
    """Parse Sonnet's response text into a structured result.

    Returns None on unrecoverable parse failure — callers should fall back to
    the existing LLM session path.
    """
    json_str = (raw_text or "").strip()
    if json_str.startswith("```"):
        # Strip markdown fences defensively even though the prompt bans them.
        json_str = json_str.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
    try:
        parsed = json.loads(json_str)
    except json.JSONDecodeError as e:
        logger.warning("extraction: JSON parse failed: %s — raw: %s", e, raw_text[:200])
        return None
    if not isinstance(parsed, dict):
        logger.warning("extraction: non-dict payload: %r", type(parsed))
        return None

    raw_actions = parsed.get("actions")
    if raw_actions is None:
        raw_actions = []
    if not isinstance(raw_actions, list):
        logger.warning("extraction: actions not a list: %r", type(raw_actions))
        raw_actions = []

    actions: list[ExtractedAction] = []
    for raw in raw_actions:
        action = _parse_action(raw)
        if action is not None:
            actions.append(action)

    entities = parsed.get("entities_mentioned") or []
    if not isinstance(entities, list):
        entities = []
    entities = [str(e) for e in entities if e]

    summary = str(parsed.get("summary") or "")

    return ExtractionResult(
        actions=actions,
        entities_mentioned=entities,
        summary=summary,
        transcript_hash=transcript_hash,
        raw_response=parsed,
    )


def extract_actions(
    transcript: str,
    *,
    active_lists: list[str] | None = None,
    current_date: date | None = None,
    api_key: str | None = None,
) -> ExtractionResult | None:
    """Extract structured actions from a transcript using Sonnet.

    Args:
        transcript: the text to analyze (already STT-transcribed or converted
            from a document).
        active_lists: names of the owner's active shared lists, injected into
            the prompt so the extractor picks the right list.
        current_date: "today" for relative-date resolution (defaults to host
            date; callers should pass an America/Sao_Paulo date).
        api_key: optional override — defaults to ANTHROPIC_API_KEY.

    Returns:
        ExtractionResult on success, None on API error or unrecoverable parse
        failure. Callers should fall back to the existing LLM session path on
        None.
    """
    if not (transcript or "").strip():
        return None

    key = (api_key or os.getenv("ANTHROPIC_API_KEY", "")).strip()
    if not key:
        logger.warning("extraction: ANTHROPIC_API_KEY not set")
        return None

    try:
        import httpx as _httpx  # lazy import for test-patchability
    except ImportError:
        logger.warning("extraction: httpx not available")
        return None

    transcript_hash = compute_transcript_hash(transcript)
    system_prompt = build_extraction_system_prompt(
        active_lists or [],
        current_date=current_date,
    )

    try:
        response = _httpx.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": EXTRACTION_MODEL,
                "max_tokens": EXTRACTION_MAX_TOKENS,
                "temperature": EXTRACTION_TEMPERATURE,
                "system": system_prompt,
                "messages": [{"role": "user", "content": transcript}],
            },
            timeout=EXTRACTION_TIMEOUT_S,
        )
        response.raise_for_status()
    except Exception as e:
        logger.warning("extraction: API call failed: %s", e)
        return None

    try:
        data = response.json()
        content_text = data.get("content", [{}])[0].get("text", "")
    except (IndexError, KeyError, ValueError) as e:
        logger.warning("extraction: response shape unexpected: %s", e)
        return None

    return _parse_response(content_text, transcript_hash)
