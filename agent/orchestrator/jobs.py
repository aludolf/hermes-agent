"""Job lifecycle helpers for the Hermes orchestrator."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence
from uuid import uuid4

from hermes_state import SessionDB

from .artifacts import LocalStorageAdapter
from .models import ArtifactType, JobStatus, NormalizedArtifact, RouteClass, SourceRef
from .router import classify_route


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex[:12]}"


def _utc_iso8601(timestamp: float | None) -> str | None:
    if timestamp is None:
        return None
    return datetime.fromtimestamp(timestamp, tz=timezone.utc).isoformat().replace("+00:00", "Z")


class OrchestratorJobService:
    """High-level façade over orchestrator persistence and routing."""

    def __init__(
        self,
        db: SessionDB,
        *,
        local_storage: LocalStorageAdapter | None = None,
    ) -> None:
        self.db = db
        self.local_storage = local_storage or LocalStorageAdapter()

    @staticmethod
    def _artifact_from_row(row: Mapping[str, Any]) -> NormalizedArtifact:
        metadata = dict(row.get("metadata_json") or {})
        provenance = dict(row.get("provenance_json") or {})
        display_name = metadata.get("display_name") or Path(str(row.get("storage_path", ""))).name
        return NormalizedArtifact(
            artifact_type=str(row["artifact_type"]),
            storage_backend=str(row["storage_backend"]),
            storage_path=str(row["storage_path"]),
            display_name=str(display_name),
            mime_type=row.get("mime_type"),
            size_bytes=row.get("size_bytes"),
            checksum=row.get("checksum"),
            source_scope=provenance.get("source_scope"),
            provenance=provenance,
            metadata=metadata,
        )

    @staticmethod
    def _next_action(job: Mapping[str, Any], route_class: str) -> str | None:
        status = str(job.get("status") or "")
        if status in {JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLED}:
            return None
        if status == JobStatus.BLOCKED_FOR_APPROVAL:
            return "await_approval"
        if route_class == RouteClass.PENDING_ROUTE:
            return "classify"
        if route_class == RouteClass.DEV_WORKFLOW:
            return "inspect_repo"
        if route_class == RouteClass.KB_CANDIDATE:
            return "review_for_publish"
        if route_class == RouteClass.ACTIONABLE_TASK:
            return "extract_tasks"
        return "parse"

    def create_job(
        self,
        *,
        job_type: str,
        requested_by: str,
        source: SourceRef,
        intent: str | None = None,
        priority: int = 0,
        metadata: dict[str, Any] | None = None,
        artifacts: Sequence[NormalizedArtifact] | None = None,
        auto_classify: bool = True,
    ) -> dict[str, Any]:
        job_id = _new_id("job")
        self.db.create_orchestrator_job(
            job_id=job_id,
            job_type=job_type,
            requested_by=requested_by,
            source_type=str(source.source_type),
            source_uri=source.source_uri,
            source_scope=source.source_scope,
            intent=intent,
            priority=priority,
            metadata_json=metadata or None,
        )

        for artifact in artifacts or ():
            artifact_record = artifact.as_record()
            self.db.create_orchestrator_artifact(
                artifact_id=_new_id("art"),
                job_id=job_id,
                artifact_type=artifact_record["artifact_type"],
                storage_backend=artifact_record["storage_backend"],
                storage_path=artifact_record["storage_path"],
                mime_type=artifact_record["mime_type"],
                checksum=artifact_record["checksum"],
                size_bytes=artifact_record["size_bytes"],
                provenance_json=artifact_record["provenance_json"],
                metadata_json=artifact_record["metadata_json"],
            )

        if auto_classify:
            self.classify_job(job_id)

        return self.get_job_summary(job_id)

    def ingest_local_path(
        self,
        path: str | Path,
        *,
        requested_by: str,
        source_uri: str | None = None,
        source_scope: str | None = None,
        job_type: str = "ingest_local_file",
        intent: str | None = None,
        target_class: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        artifact = self.local_storage.ingest(
            path,
            target_class=target_class,
            source_scope=source_scope,
            source_uri=source_uri,
            artifact_type=ArtifactType.RAW_INPUT,
            metadata=metadata,
        )
        source = SourceRef(
            source_type="local_fs",
            source_uri=source_uri or f"file://{Path(path).expanduser().resolve()}",
            source_scope=source_scope or str(Path(path).expanduser().resolve().parent),
        )
        return self.create_job(
            job_type=job_type,
            requested_by=requested_by,
            source=source,
            intent=intent,
            metadata=metadata,
            artifacts=[artifact],
            auto_classify=True,
        )

    def ingest_bytes(
        self,
        *,
        filename: str,
        data: bytes,
        requested_by: str,
        source: SourceRef,
        job_type: str = "ingest_attachment",
        intent: str | None = None,
        target_class: str | None = None,
        mime_type: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        artifact = self.local_storage.ingest_bytes(
            filename=filename,
            data=data,
            target_class=target_class,
            mime_type=mime_type,
            source_scope=source.source_scope,
            source_uri=source.source_uri,
            artifact_type=ArtifactType.RAW_INPUT,
            metadata=metadata,
        )
        return self.create_job(
            job_type=job_type,
            requested_by=requested_by,
            source=source,
            intent=intent,
            metadata=metadata,
            artifacts=[artifact],
            auto_classify=True,
        )

    def classify_job(
        self,
        job_id: str,
        *,
        manual_route_class: str | None = None,
        decision_reason: str | None = None,
    ) -> dict[str, Any]:
        job = self.db.get_orchestrator_job(job_id)
        if job is None:
            raise KeyError(f"Unknown orchestrator job: {job_id}")

        artifacts = self.db.list_orchestrator_artifacts(job_id)
        previous = self.db.get_routing_decision(job_id)
        if manual_route_class:
            route_decision = {
                "route_class": str(manual_route_class),
                "decision_reason": decision_reason or "Route manually overridden by operator.",
                "confidence": 1.0,
                "manual_override": True,
                "overridden_from": previous["route_class"] if previous else None,
            }
        else:
            route_decision = classify_route(job, artifacts).as_dict()

        self.db.record_routing_decision(
            decision_id=_new_id("route"),
            job_id=job_id,
            route_class=route_decision["route_class"],
            decision_reason=route_decision["decision_reason"],
            confidence=route_decision["confidence"],
            manual_override=route_decision["manual_override"],
            overridden_from=route_decision["overridden_from"],
        )
        return route_decision

    def request_approval(self, job_id: str, policy_name: str, *, action_id: str | None = None) -> dict[str, Any]:
        approval_id = _new_id("approval")
        self.db.create_approval_request(
            approval_id=approval_id,
            job_id=job_id,
            action_id=action_id,
            policy_name=policy_name,
        )
        self.db.update_orchestrator_job_status(job_id, JobStatus.BLOCKED_FOR_APPROVAL)
        approvals = self.db.list_approval_requests(job_id)
        return approvals[-1]

    def update_job_status(
        self,
        job_id: str,
        status: str,
        *,
        error_message: str | None = None,
        route_class: str | None = None,
    ) -> dict[str, Any]:
        self.db.update_orchestrator_job_status(
            job_id,
            status,
            error_message=error_message,
            route_class=route_class,
        )
        return self.get_job_summary(job_id)

    async def parse_pdf_job(
        self,
        job_id: str,
        *,
        summary_model: str | None = None,
    ) -> dict[str, Any]:
        """Parse PDF artifacts and generate a summary for a job.

        Extracts text from PDF artifacts using pymupdf, generates a summary
        using an LLM, stores the summary as a generated artifact, and updates
        the job status.

        Args:
            job_id: The orchestrator job to parse
            summary_model: Optional model override for summarization

        Returns:
            Updated job summary with the generated summary artifact
        """
        job = self.db.get_orchestrator_job(job_id)
        if job is None:
            raise KeyError(f"Unknown orchestrator job: {job_id}")

        self.db.update_orchestrator_job_status(job_id, JobStatus.RUNNING)

        artifacts = self.db.list_orchestrator_artifacts(job_id)
        pdf_artifacts = [
            a for a in artifacts
            if str(a.get("artifact_type")) == str(ArtifactType.RAW_INPUT)
            and (a.get("mime_type") == "application/pdf" or str(a.get("storage_path", "")).lower().endswith(".pdf"))
        ]

        if not pdf_artifacts:
            self.db.update_orchestrator_job_status(
                job_id,
                JobStatus.FAILED,
                error_message="No PDF artifacts found in job",
            )
            return self.get_job_summary(job_id)

        summaries = []
        for pdf_artifact in pdf_artifacts:
            pdf_path = Path(pdf_artifact["storage_path"])
            if not pdf_path.exists():
                continue

            try:
                import pymupdf

                doc = pymupdf.open(str(pdf_path))
                text_parts = []
                for page_num in range(len(doc)):
                    page = doc[page_num]
                    text = page.get_text()
                    if text.strip():
                        text_parts.append(f"--- Page {page_num + 1} ---\n{text}")
                doc.close()

                full_text = "\n\n".join(text_parts)
                if not full_text.strip():
                    summaries.append({"artifact": pdf_artifact, "summary": "[No readable text extracted from PDF]"})
                    continue

                from tools.web_tools import _call_summarizer_llm

                context_str = f"Source: {pdf_artifact.get('display_name', 'document.pdf')}\n\n"
                summary = await _call_summarizer_llm(
                    content=full_text[:100000],
                    context_str=context_str,
                    model=summary_model,
                    max_tokens=8000,
                )

                if summary:
                    summaries.append({"artifact": pdf_artifact, "summary": summary})
                else:
                    summaries.append({
                        "artifact": pdf_artifact,
                        "summary": f"[Summary generation failed - raw text preview]\n\n{full_text[:2000]}..."
                    })

            except ImportError:
                summaries.append({
                    "artifact": pdf_artifact,
                    "summary": "[pymupdf not available - cannot extract text]",
                })
            except Exception as e:
                summaries.append({
                    "artifact": pdf_artifact,
                    "summary": f"[Error extracting text: {str(e)}]",
                })

        combined_summary = f"# Document Summary\n\n"
        for item in summaries:
            display_name = item["artifact"].get("display_name", "document.pdf")
            combined_summary += f"## {display_name}\n\n{item['summary']}\n\n---\n\n"

        summary_bytes = combined_summary.encode("utf-8")
        summary_artifact = self.local_storage.ingest_bytes(
            filename=f"{job_id}-summary.md",
            data=summary_bytes,
            target_class="generated",
            mime_type="text/markdown",
            source_scope=None,
            source_uri=f"job://{job_id}/summary",
            artifact_type=ArtifactType.GENERATED_REPORT,
            metadata={"source_job_id": job_id, "source_artifacts": [a["artifact_id"] for a in summaries if "artifact_id" in a]},
        )

        self.db.create_orchestrator_artifact(
            artifact_id=_new_id("art"),
            job_id=job_id,
            artifact_type=summary_artifact.artifact_type,
            storage_backend=summary_artifact.storage_backend,
            storage_path=summary_artifact.storage_path,
            mime_type=summary_artifact.mime_type,
            checksum=summary_artifact.checksum,
            size_bytes=summary_artifact.size_bytes,
            provenance_json={"source_job_id": job_id},
            metadata_json=summary_artifact.metadata,
        )

        self.db.update_orchestrator_job_status(job_id, JobStatus.COMPLETED)
        return self.get_job_summary(job_id)

    def get_job_summary(self, job_id: str) -> dict[str, Any] | None:
        job = self.db.get_orchestrator_job(job_id)
        if job is None:
            return None

        artifacts = self.db.list_orchestrator_artifacts(job_id)
        route = self.db.get_routing_decision(job_id)
        approvals = self.db.list_approval_requests(job_id)

        artifact_summaries = []
        for artifact_row in artifacts:
            artifact = self._artifact_from_row(artifact_row)
            artifact_summaries.append(artifact.as_summary(artifact_row["artifact_id"]))

        route_class = route["route_class"] if route else job["route_class"]
        return {
            "job_id": job["job_id"],
            "job_type": job["job_type"],
            "status": job["status"],
            "route_class": route_class,
            "requested_by": job["requested_by"],
            "source": {
                "source_type": job["source_type"],
                "source_uri": job["source_uri"],
                "source_scope": job["source_scope"],
            },
            "artifacts": artifact_summaries,
            "next_action": self._next_action(job, route_class),
            "created_at": _utc_iso8601(job.get("created_at")),
            "started_at": _utc_iso8601(job.get("started_at")),
            "finished_at": _utc_iso8601(job.get("finished_at")),
            "metadata": job.get("metadata_json") or {},
            "error_message": job.get("error_message"),
            "routing": route,
            "approvals": approvals,
        }

    def list_job_summaries(
        self,
        *,
        requested_by: str | None = None,
        status: str | None = None,
        route_class: str | None = None,
        limit: int = 20,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        jobs = self.db.list_orchestrator_jobs(
            requested_by=requested_by,
            status=status,
            route_class=route_class,
            limit=limit,
            offset=offset,
        )
        summaries: list[dict[str, Any]] = []
        for job in jobs:
            summary = self.get_job_summary(job["job_id"])
            if summary is not None:
                summaries.append(summary)
        return summaries
