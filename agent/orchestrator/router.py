"""Route classification heuristics for orchestrator jobs."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping, Sequence

from .models import KnowledgeTier, RouteClass, RouteDecision

_DEV_JOB_HINTS = {"inspect_repo", "repo_inspect", "repo_summary", "spec_repo", "code_review"}
_KB_JOB_HINTS = {"kb_publish", "publish_kb", "kb_sync"}
_PERSONAL_JOB_HINTS = {"meeting_pack", "briefing", "trip_brief", "daily_plan"}
_TASK_JOB_HINTS = {"todo_extract", "action_items", "task_list", "follow_up"}

# Deterministic mapping from route class to knowledge tier.
_ROUTE_TO_TIER: dict[str, str] = {
    RouteClass.KB_CANDIDATE: KnowledgeTier.CANONICAL_CANDIDATE,
    RouteClass.RAW_ARCHIVE: KnowledgeTier.RAW_ONLY,
    RouteClass.REJECTED_OR_NOISE: KnowledgeTier.RAW_ONLY,
    RouteClass.PENDING_ROUTE: KnowledgeTier.RAW_ONLY,
    RouteClass.DEV_WORKFLOW: KnowledgeTier.WORKING,
    RouteClass.PERSONAL_CONTEXT: KnowledgeTier.WORKING,
    RouteClass.ACTIONABLE_TASK: KnowledgeTier.WORKING,
    RouteClass.GENERATED_OUTPUT: KnowledgeTier.WORKING,
}


def tier_for_route(route_class: str) -> str:
    """Return the knowledge tier for a given route class."""
    return _ROUTE_TO_TIER.get(str(route_class), KnowledgeTier.RAW_ONLY)


def _artifact_name(artifact: Mapping[str, Any]) -> str:
    metadata = artifact.get("metadata_json") or artifact.get("metadata") or {}
    display_name = metadata.get("display_name")
    if display_name:
        return str(display_name).lower()
    return Path(str(artifact.get("storage_path", ""))).name.lower()


def classify_route(
    job: Mapping[str, Any],
    artifacts: Sequence[Mapping[str, Any]],
) -> RouteDecision:
    """Return the canonical route decision for a job and its known artifacts."""
    metadata = job.get("metadata_json") or {}
    preferred_route = metadata.get("preferred_route_class")
    if preferred_route:
        return RouteDecision(
            route_class=str(preferred_route),
            decision_reason="Route supplied by workflow metadata override.",
            confidence=0.99,
            knowledge_tier=tier_for_route(str(preferred_route)),
        )

    job_type = str(job.get("job_type") or "").lower()
    source_type = str(job.get("source_type") or "").lower()
    intent = str(job.get("intent") or "").lower()
    names = [_artifact_name(artifact) for artifact in artifacts]

    if job_type in _DEV_JOB_HINTS or source_type == "repo" or intent in _DEV_JOB_HINTS:
        return RouteDecision(
            route_class=RouteClass.DEV_WORKFLOW,
            decision_reason="Repo-oriented input mapped to developer workflow routing.",
            confidence=0.95,
            knowledge_tier=KnowledgeTier.WORKING,
        )

    if job_type in _KB_JOB_HINTS or intent in _KB_JOB_HINTS:
        return RouteDecision(
            route_class=RouteClass.KB_CANDIDATE,
            decision_reason="Workflow intent targets knowledge-base publication.",
            confidence=0.93,
            knowledge_tier=KnowledgeTier.CANONICAL_CANDIDATE,
        )

    if job_type in _PERSONAL_JOB_HINTS or intent in _PERSONAL_JOB_HINTS:
        return RouteDecision(
            route_class=RouteClass.PERSONAL_CONTEXT,
            decision_reason="Meeting or planning workflow mapped to personal context.",
            confidence=0.9,
            knowledge_tier=KnowledgeTier.WORKING,
        )

    if job_type in _TASK_JOB_HINTS or intent in _TASK_JOB_HINTS:
        return RouteDecision(
            route_class=RouteClass.ACTIONABLE_TASK,
            decision_reason="Task-extraction workflow mapped to actionable tasks.",
            confidence=0.91,
            knowledge_tier=KnowledgeTier.WORKING,
        )

    if any(
        token in name
        for name in names
        for token in ("todo", "action", "follow-up", "followup", "checklist", "task")
    ):
        return RouteDecision(
            route_class=RouteClass.ACTIONABLE_TASK,
            decision_reason="Artifact naming suggests task-oriented follow-up content.",
            confidence=0.82,
            knowledge_tier=KnowledgeTier.WORKING,
        )

    if any(name.endswith(ext) for name in names for ext in (".png", ".jpg", ".jpeg", ".webp", ".pdf")):
        return RouteDecision(
            route_class=RouteClass.RAW_ARCHIVE,
            decision_reason="Binary evidence input should be preserved in the raw archive first.",
            confidence=0.8,
            knowledge_tier=KnowledgeTier.RAW_ONLY,
        )

    if source_type == "generated" or job_type.startswith("deliver_"):
        return RouteDecision(
            route_class=RouteClass.GENERATED_OUTPUT,
            decision_reason="Generated output should remain tracked as a deliverable artifact.",
            confidence=0.88,
            knowledge_tier=KnowledgeTier.WORKING,
        )

    return RouteDecision(
        route_class=RouteClass.RAW_ARCHIVE,
        decision_reason="No stronger workflow signal detected; preserve in raw archive by default.",
        confidence=0.7,
        knowledge_tier=KnowledgeTier.RAW_ONLY,
    )
