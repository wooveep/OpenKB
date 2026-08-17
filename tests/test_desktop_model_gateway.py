"""Behavior checks for Desktop Model Gateway retry and deadline policy."""

from __future__ import annotations

import logging
import threading
import time

import pytest

from openkb import desktop_model_gateway, desktop_model_transport
from openkb.config import LlmCredentialBundle
from openkb.desktop_import_types import DesktopRecoveryOverride
from openkb.desktop_model_gateway import (
    DesktopModelCallError,
    DesktopModelCancelledError,
    DesktopModelGateway,
    DesktopModelRequest,
    DesktopModelTransportError,
)


class FakeClock:
    def __init__(self) -> None:
        self.value = 0.0

    def __call__(self) -> float:
        return self.value


def test_gateway_retries_transient_failures_after_the_initial_attempt():
    """The initial request plus three retries use the progressive timeout budget."""
    timeouts: list[float] = []
    events = []

    def timeout_transport(_request, timeout_seconds):
        timeouts.append(timeout_seconds)
        raise TimeoutError()

    with pytest.raises(DesktopModelCallError) as error:
        DesktopModelGateway(timeout_transport).analyze(
            DesktopModelRequest("document_analysis", "guide.txt", "source"), on_event=events.append
        )

    assert timeouts == [20.0, 30.0, 40.0, 50.0]
    assert error.value.failure.code == "model_timeout"
    assert error.value.attempt_count == 4
    assert [event.status for event in events] == [
        "running",
        "retry_wait",
        "running",
        "retry_wait",
        "running",
        "retry_wait",
        "running",
        "failed",
    ]


def test_gateway_logs_each_network_attempt_with_diagnostic_detail(caplog):
    """A quarantined model-analysis failure remains diagnosable in the application log."""

    def network_transport(_request, _timeout_seconds):
        raise ConnectionError("connection reset by peer")

    with caplog.at_level(logging.INFO, logger="openkb.desktop_model_gateway"):
        with pytest.raises(DesktopModelCallError):
            DesktopModelGateway(network_transport).analyze(
                DesktopModelRequest("document_analysis", "guide.txt", "source"),
                on_event=lambda _event: None,
            )

    assert "model_attempt_failed" in caplog.text
    assert "category=model_network_transient" in caplog.text
    assert "exception_type=ConnectionError" in caplog.text
    assert "connection reset by peer" in caplog.text


def test_gateway_deadline_truncates_remaining_retries():
    """The 60-second deadline wins even if retries remain available."""
    clock = FakeClock()
    timeouts: list[float] = []

    def timeout_transport(_request, timeout_seconds):
        timeouts.append(timeout_seconds)
        clock.value += timeout_seconds
        raise TimeoutError()

    with pytest.raises(DesktopModelCallError) as error:
        DesktopModelGateway(timeout_transport, clock=clock).analyze(
            DesktopModelRequest("document_analysis", "guide.txt", "source"),
            on_event=lambda _event: None,
        )

    assert timeouts == [20.0, 30.0, 10.0]
    assert error.value.failure.code == "model_deadline_exceeded"
    assert error.value.attempt_count == 3


def test_gateway_rejects_a_response_that_arrives_at_the_hard_deadline():
    """A provider response at 60 seconds cannot escape the logical call deadline."""
    clock = FakeClock()

    def late_transport(_request, _timeout_seconds):
        clock.value = 60.0
        return "late response"

    with pytest.raises(DesktopModelCallError) as error:
        DesktopModelGateway(late_transport, clock=clock).analyze(
            DesktopModelRequest("document_analysis", "guide.txt", "source"),
            on_event=lambda _event: None,
        )

    assert error.value.failure.code == "model_deadline_exceeded"
    assert error.value.attempt_count == 1


def test_gateway_enforces_the_response_wait_when_transport_blocks(monkeypatch):
    """Adapters cannot keep the logical Model Call waiting beyond its attempt timeout."""
    release = threading.Event()
    monkeypatch.setattr(desktop_model_gateway, "INITIAL_RESPONSE_TIMEOUT_SECONDS", 0.01)
    monkeypatch.setattr(desktop_model_gateway, "MAX_AUTOMATIC_RETRIES", 0)

    def blocking_transport(_request, _timeout_seconds):
        release.wait()
        return "too late"

    try:
        with pytest.raises(DesktopModelCallError) as error:
            DesktopModelGateway(blocking_transport).analyze(
                DesktopModelRequest("document_analysis", "guide.txt", "source"),
                on_event=lambda _event: None,
            )
    finally:
        release.set()

    assert error.value.failure.code == "model_timeout"


def test_gateway_stream_timeout_does_not_wait_for_a_blocking_delta_callback(monkeypatch):
    """Slow event delivery cannot extend the provider response timeout."""
    callback_started = threading.Event()
    release_callback = threading.Event()
    monkeypatch.setattr(desktop_model_gateway, "MAX_AUTOMATIC_RETRIES", 0)

    class BlockingStreamTransport:
        def __call__(self, _request, _timeout_seconds):
            return "unused"

        def stream(self, _request, _timeout_seconds, on_delta):
            on_delta("partial")
            return "unused"

    def blocking_delta(_attempt, _delta):
        callback_started.set()
        release_callback.wait()

    started_at = time.monotonic()
    try:
        with pytest.raises(DesktopModelCallError) as error:
            DesktopModelGateway(BlockingStreamTransport(), initial_timeout_seconds=0.01).stream(
                DesktopModelRequest("grounded_answer", "answer", "source"),
                on_event=lambda _event: None,
                on_delta=blocking_delta,
            )
    finally:
        release_callback.set()

    assert callback_started.is_set()
    assert time.monotonic() - started_at < 0.5
    assert error.value.failure.code == "model_timeout"


def test_configured_concurrency_queue_does_not_consume_the_api_response_timeout(monkeypatch):
    """A queued request begins its response-time budget only after an API slot opens."""
    monkeypatch.setattr(desktop_model_gateway, "MAX_AUTOMATIC_RETRIES", 0)
    gate = desktop_model_transport._DesktopModelConcurrencyGate(1)
    first_provider_started = threading.Event()
    release_first_provider = threading.Event()
    first_finished = threading.Event()
    second_finished = threading.Event()
    second_result: list[str] = []

    def first_provider(_request, _timeout_seconds):
        first_provider_started.set()
        assert release_first_provider.wait(timeout=1)
        return "first"

    first_gateway = DesktopModelGateway(
        desktop_model_transport._ConcurrentDesktopModelTransport(first_provider, gate),
        initial_timeout_seconds=0.01,
    )
    second_gateway = DesktopModelGateway(
        desktop_model_transport._ConcurrentDesktopModelTransport(
            lambda _request, _timeout_seconds: "second", gate
        ),
        initial_timeout_seconds=0.01,
    )

    def run_first() -> None:
        with pytest.raises(DesktopModelCallError):
            first_gateway.analyze(
                DesktopModelRequest("document_analysis", "first.txt", "source"),
                on_event=lambda _event: None,
            )
        first_finished.set()

    def run_second() -> None:
        result = second_gateway.analyze(
            DesktopModelRequest("document_analysis", "second.txt", "source"),
            on_event=lambda _event: None,
        )
        second_result.append(result.content)
        second_finished.set()

    first_thread = threading.Thread(target=run_first)
    first_thread.start()
    assert first_provider_started.wait(timeout=1)
    assert first_finished.wait(timeout=1)
    second_thread = threading.Thread(target=run_second)
    second_thread.start()
    time.sleep(0.03)
    assert not second_finished.is_set()

    release_first_provider.set()
    second_thread.join(timeout=1)
    first_thread.join(timeout=1)

    assert second_finished.is_set()
    assert second_result == ["second"]


def test_configured_concurrency_queue_respects_the_logical_model_call_deadline(monkeypatch):
    """Queue time is not API response time, but it is inside the 60-second call budget."""
    monkeypatch.setattr(desktop_model_gateway, "MODEL_CALL_DEADLINE_SECONDS", 0.01)
    gate = desktop_model_transport._DesktopModelConcurrencyGate(1)
    assert gate.acquire(None, remaining_seconds=1)
    gateway = DesktopModelGateway(
        desktop_model_transport._ConcurrentDesktopModelTransport(
            lambda _request, _timeout_seconds: "unreachable", gate
        ),
        initial_timeout_seconds=0.01,
    )

    try:
        with pytest.raises(DesktopModelCallError) as error:
            gateway.analyze(
                DesktopModelRequest("document_analysis", "queued.txt", "source"),
                on_event=lambda _event: None,
            )
    finally:
        gate.release()

    assert error.value.failure.code == "model_deadline_exceeded"


def test_prepared_concurrency_slot_is_released_at_the_logical_deadline():
    """A slot acquired just before expiry does not strand later model calls."""
    clock = FakeClock()
    gate = desktop_model_transport._DesktopModelConcurrencyGate(1)

    class DeadlineTransport:
        def prepare_model_attempt(self, _is_cancelled, remaining_seconds):
            assert gate.acquire(None, remaining_seconds)
            clock.value = desktop_model_gateway.MODEL_CALL_DEADLINE_SECONDS
            return True

        def release_prepared_model_attempt(self):
            gate.release()

        def __call__(self, _request, _timeout_seconds):
            pytest.fail("provider must not run after the logical deadline")

    with pytest.raises(DesktopModelCallError) as error:
        DesktopModelGateway(DeadlineTransport(), clock=clock).analyze(
            DesktopModelRequest("document_analysis", "expired.txt", "source"),
            on_event=lambda _event: None,
        )

    assert error.value.failure.code == "model_deadline_exceeded"
    assert gate.acquire(None, remaining_seconds=1)
    gate.release()


def test_configured_desktop_gateway_turns_invalid_model_config_into_direct_failure(
    tmp_path, monkeypatch
):
    """Malformed model settings use the same visible quarantine path, not a skip."""
    kb_dir = tmp_path / "desktop-kb"
    config_path = kb_dir / ".openkb" / "config.yaml"
    config_path.parent.mkdir(parents=True)
    config_path.write_text("- not-a-config-mapping\n", encoding="utf-8")
    monkeypatch.setattr(
        desktop_model_transport,
        "resolve_credential_bundle",
        lambda _kb_dir: LlmCredentialBundle(),
    )

    gateway = desktop_model_transport.desktop_model_gateway_for(kb_dir)

    assert gateway is not None
    with pytest.raises(DesktopModelCallError) as error:
        gateway.analyze(
            DesktopModelRequest("document_analysis", "guide.txt", "source"),
            on_event=lambda _event: None,
        )
    assert error.value.failure.code == "model_configuration_invalid"


def test_recovery_override_uses_its_model_and_timeout_without_writing_config(tmp_path, monkeypatch):
    """The production factory applies a recovery override only to the returned gateway."""
    kb_dir = tmp_path / "desktop-kb"
    config_path = kb_dir / ".openkb" / "config.yaml"
    config_path.parent.mkdir(parents=True)
    config_path.write_text("model: default/model\n", encoding="utf-8")
    timeouts: list[float] = []
    models: list[object] = []

    def transport(*, model, bundle):
        models.append(model)

        def call(_request, timeout_seconds):
            timeouts.append(timeout_seconds)
            return "Recovered"

        return call

    monkeypatch.setattr(
        desktop_model_transport,
        "resolve_credential_bundle",
        lambda _kb_dir: LlmCredentialBundle(api_key="test-key"),
    )
    monkeypatch.setattr(desktop_model_transport, "DesktopLiteLLMTransport", transport)

    gateway = desktop_model_transport.desktop_model_gateway_for(
        kb_dir,
        DesktopRecoveryOverride(model="recovery/model", initial_timeout_seconds=30),
    )

    assert gateway is not None
    result = gateway.analyze(
        DesktopModelRequest("document_analysis", "guide.txt", "source"),
        on_event=lambda _event: None,
    )

    assert result.content == "Recovered"
    assert models == ["openai/recovery/model"]
    assert timeouts == [30.0]
    assert config_path.read_text(encoding="utf-8") == "model: default/model\n"


def test_gateway_does_not_retry_authentication_or_response_format_failures():
    """Direct failures remain one attempt so users can correct the actual problem."""
    calls = 0

    def authentication_transport(_request, _timeout_seconds):
        nonlocal calls
        calls += 1
        raise DesktopModelTransportError("authentication")

    with pytest.raises(DesktopModelCallError) as authentication_error:
        DesktopModelGateway(authentication_transport).analyze(
            DesktopModelRequest("document_analysis", "guide.txt", "source"),
            on_event=lambda _event: None,
        )
    assert authentication_error.value.failure.code == "model_authentication_failed"
    assert calls == 1

    with pytest.raises(DesktopModelCallError) as response_error:
        DesktopModelGateway(lambda _request, _timeout: "").analyze(
            DesktopModelRequest("document_analysis", "guide.txt", "source"),
            on_event=lambda _event: None,
        )
    assert response_error.value.failure.code == "model_response_invalid"


def test_gateway_stops_while_waiting_for_a_provider_retry_after():
    """A user stop interrupts retry backoff instead of waiting for the provider delay."""
    retry_waiting = threading.Event()
    stop = threading.Event()
    stopped = threading.Event()

    def rate_limited_transport(_request, _timeout_seconds):
        raise DesktopModelTransportError("rate_limited", retry_after_seconds=1)

    def run() -> None:
        try:
            DesktopModelGateway(rate_limited_transport).analyze(
                DesktopModelRequest("grounded_answer", "answer", "source"),
                on_event=lambda event: retry_waiting.set()
                if event.status == "retry_wait"
                else None,
                is_cancelled=stop.is_set,
            )
        except DesktopModelCancelledError:
            stopped.set()

    worker = threading.Thread(target=run)
    worker.start()
    assert retry_waiting.wait(timeout=1)
    stop.set()
    worker.join(timeout=0.5)

    assert stopped.is_set()
    assert not worker.is_alive()
