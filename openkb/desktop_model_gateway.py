"""Bounded, observable Model Calls for Desktop import stages."""

from __future__ import annotations

import json
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

INITIAL_RESPONSE_TIMEOUT_SECONDS = 20.0
RETRY_TIMEOUT_INCREMENT_SECONDS = 10.0
MAX_AUTOMATIC_RETRIES = 3
MODEL_CALL_DEADLINE_SECONDS = 60.0

ModelCallStatus = Literal["running", "retry_wait", "completed", "failed"]
ModelTransport = Callable[["DesktopModelRequest", float], object]
ModelEventCallback = Callable[["DesktopModelAttemptEvent"], None]


@dataclass(frozen=True)
class DesktopModelRequest:
    """The in-memory input passed to one provider adapter; never persisted verbatim."""

    operation: str
    document_name: str
    content: str


@dataclass(frozen=True)
class DesktopModelAttemptEvent:
    """A safe progress update for one physical Model Attempt."""

    call_id: str
    attempt: int
    status: ModelCallStatus
    timeout_seconds: float
    remaining_seconds: float
    error_code: str | None = None
    reason: str | None = None
    next_timeout_seconds: float | None = None


@dataclass(frozen=True)
class DesktopModelResult:
    """The successful response is retained only by the caller; the ledger stores its digest."""

    call_id: str
    content: str
    attempt_count: int


@dataclass(frozen=True)
class DesktopModelFailure:
    """A classified, user-safe failure for a logical Model Call."""

    code: str
    reason: str
    suggested_action: str
    retryable: bool


class DesktopModelTransportError(RuntimeError):
    """Adapter-visible provider failure with a deliberately narrow category."""

    def __init__(self, category: str, *, retry_after_seconds: float | None = None) -> None:
        super().__init__(category)
        self.category = category
        self.retry_after_seconds = retry_after_seconds


class DesktopModelCallError(RuntimeError):
    """Raised after a terminal Model Call failure has been safely classified."""

    def __init__(self, call_id: str, failure: DesktopModelFailure, attempt_count: int) -> None:
        super().__init__(failure.reason)
        self.call_id = call_id
        self.failure = failure
        self.attempt_count = attempt_count


_FAILURES: dict[str, DesktopModelFailure] = {
    "model_timeout": DesktopModelFailure(
        "model_timeout",
        "The model did not respond before the response timeout.",
        "Retry the document or increase its response timeout.",
        True,
    ),
    "model_rate_limited": DesktopModelFailure(
        "model_rate_limited",
        "The model provider is temporarily rate limiting requests.",
        "Wait briefly, then retry the document.",
        True,
    ),
    "model_network_transient": DesktopModelFailure(
        "model_network_transient",
        "The connection to the model provider was interrupted.",
        "Check the network connection, then retry the document.",
        True,
    ),
    "model_server_error": DesktopModelFailure(
        "model_server_error",
        "The model provider returned a temporary server error.",
        "Retry the document later.",
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
        "The model provider rejected this analysis input.",
        "Check the document and model settings.",
        False,
    ),
    "model_response_invalid": DesktopModelFailure(
        "model_response_invalid",
        "The model response could not be validated.",
        "Retry with another model or check its response format.",
        False,
    ),
    "model_deadline_exceeded": DesktopModelFailure(
        "model_deadline_exceeded",
        "The model call reached its 60-second response deadline.",
        "Retry the document or increase its response timeout.",
        False,
    ),
    "model_service_unavailable": DesktopModelFailure(
        "model_service_unavailable",
        "The model request could not be completed.",
        "Check the model service, then retry the document.",
        False,
    ),
}

_TRANSPORT_CATEGORIES = {
    "timeout": "model_timeout",
    "rate_limited": "model_rate_limited",
    "network": "model_network_transient",
    "server": "model_server_error",
    "authentication": "model_authentication_failed",
    "configuration": "model_configuration_invalid",
    "input": "model_input_invalid",
    "response_format": "model_response_invalid",
}


class DesktopModelGateway:
    """Run one logical provider request with the Desktop retry and deadline policy."""

    def __init__(
        self,
        transport: ModelTransport,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._transport = transport
        self._clock = clock
        self._sleep = sleep

    def analyze(
        self, request: DesktopModelRequest, *, on_event: ModelEventCallback
    ) -> DesktopModelResult:
        """Execute one bounded analysis call and emit every attempt state transition."""
        call_id = uuid.uuid4().hex
        started_at = self._clock()
        for attempt_index in range(MAX_AUTOMATIC_RETRIES + 1):
            remaining = _remaining_seconds(started_at, self._clock())
            if remaining <= 0:
                raise self._deadline_error(call_id, attempt_index, on_event)
            scheduled_timeout = INITIAL_RESPONSE_TIMEOUT_SECONDS + (
                attempt_index * RETRY_TIMEOUT_INCREMENT_SECONDS
            )
            timeout_seconds = min(scheduled_timeout, remaining)
            on_event(
                DesktopModelAttemptEvent(
                    call_id=call_id,
                    attempt=attempt_index + 1,
                    status="running",
                    timeout_seconds=timeout_seconds,
                    remaining_seconds=remaining,
                )
            )
            try:
                response = self._call_transport(request, timeout_seconds)
            except Exception as error:
                failure = classify_model_error(error)
                remaining = _remaining_seconds(started_at, self._clock())
                if remaining <= 0:
                    raise self._deadline_error(call_id, attempt_index + 1, on_event) from error
                if failure.retryable and attempt_index < MAX_AUTOMATIC_RETRIES:
                    next_timeout = min(
                        INITIAL_RESPONSE_TIMEOUT_SECONDS
                        + ((attempt_index + 1) * RETRY_TIMEOUT_INCREMENT_SECONDS),
                        remaining,
                    )
                    on_event(
                        DesktopModelAttemptEvent(
                            call_id=call_id,
                            attempt=attempt_index + 1,
                            status="retry_wait",
                            timeout_seconds=timeout_seconds,
                            remaining_seconds=remaining,
                            error_code=failure.code,
                            reason=failure.reason,
                            next_timeout_seconds=next_timeout,
                        )
                    )
                    self._wait_for_retry(error, started_at)
                    continue
                on_event(
                    DesktopModelAttemptEvent(
                        call_id=call_id,
                        attempt=attempt_index + 1,
                        status="failed",
                        timeout_seconds=timeout_seconds,
                        remaining_seconds=remaining,
                        error_code=failure.code,
                        reason=failure.reason,
                    )
                )
                raise DesktopModelCallError(call_id, failure, attempt_index + 1) from error

            now = self._clock()
            remaining = _remaining_seconds(started_at, now)
            if now - started_at >= MODEL_CALL_DEADLINE_SECONDS:
                raise self._deadline_error(call_id, attempt_index + 1, on_event)
            if not isinstance(response, str) or not response.strip():
                failure = _FAILURES["model_response_invalid"]
                on_event(
                    DesktopModelAttemptEvent(
                        call_id=call_id,
                        attempt=attempt_index + 1,
                        status="failed",
                        timeout_seconds=timeout_seconds,
                        remaining_seconds=remaining,
                        error_code=failure.code,
                        reason=failure.reason,
                    )
                )
                raise DesktopModelCallError(call_id, failure, attempt_index + 1)
            on_event(
                DesktopModelAttemptEvent(
                    call_id=call_id,
                    attempt=attempt_index + 1,
                    status="completed",
                    timeout_seconds=timeout_seconds,
                    remaining_seconds=remaining,
                )
            )
            return DesktopModelResult(
                call_id=call_id, content=response, attempt_count=attempt_index + 1
            )
        raise self._deadline_error(call_id, MAX_AUTOMATIC_RETRIES + 1, on_event)

    def _deadline_error(
        self, call_id: str, attempts: int, on_event: ModelEventCallback
    ) -> DesktopModelCallError:
        failure = _FAILURES["model_deadline_exceeded"]
        on_event(
            DesktopModelAttemptEvent(
                call_id=call_id,
                attempt=attempts,
                status="failed",
                timeout_seconds=0,
                remaining_seconds=0,
                error_code=failure.code,
                reason=failure.reason,
            )
        )
        return DesktopModelCallError(call_id, failure, attempts)

    def _call_transport(self, request: DesktopModelRequest, timeout_seconds: float) -> object:
        """Bound the wait even when an adapter fails to honor its timeout argument."""
        completed = threading.Event()
        outcome: dict[str, object] = {}

        def invoke() -> None:
            try:
                outcome["response"] = self._transport(request, timeout_seconds)
            except Exception as error:
                outcome["error"] = error
            finally:
                completed.set()

        threading.Thread(target=invoke, daemon=True, name="openkb-model-attempt").start()
        if not completed.wait(timeout_seconds):
            raise TimeoutError()
        error = outcome.get("error")
        if isinstance(error, Exception):
            raise error
        return outcome.get("response")

    def _wait_for_retry(self, error: Exception, started_at: float) -> None:
        retry_after = (
            error.retry_after_seconds
            if isinstance(error, DesktopModelTransportError)
            and isinstance(error.retry_after_seconds, (int, float))
            else 0.0
        )
        if retry_after <= 0:
            return
        remaining = _remaining_seconds(started_at, self._clock())
        if remaining > 0:
            self._sleep(min(float(retry_after), remaining))


def classify_model_error(error: Exception) -> DesktopModelFailure:
    """Map provider details to a safe Desktop error without retaining provider text."""
    if isinstance(error, DesktopModelTransportError):
        return _FAILURES[_TRANSPORT_CATEGORIES.get(error.category, "model_service_unavailable")]
    if isinstance(error, TimeoutError):
        return _FAILURES["model_timeout"]
    if isinstance(error, (ConnectionError, OSError)):
        return _FAILURES["model_network_transient"]
    status_code = getattr(error, "status_code", None)
    if isinstance(status_code, int):
        if status_code in {401, 403}:
            return _FAILURES["model_authentication_failed"]
        if status_code in {408, 429}:
            return _FAILURES["model_timeout" if status_code == 408 else "model_rate_limited"]
        if 500 <= status_code <= 599:
            return _FAILURES["model_server_error"]
        if 400 <= status_code <= 499:
            return _FAILURES["model_input_invalid"]
    if isinstance(error, (TypeError, ValueError, json.JSONDecodeError)):
        return _FAILURES["model_response_invalid"]
    return _FAILURES["model_service_unavailable"]


def _remaining_seconds(started_at: float, now: float) -> float:
    return max(0.0, MODEL_CALL_DEADLINE_SECONDS - (now - started_at))
