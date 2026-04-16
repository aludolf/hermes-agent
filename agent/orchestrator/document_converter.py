"""Universal document → markdown converter (021 US2).

Wraps Microsoft's MarkItDown (MIT-licensed, PyPI package `markitdown[all]`)
to turn uploaded documents into plain-text markdown the extraction engine
can consume. Supports 20+ formats out of the box: Word, Excel, PowerPoint,
PDF, HTML, images (with OCR), audio (with STT), and so on.

Design notes:
- Lazy import of `markitdown` — the package carries many transitive deps,
  so we don't want to pay that cost or explode at orchestrator-import time
  when it's unavailable (e.g. in a thin CI container).
- Failure is non-fatal: `convert()` returns None on any error. The caller
  (gateway) should fall through to the existing document-handling path.
- Subtitle formats (`.vtt`, `.srt`) are NOT handed to MarkItDown — we strip
  timestamps ourselves since MarkItDown has no dedicated subtitle converter
  and returning the raw subtitle text with timestamps noisily defeats
  action extraction. Pure text is what Sonnet wants.
- 50MB file-size guard — MarkItDown can OOM on large PDFs; we bail early.
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

MAX_FILE_SIZE_BYTES = 50 * 1024 * 1024  # 50MB

SUPPORTED_EXTENSIONS: frozenset[str] = frozenset({
    ".docx", ".doc",
    ".xlsx", ".xls", ".xlsm",
    ".pptx", ".ppt",
    ".pdf",
    ".html", ".htm",
    ".md",
    ".txt",
    ".csv",
    ".json",
    ".xml",
    ".rtf",
    ".vtt", ".srt",  # handled via convert_vtt_srt, not MarkItDown
    ".epub",
    ".odt", ".ods", ".odp",
})

SUBTITLE_EXTENSIONS: frozenset[str] = frozenset({".vtt", ".srt"})


_VTT_TIMESTAMP_RE = re.compile(
    r"^\s*\d{1,2}:\d{2}(?::\d{2})?[.,]\d{3}\s*-->\s*"
    r"\d{1,2}:\d{2}(?::\d{2})?[.,]\d{3}.*$",
    re.MULTILINE,
)
_SRT_CUE_NUMBER_RE = re.compile(r"^\s*\d+\s*$", re.MULTILINE)
_WEBVTT_HEADER_RE = re.compile(r"^WEBVTT.*$", re.MULTILINE)


class DocumentConverter:
    """Façade over MarkItDown for Hermes's extraction pipeline."""

    def __init__(self) -> None:
        self._markitdown: Any | None = None

    def _get_markitdown(self) -> Any | None:
        if self._markitdown is not None:
            return self._markitdown
        try:
            from markitdown import MarkItDown
        except ImportError:
            logger.warning(
                "markitdown not installed — document conversion disabled"
            )
            return None
        try:
            self._markitdown = MarkItDown()
        except Exception as e:
            logger.warning("MarkItDown init failed: %s", e)
            return None
        return self._markitdown

    # ------------------------------------------------------------------
    # Capability probe
    # ------------------------------------------------------------------

    def can_convert(self, filename: str) -> bool:
        """True if this extension is in our supported set."""
        ext = Path(filename).suffix.lower()
        return ext in SUPPORTED_EXTENSIONS

    # ------------------------------------------------------------------
    # Conversion
    # ------------------------------------------------------------------

    def convert(self, file_path: str | Path) -> str | None:
        """Convert a document at `file_path` to markdown text.

        Returns the markdown body on success, None on any error (missing
        file, unsupported type, size exceeded, corrupt file, MarkItDown
        exception). Callers should treat None as "fall back to the prior
        document handler" without surfacing the error to the user.
        """
        p = Path(file_path).expanduser()
        if not p.exists() or not p.is_file():
            logger.warning("converter: file missing: %s", p)
            return None

        ext = p.suffix.lower()
        if ext not in SUPPORTED_EXTENSIONS:
            logger.debug("converter: unsupported ext: %s", ext)
            return None

        try:
            size = p.stat().st_size
        except OSError as e:
            logger.warning("converter: stat failed %s: %s", p, e)
            return None
        if size > MAX_FILE_SIZE_BYTES:
            logger.warning(
                "converter: file too large (%d bytes > %d): %s",
                size, MAX_FILE_SIZE_BYTES, p,
            )
            return None
        if size == 0:
            logger.debug("converter: empty file: %s", p)
            return None

        # Subtitles are handled locally, not via MarkItDown.
        if ext in SUBTITLE_EXTENSIONS:
            return self.convert_vtt_srt(p)

        # Plain text passthrough — no need to go through MarkItDown for
        # formats that are already the target.
        if ext in (".txt", ".md"):
            try:
                return p.read_text(encoding="utf-8", errors="replace")
            except OSError as e:
                logger.warning("converter: read failed %s: %s", p, e)
                return None

        mi = self._get_markitdown()
        if mi is None:
            return None
        try:
            result = mi.convert(str(p))
        except Exception as e:
            logger.warning("converter: MarkItDown raised on %s: %s", p, e)
            return None
        text = getattr(result, "text_content", None)
        if text is None:
            logger.warning("converter: result had no text_content: %s", p)
            return None
        return text

    def convert_vtt_srt(self, file_path: str | Path) -> str | None:
        """Strip timestamps + cue numbers from .vtt/.srt files.

        Returns plain-text body on success, None on read failure.
        """
        p = Path(file_path).expanduser()
        try:
            raw = p.read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            logger.warning("converter: subtitle read failed %s: %s", p, e)
            return None

        cleaned = _WEBVTT_HEADER_RE.sub("", raw)
        cleaned = _VTT_TIMESTAMP_RE.sub("", cleaned)
        cleaned = _SRT_CUE_NUMBER_RE.sub("", cleaned)
        # Collapse multi-blank lines and strip each line.
        lines = [line.strip() for line in cleaned.splitlines()]
        body_lines = [line for line in lines if line]
        return "\n".join(body_lines)
