"""Bot-to-bot harness runner (021 US5).

Executes a `HarnessScenario` end-to-end against a target Telegram bot:
for each step, send the scripted message into the target chat, poll
`bot_peer_inbox.jsonl` for the target's response, match it against the
step's expected pattern, and record per-step pass/fail/timeout.

Writes an atomic JSONL line per completed run to
`harness_results.jsonl` (canonical path configurable via
HERMES_HARNESS_RESULTS_PATH). The HD Engine dashboard reads this file
via the stable contract in
specs/021-hermes-intelligence-layer/contracts/harness-results-contract.md.

Design:
- Runner takes a `send_message` callable — the gateway supplies its
  Telegram adapter send; tests supply a mock. No coupling to adapters.
- `inbox_reader` callable returns the current list of inbox records
  (newest last). Tests supply an in-memory list; prod reads JSONL.
- Run queue: per-scenario single-run lock. A second trigger for an
  already-running scenario waits briefly, then reports queued=True.
- Results go through `complete_harness_run` on SessionDB (for the DB
  cache + queries) AND `harness_results.jsonl` (the HD contract).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable

from hermes_state import SessionDB

from .regression_detector import detect_regressions

logger = logging.getLogger(__name__)

DEFAULT_STEP_TIMEOUT_MS = 15_000
MAX_STEP_TIMEOUT_MS = 120_000
INBOX_POLL_INTERVAL_S = 0.5
RECEIVED_TEXT_CAP = 1_000


SendMessage = Callable[[str, str], Awaitable[Any]]
"""async (chat_id, text) -> any — sends one step to the target bot."""

InboxReader = Callable[[], list[dict[str, Any]]]
"""() -> list of bot-peer inbox records, newest last."""


@dataclass
class StepResult:
    step: int
    sent: str
    expected_pattern: str
    match_type: str
    received: str
    status: str  # pass | fail | timeout | skipped | error
    duration_ms: int
    gate: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "step": self.step,
            "sent": self.sent,
            "expected_pattern": self.expected_pattern,
            "match_type": self.match_type,
            "received": self.received[:RECEIVED_TEXT_CAP],
            "status": self.status,
            "duration_ms": self.duration_ms,
            "gate": self.gate,
        }


@dataclass
class HarnessRun:
    run_id: str
    scenario_id: str
    scenario_name: str
    scenario_category: str
    triggered_by: str
    trigger_source: str
    target_bot: str
    target_chat_id: str
    started_at: float
    finished_at: float | None = None
    duration_ms: int | None = None
    status: str = "running"  # pass | fail | timeout | error
    steps: list[StepResult] = field(default_factory=list)
    regression_flags: list[int] = field(default_factory=list)
    last_passing_run_id: str | None = None
    error_message: str | None = None

    def as_contract_dict(self) -> dict[str, Any]:
        """Serialize in the exact harness-results-contract.md shape."""
        return {
            "run_id": self.run_id,
            "scenario_id": self.scenario_id,
            "scenario_name": self.scenario_name,
            "scenario_category": self.scenario_category,
            "triggered_by": self.triggered_by,
            "trigger_source": self.trigger_source,
            "target_bot": self.target_bot,
            "target_chat_id": self.target_chat_id,
            "started_at": _iso8601(self.started_at),
            "finished_at": _iso8601(self.finished_at) if self.finished_at else None,
            "duration_ms": self.duration_ms,
            "status": self.status,
            "steps": [s.as_dict() for s in self.steps],
            "regression_flags": list(self.regression_flags),
            "last_passing_run_id": self.last_passing_run_id,
        }


def _iso8601(ts: float) -> str:
    from datetime import datetime, timezone
    return (
        datetime.fromtimestamp(ts, tz=timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


# ---------------------------------------------------------------------------
# Step match
# ---------------------------------------------------------------------------

def step_matches(
    received: str, expected_pattern: str, match_type: str = "substring",
) -> bool:
    if received is None:
        return False
    if match_type == "exact":
        return received.strip() == expected_pattern.strip()
    if match_type == "regex":
        try:
            return bool(re.search(expected_pattern, received, re.IGNORECASE | re.DOTALL))
        except re.error:
            return False
    # default: substring, case-insensitive
    return expected_pattern.lower() in received.lower()


# ---------------------------------------------------------------------------
# Inbox IO
# ---------------------------------------------------------------------------

def default_inbox_path() -> Path:
    home = Path(os.getenv("HERMES_HOME", Path.home() / ".hermes"))
    return home / "bot_peer_inbox.jsonl"


def read_inbox_jsonl(path: Path | None = None) -> list[dict[str, Any]]:
    p = path or default_inbox_path()
    if not p.exists():
        return []
    rows: list[dict[str, Any]] = []
    try:
        with p.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except OSError as e:
        logger.warning("harness: inbox read failed: %s", e)
    return rows


def default_results_path() -> Path:
    explicit = os.getenv("HERMES_HARNESS_RESULTS_PATH", "").strip()
    if explicit:
        return Path(explicit)
    home = Path(os.getenv("HERMES_HOME", Path.home() / ".hermes"))
    return home / "harness" / "results" / "harness_results.jsonl"


def append_result_jsonl(run: HarnessRun, path: Path | None = None) -> Path:
    """Atomic append of one run record to the harness_results.jsonl file."""
    p = path or default_results_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(run.as_contract_dict(), ensure_ascii=False) + "\n"
    with p.open("a", encoding="utf-8") as f:
        f.write(line)
    return p


# ---------------------------------------------------------------------------
# Scenario execution
# ---------------------------------------------------------------------------

def _scenario_step_timeout(step: dict[str, Any], default_ms: int) -> int:
    raw = step.get("timeout_ms")
    if raw is None:
        return default_ms
    try:
        return max(1, min(MAX_STEP_TIMEOUT_MS, int(raw)))
    except (TypeError, ValueError):
        return default_ms


class HarnessRunner:
    """Executes a loaded scenario and records results."""

    def __init__(
        self,
        db: SessionDB,
        *,
        send_message: SendMessage,
        inbox_reader: InboxReader | None = None,
        results_path: Path | None = None,
    ) -> None:
        self.db = db
        self.send_message = send_message
        self.inbox_reader = inbox_reader or (lambda: read_inbox_jsonl())
        self.results_path = results_path
        # Per-scenario single-run lock (FR-023/FR-027).
        self._scenario_locks: dict[str, asyncio.Lock] = {}

    def _lock_for(self, scenario_id: str) -> asyncio.Lock:
        lock = self._scenario_locks.get(scenario_id)
        if lock is None:
            lock = asyncio.Lock()
            self._scenario_locks[scenario_id] = lock
        return lock

    async def run_scenario(
        self,
        scenario: dict[str, Any],
        *,
        triggered_by: str,
        trigger_source: str = "slash_command",
    ) -> HarnessRun:
        """Execute the scenario; blocks for the lock if another run is active."""
        scenario_id = str(scenario.get("scenario_id") or scenario.get("id") or "")
        async with self._lock_for(scenario_id):
            return await self._execute(
                scenario,
                triggered_by=triggered_by,
                trigger_source=trigger_source,
            )

    async def _execute(
        self,
        scenario: dict[str, Any],
        *,
        triggered_by: str,
        trigger_source: str,
    ) -> HarnessRun:
        scenario_id = str(scenario.get("scenario_id") or scenario.get("id") or "")
        scenario_name = str(scenario.get("name") or scenario_id)
        scenario_category = str(scenario.get("category") or "smoke")
        target_bot = str(scenario.get("target_bot") or "")
        target_chat_id = str(scenario.get("target_chat_id") or "")
        steps = scenario.get("steps") or scenario.get("steps_json")
        if isinstance(steps, str):
            try:
                steps = json.loads(steps)
            except json.JSONDecodeError:
                steps = []
        if not isinstance(steps, list):
            steps = []
        default_step_timeout_ms = int(
            scenario.get("timeout_per_step_ms") or DEFAULT_STEP_TIMEOUT_MS
        )

        started_at = time.time()
        run_id = _build_run_id(scenario_id, started_at)

        # Baseline for regression detection. The DB stores steps as a JSON
        # string; detect_regressions wants a parsed list under `steps`.
        baseline = self.db.get_last_passing_run(scenario_id)
        if baseline:
            raw_steps = baseline.get("steps_json")
            if isinstance(raw_steps, str):
                try:
                    baseline["steps"] = json.loads(raw_steps)
                except json.JSONDecodeError:
                    baseline["steps"] = []
        last_passing_run_id = baseline["run_id"] if baseline else None

        self.db.create_harness_run(
            run_id=run_id,
            scenario_id=scenario_id,
            triggered_by=triggered_by,
            trigger_source=trigger_source,
            last_passing_run_id=last_passing_run_id,
        )

        run = HarnessRun(
            run_id=run_id,
            scenario_id=scenario_id,
            scenario_name=scenario_name,
            scenario_category=scenario_category,
            triggered_by=triggered_by,
            trigger_source=trigger_source,
            target_bot=target_bot,
            target_chat_id=target_chat_id,
            started_at=started_at,
            last_passing_run_id=last_passing_run_id,
        )

        gate_failed = False
        for step in steps:
            if not isinstance(step, dict):
                continue
            step_num = int(step.get("step") or len(run.steps) + 1)
            sent = str(step.get("sent") or "")
            expected = str(step.get("expected_pattern") or "")
            match_type = str(step.get("match_type") or "substring")
            gate = bool(step.get("gate", False))
            timeout_ms = _scenario_step_timeout(step, default_step_timeout_ms)

            if gate_failed:
                run.steps.append(StepResult(
                    step=step_num, sent=sent, expected_pattern=expected,
                    match_type=match_type, received="", status="skipped",
                    duration_ms=0, gate=gate,
                ))
                continue

            step_result = await self._execute_step(
                step_num=step_num,
                sent=sent,
                expected_pattern=expected,
                match_type=match_type,
                gate=gate,
                timeout_ms=timeout_ms,
                target_chat_id=target_chat_id,
                target_bot=target_bot,
            )
            run.steps.append(step_result)
            if gate and step_result.status != "pass":
                gate_failed = True

        run.finished_at = time.time()
        run.duration_ms = int((run.finished_at - run.started_at) * 1000)

        run.status = _aggregate_status(run.steps, gate_failed=gate_failed)
        run.regression_flags = detect_regressions(
            run.as_contract_dict(), baseline,
        )

        # Persist to DB cache + HD contract JSONL.
        try:
            self.db.complete_harness_run(
                run_id,
                status=run.status,
                steps_json=json.dumps([s.as_dict() for s in run.steps]),
                regression_flags_json=json.dumps(run.regression_flags),
            )
        except Exception as e:
            logger.warning("harness: DB complete failed: %s", e)
        try:
            append_result_jsonl(run, self.results_path)
        except Exception as e:
            logger.warning("harness: JSONL append failed: %s", e)

        return run

    async def _execute_step(
        self,
        *,
        step_num: int,
        sent: str,
        expected_pattern: str,
        match_type: str,
        gate: bool,
        timeout_ms: int,
        target_chat_id: str,
        target_bot: str,
    ) -> StepResult:
        t0 = time.time()
        # Snapshot the inbox length before we send so we only consider
        # messages that arrive afterward.
        pre_send_len = len(self.inbox_reader())

        try:
            await self.send_message(target_chat_id, sent)
        except Exception as e:
            logger.warning("harness: send_message raised: %s", e)
            return StepResult(
                step=step_num, sent=sent, expected_pattern=expected_pattern,
                match_type=match_type, received="", status="error",
                duration_ms=int((time.time() - t0) * 1000), gate=gate,
            )

        deadline = t0 + (timeout_ms / 1000.0)
        matched: dict[str, Any] | None = None
        last_candidate: dict[str, Any] | None = None

        while time.time() < deadline:
            inbox = self.inbox_reader()
            for rec in inbox[pre_send_len:]:
                sender = str(rec.get("user_name") or rec.get("user_id") or "")
                if target_bot and target_bot not in sender and str(rec.get("user_id") or "") != target_bot:
                    continue
                text = str(rec.get("text") or "")
                last_candidate = rec
                if step_matches(text, expected_pattern, match_type):
                    matched = rec
                    break
            if matched is not None:
                break
            await asyncio.sleep(INBOX_POLL_INTERVAL_S)

        duration_ms = int((time.time() - t0) * 1000)

        if matched is not None:
            return StepResult(
                step=step_num, sent=sent, expected_pattern=expected_pattern,
                match_type=match_type, received=str(matched.get("text") or ""),
                status="pass", duration_ms=duration_ms, gate=gate,
            )

        if last_candidate is None:
            return StepResult(
                step=step_num, sent=sent, expected_pattern=expected_pattern,
                match_type=match_type, received="", status="timeout",
                duration_ms=duration_ms, gate=gate,
            )

        return StepResult(
            step=step_num, sent=sent, expected_pattern=expected_pattern,
            match_type=match_type,
            received=str(last_candidate.get("text") or ""),
            status="fail", duration_ms=duration_ms, gate=gate,
        )


def _build_run_id(scenario_id: str, started_at: float) -> str:
    from datetime import datetime, timezone
    dt = datetime.fromtimestamp(started_at, tz=timezone.utc)
    # Millisecond-precision stamp so back-to-back runs of the same scenario
    # (serialized through the run queue within the same second) don't collide.
    stamp = dt.strftime("%Y-%m-%dT%H-%M-%S") + f"-{dt.microsecond // 1000:03d}Z"
    return f"run_{stamp}_{scenario_id}"


def _aggregate_status(
    steps: list[StepResult], *, gate_failed: bool,
) -> str:
    if not steps:
        return "error"
    if gate_failed:
        return "fail"
    statuses = {s.status for s in steps if s.status != "skipped"}
    if "timeout" in statuses:
        return "timeout"
    if "error" in statuses:
        return "error"
    if "fail" in statuses:
        return "fail"
    return "pass"
