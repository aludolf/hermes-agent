"""YAML-based scenario loader for the harness runner (021 US6).

Reads scenario `.yaml` files from a directory and upserts each into the
`harness_scenarios_cache` table. Validation is strict but non-blocking —
an invalid file is logged and skipped, not fatal.

See specs/021-hermes-intelligence-layer/contracts/scenario-yaml-contract.md
for the canonical schema. Any change here must keep that contract honest.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from hermes_state import SessionDB

logger = logging.getLogger(__name__)

VALID_CATEGORIES = {"smoke", "regression", "edge"}
VALID_MATCH_TYPES = {"substring", "regex", "exact"}
MIN_TIMEOUT_MS = 1
MAX_TIMEOUT_MS = 120_000
DEFAULT_STEP_TIMEOUT_MS = 15_000


@dataclass
class LoadStats:
    """Summary returned by load_all_from_dir."""
    loaded: int = 0
    skipped: int = 0
    errors: list[str] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.errors is None:
            self.errors = []


class ScenarioValidationError(Exception):
    """Raised internally by validate_schema; callers only see a log line."""


class ScenarioLoader:
    """Loads scenario YAML files into `harness_scenarios_cache`."""

    def __init__(self, db: SessionDB) -> None:
        self.db = db

    # ------------------------------------------------------------------
    # Public entry points
    # ------------------------------------------------------------------

    def load_all_from_dir(self, path: str | Path) -> LoadStats:
        """Load every `*.yaml`/`*.yml` file in `path`.

        Clears the existing cache first so stale entries are removed when
        a YAML file is deleted between deploys. Individual file errors are
        logged and counted but don't abort the overall load.
        """
        stats = LoadStats()
        root = Path(path).expanduser()
        if not root.exists() or not root.is_dir():
            logger.info("scenario loader: dir missing or not a directory: %s", root)
            return stats

        # Fresh cache each startup — contract says YAML is the source of truth.
        try:
            self.db.clear_scenarios_cache()
        except Exception as e:
            logger.warning("scenario loader: clear cache failed: %s", e)

        for file in sorted(root.glob("*.y*ml")):
            try:
                self.load_one(file)
                stats.loaded += 1
            except ScenarioValidationError as e:
                stats.skipped += 1
                stats.errors.append(f"{file.name}: {e}")
                logger.warning("scenario loader: skip %s — %s", file.name, e)
            except Exception as e:
                stats.skipped += 1
                stats.errors.append(f"{file.name}: {e}")
                logger.warning("scenario loader: unexpected error on %s: %s", file.name, e)

        logger.info(
            "scenario loader: %d loaded, %d skipped from %s",
            stats.loaded, stats.skipped, root,
        )
        return stats

    def load_one(self, file_path: str | Path) -> None:
        """Parse, validate, and upsert a single scenario YAML."""
        p = Path(file_path)
        if not p.exists() or not p.is_file():
            raise ScenarioValidationError(f"file not found: {p}")

        raw = p.read_text(encoding="utf-8")
        yaml_hash = hashlib.sha256(raw.encode("utf-8")).hexdigest()

        try:
            import yaml  # PyYAML is part of the base image.
        except ImportError as e:
            raise ScenarioValidationError(f"PyYAML unavailable: {e}") from e
        try:
            data = yaml.safe_load(raw)
        except yaml.YAMLError as e:
            raise ScenarioValidationError(f"invalid YAML: {e}") from e

        if not isinstance(data, dict):
            raise ScenarioValidationError("top-level must be a mapping")

        self.validate_schema(data)
        self._upsert(data, yaml_path=str(p), yaml_hash=yaml_hash)

    # ------------------------------------------------------------------
    # Validation
    # ------------------------------------------------------------------

    def validate_schema(self, data: dict[str, Any]) -> None:
        """Strict check against scenario-yaml-contract.md.

        Raises `ScenarioValidationError` with a human-readable message on
        the first violation. The caller is expected to log + skip.
        """
        for required in ("id", "name", "category", "target_bot",
                         "target_chat_id", "steps"):
            if required not in data or data[required] in (None, ""):
                raise ScenarioValidationError(f"missing required field '{required}'")

        scenario_id = str(data["id"])
        if not scenario_id or " " in scenario_id or scenario_id != scenario_id.lower():
            raise ScenarioValidationError(
                f"id must be lowercase with no spaces (got {scenario_id!r})"
            )

        category = str(data["category"])
        if category not in VALID_CATEGORIES:
            raise ScenarioValidationError(
                f"category must be one of {sorted(VALID_CATEGORIES)} (got {category!r})"
            )

        steps = data["steps"]
        if not isinstance(steps, list) or not steps:
            raise ScenarioValidationError("steps must be a non-empty list")

        # Top-level timeout guard (optional)
        top_timeout = data.get("timeout_per_step_ms")
        if top_timeout is not None:
            self._check_timeout(top_timeout, where="timeout_per_step_ms")

        for idx, step in enumerate(steps, start=1):
            if not isinstance(step, dict):
                raise ScenarioValidationError(f"step #{idx} must be a mapping")
            declared_step = step.get("step")
            if declared_step != idx:
                raise ScenarioValidationError(
                    f"step #{idx} has `step: {declared_step}` — "
                    f"step numbers must be sequential starting at 1"
                )
            for field in ("sent", "expected_pattern"):
                if not step.get(field):
                    raise ScenarioValidationError(
                        f"step #{idx} missing required field '{field}'"
                    )
            mt = step.get("match_type", "substring")
            if mt not in VALID_MATCH_TYPES:
                raise ScenarioValidationError(
                    f"step #{idx} match_type must be one of "
                    f"{sorted(VALID_MATCH_TYPES)} (got {mt!r})"
                )
            step_timeout = step.get("timeout_ms")
            if step_timeout is not None:
                self._check_timeout(step_timeout, where=f"step #{idx} timeout_ms")

    @staticmethod
    def _check_timeout(value: Any, *, where: str) -> None:
        try:
            v = int(value)
        except (TypeError, ValueError) as e:
            raise ScenarioValidationError(f"{where} must be an integer: {e}") from e
        if not (MIN_TIMEOUT_MS <= v <= MAX_TIMEOUT_MS):
            raise ScenarioValidationError(
                f"{where} must be {MIN_TIMEOUT_MS}..{MAX_TIMEOUT_MS}ms (got {v})"
            )

    # ------------------------------------------------------------------
    # Upsert
    # ------------------------------------------------------------------

    def _upsert(
        self, data: dict[str, Any], *, yaml_path: str, yaml_hash: str,
    ) -> None:
        steps = data["steps"]
        top_timeout = int(data.get("timeout_per_step_ms") or DEFAULT_STEP_TIMEOUT_MS)
        estimated_duration_ms = sum(
            int(s.get("timeout_ms") or top_timeout) for s in steps
        )
        preconditions = data.get("preconditions")
        preconditions_json = (
            json.dumps(preconditions) if preconditions else None
        )
        self.db.upsert_scenario_from_yaml(
            scenario_id=str(data["id"]),
            name=str(data["name"]),
            category=str(data["category"]),
            steps_json=json.dumps(steps, ensure_ascii=False),
            target_bot=str(data["target_bot"]),
            target_chat_id=str(data["target_chat_id"]),
            yaml_path=yaml_path,
            yaml_hash=yaml_hash,
            description=data.get("description"),
            preconditions_json=preconditions_json,
            estimated_duration_ms=estimated_duration_ms,
        )
