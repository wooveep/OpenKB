"""Behavior checks for Desktop Model Gateway retry and deadline policy."""

from __future__ import annotations

import threading

import pytest

from openkb import desktop_model_gateway, desktop_model_transport
from openkb.config import LlmCredentialBundle
from openkb.desktop_model_gateway import (
    DesktopModelCallError,
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
