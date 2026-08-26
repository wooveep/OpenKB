"""Convert a locally invalid structured result into a terminal Model Result Failure."""

from __future__ import annotations

from openkb.desktop_model_gateway import (
    MODEL_RESULT_FAILURE_CODES,
    DesktopModelCallError,
    DesktopModelFailure,
    has_deferred_model_result_lifecycle,
    invalidate_analysis_capability,
    record_model_result_failure,
)
from openkb.desktop_structured_output import DesktopStructuredOutputInvalidError


def is_model_result_failure(failure_code: str) -> bool:
    """Return whether one stable code represents a successful-but-unusable result."""
    return failure_code in MODEL_RESULT_FAILURE_CODES


def structured_model_result_failure(
    error: DesktopStructuredOutputInvalidError,
    *,
    suggested_action: str,
) -> DesktopModelCallError:
    """Retain safe final-call metadata while discarding both invalid result bodies."""
    result = error.final_result
    return DesktopModelCallError(
        result.call_id,
        DesktopModelFailure(
            "model_response_invalid",
            str(error),
            suggested_action,
            False,
        ),
        error.attempt_count,
        observations=result.observations,
        usage=result.usage,
        provider_request_id=result.provider_request_id,
    )


def invalidate_structured_model_result(
    gateway: object,
    error: DesktopStructuredOutputInvalidError,
) -> None:
    """Correct usage and invalidate the exact profile at one shared consumer boundary."""
    failure = structured_model_result_failure(
        error,
        suggested_action=(
            "Run an explicit Analysis capability check before retrying this operation."
        ),
    )
    if not has_deferred_model_result_lifecycle(error.final_result):
        record_model_result_failure(gateway, failure.call_id, failure.failure.code)
    invalidate_analysis_capability(gateway, failure.failure.code, failure.failure.reason)
