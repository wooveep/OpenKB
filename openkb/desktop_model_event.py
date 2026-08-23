"""Normalize legacy and explicit-terminal Model Call lifecycle events."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class NormalizedModelEvent:
    call_id: str
    attempt: int
    storage_status: str
    lifecycle_status: str
    elapsed_seconds: float
    error_code: str | None
    reason: str | None
    retry_after_seconds: float | None


def normalize_model_event(event: object) -> NormalizedModelEvent:
    """Project either event generation without retaining model content."""
    lifecycle = str(getattr(event, "status"))
    storage_status = {
        "queued": "running",
        "connecting": "running",
        "awaiting_model_result": "running",
        "model_output_activity": "running",
        "validating": "running",
        "retrying": "retry_wait",
        "cancelled": "failed",
        "provider_failure": "failed",
        "network_failure": "failed",
    }.get(lifecycle, lifecycle)
    return NormalizedModelEvent(
        call_id=str(getattr(event, "call_id")),
        attempt=int(getattr(event, "attempt")),
        storage_status=storage_status,
        lifecycle_status=lifecycle,
        elapsed_seconds=float(getattr(event, "elapsed_seconds", 0.0)),
        error_code=getattr(event, "failure_code", getattr(event, "error_code", None)),
        reason=getattr(event, "reason", None),
        retry_after_seconds=getattr(event, "retry_after_seconds", None),
    )
