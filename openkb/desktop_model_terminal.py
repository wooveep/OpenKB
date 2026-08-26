"""Model Calls that end only on explicit provider, network, or user events."""

from __future__ import annotations

import math
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from queue import Empty, SimpleQueue
from typing import Literal

from openkb.desktop_model_gateway import (
    DesktopModelCallError,
    DesktopModelCancelledError,
    DesktopModelFailure,
    DesktopModelGateway,
    DesktopModelOutputObservations,
    DesktopModelRequest,
    DesktopModelResult,
    DesktopModelResultError,
    DesktopModelTransportError,
    DesktopProviderStreamEvent,
    DesktopProviderTokenUsage,
    ExecutionLane,
    classify_model_error,
    require_execution_lane,
)

MODEL_CONNECT_TIMEOUT_SECONDS = 30.0
MAX_TERMINAL_MODEL_ATTEMPTS = 3
_TERMINAL_RETRY_BACKOFF_SECONDS = (1.0, 2.0)

TerminalModelCallStatus = Literal[
    "queued",
    "connecting",
    "awaiting_model_result",
    "reasoning_output_activity",
    "model_output_activity",
    "validating",
    "completed",
    "retrying",
    "cancelled",
    "provider_failure",
    "network_failure",
    "model_result_failure",
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
    operation: str = "unknown"
    model_role: str = "default"
    provider_name: str = "scripted"
    model_name: str = "unknown"
    execution_lane: ExecutionLane = "background"
    finish_reason: str | None = None
    reasoning_observed: bool | None = None
    final_content_observed: bool | None = None
    reasoning_chunk_count: int | None = None
    final_chunk_count: int | None = None
    reasoning_character_count: int | None = None
    final_character_count: int | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None
    provider_request_id: str | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "call_id": self.call_id,
            "attempt": self.attempt,
            "status": self.status,
            "elapsed_seconds": self.elapsed_seconds,
            "failure_code": self.failure_code,
            "reason": self.reason,
            "retry_after_seconds": self.retry_after_seconds,
            "operation": self.operation,
            "model_role": self.model_role,
            "provider": self.provider_name,
            "model_name": self.model_name,
            "execution_lane": self.execution_lane,
            "attempt_id": f"{self.call_id}:{self.attempt}",
            "finish_reason": self.finish_reason,
            "reasoning_observed": self.reasoning_observed,
            "final_content_observed": self.final_content_observed,
            "reasoning_chunk_count": self.reasoning_chunk_count,
            "final_chunk_count": self.final_chunk_count,
            "reasoning_character_count": self.reasoning_character_count,
            "final_character_count": self.final_character_count,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "total_tokens": self.total_tokens,
            "provider_request_id": self.provider_request_id,
        }


@dataclass(frozen=True)
class _TerminalAttemptContext:
    call_id: str
    attempt: int
    started_at: float
    on_event: Callable[[DesktopTerminalModelEvent], None]
    operation: str
    model_role: str
    provider_name: str
    model_name: str
    execution_lane: ExecutionLane


class DesktopTerminalModelGateway(DesktopModelGateway):
    """Run a Model Call without a first-byte, read, reasoning, or total deadline."""

    def __init__(
        self,
        transport: TerminalModelTransport,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        provider_name: str = "scripted",
        model_name: str = "scripted",
    ) -> None:
        self._transport = transport
        self._clock = clock
        self._sleep = sleep
        self._provider_name = provider_name
        self._model_name = model_name

    @property
    def provider_name(self) -> str:
        return self._provider_name

    @property
    def model_name(self) -> str:
        return self._model_name

    def for_lane(self, lane: ExecutionLane) -> DesktopTerminalModelGateway:
        """Return a gateway bound to a named transport lane when supported."""
        lane = require_execution_lane(lane)
        select_lane = getattr(self._transport, "for_lane", None)
        if not callable(select_lane):
            return self
        return DesktopTerminalModelGateway(
            select_lane(lane),
            clock=self._clock,
            sleep=self._sleep,
            provider_name=self._provider_name,
            model_name=self._model_name,
        )

    def analyze(
        self,
        request: DesktopModelRequest,
        *,
        on_event: Callable[[DesktopTerminalModelEvent], None],
        is_cancelled: CancellationCallback | None = None,
    ) -> DesktopModelResult:
        """Prefer a provider stream so structured work exposes real output activity."""
        if request.supports_streaming is not False and self._streaming_transport_available():
            return self.stream(
                request,
                on_event=on_event,
                on_delta=lambda _attempt, _delta: None,
                is_cancelled=is_cancelled,
            )
        return self._analyze_non_streaming(
            request,
            on_event=on_event,
            is_cancelled=is_cancelled,
        )

    def analyze_once(
        self,
        request: DesktopModelRequest,
        *,
        on_event: Callable[[DesktopTerminalModelEvent], None],
        is_cancelled: CancellationCallback | None = None,
    ) -> DesktopModelResult:
        """Execute one physical attempt without imposing a response deadline."""
        return self._analyze_non_streaming(
            request,
            on_event=on_event,
            is_cancelled=is_cancelled,
            max_attempts=1,
        )

    def stream(
        self,
        request: DesktopModelRequest,
        *,
        on_event: Callable[[DesktopTerminalModelEvent], None],
        on_delta: Callable[[int, str], None],
        on_reset: Callable[[int], None] | None = None,
        is_cancelled: CancellationCallback | None = None,
    ) -> DesktopModelResult:
        """Stream provider output while lifecycle events retain no output content."""
        lifecycle_stream_transport = getattr(
            self._transport, "stream_until_terminal_with_lifecycle", None
        )
        stream_transport = getattr(self._transport, "stream_until_terminal", None)
        if not callable(lifecycle_stream_transport) and not callable(stream_transport):
            result = self._analyze_non_streaming(
                request,
                on_event=on_event,
                is_cancelled=is_cancelled,
            )
            on_delta(result.attempt_count, result.content)
            return result

        def call_stream(
            context: _TerminalAttemptContext,
            on_request_sent: RequestSentCallback,
            flush_request_sent: RequestSentCallback,
        ) -> object:
            active = threading.Event()
            active.set()
            activity_emitted = False
            pending_deltas: SimpleQueue[object] = SimpleQueue()
            reasoning_activity_emitted = False

            def queue_delta(delta: object) -> None:
                if not active.is_set() or not delta or _is_cancelled(is_cancelled):
                    return
                pending_deltas.put(delta)

            def flush_stream_activity() -> None:
                nonlocal activity_emitted, reasoning_activity_emitted
                flush_request_sent()
                while True:
                    try:
                        provider_event = pending_deltas.get_nowait()
                    except Empty:
                        return
                    if not active.is_set():
                        continue
                    event = (
                        provider_event
                        if isinstance(provider_event, DesktopProviderStreamEvent)
                        else DesktopProviderStreamEvent(final_content=str(provider_event))
                    )
                    if event.reasoning_character_count and not reasoning_activity_emitted:
                        self._emit(context, "reasoning_output_activity")
                        reasoning_activity_emitted = True
                    if event.final_content:
                        on_delta(context.attempt, event.final_content)
                        if not activity_emitted:
                            self._emit(context, "model_output_activity")
                            activity_emitted = True

            try:
                self._prepare_active_stream(request)
                return self._call_without_response_deadline(
                    lambda: self._call_terminal_stream_transport(
                        request,
                        queue_delta,
                        on_request_sent,
                        lifecycle_stream_transport=lifecycle_stream_transport,
                        stream_transport=stream_transport,
                    ),
                    is_cancelled=is_cancelled,
                    on_wait=flush_stream_activity,
                    on_cancel=lambda: self._cancel_active_stream(request),
                )
            finally:
                active.clear()

        return self._run(
            request,
            on_event=on_event,
            attempt_call=call_stream,
            is_cancelled=is_cancelled,
            on_retry=on_reset,
        )

    def _streaming_transport_available(self) -> bool:
        return callable(
            getattr(self._transport, "stream_until_terminal_with_lifecycle", None)
        ) or callable(getattr(self._transport, "stream_until_terminal", None))

    def _analyze_non_streaming(
        self,
        request: DesktopModelRequest,
        *,
        on_event: Callable[[DesktopTerminalModelEvent], None],
        is_cancelled: CancellationCallback | None,
        max_attempts: int = MAX_TERMINAL_MODEL_ATTEMPTS,
    ) -> DesktopModelResult:
        return self._run(
            request,
            on_event=on_event,
            attempt_call=lambda _context, on_request_sent, flush_request_sent: (
                self._call_without_response_deadline(
                    lambda: self._call_terminal_transport(request, on_request_sent),
                    is_cancelled=is_cancelled,
                    on_wait=flush_request_sent,
                )
            ),
            is_cancelled=is_cancelled,
            on_retry=None,
            max_attempts=max_attempts,
        )

    def _run(
        self,
        request: DesktopModelRequest,
        *,
        on_event: Callable[[DesktopTerminalModelEvent], None],
        attempt_call: Callable[
            [_TerminalAttemptContext, RequestSentCallback, RequestSentCallback], object
        ],
        is_cancelled: CancellationCallback | None,
        on_retry: Callable[[int], None] | None,
        max_attempts: int = MAX_TERMINAL_MODEL_ATTEMPTS,
    ) -> DesktopModelResult:
        call_id = uuid.uuid4().hex
        started_at = self._clock()
        for attempt in range(1, max_attempts + 1):
            context = _TerminalAttemptContext(
                call_id,
                attempt,
                started_at,
                on_event,
                request.operation,
                request.model_role,
                self._provider_name,
                request.model_name or self._model_name,
                request.execution_lane,
            )
            self._raise_if_cancelled(is_cancelled, context)
            self._emit(context, "queued")
            try:
                release_attempt = _prepare_terminal_model_attempt(
                    self._transport, request, is_cancelled
                )
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
            request_sent_emitted = threading.Event()
            request_sent_lock = threading.Lock()
            request_sent_elapsed: list[float | None] = [None]

            def on_request_sent() -> None:
                with request_sent_lock:
                    if not request_sent.is_set():
                        request_sent_elapsed[0] = max(0.0, self._clock() - context.started_at)
                        request_sent.set()

            def flush_request_sent() -> None:
                with request_sent_lock:
                    if (
                        not request_sent.is_set()
                        or request_sent_emitted.is_set()
                        or not attempt_active.is_set()
                    ):
                        return
                    request_sent_emitted.set()
                    self._emit(
                        context,
                        "awaiting_model_result",
                        elapsed_seconds=request_sent_elapsed[0],
                    )

            response: object | None = None
            try:
                try:
                    response = attempt_call(context, on_request_sent, flush_request_sent)
                    on_request_sent()
                    flush_request_sent()
                    if not isinstance(response, str):
                        raise ValueError("The model response must contain text.")
                    observations = getattr(
                        response,
                        "observations",
                        DesktopModelOutputObservations(
                            final_content_observed=bool(response.strip()),
                            final_chunk_count=1 if response.strip() else 0,
                            final_character_count=len(response) if response.strip() else 0,
                        ),
                    )
                    if not response.strip():
                        raise DesktopModelResultError(observations)
                finally:
                    attempt_active.clear()
                    if release_attempt is not None:
                        release_attempt()
            except DesktopModelCancelledError:
                self._emit(context, "cancelled")
                raise
            except Exception as error:
                failure = classify_terminal_model_error(error)
                observations = getattr(error, "observations", None)
                retry_after = _retry_after_seconds(error)
                self._emit_failure(
                    context,
                    failure,
                    retry_after_seconds=retry_after,
                    observations=observations,
                    usage=getattr(response, "usage", None),
                    provider_request_id=getattr(response, "provider_request_id", None),
                )
                if failure.retryable and attempt < max_attempts:
                    self._emit(
                        context,
                        "retrying",
                        failure_code=failure.code,
                        reason=failure.reason,
                        retry_after_seconds=retry_after,
                    )
                    if on_retry is not None:
                        on_retry(attempt + 1)
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
                raise DesktopModelCallError(
                    call_id,
                    failure,
                    attempt,
                    observations=observations,
                    usage=getattr(response, "usage", None),
                    provider_request_id=getattr(response, "provider_request_id", None),
                ) from error
            self._raise_if_cancelled(is_cancelled, context)
            observations = getattr(response, "observations", None)
            self._emit(context, "validating", observations=observations)
            self._emit(
                context,
                "completed",
                observations=observations,
                usage=getattr(response, "usage", None),
                provider_request_id=getattr(response, "provider_request_id", None),
            )
            return DesktopModelResult(
                call_id=call_id,
                content=response,
                attempt_count=attempt,
                usage=getattr(response, "usage", None),
                provider_request_id=getattr(response, "provider_request_id", None),
                observations=observations,
            )
        raise AssertionError("Terminal Model Call exhausted attempts without a result.")

    def _call_without_response_deadline(
        self,
        call: Callable[[], object],
        *,
        is_cancelled: CancellationCallback | None,
        on_wait: Callable[[], None] | None = None,
        on_cancel: Callable[[], None] | None = None,
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
            if on_wait is not None:
                on_wait()
            if _is_cancelled(is_cancelled):
                if on_cancel is not None:
                    on_cancel()
                raise DesktopModelCancelledError()
        if on_wait is not None:
            on_wait()
        if _is_cancelled(is_cancelled):
            raise DesktopModelCancelledError()
        error = outcome.get("error")
        if isinstance(error, Exception):
            raise error
        return outcome.get("response")

    def _cancel_active_stream(self, request: DesktopModelRequest) -> None:
        cancel = getattr(self._transport, "cancel_active_stream", None)
        if callable(cancel):
            cancel(request)

    def _prepare_active_stream(self, request: DesktopModelRequest) -> None:
        prepare = getattr(self._transport, "prepare_active_stream", None)
        if callable(prepare):
            prepare(request)

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
        elapsed_seconds: float | None = None,
        observations: DesktopModelOutputObservations | None = None,
        usage: DesktopProviderTokenUsage | None = None,
        provider_request_id: str | None = None,
    ) -> None:
        context.on_event(
            DesktopTerminalModelEvent(
                call_id=context.call_id,
                attempt=context.attempt,
                status=status,
                elapsed_seconds=(
                    max(0.0, self._clock() - context.started_at)
                    if elapsed_seconds is None
                    else elapsed_seconds
                ),
                failure_code=failure_code,
                reason=reason,
                retry_after_seconds=retry_after_seconds,
                operation=context.operation,
                model_role=context.model_role,
                provider_name=context.provider_name,
                model_name=context.model_name,
                execution_lane=context.execution_lane,
                finish_reason=observations.finish_reason if observations is not None else None,
                reasoning_observed=(
                    observations.reasoning_observed if observations is not None else None
                ),
                final_content_observed=(
                    observations.final_content_observed if observations is not None else None
                ),
                reasoning_chunk_count=(
                    observations.reasoning_chunk_count if observations is not None else None
                ),
                final_chunk_count=(
                    observations.final_chunk_count if observations is not None else None
                ),
                reasoning_character_count=(
                    observations.reasoning_character_count if observations is not None else None
                ),
                final_character_count=(
                    observations.final_character_count if observations is not None else None
                ),
                input_tokens=usage.input_tokens if usage is not None else None,
                output_tokens=usage.output_tokens if usage is not None else None,
                total_tokens=usage.total_tokens if usage is not None else None,
                provider_request_id=provider_request_id,
            )
        )

    def _emit_failure(
        self,
        context: _TerminalAttemptContext,
        failure: DesktopModelFailure,
        *,
        retry_after_seconds: float | None,
        observations: DesktopModelOutputObservations | None = None,
        usage: DesktopProviderTokenUsage | None = None,
        provider_request_id: str | None = None,
    ) -> None:
        self._emit(
            context,
            (
                "model_result_failure"
                if failure.code
                in {
                    "empty_final_result",
                    "reasoning_only_result",
                    "reasoning_output_exhausted",
                }
                else "network_failure"
                if failure.code == "model_network_transient"
                else "provider_failure"
            ),
            failure_code=failure.code,
            reason=failure.reason,
            retry_after_seconds=retry_after_seconds,
            observations=observations,
            usage=usage,
            provider_request_id=provider_request_id,
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
    transport: object,
    request: DesktopModelRequest,
    is_cancelled: CancellationCallback | None,
) -> AttemptRelease | None:
    prepare_request = getattr(transport, "prepare_terminal_model_request", None)
    if callable(prepare_request):
        prepared = prepare_request(request, is_cancelled)
        if callable(prepared):
            return prepared
    prepare = getattr(transport, "prepare_terminal_model_attempt", None)
    if callable(prepare):
        prepared = prepare(is_cancelled)
        if callable(prepared):
            return prepared
        release = getattr(transport, "release_prepared_model_attempt", None)
        if callable(release):
            return release
    return None
