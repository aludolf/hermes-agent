"""Canonical domain model enums and dataclasses for the Hermes orchestrator."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any


class StrEnum(str, Enum):
    """`str` + `Enum` helper with stable stringification."""

    def __str__(self) -> str:
        return str(self.value)


class RouteClass(StrEnum):
    PENDING_ROUTE = "pending_route"
    KB_CANDIDATE = "kb_candidate"
    RAW_ARCHIVE = "raw_archive"
    PERSONAL_CONTEXT = "personal_context"
    DEV_WORKFLOW = "dev_workflow"
    ACTIONABLE_TASK = "actionable_task"
    GENERATED_OUTPUT = "generated_output"
    REJECTED_OR_NOISE = "rejected_or_noise"


class JobStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    BLOCKED_FOR_APPROVAL = "blocked_for_approval"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ActionStatus(StrEnum):
    PENDING = "pending"
    AWAITING_APPROVAL = "awaiting_approval"
    EXECUTING = "executing"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ApprovalStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    DENIED = "denied"
    EXPIRED = "expired"
    CANCELLED = "cancelled"


class SourceType(StrEnum):
    TELEGRAM = "telegram"
    LOCAL_FS = "local_fs"
    GOOGLE_DRIVE = "google_drive"
    REPO = "repo"
    WEB = "web"
    CRON = "cron"
    GENERATED = "generated"


class StorageBackend(StrEnum):
    LOCAL_FS = "local_fs"
    GOOGLE_DRIVE = "google_drive"
    REPO = "repo"
    KB_REPO = "kb_repo"
    GENERATED = "generated"
    TELEGRAM = "telegram"


class ArtifactType(StrEnum):
    RAW_INPUT = "raw_input"
    PARSED_TEXT = "parsed_text"
    OCR_TEXT = "ocr_text"
    GENERATED_REPORT = "generated_report"
    SLIDE_SOURCE = "slide_source"
    KB_MANIFEST = "kb_manifest"
    DELIVERED_MESSAGE = "delivered_message"


# ---------------------------------------------------------------------------
# 002: Knowledge Layer Split enums
# ---------------------------------------------------------------------------

class KnowledgeTier(StrEnum):
    """Which knowledge layer an item belongs to."""
    RAW_ONLY = "raw_only"
    WORKING = "working"
    CANONICAL_CANDIDATE = "canonical_candidate"


class CandidateState(StrEnum):
    """Lifecycle state of a canonical candidate."""
    PENDING_REVIEW = "pending_review"
    APPROVED_FOR_PUBLISH = "approved_for_publish"
    REJECTED = "rejected"
    PUBLISHED = "published"
    EXPIRED = "expired"


class ConsumptionMode(StrEnum):
    """How a bot or workflow may consume knowledge layers."""
    CANONICAL_ONLY = "canonical_only"
    CANONICAL_FIRST = "canonical_first"
    WORKING_ONLY = "working_only"
    WORKING_PLUS_CANONICAL = "working_plus_canonical"


class ArtifactKind(StrEnum):
    """Kind of durable working artifact."""
    SUMMARY = "summary"
    REPORT = "report"
    MEETING_PACK = "meeting_pack"
    REPO_INSPECTION = "repo_inspection"
    DRAFT_NOTE = "draft_note"
    TASK_BRIEF = "task_brief"
    OTHER = "other"


class WorkingArtifactStatus(StrEnum):
    """Lifecycle state of a working artifact."""
    ACTIVE = "active"
    SUPERSEDED = "superseded"
    ARCHIVED = "archived"


class ValidationStatus(StrEnum):
    """Validation state for canonical publications."""
    PENDING = "pending"
    PASSED = "passed"
    FAILED = "failed"


class WorkingVisibility(StrEnum):
    """What working content a consumer can see."""
    NONE = "none"
    OWN_OUTPUTS = "own_outputs"
    SHARED_OUTPUTS = "shared_outputs"


@dataclass(frozen=True)
class SourceRef:
    """Normalized origin metadata for an orchestrator job."""

    source_type: str
    source_uri: str | None = None
    source_scope: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "source_type": str(self.source_type),
            "source_uri": self.source_uri,
            "source_scope": self.source_scope,
        }


@dataclass(frozen=True)
class NormalizedArtifact:
    """Shared artifact contract used between adapters, jobs, and reporting."""

    artifact_type: str
    storage_backend: str
    storage_path: str
    display_name: str
    mime_type: str | None = None
    size_bytes: int | None = None
    checksum: str | None = None
    source_scope: str | None = None
    provenance: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def path_name(self) -> str:
        return Path(self.storage_path).name

    def as_record(self) -> dict[str, Any]:
        record_metadata = dict(self.metadata)
        record_metadata.setdefault("display_name", self.display_name)

        record_provenance = dict(self.provenance)
        if self.source_scope and "source_scope" not in record_provenance:
            record_provenance["source_scope"] = self.source_scope

        return {
            "artifact_type": str(self.artifact_type),
            "storage_backend": str(self.storage_backend),
            "storage_path": self.storage_path,
            "mime_type": self.mime_type,
            "size_bytes": self.size_bytes,
            "checksum": self.checksum,
            "metadata_json": record_metadata or None,
            "provenance_json": record_provenance or None,
        }

    def as_summary(self, artifact_id: str | None = None) -> dict[str, Any]:
        summary = {
            "artifact_type": str(self.artifact_type),
            "storage_backend": str(self.storage_backend),
            "storage_path": self.storage_path,
            "display_name": self.display_name,
            "mime_type": self.mime_type,
            "size_bytes": self.size_bytes,
            "checksum": self.checksum,
            "source_scope": self.source_scope,
        }
        if artifact_id:
            summary["artifact_id"] = artifact_id
        return summary


@dataclass(frozen=True)
class RouteDecision:
    """Structured route-classification result."""

    route_class: str
    decision_reason: str
    confidence: float
    knowledge_tier: str = "raw_only"
    manual_override: bool = False
    overridden_from: str | None = None
    overridden_from_tier: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "route_class": str(self.route_class),
            "knowledge_tier": str(self.knowledge_tier),
            "decision_reason": self.decision_reason,
            "confidence": self.confidence,
            "manual_override": self.manual_override,
            "overridden_from": self.overridden_from,
            "overridden_from_tier": self.overridden_from_tier,
        }
