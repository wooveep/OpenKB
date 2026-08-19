"""Bounded, observable Model Calls for Desktop import stages."""

from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal, cast

INITIAL_RESPONSE_TIMEOUT_SECONDS = 20.0
RETRY_TIMEOUT_INCREMENT_SECONDS = 10.0
MAX_AUTOMATIC_RETRIES = 3
MODEL_CALL_DEADLINE_SECONDS = 60.0

ModelCallStatus = Literal["running", "retry_wait", "completed", "failed"]
ModelTransport = Callable[["DesktopModelRequest", float], object]
ModelEventCallback = Callable[["DesktopModelAttemptEvent"], None]
ModelTransportDeltaCallback = Callable[[str], None]
ModelDeltaCallback = Callable[[int, str], None]
ModelStreamTransport = Callable[["DesktopModelRequest", float, ModelTransportDeltaCallback], object]
RetryCallback = Callable[[int], None]
CancellationCallback = Callable[[], bool]

logger = logging.getLogger(__name__)


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
    """Raised after a terminal Model Call failure has been safely classified."""

    def __init__(self, call_id: str, failure: DesktopModelFailure, attempt_count: int) -> None:
        super().__init__(failure.reason)
        self.call_id = call_id
        self.failure = failure
        self.attempt_count = attempt_count


class DesktopModelCancelledError(RuntimeError):
    """Stop one logical model call without classifying it as a provider failure."""


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
        initial_timeout_seconds: float = INITIAL_RESPONSE_TIMEOUT_SECONDS,
        provider_name: str = "scripted",
        model_name: str = "scripted",
    ) -> None:
        if not 0 < initial_timeout_seconds <= MODEL_CALL_DEADLINE_SECONDS:
            raise ValueError("Initial model response timeout must be between 0 and 60 seconds.")
        self._transport = transport
        self._clock = clock
        self._sleep = sleep
        self._initial_timeout_seconds = initial_timeout_seconds
        self._provider_name = provider_name
        self._model_name = model_name

    @property
    def provider_name(self) -> str:
        """Return non-secret provider metadata suitable for Stage checkpoints."""
        return self._provider_name

    @property
    def model_name(self) -> str:
        """Return non-secret model metadata suitable for Stage checkpoints."""
        return self._model_name

    def analyze(
        self,
        request: DesktopModelRequest,
        *,
        on_event: ModelEventCallback,
        is_cancelled: CancellationCallback | None = None,
    ) -> DesktopModelResult:
        """Execute one bounded analysis call and emit every attempt state transition."""
        return self._run(
            request,
            on_event=on_event,
            attempt_call=lambda current_request, timeout, _attempt: self._call_transport(
                current_request, timeout, is_cancelled=is_cancelled
            ),
            is_cancelled=is_cancelled,
        )

    def analyze_once(
        self,
        request: DesktopModelRequest,
        *,
        on_event: ModelEventCallback,
        timeout_seconds: float = INITIAL_RESPONSE_TIMEOUT_SECONDS,
        is_cancelled: CancellationCallback | None = None,
    ) -> DesktopModelResult:
        """Execute exactly one provider attempt within one bounded response deadline."""
        if timeout_seconds <= 0 or timeout_seconds > MODEL_CALL_DEADLINE_SECONDS:
            raise ValueError("One-shot model timeout must be between 0 and 60 seconds.")
        return self._run(
            request,
            on_event=on_event,
            attempt_call=lambda current_request, timeout, _attempt: self._call_transport(
                current_request, timeout, is_cancelled=is_cancelled
            ),
            is_cancelled=is_cancelled,
            max_automatic_retries=0,
            deadline_seconds=timeout_seconds,
            initial_timeout_seconds=timeout_seconds,
        )

    def stream(
        self,
        request: DesktopModelRequest,
        *,
        on_event: ModelEventCallback,
        on_delta: ModelDeltaCallback,
        on_reset: RetryCallback | None = None,
        is_cancelled: CancellationCallback | None = None,
    ) -> DesktopModelResult:
        """Emit production response deltas while preserving the existing call policy."""
        stream_transport = getattr(self._transport, "stream", None)
        if not callable(stream_transport):
            result = self.analyze(request, on_event=on_event, is_cancelled=is_cancelled)
            on_delta(1, result.content)
            return result
        transport = cast(ModelStreamTransport, stream_transport)
        return self._run(
            request,
            on_event=on_event,
            on_retry=on_reset,
            attempt_call=lambda current_request, timeout, attempt: self._call_stream_transport(
                current_request,
                timeout,
                transport,
                lambda delta: on_delta(attempt, delta),
                is_cancelled=is_cancelled,
            ),
            is_cancelled=is_cancelled,
        )

    def _run(
        self,
        request: DesktopModelRequest,
        *,
        on_event: ModelEventCallback,
        attempt_call: Callable[[DesktopModelRequest, float, int], object],
        on_retry: RetryCallback | None = None,
        is_cancelled: CancellationCallback | None = None,
        max_automatic_retries: int | None = None,
        deadline_seconds: float | None = None,
        initial_timeout_seconds: float | None = None,
    ) -> DesktopModelResult:
        retry_limit = (
            MAX_AUTOMATIC_RETRIES if max_automatic_retries is None else max_automatic_retries
        )
        deadline = MODEL_CALL_DEADLINE_SECONDS if deadline_seconds is None else deadline_seconds
        initial_timeout = (
            self._initial_timeout_seconds
            if initial_timeout_seconds is None
            else initial_timeout_seconds
        )
        call_id = uuid.uuid4().hex
        started_at = self._clock()
        for attempt_index in range(retry_limit + 1):
            if _is_cancelled(is_cancelled):
                raise DesktopModelCancelledError()
            remaining = _remaining_seconds(started_at, self._clock(), deadline)
            if remaining <= 0:
                raise self._deadline_error(call_id, attempt_index + 1, on_event)
            if not _prepare_transport_attempt(self._transport, is_cancelled, remaining):
                raise self._deadline_error(call_id, attempt_index + 1, on_event)
            try:
                remaining = _remaining_seconds(started_at, self._clock(), deadline)
                if remaining <= 0:
                    raise self._deadline_error(call_id, attempt_index + 1, on_event)
                scheduled_timeout = initial_timeout + (
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
            except BaseException:
                _release_prepared_transport_attempt(self._transport)
                raise
            try:
                response = attempt_call(request, timeout_seconds, attempt_index + 1)
            except DesktopModelCancelledError:
                raise
            except Exception as error:
                failure = classify_model_error(error)
                remaining = _remaining_seconds(started_at, self._clock(), deadline)
                exception_type, diagnostic_detail = _diagnostic_error_detail(error)
                logger.warning(
                    "model_attempt_failed call_id=%s operation=%s document=%r attempt=%s "
                    "category=%s retryable=%s exception_type=%s detail=%r",
                    call_id,
                    request.operation,
                    request.document_name,
                    attempt_index + 1,
                    failure.code,
                    failure.retryable,
                    exception_type,
                    diagnostic_detail,
                )
                if remaining <= 0:
                    raise self._deadline_error(call_id, attempt_index + 1, on_event) from error
                if failure.retryable and attempt_index < retry_limit:
                    next_timeout = min(
                        initial_timeout + ((attempt_index + 1) * RETRY_TIMEOUT_INCREMENT_SECONDS),
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
                    if on_retry is not None:
                        on_retry(attempt_index + 2)
                    if _is_cancelled(is_cancelled):
                        raise DesktopModelCancelledError() from error
                    self._wait_for_retry(
                        error,
                        started_at,
                        deadline,
                        is_cancelled=is_cancelled,
                    )
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

            if _is_cancelled(is_cancelled):
                raise DesktopModelCancelledError()
            now = self._clock()
            remaining = _remaining_seconds(started_at, now, deadline)
            if now - started_at >= deadline:
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
        raise self._deadline_error(call_id, retry_limit + 1, on_event)

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

    def _call_transport(
        self,
        request: DesktopModelRequest,
        timeout_seconds: float,
        *,
        is_cancelled: CancellationCallback | None,
    ) -> object:
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
        _wait_for_model_response(completed, timeout_seconds, is_cancelled)
        error = outcome.get("error")
        if isinstance(error, Exception):
            raise error
        return outcome.get("response")

    def _call_stream_transport(
        self,
        request: DesktopModelRequest,
        timeout_seconds: float,
        transport: ModelStreamTransport,
        on_delta: ModelTransportDeltaCallback,
        *,
        is_cancelled: CancellationCallback | None,
    ) -> object:
        """Bound one provider stream and discard late chunks after a timeout."""
        active = threading.Event()
        active.set()
        completed = threading.Event()
        outcome: dict[str, object] = {}

        def emit(delta: str) -> None:
            if active.is_set() and delta:
                on_delta(delta)

        def invoke() -> None:
            try:
                outcome["response"] = transport(request, timeout_seconds, emit)
            except Exception as error:
                outcome["error"] = error
            finally:
                completed.set()

        threading.Thread(target=invoke, daemon=True, name="openkb-model-stream-attempt").start()
        try:
            _wait_for_model_response(completed, timeout_seconds, is_cancelled)
        finally:
            active.clear()
        error = outcome.get("error")
        if isinstance(error, Exception):
            raise error
        return outcome.get("response")

    def _wait_for_retry(
        self,
        error: Exception,
        started_at: float,
        deadline_seconds: float,
        *,
        is_cancelled: CancellationCallback | None,
    ) -> None:
        retry_after = (
            error.retry_after_seconds
            if isinstance(error, DesktopModelTransportError)
            and isinstance(error.retry_after_seconds, (int, float))
            else 0.0
        )
        if retry_after <= 0:
            return
        remaining = _remaining_seconds(started_at, self._clock(), deadline_seconds)
        wait_remaining = min(float(retry_after), remaining)
        while wait_remaining > 0:
            if _is_cancelled(is_cancelled):
                raise DesktopModelCancelledError()
            interval = min(0.05, wait_remaining)
            self._sleep(interval)
            wait_remaining -= interval


def _wait_for_model_response(
    completed: threading.Event,
    timeout_seconds: float,
    is_cancelled: CancellationCallback | None,
) -> None:
    """Poll an untrusted adapter wait so a Desktop answer can stop promptly."""
    deadline = time.monotonic() + timeout_seconds
    while True:
        if _is_cancelled(is_cancelled):
            raise DesktopModelCancelledError()
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError()
        if completed.wait(min(0.05, remaining)):
            if _is_cancelled(is_cancelled):
                raise DesktopModelCancelledError()
            return


def _prepare_transport_attempt(
    transport: object, is_cancelled: CancellationCallback | None, remaining_seconds: float
) -> bool:
    """Queue a provider slot before, rather than inside, response-time accounting."""
    prepare = getattr(transport, "prepare_model_attempt", None)
    if callable(prepare):
        return prepare(is_cancelled, remaining_seconds) is not False
    return True


def _release_prepared_transport_attempt(transport: object) -> None:
    """Return a slot reserved before the provider call could begin."""
    release = getattr(transport, "release_prepared_model_attempt", None)
    if callable(release):
        release()


def _is_cancelled(callback: CancellationCallback | None) -> bool:
    return callback is not None and callback()


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


def _diagnostic_error_detail(error: Exception) -> tuple[str, str]:
    """Return local-log detail without changing the stable user-facing failure text."""
    if isinstance(error, DesktopModelTransportError):
        return (
            error.diagnostic_type or type(error).__name__,
            error.diagnostic_detail or error.category,
        )
    detail = str(error).strip() or type(error).__name__
    return type(error).__name__, detail[:500]


def _remaining_seconds(started_at: float, now: float, deadline_seconds: float) -> float:
    return max(0.0, deadline_seconds - (now - started_at))
