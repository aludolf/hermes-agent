"""Tests for orchestrator action policy decisions."""

from agent.orchestrator.policy import evaluate_action_policy


def test_read_only_actions_skip_approval():
    decision = evaluate_action_policy("github", is_mutating=False)

    assert decision.allowed is True
    assert decision.requires_approval is False


def test_mutating_action_requires_approval_without_allowlist():
    decision = evaluate_action_policy("google_drive", is_mutating=True)

    assert decision.allowed is False
    assert decision.requires_approval is True
    assert decision.policy_name == "google_drive_write_requires_approval"


def test_allowlisted_mutation_can_proceed():
    decision = evaluate_action_policy(
        "telegram",
        is_mutating=True,
        auto_allowed_targets={"telegram"},
    )

    assert decision.allowed is True
    assert decision.requires_approval is False
