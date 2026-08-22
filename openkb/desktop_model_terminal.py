"""Model Calls that end only on explicit provider, network, or user events."""

from __future__ import annotations

import math
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from openkb.desktop_model_gateway import (
    DesktopModelCallError,
    DesktopModelCancelledError,
    DesktopModelFailure,
    DesktopModelRequest,
    DesktopModelResult,
    DesktopModelTransportError,
    classify_model_error,
)

MODEL_CONNECT_TIMEOUT_SECONDS = 30.0
MAX_TERMINAL_MODEL_ATTEMPTS = 3
_TERMINAL_RETRY_BACKOFF_SECONDS = (1.0, 2.0)

TerminalModelCallStatus = Literal[
    "queued",
    "connecting",
    "awaiting_model_result",
    "model_output_activity",
    "completed",
    "retrying",
    "cancelled",
    "provider_failure",
    "network_failure",
]
TerminalModelTransport = Callable[[DesktopModelRequest, float], object]
CancellationCallback = Callable[[], bool]
RequestSentCallback = Callable[[], None]
AttemptRelease = Callable[[], None]

_NETWORK_FAILURE = DesktopModelFailure(
    "model_network_transient",
    "The connection to the model provider failed or was interrupted.",
    "Check the network connection, then retry.",
    True,
)
_PROVIDER_FAILURE = DesktopModelFailure(
    "model_provider_failure",
    "The model provider explicitly rejected or ended the request.",
    "Check the provider status and retry the request.",
    True,
)


@dataclass(frozen=True)
class DesktopTerminalModelEvent:
    """A content-free lifecycle event for one physical Model Attempt."""

    call_id: str
    attempt: int
    status: TerminalModelCallStatus
    elapsed_seconds: float
    failure_code: str | None = None
    reason: str | None = None
    retry_after_seconds: float | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "call_id": self.call_id,
            "attempt": self.attempt,
            "status": self.status,
            "elapsed_seconds": self.elapsed_seconds,
            "failure_code": self.failure_code,
            "reason": self.reason,
            "retry_after_seconds": self.retry_after_seconds,
        }


@dataclass(frozen=True)
class _TerminalAttemptContext:
    call_id: str
    attempt: int
    started_at: float
    on_event: Callable[[DesktopTerminalModelEvent], None]


class DesktopTerminalModelGateway:
    """Run a Model Call without a first-byte, read, reasoning, or total deadline."""

    def __init__(
        self,
        transport: TerminalModelTransport,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._transport = transport
        self._clock = clock
        self._sleep = sleep

    def analyze(
        self,
        request: DesktopModelRequest,
        *,
        on_event: Callable[[DesktopTerminalModelEvent], None],
        is_cancelled: CancellationCallback | None = None,
    ) -> DesktopModelResult:
        return self._run(
            request,
            on_event=on_event,
            attempt_call=lambda _context, on_request_sent: (
                self._call_without_response_deadline(
                    lambda: self._call_terminal_transport(request, on_request_sent),
                    is_cancelled=is_cancelled,
                )
            ),
            is_cancelled=is_cancelled,
        )

    def stream(
        self,
        request: DesktopModelRequest,
        *,
        on_event: Callable[[DesktopTerminalModelEvent], None],
        on_delta: Callable[[int, str], None],
        is_cancelled: CancellationCallback | None = None,
    ) -> DesktopModelResult:
        """Stream provider output while lifecycle events retain no output content."""
        lifecycle_stream_transport = getattr(
            self._transport, "stream_until_terminal_with_lifecycle", None
        )
        stream_transport = getattr(self._transport, "stream_until_terminal", None)
        if not callable(lifecycle_stream_transport) and not callable(stream_transport):
            result = self.analyze(request, on_event=on_event, is_cancelled=is_cancelled)
            on_delta(result.attempt_count, result.content)
            return result

        def call_stream(
            context: _TerminalAttemptContext,
            on_request_sent: RequestSentCallback,
        ) -> object:
            active = threading.Event()
            active.set()

            def emit(delta: str) -> None:
                if not active.is_set() or not delta or _is_cancelled(is_cancelled):
                    return
                on_delta(context.attempt, delta)
                self._emit(context, "model_output_activity")

            try:
                return self._call_without_response_deadline(
                    lambda: self._call_terminal_stream_transport(
                        request,
                        emit,
                        on_request_sent,
                        lifecycle_stream_transport=lifecycle_stream_transport,
                        stream_transport=stream_transport,
                    ),
                    is_cancelled=is_cancelled,
                )
            finally:
                active.clear()

        return self._run(
            request,
            on_event=on_event,
            attempt_call=call_stream,
            is_cancelled=is_cancelled,
        )

    def _run(
        self,
        request: DesktopModelRequest,
        *,
        on_event: Callable[[DesktopTerminalModelEvent], None],
        attempt_call: Callable[[_TerminalAttemptContext, RequestSentCallback], object],
        is_cancelled: CancellationCallback | None,
    ) -> DesktopModelResult:
        call_id = uuid.uuid4().hex
        started_at = self._clock()
        for attempt in range(1, MAX_TERMINAL_MODEL_ATTEMPTS + 1):
            context = _TerminalAttemptContext(call_id, attempt, started_at, on_event)
            self._raise_if_cancelled(is_cancelled, context)
            self._emit(context, "queued")
            try:
                release_attempt = _prepare_terminal_model_attempt(self._transport, is_cancelled)
            except DesktopModelCancelledError:
                self._emit(context, "cancelled")
                raise
            if _is_cancelled(is_cancelled):
                if release_attempt is not None:
                    release_attempt()
                self._emit(context, "cancelled")
                raise DesktopModelCancelledError()
            self._emit(context, "connecting")
            attempt_active = threading.Event()
            attempt_active.set()
            request_sent = threading.Event()
            request_sent_lock = threading.Lock()

            def on_request_sent() -> None:
                with request_sent_lock:
                    if request_sent.is_set() or not attempt_active.is_set():
                        return
                    request_sent.set()
                    self._emit(context, "awaiting_model_result")

            try:
                try:
                    response = attempt_call(context, on_request_sent)
                    on_request_sent()
                    if not isinstance(response, str) or not response.strip():
                        raise ValueError("The model response must contain text.")
                finally:
                    attempt_active.clear()
                    if release_attempt is not None:
                        release_attempt()
            except DesktopModelCancelledError:
                self._emit(context, "cancelled")
                raise
            except Exception as error:
                failure = classify_terminal_model_error(error)
                retry_after = _retry_after_seconds(error)
                self._emit_failure(
                    context,
                    failure,
                    retry_after_seconds=retry_after,
                )
                if failure.retryable and attempt < MAX_TERMINAL_MODEL_ATTEMPTS:
                    self._emit(
                        context,
                        "retrying",
                        failure_code=failure.code,
                        reason=failure.reason,
                        retry_after_seconds=retry_after,
                    )
                    try:
                        self._wait_for_retry(
                            retry_after,
                            attempt=attempt,
                            is_cancelled=is_cancelled,
                        )
                    except DesktopModelCancelledError:
                        self._emit(context, "cancelled")
                        raise
                    continue
                raise DesktopModelCallError(call_id, failure, attempt) from error
            self._raise_if_cancelled(is_cancelled, context)
            self._emit(context, "completed")
            return DesktopModelResult(call_id=call_id, content=response, attempt_count=attempt)
        raise AssertionError("Terminal Model Call exhausted attempts without a result.")

    def _call_without_response_deadline(
        self,
        call: Callable[[], object],
        *,
        is_cancelled: CancellationCallback | None,
    ) -> object:
        completed = threading.Event()
        outcome: dict[str, object] = {}

        def invoke() -> None:
            try:
                outcome["response"] = call()
            except Exception as error:
                outcome["error"] = error
            finally:
                completed.set()

        threading.Thread(
            target=invoke,
            daemon=True,
            name="openkb-terminal-model-attempt",
        ).start()
        while not completed.wait(0.05):
            if _is_cancelled(is_cancelled):
                raise DesktopModelCancelledError()
        if _is_cancelled(is_cancelled):
            raise DesktopModelCancelledError()
        error = outcome.get("error")
        if isinstance(error, Exception):
            raise error
        return outcome.get("response")

    def _call_terminal_transport(
        self,
        request: DesktopModelRequest,
        on_request_sent: RequestSentCallback,
    ) -> object:
        lifecycle_call = getattr(self._transport, "call_until_terminal_with_lifecycle", None)
        if callable(lifecycle_call):
            return lifecycle_call(
                request,
                MODEL_CONNECT_TIMEOUT_SECONDS,
                on_request_sent,
            )
        call_until_terminal = getattr(self._transport, "call_until_terminal", None)
        on_request_sent()
        if callable(call_until_terminal):
            return call_until_terminal(request, MODEL_CONNECT_TIMEOUT_SECONDS)
        return self._transport(request, MODEL_CONNECT_TIMEOUT_SECONDS)

    def _call_terminal_stream_transport(
        self,
        request: DesktopModelRequest,
        on_delta: Callable[[str], None],
        on_request_sent: RequestSentCallback,
        *,
        lifecycle_stream_transport: object,
        stream_transport: object,
    ) -> object:
        if callable(lifecycle_stream_transport):
            return lifecycle_stream_transport(
                request,
                MODEL_CONNECT_TIMEOUT_SECONDS,
                on_delta,
                on_request_sent,
            )
        if not callable(stream_transport):
            raise TypeError("Terminal stream transport is not callable.")
        on_request_sent()
        return stream_transport(request, MODEL_CONNECT_TIMEOUT_SECONDS, on_delta)

    def _wait_for_retry(
        self,
        retry_after_seconds: float | None,
        *,
        attempt: int,
        is_cancelled: CancellationCallback | None,
    ) -> None:
        remaining = (
            retry_after_seconds
            or _TERMINAL_RETRY_BACKOFF_SECONDS[
                min(attempt - 1, len(_TERMINAL_RETRY_BACKOFF_SECONDS) - 1)
            ]
        )
        while remaining > 0:
            if _is_cancelled(is_cancelled):
                raise DesktopModelCancelledError()
            interval = min(0.05, remaining)
            self._sleep(interval)
            remaining -= interval

    def _raise_if_cancelled(
        self,
        is_cancelled: CancellationCallback | None,
        context: _TerminalAttemptContext,
    ) -> None:
        if _is_cancelled(is_cancelled):
            self._emit(context, "cancelled")
            raise DesktopModelCancelledError()

    def _emit(
        self,
        context: _TerminalAttemptContext,
        status: TerminalModelCallStatus,
        *,
        failure_code: str | None = None,
        reason: str | None = None,
        retry_after_seconds: float | None = None,
    ) -> None:
        context.on_event(
            DesktopTerminalModelEvent(
                call_id=context.call_id,
                attempt=context.attempt,
                status=status,
                elapsed_seconds=max(0.0, self._clock() - context.started_at),
                failure_code=failure_code,
                reason=reason,
                retry_after_seconds=retry_after_seconds,
            )
        )

    def _emit_failure(
        self,
        context: _TerminalAttemptContext,
        failure: DesktopModelFailure,
        *,
        retry_after_seconds: float | None,
    ) -> None:
        self._emit(
            context,
            (
                "network_failure"
                if failure.code == "model_network_transient"
                else "provider_failure"
            ),
            failure_code=failure.code,
            reason=failure.reason,
            retry_after_seconds=retry_after_seconds,
        )


def classify_terminal_model_error(error: Exception) -> DesktopModelFailure:
    """Classify explicit failures without turning elapsed model work into a timeout."""
    if isinstance(error, DesktopModelTransportError):
        if error.category in {"network", "timeout", "network_timeout"}:
            return _NETWORK_FAILURE
        if error.category in {"provider", "provider_timeout"}:
            return _PROVIDER_FAILURE
    if isinstance(error, TimeoutError):
        return _NETWORK_FAILURE
    status_code = getattr(error, "status_code", None)
    if status_code == 408:
        return _PROVIDER_FAILURE
    return classify_model_error(error)


def _retry_after_seconds(error: Exception) -> float | None:
    if not isinstance(error, DesktopModelTransportError):
        return None
    value = error.retry_after_seconds
    if not isinstance(value, (int, float)) or not math.isfinite(float(value)) or value <= 0:
        return None
    return float(value)


def _is_cancelled(callback: CancellationCallback | None) -> bool:
    return callback is not None and callback()


def _prepare_terminal_model_attempt(
    transport: object, is_cancelled: CancellationCallback | None
) -> AttemptRelease | None:
    prepare = getattr(transport, "prepare_terminal_model_attempt", None)
    if callable(prepare):
        prepared = prepare(is_cancelled)
        if callable(prepared):
            return prepared
        release = getattr(transport, "release_prepared_model_attempt", None)
        if callable(release):
            return release
    return None
