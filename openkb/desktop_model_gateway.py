"""Stable Model Call types and the explicit-terminal gateway entry point."""

from __future__ import annotations

import json
import math
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal, cast, get_args

if TYPE_CHECKING:
    from openkb.desktop_model_terminal import DesktopTerminalModelEvent

ModelConnectTransport = Callable[["DesktopModelRequest", float], object]
CancellationCallback = Callable[[], bool]
ExecutionLane = Literal["background", "interactive"]
ModelResultLifecycleStatus = Literal["completed", "model_result_failure"]
ModelResultFailureCode = Literal[
    "empty_final_result",
    "reasoning_only_result",
    "reasoning_output_exhausted",
    "model_response_invalid",
]
MODEL_RESULT_FAILURE_CODES: frozenset[str] = frozenset(get_args(ModelResultFailureCode))
ModelResultLifecycleFinalizer = Callable[[ModelResultLifecycleStatus, str | None, str | None], None]


def require_execution_lane(value: str) -> ExecutionLane:
    """Validate an execution lane at an untyped Desktop protocol boundary."""
    if value not in {"background", "interactive"}:
        raise ValueError(f"Unknown model execution lane: {value}")
    return cast(ExecutionLane, value)


@dataclass(frozen=True)
class DesktopModelRequest:
    """The in-memory provider input; request and response bodies are never persisted."""

    operation: str
    document_name: str
    content: str
    model_role: str = "default"
    model_name: str | None = None
    context_capacity: int | None = None
    document_input_capacity: int | None = None
    reasoning_effort: str | None = None
    provider_adapter: str | None = None
    provider_adapter_version: str | None = None
    structured_output_mode: str | None = None
    response_schema: dict[str, object] | None = None
    local_validation_required: bool = False
    response_example: dict[str, object] | None = None
    response_schema_name: str | None = None
    generation_parameters: dict[str, object] | None = None
    capability_identity: str | None = None
    prompt_contract_digest: str | None = None
    parent_operation: str | None = None
    parent_prompt_contract_digest: str | None = None
    prompt_contract_version: str | None = None
    prompt_contract_snapshot: dict[str, object] | None = None
    supports_streaming: bool | None = None
    job_id: str | None = None
    stage_run_id: str | None = None
    batch_id: str | None = None
    execution_lane: ExecutionLane = "background"
    response_timeout_seconds: float | None = None

    def __post_init__(self) -> None:
        require_execution_lane(self.execution_lane)
        if self.response_timeout_seconds is not None and (
            not math.isfinite(self.response_timeout_seconds) or self.response_timeout_seconds <= 0
        ):
            raise ValueError("Model response timeout must be finite and positive.")


@dataclass(frozen=True)
class DesktopProviderTokenUsage:
    input_tokens: int
    output_tokens: int
    total_tokens: int


@dataclass(frozen=True)
class DesktopModelOutputObservations:
    """Content-free facts observed while receiving one provider result."""

    finish_reason: str | None = None
    reasoning_observed: bool = False
    final_content_observed: bool = False
    reasoning_chunk_count: int = 0
    final_chunk_count: int = 0
    reasoning_character_count: int = 0
    final_character_count: int = 0
    output_limit_reached: bool = False


@dataclass(frozen=True)
class DesktopProviderStreamEvent:
    """Provider stream signal; reasoning text is retained only in memory for TRACE."""

    final_content: str = ""
    sensitive_reasoning_content: str = field(default="", repr=False, compare=False)
    reasoning_character_count: int = 0
    finish_reason: str | None = None
    output_limit_reached: bool = False


class DesktopModelProviderResponse(str):
    """String-compatible output plus ephemeral evidence for failed TRACE captures."""

    usage: DesktopProviderTokenUsage | None
    provider_request_id: str | None
    observations: DesktopModelOutputObservations
    sensitive_reasoning_content: str

    def __new__(
        cls,
        content: str,
        *,
        usage: DesktopProviderTokenUsage | None = None,
        provider_request_id: str | None = None,
        observations: DesktopModelOutputObservations | None = None,
        sensitive_reasoning_content: str = "",
    ) -> DesktopModelProviderResponse:
        value = super().__new__(cls, content)
        value.usage = usage
        value.provider_request_id = provider_request_id
        value.observations = observations or DesktopModelOutputObservations(
            final_content_observed=bool(content.strip()),
            final_chunk_count=1 if content.strip() else 0,
            final_character_count=len(content) if content.strip() else 0,
        )
        value.sensitive_reasoning_content = sensitive_reasoning_content
        return value


@dataclass(frozen=True)
class DesktopModelResult:
    """A provider result whose local-validation owner controls its final lifecycle."""

    call_id: str
    content: str
    attempt_count: int
    usage: DesktopProviderTokenUsage | None = None
    provider_request_id: str | None = None
    observations: DesktopModelOutputObservations | None = None
    diagnostic_context: dict[str, object] = field(default_factory=dict, repr=False, compare=False)
    sensitive_request_payload: str | None = field(default=None, repr=False, compare=False)
    sensitive_reasoning_content: str = field(default="", repr=False, compare=False)
    _lifecycle_finalizer: ModelResultLifecycleFinalizer | None = field(
        default=None,
        repr=False,
        compare=False,
    )


def complete_model_result(result: DesktopModelResult) -> None:
    """Terminal a deferred provider result only after local acceptance succeeds."""
    if result._lifecycle_finalizer is not None:
        result._lifecycle_finalizer("completed", None, None)


def reject_model_result(
    result: DesktopModelResult,
    *,
    failure_code: ModelResultFailureCode,
    reason: str,
) -> None:
    """Terminal a deferred provider result with a content-free validation failure."""
    if failure_code not in MODEL_RESULT_FAILURE_CODES:
        raise ValueError(f"Unknown Model Result Failure code: {failure_code}")
    if result._lifecycle_finalizer is not None:
        result._lifecycle_finalizer("model_result_failure", failure_code, reason)


def has_deferred_model_result_lifecycle(result: DesktopModelResult) -> bool:
    """Return whether local validation, rather than the provider seam, owns terminalization."""
    return result._lifecycle_finalizer is not None


@dataclass(frozen=True)
class DesktopModelFailure:
    """A classified, content-free explicit failure."""

    code: str
    reason: str
    suggested_action: str
    retryable: bool


class DesktopModelTransportError(RuntimeError):
    """Provider/transport failure with a deliberately narrow safe category."""

    def __init__(
        self,
        category: str,
        *,
        retry_after_seconds: float | None = None,
        diagnostic_type: str | None = None,
        diagnostic_detail: str | None = None,
        sensitive_detail: str | None = None,
    ) -> None:
        super().__init__(category)
        self.category = category
        self.retry_after_seconds = retry_after_seconds
        self.diagnostic_type = diagnostic_type
        self.diagnostic_detail = diagnostic_detail
        self.sensitive_detail = sensitive_detail


class DesktopModelCallError(RuntimeError):
    """Raised only after an explicit provider, network, or validation terminal event."""

    def __init__(
        self,
        call_id: str,
        failure: DesktopModelFailure,
        attempt_count: int,
        *,
        observations: DesktopModelOutputObservations | None = None,
        usage: DesktopProviderTokenUsage | None = None,
        provider_request_id: str | None = None,
        failure_event_id: str | None = None,
        elapsed_seconds: float | None = None,
        diagnostic_context: dict[str, object] | None = None,
    ) -> None:
        super().__init__(failure.reason)
        self.call_id = call_id
        self.failure = failure
        self.attempt_count = attempt_count
        self.observations = observations
        self.usage = usage
        self.provider_request_id = provider_request_id
        self.diagnostic_context = dict(diagnostic_context or {})
        context_failure_id = self.diagnostic_context.get("failure_event_id")
        self.failure_event_id = failure_event_id or (
            context_failure_id if isinstance(context_failure_id, str) else None
        )
        self.elapsed_seconds = elapsed_seconds


class DesktopModelCancelledError(RuntimeError):
    """User or application interruption, never a provider failure or elapsed timeout."""


class DesktopModelResultError(RuntimeError):
    """A provider request ended successfully but yielded no usable final result."""

    def __init__(self, observations: DesktopModelOutputObservations) -> None:
        if observations.reasoning_observed:
            code = (
                "reasoning_output_exhausted"
                if observations.output_limit_reached
                else "reasoning_only_result"
            )
        else:
            code = "empty_final_result"
        super().__init__(code)
        self.code = code
        self.observations = observations


_FAILURES: dict[str, DesktopModelFailure] = {
    "empty_final_result": DesktopModelFailure(
        "empty_final_result",
        "The model returned no usable final result.",
        "Run the model capability check before trying again.",
        False,
    ),
    "reasoning_only_result": DesktopModelFailure(
        "reasoning_only_result",
        "The model returned reasoning but no usable final result.",
        "Run the model capability check before trying again.",
        False,
    ),
    "reasoning_output_exhausted": DesktopModelFailure(
        "reasoning_output_exhausted",
        "The model exhausted its output limit in reasoning before returning a final result.",
        "Use a compatible Analysis profile and run the model capability check.",
        False,
    ),
    "model_rate_limited": DesktopModelFailure(
        "model_rate_limited",
        "The model provider is temporarily rate limiting requests.",
        "Wait briefly, then retry the document.",
        True,
    ),
    "model_network_transient": DesktopModelFailure(
        "model_network_transient",
        "The connection to the model provider failed or was interrupted.",
        "Check the network connection, then retry.",
        True,
    ),
    "model_server_error": DesktopModelFailure(
        "model_server_error",
        "The model provider returned a temporary server error.",
        "Retry the document later.",
        True,
    ),
    "model_provider_failure": DesktopModelFailure(
        "model_provider_failure",
        "The model provider explicitly rejected or ended the request.",
        "Check the provider status and retry the request.",
        True,
    ),
    "model_authentication_failed": DesktopModelFailure(
        "model_authentication_failed",
        "The model credentials were rejected.",
        "Check the configured model credentials.",
        False,
    ),
    "model_configuration_invalid": DesktopModelFailure(
        "model_configuration_invalid",
        "The selected model configuration is invalid.",
        "Check the model and endpoint settings.",
        False,
    ),
    "model_input_invalid": DesktopModelFailure(
        "model_input_invalid",
        "The model provider rejected this input.",
        "Check the document and model settings.",
        False,
    ),
    "model_response_invalid": DesktopModelFailure(
        "model_response_invalid",
        "The model response could not be validated.",
        "Retry with another model or check its response format.",
        False,
    ),
    "model_service_unavailable": DesktopModelFailure(
        "model_service_unavailable",
        "The model request could not be completed.",
        "Check the model service, then retry the request.",
        False,
    ),
}

_TRANSPORT_CATEGORIES = {
    "timeout": "model_network_transient",
    "network_timeout": "model_network_transient",
    "network": "model_network_transient",
    "rate_limited": "model_rate_limited",
    "server": "model_server_error",
    "provider": "model_provider_failure",
    "provider_timeout": "model_provider_failure",
    "authentication": "model_authentication_failed",
    "configuration": "model_configuration_invalid",
    "input": "model_input_invalid",
    "response_format": "model_response_invalid",
}


class DesktopModelGateway:
    """Compatibility construction name for the sole explicit-terminal implementation."""

    def __new__(cls, *args: Any, **kwargs: Any):
        if cls is not DesktopModelGateway:
            return super().__new__(cls)
        if not args:
            raise TypeError("DesktopModelGateway requires a connect-capable transport.")
        transport = args[0]
        if len(args) != 1:
            raise TypeError("DesktopModelGateway accepts exactly one transport argument.")
        from openkb.desktop_model_terminal import DesktopTerminalModelGateway

        return DesktopTerminalModelGateway(transport, **kwargs)

    @property
    def provider_name(self) -> str:
        raise NotImplementedError

    @property
    def model_name(self) -> str:
        raise NotImplementedError

    def for_lane(self, lane: ExecutionLane) -> DesktopModelGateway:
        raise NotImplementedError

    def analysis_capability_verified(self) -> bool:
        """Return whether this gateway's current Analysis profile may dispatch work."""
        return True

    def invalidate_analysis_capability(self, failure_code: str, reason: str) -> None:
        """Invalidate current-profile evidence when the gateway owns such a cache."""
        del failure_code, reason

    def invalidate_corroborated_analysis_capability(self, failure_code: str, reason: str) -> None:
        """Invalidate shared evidence after independent operations corroborate a breach."""
        del failure_code, reason

    def answer_capability_verified(self) -> bool:
        """Return whether this gateway's exact streamed Answer identity was checked."""
        return True

    def record_model_result_failure(self, call_id: str, failure_code: str) -> None:
        """Correct a provider completion after local result validation rejects it."""
        del call_id, failure_code

    def analyze(
        self,
        request: DesktopModelRequest,
        *,
        on_event: Callable[[DesktopTerminalModelEvent], None],
        is_cancelled: CancellationCallback | None = None,
    ) -> DesktopModelResult:
        raise NotImplementedError

    def analyze_once(
        self,
        request: DesktopModelRequest,
        *,
        on_event: Callable[[DesktopTerminalModelEvent], None],
        is_cancelled: CancellationCallback | None = None,
    ) -> DesktopModelResult:
        raise NotImplementedError

    def stream(
        self,
        request: DesktopModelRequest,
        *,
        on_event: Callable[[DesktopTerminalModelEvent], None],
        on_delta: Callable[[int, str], None],
        on_reset: Callable[[int], None] | None = None,
        is_cancelled: CancellationCallback | None = None,
    ) -> DesktopModelResult:
        raise NotImplementedError


def gateway_analysis_capability_verified(gateway: object) -> bool:
    """Preserve compatibility for simple gateways without a capability cache."""
    verifier = getattr(gateway, "analysis_capability_verified", None)
    return bool(verifier()) if callable(verifier) else True


def gateway_answer_capability_verified(gateway: object) -> bool:
    """Preserve compatibility for simple gateways without an Answer evidence cache."""
    verifier = getattr(gateway, "answer_capability_verified", None)
    return bool(verifier()) if callable(verifier) else True


def invalidate_analysis_capability(
    gateway: object,
    failure_code: str,
    reason: str,
) -> None:
    """Invalidate cache-backed gateways while keeping test/local gateways structural."""
    invalidator = getattr(gateway, "invalidate_analysis_capability", None)
    if callable(invalidator):
        invalidator(failure_code, reason)


def invalidate_corroborated_analysis_capability(
    gateway: object,
    failure_code: str,
    reason: str,
) -> None:
    """Invalidate cache-backed gateways only after operation-level corroboration."""
    invalidator = getattr(gateway, "invalidate_corroborated_analysis_capability", None)
    if callable(invalidator):
        invalidator(failure_code, reason)


def record_model_result_failure(
    gateway: object,
    call_id: str,
    failure_code: str,
) -> None:
    """Correct usage for cache-backed gateways while simple test gateways remain valid."""
    recorder = getattr(gateway, "record_model_result_failure", None)
    if callable(recorder):
        recorder(call_id, failure_code)


def classify_model_error(error: Exception) -> DesktopModelFailure:
    """Map an explicit terminal cause without inventing an elapsed-time failure."""
    if isinstance(error, DesktopModelResultError):
        return _FAILURES[error.code]
    if isinstance(error, DesktopModelTransportError):
        code = _TRANSPORT_CATEGORIES.get(error.category, "model_service_unavailable")
        return _FAILURES[code]
    if isinstance(error, (TimeoutError, ConnectionError, OSError)):
        return _FAILURES["model_network_transient"]
    status_code = getattr(error, "status_code", None)
    if isinstance(status_code, int):
        if status_code in {401, 403}:
            return _FAILURES["model_authentication_failed"]
        if status_code == 408:
            return _FAILURES["model_provider_failure"]
        if status_code == 429:
            return _FAILURES["model_rate_limited"]
        if 500 <= status_code <= 599:
            return _FAILURES["model_server_error"]
        if 400 <= status_code <= 499:
            return _FAILURES["model_input_invalid"]
    if isinstance(error, (TypeError, ValueError, json.JSONDecodeError)):
        return _FAILURES["model_response_invalid"]
    return _FAILURES["model_service_unavailable"]
