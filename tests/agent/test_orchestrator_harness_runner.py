"""Tests for the bot-to-bot harness runner (021 US5)."""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest

from agent.orchestrator.harness_runner import (
    DEFAULT_STEP_TIMEOUT_MS,
    HarnessRun,
    HarnessRunner,
    StepResult,
    append_result_jsonl,
    step_matches,
)
from hermes_state import SessionDB


# ---------------------------------------------------------------------------
# step_matches — pattern semantics
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("received,pattern,mt,expected", [
    ("Welcome to Rauru — HD bot", "Welcome to Rauru", "substring", True),
    ("welcome to rauru", "Welcome to Rauru", "substring", True),  # case-insensitive
    ("Bye", "Welcome", "substring", False),
    ("date of birth please", r"(date of birth|hour)", "regex", True),
    ("hour of birth?", r"(date of birth|hour)", "regex", True),
    ("OK",                        "OK", "exact", True),
    ("ok",                        "OK", "exact", False),
    ("short", "(", "regex", False),  # bad regex → False
])
def test_step_matches(received, pattern, mt, expected):
    assert step_matches(received, pattern, mt) is expected


# ---------------------------------------------------------------------------
# Run a scenario end-to-end with fake inbox + fake send
# ---------------------------------------------------------------------------

def _scenario(**overrides) -> dict[str, Any]:
    base = {
        "scenario_id": "rauru_smoke",
        "name": "Rauru smoke",
        "category": "smoke",
        "target_bot": "Rauru_HD_bot",
        "target_chat_id": "-1003992792803",
        "timeout_per_step_ms": 5000,
        "steps": [
            {"step": 1, "sent": "/start", "expected_pattern": "Welcome",
             "match_type": "substring", "gate": True},
            {"step": 2, "sent": "English", "expected_pattern": "date of birth",
             "match_type": "substring"},
        ],
    }
    base.update(overrides)
    return base


class _FakeInbox:
    """Mutable inbox the test can append to between polls."""
    def __init__(self) -> None:
        self.records: list[dict[str, Any]] = []

    def __call__(self) -> list[dict[str, Any]]:
        # Return a snapshot (so the runner's slice doesn't mutate later).
        return list(self.records)

    def add_reply(self, text: str, user_name: str = "Rauru_HD_bot") -> None:
        self.records.append({
            "ts": time.time(),
            "user_name": user_name,
            "user_id": "rauru_bot_id",
            "text": text,
        })


def _run_async(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def test_happy_path_all_steps_pass(tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    inbox = _FakeInbox()
    sent_messages: list[tuple[str, str]] = []

    async def send(chat_id: str, text: str):
        sent_messages.append((chat_id, text))
        # Synthesize the expected reply immediately so polling finds it.
        if text == "/start":
            inbox.add_reply("Welcome to Rauru!")
        elif text == "English":
            inbox.add_reply("Please send your date of birth")

    runner = HarnessRunner(
        db, send_message=send, inbox_reader=inbox,
        results_path=tmp_path / "harness_results.jsonl",
    )
    try:
        run = _run_async(runner.run_scenario(
            _scenario(), triggered_by="hermes:owner:1", trigger_source="slash_command",
        ))
        assert run.status == "pass"
        assert len(run.steps) == 2
        assert all(s.status == "pass" for s in run.steps)
        assert sent_messages == [
            ("-1003992792803", "/start"),
            ("-1003992792803", "English"),
        ]

        # JSONL written in contract shape
        out = (tmp_path / "harness_results.jsonl").read_text("utf-8").strip()
        record = json.loads(out)
        assert record["scenario_id"] == "rauru_smoke"
        assert record["status"] == "pass"
        assert record["steps"][0]["status"] == "pass"
        assert "run_id" in record

        # DB row exists
        runs = db.list_harness_runs_by_scenario("rauru_smoke")
        assert len(runs) == 1
        assert runs[0]["status"] == "pass"
    finally:
        db.close()


def test_gate_failure_short_circuits(tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    inbox = _FakeInbox()

    async def send(chat_id: str, text: str):
        if text == "/start":
            inbox.add_reply("something wrong")  # won't match "Welcome"

    runner = HarnessRunner(
        db, send_message=send, inbox_reader=inbox,
        results_path=tmp_path / "harness_results.jsonl",
    )
    scen = _scenario()
    scen["steps"][0]["timeout_ms"] = 200  # fail fast for this test
    try:
        run = _run_async(runner.run_scenario(
            scen, triggered_by="t", trigger_source="slash_command",
        ))
        assert run.status == "fail"
        assert run.steps[0].status == "fail"
        assert run.steps[1].status == "skipped"
    finally:
        db.close()


def test_step_timeout_when_no_reply(tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    inbox = _FakeInbox()

    async def send(chat_id: str, text: str):
        pass  # never responds

    runner = HarnessRunner(
        db, send_message=send, inbox_reader=inbox,
        results_path=tmp_path / "harness_results.jsonl",
    )
    scen = {
        "scenario_id": "s1", "name": "t", "category": "smoke",
        "target_bot": "Rauru_HD_bot", "target_chat_id": "c1",
        "steps": [{"step": 1, "sent": "ping", "expected_pattern": "pong",
                   "timeout_ms": 300}],
    }
    try:
        run = _run_async(runner.run_scenario(
            scen, triggered_by="t", trigger_source="slash_command",
        ))
        assert run.status == "timeout"
        assert run.steps[0].status == "timeout"
        assert run.steps[0].duration_ms >= 250
    finally:
        db.close()


def test_send_message_exception_marks_step_error(tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    inbox = _FakeInbox()

    async def send(chat_id: str, text: str):
        raise RuntimeError("telegram down")

    runner = HarnessRunner(
        db, send_message=send, inbox_reader=inbox,
        results_path=tmp_path / "harness_results.jsonl",
    )
    try:
        run = _run_async(runner.run_scenario(
            {"scenario_id": "s", "name": "t", "category": "smoke",
             "target_bot": "Rauru_HD_bot", "target_chat_id": "c",
             "steps": [{"step": 1, "sent": "hi", "expected_pattern": "hi",
                        "timeout_ms": 200}]},
            triggered_by="t", trigger_source="slash_command",
        ))
        assert run.steps[0].status == "error"
        assert run.status == "error"
    finally:
        db.close()


def test_regression_flags_set_against_baseline(tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    # Pre-seed a passing baseline.
    baseline_id = "run_baseline_s1"
    db.create_harness_run(
        run_id=baseline_id, scenario_id="s1",
        triggered_by="t", trigger_source="slash_command",
    )
    db.complete_harness_run(
        baseline_id, status="pass",
        steps_json=json.dumps([
            {"step": 1, "status": "pass"},
            {"step": 2, "status": "pass"},
        ]),
    )

    inbox = _FakeInbox()

    async def send(chat_id: str, text: str):
        if text == "good":
            inbox.add_reply("ok")
        # step 2 gets no response → will regress

    runner = HarnessRunner(
        db, send_message=send, inbox_reader=inbox,
        results_path=tmp_path / "harness_results.jsonl",
    )
    scen = {
        "scenario_id": "s1", "name": "t", "category": "smoke",
        "target_bot": "Rauru_HD_bot", "target_chat_id": "c",
        "steps": [
            {"step": 1, "sent": "good", "expected_pattern": "ok", "timeout_ms": 500},
            {"step": 2, "sent": "silent", "expected_pattern": "whatever", "timeout_ms": 300},
        ],
    }
    try:
        run = _run_async(runner.run_scenario(
            scen, triggered_by="t", trigger_source="slash_command",
        ))
        assert run.regression_flags == [2]
        assert run.last_passing_run_id == baseline_id
    finally:
        db.close()


def test_run_queue_blocks_concurrent_same_scenario(tmp_path):
    """Two runs of the same scenario in parallel → second waits for the first."""
    db = SessionDB(db_path=tmp_path / "state.db")
    inbox = _FakeInbox()
    call_order: list[str] = []

    async def send(chat_id: str, text: str):
        call_order.append(f"send:{text}")
        await asyncio.sleep(0.1)
        if text.startswith("run"):
            inbox.add_reply(text)  # echo back to satisfy substring match

    runner = HarnessRunner(
        db, send_message=send, inbox_reader=inbox,
        results_path=tmp_path / "harness_results.jsonl",
    )
    scen_a = {
        "scenario_id": "s1", "name": "t", "category": "smoke",
        "target_bot": "Rauru_HD_bot", "target_chat_id": "c",
        "steps": [{"step": 1, "sent": "runA", "expected_pattern": "runA",
                   "timeout_ms": 1000}],
    }
    scen_b = dict(scen_a)
    scen_b_steps = [{"step": 1, "sent": "runB", "expected_pattern": "runB",
                     "timeout_ms": 1000}]
    scen_b["steps"] = scen_b_steps

    async def drive():
        await asyncio.gather(
            runner.run_scenario(scen_a, triggered_by="t", trigger_source="slash_command"),
            runner.run_scenario(scen_b, triggered_by="t", trigger_source="slash_command"),
        )

    try:
        loop = asyncio.new_event_loop()
        loop.run_until_complete(drive())
        # Queue guarantees runA fully completes before runB starts sending.
        run_a_idx = call_order.index("send:runA")
        run_b_idx = call_order.index("send:runB")
        assert run_a_idx < run_b_idx
    finally:
        db.close()


# ---------------------------------------------------------------------------
# JSONL contract shape
# ---------------------------------------------------------------------------

def test_jsonl_contract_shape(tmp_path):
    run = HarnessRun(
        run_id="run_x", scenario_id="s1", scenario_name="test",
        scenario_category="smoke", triggered_by="hermes:owner:1",
        trigger_source="slash_command", target_bot="Rauru_HD_bot",
        target_chat_id="-1001", started_at=time.time(),
    )
    run.finished_at = run.started_at + 1
    run.duration_ms = 1000
    run.status = "pass"
    run.steps = [StepResult(
        step=1, sent="hi", expected_pattern="hi", match_type="substring",
        received="hi there", status="pass", duration_ms=100, gate=True,
    )]
    path = tmp_path / "results.jsonl"
    append_result_jsonl(run, path)
    record = json.loads(path.read_text("utf-8").strip())
    for key in (
        "run_id", "scenario_id", "scenario_name", "scenario_category",
        "triggered_by", "trigger_source", "target_bot", "target_chat_id",
        "started_at", "finished_at", "duration_ms", "status", "steps",
        "regression_flags", "last_passing_run_id",
    ):
        assert key in record, f"missing contract field {key}"
    assert record["started_at"].endswith("Z")
    assert record["steps"][0]["step"] == 1
    assert record["steps"][0]["gate"] is True
