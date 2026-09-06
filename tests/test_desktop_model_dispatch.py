"""Provider quota dispatch remains bounded, fair, and cancellable."""

from __future__ import annotations

import threading
import time
from typing import Any, cast

import pytest

from openkb.models.dispatch import (
    _ConcurrentDesktopModelTransport,
    _DesktopModelConcurrencyGate,
    _DesktopModelRateLimiter,
    _estimated_request_tokens,
)
from openkb.models.gateway import (
    DesktopModelCancelledError,
    DesktopModelProviderResponse,
    DesktopModelRequest,
    DesktopProviderTokenUsage,
)
from openkb.models.terminal import DesktopTerminalModelGateway


def _gateway(provider, limiter, *, maximum: int = 2):
    return DesktopTerminalModelGateway(
        _ConcurrentDesktopModelTransport(
            provider,
            _DesktopModelConcurrencyGate(maximum),
            rate_limiter=limiter,
            lane_factory=lambda _lane: _DesktopModelConcurrencyGate(maximum),
        )
    )


def test_rpm_wait_is_queue_time_not_a_model_deadline() -> None:
    called_at: list[float] = []

    def provider(_request, _timeout):
        called_at.append(time.monotonic())
        return "ok"

    gateway = _gateway(provider, _DesktopModelRateLimiter(1, None, window_seconds=0.08))
    request = DesktopModelRequest("connection_test", "settings", "reply ok")

    gateway.analyze(request, on_event=lambda _event: None)
    gateway.analyze(request, on_event=lambda _event: None)

    assert called_at[1] - called_at[0] >= 0.06


def test_provider_usage_reconciles_conservative_tpm_reservation() -> None:
    request = DesktopModelRequest("connection_test", "settings", "reply ok")
    estimated = _estimated_request_tokens(request)
    usage = DesktopProviderTokenUsage(input_tokens=1, output_tokens=0, total_tokens=1)

    def provider(_request, _timeout):
        return DesktopModelProviderResponse("ok", usage=usage)

    gateway = _gateway(provider, _DesktopModelRateLimiter(None, estimated + 1))

    started = time.monotonic()
    gateway.analyze(request, on_event=lambda _event: None)
    gateway.analyze(request, on_event=lambda _event: None)

    assert time.monotonic() - started < 0.5


def test_rate_wait_can_be_cancelled_before_provider_dispatch() -> None:
    provider_calls = 0
    cancel = threading.Event()
    queued = threading.Event()
    finished = threading.Event()
    failures: list[Exception] = []

    def provider(_request, _timeout):
        nonlocal provider_calls
        provider_calls += 1
        return "ok"

    gateway = _gateway(provider, _DesktopModelRateLimiter(1, None))
    request = DesktopModelRequest("connection_test", "settings", "reply ok")
    gateway.analyze(request, on_event=lambda _event: None)

    def run() -> None:
        try:
            gateway.analyze(
                request,
                on_event=lambda event: queued.set() if event.status == "queued" else None,
                is_cancelled=cancel.is_set,
            )
        except Exception as error:
            failures.append(error)
        finally:
            finished.set()

    worker = threading.Thread(target=run)
    worker.start()
    assert queued.wait(timeout=1)
    cancel.set()
    assert finished.wait(timeout=0.5)
    worker.join(timeout=1)

    assert provider_calls == 1
    assert len(failures) == 1
    assert isinstance(failures[0], DesktopModelCancelledError)


def test_interactive_waiter_precedes_queued_background_work() -> None:
    order: list[str] = []

    def provider(request, _timeout):
        order.append(request.document_name)
        return "ok"

    limiter = _DesktopModelRateLimiter(1, None, window_seconds=0.08)
    background = _gateway(provider, limiter, maximum=1)
    interactive = background.for_lane("interactive")
    background.analyze(
        DesktopModelRequest("connection_test", "first", "reply ok"),
        on_event=lambda _event: None,
    )
    queued_background = threading.Event()
    queued_interactive = threading.Event()

    def run(gateway, name: str, queued: threading.Event) -> None:
        gateway.analyze(
            DesktopModelRequest("connection_test", name, "reply ok"),
            on_event=lambda event: queued.set() if event.status == "queued" else None,
        )

    background_worker = threading.Thread(
        target=run, args=(background, "background", queued_background)
    )
    interactive_worker = threading.Thread(
        target=run, args=(interactive, "interactive", queued_interactive)
    )
    background_worker.start()
    assert queued_background.wait(timeout=1)
    interactive_worker.start()
    assert queued_interactive.wait(timeout=1)
    background_worker.join(timeout=1)
    interactive_worker.join(timeout=1)

    assert order == ["first", "interactive", "background"]


def test_rate_limiter_rotates_quota_across_document_jobs() -> None:
    order: list[str] = []

    def provider(request, _timeout):
        order.append(request.document_name)
        return "ok"

    limiter = _DesktopModelRateLimiter(1, None, window_seconds=0.08)
    gateway = _gateway(provider, limiter, maximum=3)
    gateway.analyze(
        DesktopModelRequest("connection_test", "a-first", "reply ok", job_id="document-a"),
        on_event=lambda _event: None,
    )

    def run(name: str, job_id: str) -> None:
        gateway.analyze(
            DesktopModelRequest("connection_test", name, "reply ok", job_id=job_id),
            on_event=lambda _event: None,
        )

    document_a = threading.Thread(target=run, args=("a-second", "document-a"))
    document_b = threading.Thread(target=run, args=("b-first", "document-b"))
    document_a.start()
    _wait_for_rate_waiter(limiter, "document-a")
    document_b.start()
    _wait_for_rate_waiter(limiter, "document-b")
    document_a.join(timeout=1)
    document_b.join(timeout=1)

    assert order == ["a-first", "b-first", "a-second"]


def test_background_gate_rotates_capacity_across_document_jobs() -> None:
    gate = _DesktopModelConcurrencyGate(1)
    gate.acquire_until_cancelled(None, group="document-a")
    started: list[str] = []

    def run(group: str) -> None:
        gate.acquire_until_cancelled(None, group=group)
        started.append(group)
        gate.release()

    document_a = threading.Thread(target=run, args=("document-a",))
    document_b = threading.Thread(target=run, args=("document-b",))
    document_a.start()
    _wait_for_waiter(gate, "document-a")
    document_b.start()
    _wait_for_waiter(gate, "document-b")

    gate.release()
    document_a.join(timeout=1)
    document_b.join(timeout=1)

    assert started == ["document-b", "document-a"]


def test_unknown_execution_lane_is_rejected_at_the_transport_boundary() -> None:
    gateway = _gateway(lambda *_args: "ok", _DesktopModelRateLimiter(None, None))

    with pytest.raises(ValueError, match="Unknown model execution lane"):
        gateway.for_lane(cast(Any, "bulk"))


def _wait_for_waiter(gate: _DesktopModelConcurrencyGate, group: str) -> None:
    deadline = time.monotonic() + 1
    while time.monotonic() < deadline:
        with gate._condition:
            if group in gate._waiters_by_group:
                return
        time.sleep(0.005)
    raise AssertionError(f"Timed out waiting for dispatch group {group}")


def _wait_for_rate_waiter(limiter: _DesktopModelRateLimiter, group: str) -> None:
    deadline = time.monotonic() + 1
    while time.monotonic() < deadline:
        with limiter._condition:
            if group in limiter._background_waiters_by_group:
                return
        time.sleep(0.005)
    raise AssertionError(f"Timed out waiting for rate-limit group {group}")
