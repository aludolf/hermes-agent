"""Filesystem helpers for Hermes orchestrator raw storage."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Dict

from hermes_constants import get_hermes_home

RAW_STORAGE_BUCKETS = (
    "inbox",
    "web",
    "pdf",
    "screenshots",
    "ocr",
    "audio",
    "transcripts",
    "repos",
    "personal",
    "tasks",
    "generated",
    "rejected",
)


def get_raw_storage_root() -> Path:
    """Return the orchestrator raw root under the active Hermes profile."""
    return get_hermes_home() / "raw"


def get_raw_bucket_dir(bucket: str) -> Path:
    """Return the bucket directory, rejecting unknown bucket names."""
    bucket_name = str(bucket or "").strip().lower()
    if bucket_name not in RAW_STORAGE_BUCKETS:
        allowed = ", ".join(RAW_STORAGE_BUCKETS)
        raise ValueError(f"Unknown raw storage bucket '{bucket}'. Expected one of: {allowed}")
    return get_raw_storage_root() / bucket_name


def ensure_raw_storage_layout() -> Dict[str, Path]:
    """Create the canonical raw storage tree and return bucket paths."""
    raw_root = get_raw_storage_root()
    raw_root.mkdir(parents=True, exist_ok=True)

    created: Dict[str, Path] = {}
    for bucket in RAW_STORAGE_BUCKETS:
        path = raw_root / bucket
        path.mkdir(parents=True, exist_ok=True)
        created[bucket] = path
    return created


def build_raw_artifact_path(
    bucket: str,
    filename: str,
    *,
    created_at: datetime | None = None,
) -> Path:
    """Return a dated file path under a raw storage bucket.

    The path is not created automatically; callers can materialize it after they
    have the file contents ready.
    """
    created = created_at or datetime.utcnow()
    safe_name = Path(filename).name or "artifact.bin"
    bucket_dir = get_raw_bucket_dir(bucket)
    dated_dir = bucket_dir / created.strftime("%Y-%m-%d")
    dated_dir.mkdir(parents=True, exist_ok=True)
    return dated_dir / safe_name
