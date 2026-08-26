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
    finish_reason: str | None
    reasoning_observed: bool | None
    final_content_observed: bool | None
    reasoning_chunk_count: int | None
    final_chunk_count: int | None
    reasoning_character_count: int | None
    final_character_count: int | None
    input_tokens: int | None
    output_tokens: int | None
    total_tokens: int | None
    provider_request_id: str | None


def normalize_model_event(event: object) -> NormalizedModelEvent:
    """Project either event generation without retaining model content."""
    lifecycle = str(getattr(event, "status"))
    storage_status = {
        "queued": "running",
        "connecting": "running",
        "awaiting_model_result": "running",
        "reasoning_output_activity": "running",
        "model_output_activity": "running",
        "validating": "running",
        "retrying": "retry_wait",
        "cancelled": "failed",
        "provider_failure": "failed",
        "network_failure": "failed",
        "model_result_failure": "failed",
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
        finish_reason=getattr(event, "finish_reason", None),
        reasoning_observed=getattr(event, "reasoning_observed", None),
        final_content_observed=getattr(event, "final_content_observed", None),
        reasoning_chunk_count=getattr(event, "reasoning_chunk_count", None),
        final_chunk_count=getattr(event, "final_chunk_count", None),
        reasoning_character_count=getattr(event, "reasoning_character_count", None),
        final_character_count=getattr(event, "final_character_count", None),
        input_tokens=getattr(event, "input_tokens", None),
        output_tokens=getattr(event, "output_tokens", None),
        total_tokens=getattr(event, "total_tokens", None),
        provider_request_id=getattr(event, "provider_request_id", None),
    )
