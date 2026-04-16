"""Per-step regression detection for harness runs (021 US5).

Compares a finished HarnessRun against its last-passing baseline for the
same scenario and flags step numbers that regressed — i.e. passed in the
baseline but failed (or timed out) in the current run.

Design:
- "Regression" requires a baseline. Steps that always failed → NOT flagged.
- New steps (added to the scenario after the baseline) have no baseline
  match → NOT flagged. They'll become comparable after the first clean
  pass of the new scenario version.
- Step IDs are matched by 1-based `step` number. Reordering steps in the
  YAML between runs can produce misleading flags — that's on the author.

Pure function; no I/O.
"""

from __future__ import annotations

from typing import Any, Iterable


_FAILING_STATUSES = {"fail", "timeout", "error"}


def _steps_dict(run: dict[str, Any] | None) -> dict[int, dict[str, Any]]:
    if run is None:
        return {}
    steps = run.get("steps") or []
    if isinstance(steps, dict):
        steps = list(steps.values())
    out: dict[int, dict[str, Any]] = {}
    for s in steps:
        if not isinstance(s, dict):
            continue
        step_num = s.get("step")
        try:
            key = int(step_num) if step_num is not None else None
        except (TypeError, ValueError):
            continue
        if key is None:
            continue
        out[key] = s
    return out


def detect_regressions(
    current_run: dict[str, Any],
    last_passing_run: dict[str, Any] | None,
) -> list[int]:
    """Return step numbers that regressed relative to the baseline.

    A step regresses when:
    - current step status ∈ {fail, timeout, error}, AND
    - baseline has that step number with status == "pass".

    Returns an empty list if `last_passing_run` is None (no baseline yet).
    """
    if last_passing_run is None:
        return []

    current_steps = _steps_dict(current_run)
    baseline_steps = _steps_dict(last_passing_run)

    regressed: list[int] = []
    for step_num, cur in current_steps.items():
        cur_status = str(cur.get("status", "")).lower()
        if cur_status not in _FAILING_STATUSES:
            continue
        base = baseline_steps.get(step_num)
        if base is None:
            continue  # new step, no baseline comparison
        base_status = str(base.get("status", "")).lower()
        if base_status == "pass":
            regressed.append(step_num)
    regressed.sort()
    return regressed
