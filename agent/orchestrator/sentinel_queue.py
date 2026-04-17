"""Shared extraction queue consumed by Teams and Email sentinels (022).

SentinelExtractionQueue is a single asyncio.Queue(100) worker that
serializes calls to perform_extraction so the AI rate-limiter isn't
flooded when many messages arrive simultaneously.

Usage:
    queue = SentinelExtractionQueue(session_db=db, ...)
    await queue.start()          # launches background consumer
    await queue.enqueue(item)
    await queue.stop()
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Coroutine

logger = logging.getLogger(__name__)

_STOP_SENTINEL = object()


@dataclass
class ExtractionItem:
    """One unit of work for the sentinel extraction consumer."""

    transcript: str
    sender_id: str
    sender_role: str
    source_type: str
    source_format: str = "text"
    chat_id: str = ""
    sender_capabilities: set = field(default_factory=lambda: {"all"})
    suppress_action_routing: bool = False
    execution_mode_override: str | None = None
    extra_kwargs: dict = field(default_factory=dict)


class SentinelExtractionQueue:
    """Bounded async queue with a single background consumer.

    The consumer calls `extraction_fn(item)` for each ExtractionItem and
    then calls `summary_fn(outcome, item)` to dispatch Telegram summaries.
    """

    def __init__(
        self,
        *,
        session_db,
        extraction_fn: Callable[[ExtractionItem], Coroutine],
        summary_fn: Callable[[Any, ExtractionItem], Coroutine] | None = None,
        maxsize: int = 100,
    ) -> None:
        self._db = session_db
        self._extraction_fn = extraction_fn
        self._summary_fn = summary_fn
        self._queue: asyncio.Queue = asyncio.Queue(maxsize=maxsize)
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        """Launch the background consumer task."""
        if self._task and not self._task.done():
            return
        self._task = asyncio.create_task(self._consume(), name="sentinel-extraction-consumer")
        logger.info("SentinelExtractionQueue consumer started")

    async def stop(self) -> None:
        """Drain the queue and shut down the consumer."""
        await self._queue.put(_STOP_SENTINEL)
        if self._task:
            try:
                await asyncio.wait_for(self._task, timeout=30)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                self._task.cancel()

    async def enqueue(self, item: ExtractionItem) -> None:
        """Put an extraction item on the queue (blocks if full)."""
        await self._queue.put(item)

    def enqueue_nowait(self, item: ExtractionItem) -> None:
        """Non-blocking enqueue; drops item with a warning if queue is full."""
        try:
            self._queue.put_nowait(item)
        except asyncio.QueueFull:
            logger.warning(
                "SentinelExtractionQueue full — dropping item source_type=%s",
                item.source_type,
            )

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    async def _consume(self) -> None:
        while True:
            item = await self._queue.get()
            if item is _STOP_SENTINEL:
                self._queue.task_done()
                break
            try:
                outcome = await self._extraction_fn(item)
                if self._summary_fn and outcome is not None:
                    try:
                        await self._summary_fn(outcome, item)
                    except Exception as exc:  # pragma: no cover
                        logger.warning("summary_fn error: %s", exc)
            except Exception as exc:
                logger.exception(
                    "Sentinel extraction error for source_type=%s: %s",
                    getattr(item, "source_type", "?"),
                    exc,
                )
            finally:
                self._queue.task_done()
        logger.info("SentinelExtractionQueue consumer stopped")
