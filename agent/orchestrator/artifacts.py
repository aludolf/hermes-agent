"""Artifact adapters and normalization helpers for orchestrator storage."""

from __future__ import annotations

import hashlib
import mimetypes
import shutil
from pathlib import Path
from typing import Any, Iterable
from uuid import uuid4

from .models import ArtifactType, NormalizedArtifact, StorageBackend
from .storage import (
    RAW_STORAGE_BUCKETS,
    build_raw_artifact_path,
    ensure_raw_storage_layout,
    get_raw_bucket_dir,
)

_REPO_EXTENSIONS = {
    ".c",
    ".cc",
    ".cpp",
    ".cs",
    ".diff",
    ".go",
    ".java",
    ".js",
    ".json",
    ".jsx",
    ".lock",
    ".md",
    ".patch",
    ".php",
    ".py",
    ".rb",
    ".rs",
    ".sh",
    ".sql",
    ".swift",
    ".toml",
    ".ts",
    ".tsx",
    ".yaml",
    ".yml",
}


def _sha256_bytes(data: bytes) -> str:
    return f"sha256:{hashlib.sha256(data).hexdigest()}"


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return f"sha256:{digest.hexdigest()}"


def _guess_mime_type(path_or_name: str, explicit_mime: str | None = None) -> str | None:
    if explicit_mime:
        return explicit_mime
    guess, _ = mimetypes.guess_type(path_or_name)
    return guess


def choose_bucket(
    target_class: str | None,
    *,
    filename: str,
    mime_type: str | None = None,
) -> str:
    """Map a file or explicit target class onto the canonical raw buckets."""
    candidate = str(target_class or "").strip().lower()
    if candidate in RAW_STORAGE_BUCKETS:
        return candidate

    suffix = Path(filename).suffix.lower()
    mime = (mime_type or "").lower()

    if candidate in {"repo", "dev_workflow"}:
        return "repos"
    if candidate in {"generated", "generated_output"}:
        return "generated"
    if candidate in {"personal_context"}:
        return "personal"
    if candidate in {"actionable_task"}:
        return "tasks"
    if candidate in {"rejected_or_noise"}:
        return "rejected"

    if mime == "application/pdf" or suffix == ".pdf":
        return "pdf"
    if mime.startswith("image/"):
        return "screenshots"
    if mime.startswith("audio/"):
        return "audio"
    if suffix in {".mp3", ".m4a", ".wav", ".ogg"}:
        return "audio"
    if suffix in _REPO_EXTENSIONS:
        return "repos"
    return "inbox"


class LocalStorageAdapter:
    """Filesystem-backed artifact adapter for local/raw Hermes storage."""

    def __init__(
        self,
        *,
        allowed_roots: Iterable[Path] | None = None,
        output_root: Path | None = None,
        allow_delete: bool = False,
    ) -> None:
        self.allowed_roots = [Path(root).expanduser().resolve() for root in (allowed_roots or [])]
        ensure_raw_storage_layout()
        self.output_root = (output_root or get_raw_bucket_dir("generated")).expanduser().resolve()
        self.allow_delete = allow_delete

    def _resolve_input_path(self, ref: str | Path) -> Path:
        resolved = Path(ref).expanduser().resolve()
        if not resolved.exists():
            raise FileNotFoundError(resolved)

        if self.allowed_roots and not any(
            resolved.is_relative_to(root) for root in self.allowed_roots
        ):
            raise PermissionError(f"Path {resolved} is outside the allowed local roots")
        return resolved

    @staticmethod
    def _dedupe_destination(path: Path) -> Path:
        if not path.exists():
            return path
        return path.with_name(f"{path.stem}-{uuid4().hex[:8]}{path.suffix}")

    def stat(self, ref: str | Path) -> dict[str, Any]:
        path = self._resolve_input_path(ref)
        return {
            "storage_backend": str(StorageBackend.LOCAL_FS),
            "storage_path": str(path),
            "display_name": path.name,
            "mime_type": _guess_mime_type(path.name),
            "size_bytes": path.stat().st_size,
            "checksum": _sha256_file(path),
            "source_scope": str(path.parent),
        }

    def ingest(
        self,
        ref: str | Path,
        target_class: str | None,
        *,
        display_name: str | None = None,
        source_scope: str | None = None,
        source_uri: str | None = None,
        artifact_type: str = ArtifactType.RAW_INPUT,
        metadata: dict[str, Any] | None = None,
    ) -> NormalizedArtifact:
        src = self._resolve_input_path(ref)
        safe_display_name = Path(display_name or src.name).name or src.name
        mime_type = _guess_mime_type(safe_display_name)
        bucket = choose_bucket(target_class, filename=safe_display_name, mime_type=mime_type)
        dest = self._dedupe_destination(build_raw_artifact_path(bucket, safe_display_name))
        shutil.copy2(src, dest)

        provenance = {"source_path": str(src)}
        if source_uri:
            provenance["source_uri"] = source_uri

        return NormalizedArtifact(
            artifact_type=str(artifact_type),
            storage_backend=str(StorageBackend.LOCAL_FS),
            storage_path=str(dest),
            display_name=safe_display_name,
            mime_type=mime_type,
            size_bytes=dest.stat().st_size,
            checksum=_sha256_file(dest),
            source_scope=source_scope or str(src.parent),
            provenance=provenance,
            metadata=dict(metadata or {}),
        )

    def ingest_bytes(
        self,
        filename: str,
        data: bytes,
        target_class: str | None,
        *,
        mime_type: str | None = None,
        source_scope: str | None = None,
        source_uri: str | None = None,
        artifact_type: str = ArtifactType.RAW_INPUT,
        metadata: dict[str, Any] | None = None,
    ) -> NormalizedArtifact:
        guessed_mime = _guess_mime_type(filename, explicit_mime=mime_type)
        bucket = choose_bucket(target_class, filename=filename, mime_type=guessed_mime)
        dest = self._dedupe_destination(build_raw_artifact_path(bucket, filename))
        dest.write_bytes(data)

        provenance = {}
        if source_uri:
            provenance["source_uri"] = source_uri

        return NormalizedArtifact(
            artifact_type=str(artifact_type),
            storage_backend=str(StorageBackend.LOCAL_FS),
            storage_path=str(dest),
            display_name=Path(filename).name,
            mime_type=guessed_mime,
            size_bytes=len(data),
            checksum=_sha256_bytes(data),
            source_scope=source_scope,
            provenance=provenance,
            metadata=dict(metadata or {}),
        )

    def open(self, ref: str | Path) -> Path:
        return self._resolve_input_path(ref)

    def write(self, output_ref: str | Path, artifact: NormalizedArtifact) -> dict[str, Any]:
        destination = Path(output_ref).expanduser().resolve()
        if not destination.is_relative_to(self.output_root):
            raise PermissionError(
                f"Output path {destination} is outside the configured output root {self.output_root}"
            )

        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(Path(artifact.storage_path), destination)
        return self.stat(destination)

    def list(self, scope: str | Path, cursor: int | None = None) -> list[dict[str, Any]]:
        directory = self._resolve_input_path(scope)
        if not directory.is_dir():
            raise NotADirectoryError(directory)

        start = max(int(cursor or 0), 0)
        entries = sorted(directory.iterdir(), key=lambda item: item.name)
        return [self.stat(entry) for entry in entries[start:] if entry.is_file()]

    def delete(self, ref: str | Path) -> None:
        if not self.allow_delete:
            raise PermissionError("LocalStorageAdapter.delete is disabled unless explicitly allowed")
        path = self._resolve_input_path(ref)
        path.unlink()
