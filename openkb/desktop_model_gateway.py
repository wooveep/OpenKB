"""Stable Model Call types and the explicit-terminal gateway entry point."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal, cast

if TYPE_CHECKING:
    from openkb.desktop_model_terminal import DesktopTerminalModelEvent

ModelConnectTransport = Callable[["DesktopModelRequest", float], object]
CancellationCallback = Callable[[], bool]
ExecutionLane = Literal["background", "interactive"]


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
    response_schema: dict[str, object] | None = None
    response_schema_name: str | None = None
    generation_parameters: dict[str, object] | None = None
    prompt_contract_digest: str | None = None
    prompt_contract_version: str | None = None
    prompt_contract_snapshot: dict[str, object] | None = None
    supports_streaming: bool | None = None
    job_id: str | None = None
    stage_run_id: str | None = None
    batch_id: str | None = None
    execution_lane: ExecutionLane = "background"

    def __post_init__(self) -> None:
        require_execution_lane(self.execution_lane)


@dataclass(frozen=True)
class DesktopProviderTokenUsage:
    input_tokens: int
    output_tokens: int
    total_tokens: int


class DesktopModelProviderResponse(str):
    """String-compatible output carrying only safe provider metadata."""

    usage: DesktopProviderTokenUsage | None
    provider_request_id: str | None

    def __new__(
        cls,
        content: str,
        *,
        usage: DesktopProviderTokenUsage | None = None,
        provider_request_id: str | None = None,
    ) -> DesktopModelProviderResponse:
        value = super().__new__(cls, content)
        value.usage = usage
        value.provider_request_id = provider_request_id
        return value


@dataclass(frozen=True)
class DesktopModelResult:
    """A successful logical call; only callers retain its content."""

    call_id: str
    content: str
    attempt_count: int
    usage: DesktopProviderTokenUsage | None = None
    provider_request_id: str | None = None


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
    ) -> None:
        super().__init__(category)
        self.category = category
        self.retry_after_seconds = retry_after_seconds
        self.diagnostic_type = diagnostic_type
        self.diagnostic_detail = diagnostic_detail


class DesktopModelCallError(RuntimeError):
    """Raised only after an explicit provider, network, or validation terminal event."""

    def __init__(self, call_id: str, failure: DesktopModelFailure, attempt_count: int) -> None:
        super().__init__(failure.reason)
        self.call_id = call_id
        self.failure = failure
        self.attempt_count = attempt_count


class DesktopModelCancelledError(RuntimeError):
    """User or application interruption, never a provider failure or elapsed timeout."""


_FAILURES: dict[str, DesktopModelFailure] = {
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


def classify_model_error(error: Exception) -> DesktopModelFailure:
    """Map an explicit terminal cause without inventing an elapsed-time failure."""
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
