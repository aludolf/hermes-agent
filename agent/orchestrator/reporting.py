"""Formatting helpers for orchestrator status surfaces."""

from __future__ import annotations

from typing import Mapping, Sequence


def _render_artifact_line(artifact: Mapping[str, object]) -> str:
    display_name = artifact.get("display_name") or artifact.get("storage_path") or "artifact"
    artifact_type = artifact.get("artifact_type") or "artifact"
    return f"- `{artifact.get('artifact_id', '?')}` {artifact_type}: {display_name}"


def _render_working_artifact_line(wa: Mapping[str, object]) -> str:
    title = wa.get("title") or wa.get("working_id") or "working artifact"
    kind = wa.get("artifact_kind") or "unknown"
    status = wa.get("status") or "active"
    return f"- `{wa.get('working_id', '?')}` {kind}: {title} [{status}]"


def format_job_summary(summary: Mapping[str, object]) -> str:
    """Render a single job summary in a Telegram-friendly markdown block."""
    lines = [
        "🧭 **Hermes Orchestrator Job**",
        "",
        f"**Job ID:** `{summary['job_id']}`",
        f"**Type:** `{summary['job_type']}`",
        f"**Status:** `{summary['status']}`",
        f"**Route:** `{summary['route_class']}`",
    ]

    knowledge_tier = summary.get("knowledge_tier")
    if knowledge_tier:
        lines.append(f"**Tier:** `{knowledge_tier}`")

    next_action = summary.get("next_action")
    if next_action:
        lines.append(f"**Next Action:** `{next_action}`")

    created_at = summary.get("created_at")
    if created_at:
        lines.append(f"**Created:** {created_at}")

    error_message = summary.get("error_message")
    if error_message:
        lines.append(f"**Error:** {error_message}")

    artifacts = summary.get("artifacts") or []
    if artifacts:
        lines.extend(["", "**Artifacts:**"])
        lines.extend(_render_artifact_line(artifact) for artifact in artifacts)

    working_artifacts = summary.get("working_artifacts") or []
    if working_artifacts:
        lines.extend(["", "**Working Outputs:**"])
        lines.extend(_render_working_artifact_line(wa) for wa in working_artifacts)

    approvals = summary.get("approvals") or []
    if approvals:
        pending = [item for item in approvals if item.get("status") == "pending"]
        if pending:
            lines.extend(["", f"**Approvals Pending:** {len(pending)}"])

    routing = summary.get("routing") or {}
    if routing and routing.get("decision_reason"):
        lines.extend(["", f"_Routing:_ {routing['decision_reason']}"])

    return "\n".join(lines)


def format_job_summary_list(
    summaries: Sequence[Mapping[str, object]],
    *,
    title: str = "Recent Orchestrator Jobs",
) -> str:
    """Render a compact multi-job overview."""
    if not summaries:
        return "No orchestrator jobs recorded yet."

    lines = [f"🧭 **{title}**", ""]
    for summary in summaries:
        tier = summary.get("knowledge_tier", "")
        tier_tag = f" [{tier}]" if tier else ""
        lines.append(
            f"- `{summary['job_id']}` `{summary['job_type']}` "
            f"-> `{summary['status']}` / `{summary['route_class']}`{tier_tag}"
        )
    return "\n".join(lines)
