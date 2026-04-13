"""Approval and mutation policy helpers for orchestrator actions."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable


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
