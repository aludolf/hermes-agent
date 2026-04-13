"""Tests for orchestrator artifact adapters."""

from pathlib import Path

import pytest

from agent.orchestrator.artifacts import LocalStorageAdapter


def test_local_storage_adapter_ingests_file_into_pdf_bucket(tmp_path):
    source_dir = tmp_path / "source"
    source_dir.mkdir()
    source_file = source_dir / "statement.pdf"
    source_file.write_bytes(b"%PDF-1.4 fake pdf")

    adapter = LocalStorageAdapter(allowed_roots=[source_dir])
    artifact = adapter.ingest(source_file, target_class=None)

    assert Path(artifact.storage_path).exists()
    assert "/raw/pdf/" in artifact.storage_path.replace("\\", "/")
    assert artifact.display_name == "statement.pdf"
    assert artifact.checksum.startswith("sha256:")


def test_local_storage_adapter_ingests_bytes_into_screenshot_bucket():
    adapter = LocalStorageAdapter()
    artifact = adapter.ingest_bytes(
        filename="capture.png",
        data=b"png-bytes",
        target_class=None,
        mime_type="image/png",
        source_uri="telegram://message/42",
    )

    assert Path(artifact.storage_path).exists()
    assert "/raw/screenshots/" in artifact.storage_path.replace("\\", "/")
    assert artifact.provenance["source_uri"] == "telegram://message/42"


def test_local_storage_adapter_delete_disabled_by_default(tmp_path):
    source_dir = tmp_path / "source"
    source_dir.mkdir()
    source_file = source_dir / "note.txt"
    source_file.write_text("hello", encoding="utf-8")

    adapter = LocalStorageAdapter(allowed_roots=[source_dir])
    with pytest.raises(PermissionError):
        adapter.delete(source_file)
