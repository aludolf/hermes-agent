"""Hermes general orchestrator foundations.

This package contains the shared domain model and storage helpers for the
general orchestrator initiative.
"""

from .action_router import (
    RoutedAction,
    RoutedHandlers,
    format_extraction_response,
    format_preview_response,
    new_extraction_id,
    new_preview_id,
    route_actions,
)
from .briefing import BriefingBuilder
from .calendar_bridge import CalendarBridge, parse_portuguese_datetime
from .contacts import ContactManager
from .document_converter import (
    DocumentConverter,
    MAX_FILE_SIZE_BYTES as DOCUMENT_MAX_FILE_SIZE_BYTES,
    SUPPORTED_EXTENSIONS as DOCUMENT_SUPPORTED_EXTENSIONS,
)
from .extraction import (
    ExtractedAction,
    ExtractionResult,
    compute_transcript_hash,
    extract_actions,
)
from .extraction_prompts import build_extraction_system_prompt
from .lists import ListManager
from .reminders import ReminderService
from .models import (
    ActionStatus,
    ActionType,
    ApprovalStatus,
    ArtifactKind,
    ArtifactType,
    CandidateState,
    Capability,
    ConsumptionMode,
    ContactRole,
    EntityType,
    ExecutionMode,
    JobStatus,
    KnowledgeTier,
    ListStatus,
    ListType,
    MatchType,
    NormalizedArtifact,
    PreviewStatus,
    RouteClass,
    RouteDecision,
    RunStatus,
    ScenarioCategory,
    SourceKind,
    SourceRef,
    SourceType,
    StepStatus,
    StorageBackend,
    TriggerSource,
    ValidationStatus,
    WorkingArtifactStatus,
    WorkingVisibility,
)
from .policy import ActionPolicyDecision, evaluate_action_policy
from .reporting import format_job_summary, format_job_summary_list
from .router import classify_route
from .storage import (
    RAW_STORAGE_BUCKETS,
    build_raw_artifact_path,
    ensure_raw_storage_layout,
    get_raw_bucket_dir,
    get_raw_storage_root,
)
from .artifacts import LocalStorageAdapter
from .jobs import OrchestratorJobService

__all__ = [
    "ActionPolicyDecision",
    "ActionStatus",
    "ActionType",
    "ApprovalStatus",
    "ArtifactKind",
    "ArtifactType",
    "BriefingBuilder",
    "CalendarBridge",
    "CandidateState",
    "Capability",
    "ConsumptionMode",
    "ContactManager",
    "ContactRole",
    "DOCUMENT_MAX_FILE_SIZE_BYTES",
    "DOCUMENT_SUPPORTED_EXTENSIONS",
    "DocumentConverter",
    "EntityType",
    "ExecutionMode",
    "ExtractedAction",
    "ExtractionResult",
    "JobStatus",
    "KnowledgeTier",
    "ListManager",
    "ListStatus",
    "ListType",
    "LocalStorageAdapter",
    "MatchType",
    "NormalizedArtifact",
    "OrchestratorJobService",
    "PreviewStatus",
    "RAW_STORAGE_BUCKETS",
    "ReminderService",
    "ReminderStatus",
    "RoutedAction",
    "RoutedHandlers",
    "RouteClass",
    "RouteDecision",
    "RunStatus",
    "ScenarioCategory",
    "SourceKind",
    "SourceRef",
    "SourceType",
    "StepStatus",
    "StorageBackend",
    "TriggerSource",
    "ValidationStatus",
    "WorkingArtifactStatus",
    "WorkingVisibility",
    "build_extraction_system_prompt",
    "build_raw_artifact_path",
    "classify_route",
    "compute_transcript_hash",
    "ensure_raw_storage_layout",
    "evaluate_action_policy",
    "extract_actions",
    "format_extraction_response",
    "format_job_summary",
    "format_job_summary_list",
    "format_preview_response",
    "get_raw_bucket_dir",
    "get_raw_storage_root",
    "new_extraction_id",
    "new_preview_id",
    "parse_portuguese_datetime",
    "route_actions",
]
