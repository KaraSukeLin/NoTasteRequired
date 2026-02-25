from __future__ import annotations

from datetime import datetime, timezone

from app.models import PhaseName, PhaseTraceRecord


class TraceRecorder:
    def start_phase(self, phase: PhaseName, input_summary: str) -> dict[str, object]:
        return {
            "phase": phase,
            "input_summary": input_summary,
            "started_at": datetime.now(timezone.utc),
        }

    def complete_phase(
        self,
        token: dict[str, object],
        *,
        output_summary: str,
        status: str = "completed",
        error_type: str | None = None,
        artifact_refs: list[str] | None = None,
    ) -> PhaseTraceRecord:
        started_at = token["started_at"]
        assert isinstance(started_at, datetime)
        latency_ms = int((datetime.now(timezone.utc) - started_at).total_seconds() * 1000)
        return PhaseTraceRecord(
            phase=token["phase"],
            status=status,
            input_summary=str(token["input_summary"]),
            output_summary=output_summary,
            latency_ms=latency_ms,
            error_type=error_type,
            artifact_refs=artifact_refs or [],
        )

