"""Approval and mutation policy helpers for orchestrator actions."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable
from uuid import uuid4


@dataclass(frozen=True)
class ActionPolicyDecision:
    """Normalized answer to whether an action may proceed immediately."""

    allowed: bool
    requires_approval: bool
    policy_name: str | None
    reason: str


def evaluate_action_policy(
    target_system: str,
    *,
    is_mutating: bool,
    auto_allowed_targets: Iterable[str] | None = None,
) -> ActionPolicyDecision:
    """Decide whether an action may execute immediately or needs approval."""
    target = str(target_system or "").strip().lower()
    allowed_targets = {str(item).strip().lower() for item in (auto_allowed_targets or [])}

    if not is_mutating:
        return ActionPolicyDecision(
            allowed=True,
            requires_approval=False,
            policy_name=None,
            reason="Read-only actions may proceed without approval.",
        )

    if target in allowed_targets:
        return ActionPolicyDecision(
            allowed=True,
            requires_approval=False,
            policy_name=f"{target}_mutations_allowed",
            reason="Target system is explicitly allowed for direct mutation.",
        )

    policy_name = f"{target or 'external'}_write_requires_approval"
    return ActionPolicyDecision(
        allowed=False,
        requires_approval=True,
        policy_name=policy_name,
        reason="Mutating external actions must be approved before execution.",
    )


# =========================================================================
# Knowledge Layer: Consumption Policies (US4)
# =========================================================================

# Default policies for well-known consumers
_DEFAULT_POLICIES: dict[str, dict[str, Any]] = {
    "rauru-hd-bot": {
        "mode": "canonical_only",
        "fallback_allowed": False,
        "working_visibility": "none",
    },
    "hermes-general-assistant": {
        "mode": "canonical_first",
        "fallback_allowed": True,
        "working_visibility": "shared_outputs",
    },
    "repo-briefing-workflow": {
        "mode": "working_plus_canonical",
        "fallback_allowed": True,
        "working_visibility": "shared_outputs",
    },
}


def resolve_consumption_policy(db: Any, *, consumer_name: str) -> dict[str, Any]:
    """Resolve the consumption policy for a named consumer.

    Checks the DB first; falls back to built-in defaults.
    Returns a dict with mode, fallback_allowed, working_visibility.
    """
    stored = db.get_consumption_policy(consumer_name)
    if stored:
        return {
            "consumer_name": stored["consumer_name"],
            "mode": stored["mode"],
            "fallback_allowed": stored["fallback_allowed"],
            "working_visibility": stored["working_visibility"],
        }

    default = _DEFAULT_POLICIES.get(consumer_name)
    if default:
        # Persist the default so it becomes auditable
        policy_id = f"pol_{uuid4().hex[:12]}"
        db.create_consumption_policy(
            policy_id=policy_id,
            consumer_name=consumer_name,
            mode=default["mode"],
            fallback_allowed=default["fallback_allowed"],
            working_visibility=default["working_visibility"],
        )
        return {"consumer_name": consumer_name, **default}

    # Unknown consumer — default to canonical_first
    return {
        "consumer_name": consumer_name,
        "mode": "canonical_first",
        "fallback_allowed": True,
        "working_visibility": "none",
    }


@dataclass(frozen=True)
class LayerQueryResult:
    """Result of a layer-aware knowledge query."""

    items: list[dict[str, Any]]
    source_layer: str
    consumer_name: str
    fallback_used: bool = False


def query_knowledge(
    db: Any,
    *,
    consumer_name: str,
    query_scope: str | None = None,
    limit: int = 20,
) -> LayerQueryResult:
    """Query knowledge respecting the consumer's layer policy.

    canonical_only: only returns published canonical entries.
    canonical_first: canonical entries preferred, fallback to working if allowed.
    working_only: only working artifacts.
    working_plus_canonical: both layers, labeled by source.
    """
    policy = resolve_consumption_policy(db, consumer_name=consumer_name)
    mode = policy["mode"]
    fallback = policy["fallback_allowed"]

    canonical = db.list_canonical_candidates(status="published", limit=limit)
    working = db.list_working_artifacts(status="active", limit=limit)

    if mode == "canonical_only":
        return LayerQueryResult(
            items=[_label(c, "canonical") for c in canonical],
            source_layer="canonical",
            consumer_name=consumer_name,
        )

    if mode == "working_only":
        return LayerQueryResult(
            items=[_label(w, "working") for w in working],
            source_layer="working",
            consumer_name=consumer_name,
        )

    if mode == "canonical_first":
        if canonical:
            return LayerQueryResult(
                items=[_label(c, "canonical") for c in canonical],
                source_layer="canonical",
                consumer_name=consumer_name,
            )
        if fallback and working:
            return LayerQueryResult(
                items=[_label(w, "working") for w in working],
                source_layer="working",
                consumer_name=consumer_name,
                fallback_used=True,
            )
        return LayerQueryResult(
            items=[], source_layer="canonical",
            consumer_name=consumer_name,
        )

    # working_plus_canonical
    combined = [_label(c, "canonical") for c in canonical]
    combined.extend(_label(w, "working") for w in working)
    return LayerQueryResult(
        items=combined,
        source_layer="mixed",
        consumer_name=consumer_name,
    )


def _label(item: dict[str, Any], layer: str) -> dict[str, Any]:
    """Tag an item with its source layer for consumer clarity."""
    return {**item, "_source_layer": layer}
