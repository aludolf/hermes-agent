"""Cross-layer publication orchestration for canonical promotion.

Manages the lifecycle of canonical candidates: creation, approval/rejection,
and publication into Domains_KB. Every operation persists durable state
for lineage and audit.
"""

from __future__ import annotations

import time
from typing import Any
from uuid import uuid4

from .models import CandidateState, ValidationStatus


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex[:12]}"


class PromotionService:
    """Façade for canonical candidate lifecycle and promotion."""

    def __init__(self, db: Any) -> None:
        self.db = db

    # ------------------------------------------------------------------
    # Candidate lifecycle
    # ------------------------------------------------------------------

    def create_candidate(
        self,
        *,
        job_id: str,
        evidence_id: str,
        working_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> str:
        """Create a canonical candidate from a job's evidence (and optional working artifact).

        Returns the candidate_id.
        """
        candidate_id = _new_id("cand")
        self.db.create_canonical_candidate(
            candidate_id=candidate_id,
            job_id=job_id,
            evidence_id=evidence_id,
            working_id=working_id,
            status=CandidateState.PENDING_REVIEW,
            metadata_json=metadata,
        )
        return candidate_id

    def get_candidate(self, candidate_id: str) -> dict[str, Any] | None:
        """Retrieve a candidate by ID."""
        return self.db.get_canonical_candidate(candidate_id)

    def list_candidates(
        self, *, status: str | None = None, limit: int = 20
    ) -> list[dict[str, Any]]:
        """List candidates, optionally filtered by status."""
        return self.db.list_canonical_candidates(status=status, limit=limit)

    # ------------------------------------------------------------------
    # Approval / rejection
    # ------------------------------------------------------------------

    def approve(
        self,
        *,
        candidate_id: str,
        resolved_by: str,
        note: str | None = None,
    ) -> dict[str, Any]:
        """Approve a candidate for canonical publication."""
        decision_id = _new_id("dec")
        now = time.time()
        self.db.create_promotion_decision(
            decision_id=decision_id,
            candidate_id=candidate_id,
            status="approved",
            policy_name="explicit_operator_approval",
            requested_at=now,
            resolved_at=now,
            resolved_by=resolved_by,
            resolution_note=note,
        )
        self.db.update_candidate_status(
            candidate_id, CandidateState.APPROVED_FOR_PUBLISH
        )
        return {
            "decision_id": decision_id,
            "candidate_id": candidate_id,
            "status": "approved",
            "resolved_by": resolved_by,
            "resolution_note": note,
        }

    def reject(
        self,
        *,
        candidate_id: str,
        resolved_by: str,
        note: str | None = None,
    ) -> dict[str, Any]:
        """Reject a candidate — it stays traceable but is not published."""
        decision_id = _new_id("dec")
        now = time.time()
        self.db.create_promotion_decision(
            decision_id=decision_id,
            candidate_id=candidate_id,
            status="rejected",
            policy_name="explicit_operator_rejection",
            requested_at=now,
            resolved_at=now,
            resolved_by=resolved_by,
            resolution_note=note,
        )
        self.db.update_candidate_status(candidate_id, CandidateState.REJECTED)
        return {
            "decision_id": decision_id,
            "candidate_id": candidate_id,
            "status": "rejected",
            "resolved_by": resolved_by,
            "resolution_note": note,
        }

    # ------------------------------------------------------------------
    # Publication
    # ------------------------------------------------------------------

    def publish(
        self,
        *,
        candidate_id: str,
        destination_repo: str = "Domains_KB",
        entry_ids: list[str] | None = None,
        files_changed: list[str] | None = None,
        published_by: str | None = None,
    ) -> dict[str, Any]:
        """Publish an approved candidate to the canonical destination.

        The candidate must be in `approved_for_publish` state.
        Returns the publication record.
        """
        candidate = self.db.get_canonical_candidate(candidate_id)
        if not candidate:
            raise ValueError(f"Candidate {candidate_id} not found")
        if candidate["status"] != CandidateState.APPROVED_FOR_PUBLISH:
            raise ValueError(
                f"Candidate {candidate_id} is '{candidate['status']}', not approved_for_publish"
            )

        publication_id = _new_id("pub")
        now = time.time()
        self.db.create_canonical_publication(
            publication_id=publication_id,
            candidate_id=candidate_id,
            destination_repo=destination_repo,
            entry_ids_json=entry_ids or [],
            files_changed_json=files_changed or [],
            validation_status=ValidationStatus.PASSED,
            published_at=now,
            published_by=published_by,
        )
        self.db.update_candidate_status(candidate_id, CandidateState.PUBLISHED)

        return {
            "publication_id": publication_id,
            "candidate_id": candidate_id,
            "destination_repo": destination_repo,
            "entry_ids": entry_ids or [],
            "validation_status": ValidationStatus.PASSED,
            "published_at": now,
        }
