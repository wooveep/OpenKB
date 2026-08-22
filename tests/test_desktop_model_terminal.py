"""Contract checks for Model Calls that wait for explicit terminal events."""

from __future__ import annotations

import http.server
import threading

import pytest

from openkb import desktop_model_transport
from openkb.config import LlmCredentialBundle
from openkb.desktop_model_gateway import (
    DesktopModelCallError,
    DesktopModelCancelledError,
    DesktopModelRequest,
    DesktopModelTransportError,
)
from openkb.desktop_model_settings import DesktopModelSettings
from openkb.desktop_model_terminal import (
    MAX_TERMINAL_MODEL_ATTEMPTS,
    MODEL_CONNECT_TIMEOUT_SECONDS,
    DesktopTerminalModelEvent,
    DesktopTerminalModelGateway,
    TerminalModelCallStatus,
)


class FakeClock:
    def __init__(self) -> None:
        self.value = 0.0

    def __call__(self) -> float:
        return self.value


class HttpFailure(RuntimeError):
    def __init__(self, status_code: int) -> None:
        super().__init__(f"HTTP {status_code}")
        self.status_code = status_code


def test_terminal_gateway_accepts_a_result_after_180_seconds_of_virtual_silence() -> None:
    """Thinking and generation time never become a response deadline."""
    clock = FakeClock()
    calls: list[float] = []
    events: list[DesktopTerminalModelEvent] = []

    class SilentProvider:
        def __call__(self, _request: DesktopModelRequest, _connect_timeout_seconds: float) -> str:
            raise AssertionError("The lifecycle-aware terminal seam must be used.")

        def call_until_terminal_with_lifecycle(
            self,
            _request: DesktopModelRequest,
            connect_timeout_seconds: float,
            on_request_sent,
        ) -> str:
            calls.append(connect_timeout_seconds)
            clock.value += 1
            on_request_sent()
            clock.value += 180
            return "OK"

    result = DesktopTerminalModelGateway(SilentProvider(), clock=clock).analyze(
        DesktopModelRequest("connection_test", "Model settings", "Reply with OK."),
        on_event=events.append,
    )

    assert result.content == "OK"
    assert result.attempt_count == 1
    assert calls == [MODEL_CONNECT_TIMEOUT_SECONDS]
    assert [event.status for event in events] == [
        "queued",
        "connecting",
        "awaiting_model_result",
        "completed",
    ]
    assert [event.elapsed_seconds for event in events] == [0, 0, 1, 181]


@pytest.mark.parametrize("model", ("openai/test-model", "deepseek/test-model"))
def test_litellm_marks_request_awaiting_before_response_headers_arrive(model: str) -> None:
    request_body_received = threading.Event()
    release_response = threading.Event()
    awaiting_result = threading.Event()
    finished = threading.Event()
    events: list[DesktopTerminalModelEvent] = []
    errors: list[Exception] = []
    results: list[str] = []

    class SilentResponseHandler(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_POST(self) -> None:
            content_length = int(self.headers.get("content-length", "0"))
            self.rfile.read(content_length)
            request_body_received.set()
            release_response.wait(timeout=2)
            body = (
                b'data: {"id":"test","object":"chat.completion.chunk","created":1,'
                b'"model":"test-model","choices":[{"index":0,"delta":'
                b'{"content":"OK"},"finish_reason":null}]}\n\n'
                b"data: [DONE]\n\n"
            )
            self.send_response(200)
            self.send_header("content-type", "text/event-stream")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, _format: str, *args: object) -> None:
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), SilentResponseHandler)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    gateway = DesktopTerminalModelGateway(
        desktop_model_transport.DesktopLiteLLMTransport(
            model=model,
            bundle=LlmCredentialBundle(
                api_key="test-key",
                base_url=f"http://127.0.0.1:{server.server_port}/v1",
            ),
        )
    )

    def capture_event(event: DesktopTerminalModelEvent) -> None:
        events.append(event)
        if event.status == "awaiting_model_result":
            awaiting_result.set()

    def run() -> None:
        try:
            result = gateway.stream(
                DesktopModelRequest("connection_test", "Model settings", "Reply with OK."),
                on_event=capture_event,
                on_delta=lambda _attempt, _delta: None,
            )
            results.append(result.content)
        except Exception as error:
            errors.append(error)
        finally:
            finished.set()

    worker = threading.Thread(target=run)
    worker.start()
    try:
        assert request_body_received.wait(timeout=5)
        assert awaiting_result.wait(timeout=0.5)
        assert not finished.is_set()
        assert [event.status for event in events] == [
            "queued",
            "connecting",
            "awaiting_model_result",
        ]
    finally:
        release_response.set()
        worker.join(timeout=2)
        server.shutdown()
        server.server_close()
        server_thread.join(timeout=1)

    assert errors == []
    assert results == ["OK"]


def test_terminal_gateway_retries_temporary_provider_failures_three_total_attempts() -> None:
    """Explicit failures retry by count even when the elapsed time is already large."""
    clock = FakeClock()
    calls = 0
    events: list[DesktopTerminalModelEvent] = []

    def unavailable_provider(_request: DesktopModelRequest, _connect_timeout_seconds: float) -> str:
        nonlocal calls
        calls += 1
        clock.value += 100
        raise DesktopModelTransportError("server")

    with pytest.raises(DesktopModelCallError) as captured:
        DesktopTerminalModelGateway(
            unavailable_provider,
            clock=clock,
            sleep=lambda _seconds: None,
        ).analyze(
            DesktopModelRequest("connection_test", "Model settings", "Reply with OK."),
            on_event=events.append,
        )

    assert calls == MAX_TERMINAL_MODEL_ATTEMPTS == 3
    assert captured.value.attempt_count == 3
    assert captured.value.failure.code == "model_server_error"
    assert [event.status for event in events] == [
        "queued",
        "connecting",
        "awaiting_model_result",
        "provider_failure",
        "retrying",
        "queued",
        "connecting",
        "awaiting_model_result",
        "provider_failure",
        "retrying",
        "queued",
        "connecting",
        "awaiting_model_result",
        "provider_failure",
    ]


@pytest.mark.parametrize(
    ("error_factory", "failure_code", "failure_status", "attempt_count"),
    (
        (TimeoutError, "model_network_transient", "network_failure", 3),
        (ConnectionError, "model_network_transient", "network_failure", 3),
        (lambda: HttpFailure(408), "model_provider_failure", "provider_failure", 3),
        (lambda: HttpFailure(429), "model_rate_limited", "provider_failure", 3),
        (lambda: HttpFailure(503), "model_server_error", "provider_failure", 3),
        (lambda: HttpFailure(504), "model_server_error", "provider_failure", 3),
        (
            lambda: HttpFailure(401),
            "model_authentication_failed",
            "provider_failure",
            1,
        ),
    ),
)
def test_terminal_gateway_classifies_explicit_failures_without_a_deadline_timeout(
    error_factory,
    failure_code: str,
    failure_status: TerminalModelCallStatus,
    attempt_count: int,
) -> None:
    calls = 0
    events: list[DesktopTerminalModelEvent] = []

    def failing_provider(_request: DesktopModelRequest, _connect_timeout_seconds: float) -> str:
        nonlocal calls
        calls += 1
        raise error_factory()

    with pytest.raises(DesktopModelCallError) as captured:
        DesktopTerminalModelGateway(
            failing_provider,
            sleep=lambda _seconds: None,
        ).analyze(
            DesktopModelRequest("connection_test", "Model settings", "Reply with OK."),
            on_event=events.append,
        )

    assert calls == attempt_count
    assert captured.value.attempt_count == attempt_count
    assert captured.value.failure.code == failure_code
    assert [event.status for event in events].count(failure_status) == attempt_count
    assert all(event.failure_code != "model_timeout" for event in events)


def test_terminal_gateway_honors_retry_after_without_a_total_elapsed_budget() -> None:
    clock = FakeClock()
    calls = 0
    sleeps: list[float] = []
    events: list[DesktopTerminalModelEvent] = []

    def rate_limited_provider(
        _request: DesktopModelRequest, _connect_timeout_seconds: float
    ) -> str:
        nonlocal calls
        calls += 1
        if calls < 3:
            raise DesktopModelTransportError("rate_limited", retry_after_seconds=75)
        return "OK"

    def advance(seconds: float) -> None:
        sleeps.append(seconds)
        clock.value += seconds

    result = DesktopTerminalModelGateway(
        rate_limited_provider,
        clock=clock,
        sleep=advance,
    ).analyze(
        DesktopModelRequest("connection_test", "Model settings", "Reply with OK."),
        on_event=events.append,
    )

    assert result.attempt_count == 3
    assert sum(sleeps) == pytest.approx(150)
    assert [event.retry_after_seconds for event in events if event.status == "retrying"] == [
        75,
        75,
    ]


def test_terminal_gateway_uses_bounded_backoff_without_retry_after() -> None:
    calls = 0
    sleeps: list[float] = []

    def unavailable_provider(_request: DesktopModelRequest, _connect_timeout_seconds: float) -> str:
        nonlocal calls
        calls += 1
        if calls < MAX_TERMINAL_MODEL_ATTEMPTS:
            raise DesktopModelTransportError("server")
        return "OK"

    result = DesktopTerminalModelGateway(
        unavailable_provider,
        sleep=sleeps.append,
    ).analyze(
        DesktopModelRequest("connection_test", "Model settings", "Reply with OK."),
        on_event=lambda _event: None,
    )

    assert result.attempt_count == MAX_TERMINAL_MODEL_ATTEMPTS
    assert sum(sleeps) == pytest.approx(3.0)


def test_terminal_gateway_user_cancel_ends_an_active_provider_wait() -> None:
    provider_started = threading.Event()
    release_provider = threading.Event()
    cancel = threading.Event()
    finished = threading.Event()
    events: list[DesktopTerminalModelEvent] = []
    errors: list[Exception] = []

    def blocking_provider(_request: DesktopModelRequest, _connect_timeout_seconds: float) -> str:
        provider_started.set()
        release_provider.wait()
        return "late"

    def run() -> None:
        try:
            DesktopTerminalModelGateway(blocking_provider).analyze(
                DesktopModelRequest("connection_test", "Model settings", "Reply with OK."),
                on_event=events.append,
                is_cancelled=cancel.is_set,
            )
        except Exception as error:
            errors.append(error)
        finally:
            finished.set()

    worker = threading.Thread(target=run)
    worker.start()
    assert provider_started.wait(timeout=1)
    cancel.set()
    try:
        assert finished.wait(timeout=0.5)
    finally:
        release_provider.set()
        worker.join(timeout=1)

    assert len(errors) == 1
    assert isinstance(errors[0], DesktopModelCancelledError)
    assert [event.status for event in events][-1] == "cancelled"
    assert "completed" not in [event.status for event in events]


def test_cancelled_terminal_attempt_releases_its_concurrency_slot_immediately() -> None:
    gate = desktop_model_transport._DesktopModelConcurrencyGate(1)
    first_started = threading.Event()
    release_first = threading.Event()
    first_cancelled = threading.Event()
    first_finished = threading.Event()
    second_finished = threading.Event()
    errors: list[Exception] = []
    results: list[str] = []

    def provider(request: DesktopModelRequest, _connect_timeout_seconds: float) -> str:
        if request.content == "first":
            first_started.set()
            release_first.wait()
            return "late"
        return "second completed"

    gateway = DesktopTerminalModelGateway(
        desktop_model_transport._ConcurrentDesktopModelTransport(provider, gate)
    )

    def run_first() -> None:
        try:
            gateway.analyze(
                DesktopModelRequest("connection_test", "Model settings", "first"),
                on_event=lambda _event: None,
                is_cancelled=first_cancelled.is_set,
            )
        except Exception as error:
            errors.append(error)
        finally:
            first_finished.set()

    def run_second() -> None:
        result = gateway.analyze(
            DesktopModelRequest("connection_test", "Model settings", "second"),
            on_event=lambda _event: None,
        )
        results.append(result.content)
        second_finished.set()

    first_worker = threading.Thread(target=run_first)
    second_worker = threading.Thread(target=run_second)
    first_worker.start()
    assert first_started.wait(timeout=1)
    first_cancelled.set()
    assert first_finished.wait(timeout=0.5)

    second_worker.start()
    try:
        assert second_finished.wait(timeout=0.5)
    finally:
        release_first.set()
        first_worker.join(timeout=1)
        second_worker.join(timeout=1)

    assert len(errors) == 1
    assert isinstance(errors[0], DesktopModelCancelledError)
    assert results == ["second completed"]


def test_terminal_gateway_reports_stream_activity_without_putting_output_in_events() -> None:
    events: list[DesktopTerminalModelEvent] = []
    deltas: list[tuple[int, str]] = []

    class StreamingProvider:
        def __call__(self, _request: DesktopModelRequest, _connect_timeout_seconds: float) -> str:
            raise AssertionError("The streaming seam must be used.")

        def stream_until_terminal_with_lifecycle(
            self,
            _request: DesktopModelRequest,
            connect_timeout_seconds: float,
            on_delta,
            on_request_sent,
        ) -> str:
            assert connect_timeout_seconds == MODEL_CONNECT_TIMEOUT_SECONDS
            on_request_sent()
            on_delta("private provider output")
            return "private provider output"

    result = DesktopTerminalModelGateway(StreamingProvider()).stream(
        DesktopModelRequest("connection_test", "Model settings", "Reply with OK."),
        on_event=events.append,
        on_delta=lambda attempt, delta: deltas.append((attempt, delta)),
    )

    assert result.content == "private provider output"
    assert deltas == [(1, "private provider output")]
    assert "model_output_activity" in [event.status for event in events]
    assert all("private provider output" not in repr(event.as_dict()) for event in events)


def test_terminal_gateway_cancel_ends_retry_after_wait() -> None:
    cancel = threading.Event()
    events: list[DesktopTerminalModelEvent] = []

    def rate_limited(_request: DesktopModelRequest, _connect_timeout_seconds: float) -> str:
        raise DesktopModelTransportError("rate_limited", retry_after_seconds=10)

    def cancel_during_sleep(_seconds: float) -> None:
        cancel.set()

    with pytest.raises(DesktopModelCancelledError):
        DesktopTerminalModelGateway(rate_limited, sleep=cancel_during_sleep).analyze(
            DesktopModelRequest("connection_test", "Model settings", "Reply with OK."),
            on_event=events.append,
            is_cancelled=cancel.is_set,
        )

    assert events[-1].status == "cancelled"


def test_litellm_stream_disconnect_is_a_retryable_network_failure(monkeypatch) -> None:
    class RemoteProtocolError(RuntimeError):
        pass

    class DisconnectingStream:
        def __iter__(self):
            raise RemoteProtocolError("peer disconnected")

    monkeypatch.setattr("litellm.completion", lambda **_kwargs: DisconnectingStream())
    events: list[DesktopTerminalModelEvent] = []
    gateway = DesktopTerminalModelGateway(
        desktop_model_transport.DesktopLiteLLMTransport(
            model="openai/test-model",
            bundle=LlmCredentialBundle(api_key="test-key", base_url="https://model.test/v1"),
        ),
        sleep=lambda _seconds: None,
    )

    with pytest.raises(DesktopModelCallError) as captured:
        gateway.stream(
            DesktopModelRequest("connection_test", "Model settings", "Reply with OK."),
            on_event=events.append,
            on_delta=lambda _attempt, _delta: None,
        )

    assert captured.value.attempt_count == MAX_TERMINAL_MODEL_ATTEMPTS
    assert captured.value.failure.code == "model_network_transient"
    assert [event.status for event in events].count("network_failure") == 3


def test_litellm_terminal_transport_preserves_retry_after(monkeypatch) -> None:
    class Response:
        status_code = 429
        headers = {"retry-after": "2.5"}

    class ProviderRateLimitError(RuntimeError):
        response = Response()

    def completion(**_kwargs):
        raise ProviderRateLimitError("rate limited")

    monkeypatch.setattr("litellm.completion", completion)
    transport = desktop_model_transport.DesktopLiteLLMTransport(
        model="openai/test-model",
        bundle=LlmCredentialBundle(api_key="test-key", base_url="https://model.test/v1"),
    )

    with pytest.raises(DesktopModelTransportError) as captured:
        transport.call_until_terminal(
            DesktopModelRequest("connection_test", "Model settings", "Reply with OK."),
            MODEL_CONNECT_TIMEOUT_SECONDS,
        )

    assert captured.value.category == "rate_limited"
    assert captured.value.retry_after_seconds == 2.5


def test_terminal_gateway_queue_wait_has_no_elapsed_deadline() -> None:
    gate = desktop_model_transport._DesktopModelConcurrencyGate(1)
    assert gate.acquire(None, remaining_seconds=1)
    provider_called = threading.Event()
    queued = threading.Event()
    finished = threading.Event()
    results: list[str] = []

    def provider(_request: DesktopModelRequest, _connect_timeout_seconds: float) -> str:
        provider_called.set()
        return "OK"

    gateway = DesktopTerminalModelGateway(
        desktop_model_transport._ConcurrentDesktopModelTransport(provider, gate)
    )

    def run() -> None:
        result = gateway.analyze(
            DesktopModelRequest("connection_test", "Model settings", "Reply with OK."),
            on_event=lambda event: queued.set() if event.status == "queued" else None,
        )
        results.append(result.content)
        finished.set()

    worker = threading.Thread(target=run)
    worker.start()
    assert queued.wait(timeout=1)
    assert not provider_called.wait(timeout=0.05)
    gate.release()
    worker.join(timeout=1)

    assert finished.is_set()
    assert results == ["OK"]


def test_settings_connection_factory_selects_the_terminal_policy(monkeypatch, tmp_path) -> None:
    class FakeTransport:
        def __init__(self, *, model, bundle) -> None:
            self.model = model
            self.bundle = bundle

        def __call__(self, _request, _timeout_seconds):
            return "legacy"

    monkeypatch.setattr(desktop_model_transport, "DesktopLiteLLMTransport", FakeTransport)
    settings = DesktopModelSettings(
        provider="custom",
        model="test/model",
        api_base_url="https://model.test/v1",
        api_key="test-key",
        max_concurrent_model_calls=1,
        initial_timeout_seconds=1,
    )

    gateway = desktop_model_transport.desktop_model_gateway_for_settings(tmp_path, settings)

    assert isinstance(gateway, DesktopTerminalModelGateway)
