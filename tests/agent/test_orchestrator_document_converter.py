"""Tests for the DocumentConverter (021 US2)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from agent.orchestrator.document_converter import (
    MAX_FILE_SIZE_BYTES,
    SUBTITLE_EXTENSIONS,
    SUPPORTED_EXTENSIONS,
    DocumentConverter,
)


# ---------------------------------------------------------------------------
# can_convert
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("filename", [
    "memo.docx", "report.pdf", "deck.pptx", "notes.md",
    "transcript.vtt", "movie.srt", "book.epub", "data.csv",
    "invoice.xlsx", "plain.txt", "web.html", "config.json",
])
def test_can_convert_accepts_supported_extensions(filename):
    assert DocumentConverter().can_convert(filename) is True


@pytest.mark.parametrize("filename", [
    "photo.jpg", "audio.ogg", "binary.exe", "archive.zip",
    "stylesheet.css", "video.mp4",
])
def test_can_convert_rejects_unsupported_extensions(filename):
    assert DocumentConverter().can_convert(filename) is False


def test_can_convert_is_case_insensitive():
    assert DocumentConverter().can_convert("REPORT.DOCX") is True
    assert DocumentConverter().can_convert("Deck.PPTX") is True


# ---------------------------------------------------------------------------
# convert — guards
# ---------------------------------------------------------------------------

def test_convert_returns_none_for_missing_file(tmp_path):
    missing = tmp_path / "nope.docx"
    assert DocumentConverter().convert(missing) is None


def test_convert_returns_none_for_unsupported_extension(tmp_path):
    p = tmp_path / "file.exe"
    p.write_bytes(b"binary")
    assert DocumentConverter().convert(p) is None


def test_convert_returns_none_for_empty_file(tmp_path):
    p = tmp_path / "empty.txt"
    p.write_text("", encoding="utf-8")
    assert DocumentConverter().convert(p) is None


def test_convert_returns_none_when_file_too_large(tmp_path, monkeypatch):
    # Patch the limit down so we don't actually create a 50MB file.
    p = tmp_path / "big.txt"
    p.write_bytes(b"a" * 1024)
    monkeypatch.setattr(
        "agent.orchestrator.document_converter.MAX_FILE_SIZE_BYTES", 512,
    )
    # Reload class to pick up the patched constant? The constant is read
    # inside convert(), so monkeypatching the module attribute is enough
    # when the reference is via the module — but convert() uses the
    # imported symbol directly, so we need to patch via the class path.
    from agent.orchestrator import document_converter as dc_mod
    dc_mod.MAX_FILE_SIZE_BYTES = 512
    try:
        assert DocumentConverter().convert(p) is None
    finally:
        dc_mod.MAX_FILE_SIZE_BYTES = MAX_FILE_SIZE_BYTES


# ---------------------------------------------------------------------------
# convert — plain-text passthrough
# ---------------------------------------------------------------------------

def test_convert_passes_through_txt_without_markitdown(tmp_path):
    p = tmp_path / "notes.txt"
    body = "Comprar café\nLigar pro João\n"
    p.write_text(body, encoding="utf-8")

    conv = DocumentConverter()
    # Sentinel: should never touch markitdown for plain text.
    conv._markitdown = object()  # would raise if called
    assert conv.convert(p) == body


def test_convert_passes_through_md_without_markitdown(tmp_path):
    p = tmp_path / "note.md"
    p.write_text("# Title\n- item a\n- item b\n", encoding="utf-8")
    conv = DocumentConverter()
    conv._markitdown = object()
    assert "- item a" in conv.convert(p)


# ---------------------------------------------------------------------------
# convert — MarkItDown error paths (mocked)
# ---------------------------------------------------------------------------

def test_convert_returns_none_when_markitdown_raises(tmp_path):
    p = tmp_path / "doc.docx"
    p.write_bytes(b"\x50\x4b\x03\x04" + b"x" * 200)  # pretend zip header
    conv = DocumentConverter()
    fake = MagicMock()
    fake.convert.side_effect = RuntimeError("corrupt file")
    conv._markitdown = fake
    assert conv.convert(p) is None
    fake.convert.assert_called_once()


def test_convert_returns_none_when_markitdown_result_has_no_text(tmp_path):
    p = tmp_path / "doc.docx"
    p.write_bytes(b"\x50\x4b\x03\x04" + b"x" * 200)
    conv = DocumentConverter()
    fake = MagicMock()
    fake.convert.return_value = MagicMock(text_content=None)
    conv._markitdown = fake
    assert conv.convert(p) is None


def test_convert_returns_text_on_success(tmp_path):
    p = tmp_path / "doc.docx"
    p.write_bytes(b"\x50\x4b\x03\x04" + b"x" * 200)
    conv = DocumentConverter()
    fake = MagicMock()
    fake.convert.return_value = MagicMock(
        text_content="# Action Items\n- call Joao\n- buy flowers",
    )
    conv._markitdown = fake
    out = conv.convert(p)
    assert out is not None
    assert "call Joao" in out


# ---------------------------------------------------------------------------
# convert_vtt_srt
# ---------------------------------------------------------------------------

def test_convert_vtt_strips_timestamps_and_header(tmp_path):
    vtt = (
        "WEBVTT\n"
        "\n"
        "00:00:01.000 --> 00:00:03.500\n"
        "Olá pessoal\n"
        "\n"
        "00:00:03.600 --> 00:00:05.200\n"
        "vamos começar a reunião\n"
    )
    p = tmp_path / "meet.vtt"
    p.write_text(vtt, encoding="utf-8")
    out = DocumentConverter().convert(p)
    assert out is not None
    assert "WEBVTT" not in out
    assert "-->" not in out
    assert "Olá pessoal" in out
    assert "vamos começar a reunião" in out


def test_convert_srt_strips_cue_numbers(tmp_path):
    srt = (
        "1\n"
        "00:00:01,000 --> 00:00:03,500\n"
        "primeira linha\n"
        "\n"
        "2\n"
        "00:00:03,600 --> 00:00:05,200\n"
        "segunda linha\n"
    )
    p = tmp_path / "movie.srt"
    p.write_text(srt, encoding="utf-8")
    out = DocumentConverter().convert(p)
    assert out is not None
    assert "1" not in out.split("\n")
    assert "-->" not in out
    assert "primeira linha" in out
    assert "segunda linha" in out


# ---------------------------------------------------------------------------
# Lazy markitdown import
# ---------------------------------------------------------------------------

def test_get_markitdown_returns_none_when_unavailable(tmp_path, monkeypatch):
    """Simulate markitdown not being installed — convert() should return None."""
    import sys
    monkeypatch.setitem(sys.modules, "markitdown", None)
    conv = DocumentConverter()
    # Force re-import path
    conv._markitdown = None
    p = tmp_path / "doc.docx"
    p.write_bytes(b"\x50\x4b\x03\x04" + b"x" * 200)
    assert conv.convert(p) is None


# ---------------------------------------------------------------------------
# Constants sanity
# ---------------------------------------------------------------------------

def test_subtitle_extensions_are_subset_of_supported():
    assert SUBTITLE_EXTENSIONS <= SUPPORTED_EXTENSIONS


def test_size_limit_is_50mb():
    assert MAX_FILE_SIZE_BYTES == 50 * 1024 * 1024
