from datetime import datetime
from pathlib import Path

from agent.orchestrator.storage import (
    RAW_STORAGE_BUCKETS,
    build_raw_artifact_path,
    ensure_raw_storage_layout,
    get_raw_bucket_dir,
    get_raw_storage_root,
)


def test_ensure_raw_storage_layout_creates_all_buckets(monkeypatch, tmp_path):
    hermes_home = tmp_path / "profile"
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))

    layout = ensure_raw_storage_layout()

    assert get_raw_storage_root() == hermes_home / "raw"
    assert set(layout.keys()) == set(RAW_STORAGE_BUCKETS)
    for bucket, path in layout.items():
        assert path == hermes_home / "raw" / bucket
        assert path.is_dir()


def test_build_raw_artifact_path_uses_date_folder(monkeypatch, tmp_path):
    hermes_home = tmp_path / "profile"
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))

    path = build_raw_artifact_path(
        "pdf",
        "../statement.pdf",
        created_at=datetime(2026, 4, 12, 15, 30),
    )

    assert path == hermes_home / "raw" / "pdf" / "2026-04-12" / "statement.pdf"
    assert path.parent.is_dir()


def test_get_raw_bucket_dir_rejects_unknown_bucket(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "profile"))

    try:
        get_raw_bucket_dir("unknown")
    except ValueError as exc:
        assert "Unknown raw storage bucket" in str(exc)
    else:
        raise AssertionError("Expected ValueError for unknown bucket")
