"""One state machine for explicit role-specific Model Capability Checks."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from openkb.desktop_model_capability_check import (
    ModelCapabilityRole,
    validate_capability_result,
)
from openkb.desktop_model_capability_store import (
    DesktopCapabilityEvidenceProfile,
    DesktopModelCapabilityStore,
)
from openkb.desktop_model_failure_logging import own_capability_model_result_failure
from openkb.desktop_model_gateway import (
    DesktopModelCallError,
    DesktopModelCancelledError,
    DesktopModelRequest,
    DesktopModelResult,
    complete_model_result,
    reject_model_result,
)


class DesktopModelCapabilityVerificationError(RuntimeError):
    """A sanitized terminal outcome from one role-specific capability check."""

    def __init__(
        self,
        role: ModelCapabilityRole,
        code: str,
        reason: str,
        *,
        failure_event_id: str | None = None,
    ) -> None:
        super().__init__(reason)
        self.role = role
        self.code = code
        self.reason = reason
        self.failure_event_id = failure_event_id


@dataclass(frozen=True)
class DesktopModelCapabilityVerification:
    """Content-free successful outcome returned to a Desktop Engine route."""

    role: ModelCapabilityRole
    model: str
    attempt_count: int
    profile_identity: str | None
    cached: bool = False

    def as_dict(self) -> dict[str, object]:
        return {
            "role": self.role,
            "model": self.model,
            "status": "verified",
            "attempt_count": self.attempt_count,
            "profile_identity": self.profile_identity,
            "cached": self.cached,
        }


def verify_model_capability(
    kb_dir: Path,
    *,
    role: ModelCapabilityRole,
    model: str,
    profile: DesktopCapabilityEvidenceProfile | None,
    gateway: object,
    request: DesktopModelRequest,
    on_event: Callable[[Any], None],
    is_cancelled: Callable[[], bool] | None,
    reuse_verified: bool = False,
) -> DesktopModelCapabilityVerification:
    """Run one check and own every durable cache transition around it."""
    store = DesktopModelCapabilityStore(kb_dir)
    if profile is not None and reuse_verified and store.is_verified(profile):
        return DesktopModelCapabilityVerification(
            role,
            model,
            0,
            profile.identity,
            cached=True,
        )
    if profile is not None:
        store.begin(profile)
    result: DesktopModelResult | None = None
    try:
        if role == "answer":
            invoke = getattr(gateway, "stream")
            result = invoke(
                request,
                on_event=on_event,
                on_delta=lambda _attempt, _delta: None,
                is_cancelled=is_cancelled,
            )
        else:
            invoke = getattr(gateway, "analyze")
            result = invoke(
                request,
                on_event=on_event,
                is_cancelled=is_cancelled,
            )
        assert result is not None
        validate_capability_result(request.operation, result.content)
    except DesktopModelCancelledError as error:
        if profile is not None:
            store.mark_cancelled(profile)
        raise DesktopModelCapabilityVerificationError(
            role,
            "request_cancelled",
            f"{role.title()} Model Capability Check was cancelled.",
        ) from error
    except DesktopModelCallError as error:
        if profile is not None:
            store.mark_failed(
                profile,
                failure_code=error.failure.code,
                reason=error.failure.reason,
            )
        raise DesktopModelCapabilityVerificationError(
            role,
            error.failure.code,
            error.failure.reason,
            failure_event_id=error.failure_event_id,
        ) from error
    except ValueError as error:
        reason = str(error)
        failure_event_id = None
        if result is not None:
            reject_model_result(
                result,
                failure_code="model_response_invalid",
                reason=reason,
            )
            failure_event_id = own_capability_model_result_failure(
                request=request,
                result=result,
                error=error,
            )
        if profile is not None:
            store.mark_failed(
                profile,
                failure_code="model_capability_check_failed",
                reason=reason,
            )
        raise DesktopModelCapabilityVerificationError(
            role,
            "model_capability_check_failed",
            reason,
            failure_event_id=failure_event_id,
        ) from error
    assert result is not None
    complete_model_result(result)
    if profile is not None:
        store.mark_verified(profile)
    return DesktopModelCapabilityVerification(
        role,
        model,
        result.attempt_count,
        profile.identity if profile is not None else None,
    )
