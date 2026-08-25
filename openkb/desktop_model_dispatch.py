"""KB-local concurrency and provider-rate dispatch for Desktop model calls."""

from __future__ import annotations

import json
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from openkb.desktop_knowledge_analysis_plan import estimate_model_tokens
from openkb.desktop_model_active_streams import once
from openkb.desktop_model_gateway import (
    DesktopModelCancelledError,
    DesktopModelRequest,
    DesktopModelTransportError,
    ExecutionLane,
    require_execution_lane,
)
from openkb.desktop_prompt_contracts import prompt_contract_for

CancellationCallback = Callable[[], bool]


@dataclass(frozen=True)
class _ConcurrencyTicket:
    group: object


class _DesktopModelConcurrencyGate:
    """A KB-local limiter that rotates capacity fairly across documents."""

    def __init__(self, maximum: int) -> None:
        self._maximum = maximum
        self._active = 0
        self._condition = threading.Condition()
        self._waiters_by_group: dict[object, deque[_ConcurrencyTicket]] = {}
        self._ready_groups: deque[object] = deque()
        self._last_started_group: object | None = None

    def configure(self, maximum: int) -> None:
        with self._condition:
            self._maximum = maximum
            self._condition.notify_all()

    def acquire_until_cancelled(
        self,
        is_cancelled: CancellationCallback | None,
        *,
        group: str | None = None,
    ) -> None:
        """Wait for capacity without converting queue time into a Model Call deadline."""
        group_key: object = group if group is not None else object()
        ticket = _ConcurrencyTicket(group_key)
        with self._condition:
            group_waiters = self._waiters_by_group.get(group_key)
            if group_waiters is None:
                group_waiters = deque()
                self._waiters_by_group[group_key] = group_waiters
                self._ready_groups.append(group_key)
            group_waiters.append(ticket)
            while not self._can_admit(ticket):
                if is_cancelled is not None and is_cancelled():
                    self._remove_waiter(ticket)
                    raise DesktopModelCancelledError()
                self._condition.wait(0.05)
            group_waiters.popleft()
            self._ready_groups.remove(group_key)
            if group_waiters:
                self._ready_groups.append(group_key)
            else:
                del self._waiters_by_group[group_key]
            self._active += 1
            self._last_started_group = group_key

    def release(self) -> None:
        with self._condition:
            self._active -= 1
            self._condition.notify_all()

    def _can_admit(self, ticket: _ConcurrencyTicket) -> bool:
        group_waiters = self._waiters_by_group[ticket.group]
        return (
            self._active < self._maximum
            and self._next_ready_group() == ticket.group
            and group_waiters[0] is ticket
        )

    def _next_ready_group(self) -> object:
        first = self._ready_groups[0]
        if len(self._ready_groups) > 1 and first == self._last_started_group:
            return self._ready_groups[1]
        return first

    def _remove_waiter(self, ticket: _ConcurrencyTicket) -> None:
        group_waiters = self._waiters_by_group[ticket.group]
        group_waiters.remove(ticket)
        if not group_waiters:
            del self._waiters_by_group[ticket.group]
            self._ready_groups.remove(ticket.group)
        self._condition.notify_all()


@dataclass
class _RateReservation:
    created_at: float
    tokens: int


class _DesktopModelRateLimiter:
    """Reserve RPM/TPM capacity across model roles, prioritizing interactive work."""

    def __init__(
        self,
        requests_per_minute: int | None,
        tokens_per_minute: int | None,
        *,
        window_seconds: float = 60.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._requests_per_minute = requests_per_minute
        self._tokens_per_minute = tokens_per_minute
        self._window_seconds = window_seconds
        self._clock = clock
        self._condition = threading.Condition()
        self._reservations: deque[_RateReservation] = deque()
        self._by_request: dict[int, deque[_RateReservation]] = {}
        self._interactive_waiters: deque[object] = deque()
        self._background_waiters_by_group: dict[object, deque[_ConcurrencyTicket]] = {}
        self._background_ready_groups: deque[object] = deque()
        self._last_background_group: object | None = None

    def configure(self, requests_per_minute: int | None, tokens_per_minute: int | None) -> None:
        with self._condition:
            self._requests_per_minute = requests_per_minute
            self._tokens_per_minute = tokens_per_minute
            self._condition.notify_all()

    def acquire(
        self,
        request: DesktopModelRequest,
        *,
        lane: ExecutionLane,
        is_cancelled: CancellationCallback | None,
    ) -> None:
        estimated_tokens = _estimated_request_tokens(request)
        ticket: object | _ConcurrencyTicket
        if lane == "interactive":
            ticket = object()
        else:
            ticket = _ConcurrencyTicket(_request_dispatch_group(request))
        with self._condition:
            if self._tokens_per_minute is not None and estimated_tokens > self._tokens_per_minute:
                raise DesktopModelTransportError(
                    "configuration",
                    diagnostic_detail=(
                        "The configured TPM limit is smaller than this request's estimated "
                        "input and maximum output tokens."
                    ),
                )
            self._append_waiter(ticket, lane)
            while True:
                self._discard_expired(self._clock())
                if self._is_next(ticket, lane) and self._has_capacity(estimated_tokens):
                    self._admit_waiter(ticket, lane)
                    reservation = _RateReservation(self._clock(), estimated_tokens)
                    self._reservations.append(reservation)
                    self._by_request.setdefault(id(request), deque()).append(reservation)
                    self._condition.notify_all()
                    return
                if is_cancelled is not None and is_cancelled():
                    self._remove_waiter(ticket, lane)
                    self._condition.notify_all()
                    raise DesktopModelCancelledError()
                self._condition.wait(0.05)

    def reconcile(self, request: DesktopModelRequest, response: object) -> None:
        usage = getattr(response, "usage", None)
        actual_tokens = getattr(usage, "total_tokens", None)
        if isinstance(actual_tokens, bool) or not isinstance(actual_tokens, int):
            return
        with self._condition:
            pending = self._by_request.get(id(request))
            if pending:
                pending[0].tokens = max(0, actual_tokens)
                self._condition.notify_all()

    def finalize(self, request: DesktopModelRequest) -> None:
        with self._condition:
            pending = self._by_request.get(id(request))
            if pending:
                pending.popleft()
                if not pending:
                    self._by_request.pop(id(request), None)

    def _discard_expired(self, now: float) -> None:
        boundary = now - self._window_seconds
        while self._reservations and self._reservations[0].created_at <= boundary:
            self._reservations.popleft()

    def _append_waiter(self, ticket: object | _ConcurrencyTicket, lane: ExecutionLane) -> None:
        if lane == "interactive":
            self._interactive_waiters.append(ticket)
            return
        assert isinstance(ticket, _ConcurrencyTicket)
        group_waiters = self._background_waiters_by_group.get(ticket.group)
        if group_waiters is None:
            group_waiters = deque()
            self._background_waiters_by_group[ticket.group] = group_waiters
            self._background_ready_groups.append(ticket.group)
        group_waiters.append(ticket)

    def _is_next(self, ticket: object | _ConcurrencyTicket, lane: ExecutionLane) -> bool:
        if lane == "interactive":
            return bool(self._interactive_waiters) and self._interactive_waiters[0] is ticket
        if self._interactive_waiters:
            return False
        assert isinstance(ticket, _ConcurrencyTicket)
        group_waiters = self._background_waiters_by_group[ticket.group]
        return self._next_background_group() == ticket.group and group_waiters[0] is ticket

    def _next_background_group(self) -> object:
        first = self._background_ready_groups[0]
        if len(self._background_ready_groups) > 1 and first == self._last_background_group:
            return self._background_ready_groups[1]
        return first

    def _admit_waiter(self, ticket: object | _ConcurrencyTicket, lane: ExecutionLane) -> None:
        if lane == "interactive":
            self._interactive_waiters.popleft()
            return
        assert isinstance(ticket, _ConcurrencyTicket)
        group_waiters = self._background_waiters_by_group[ticket.group]
        group_waiters.popleft()
        self._background_ready_groups.remove(ticket.group)
        if group_waiters:
            self._background_ready_groups.append(ticket.group)
        else:
            del self._background_waiters_by_group[ticket.group]
        self._last_background_group = ticket.group

    def _remove_waiter(self, ticket: object | _ConcurrencyTicket, lane: ExecutionLane) -> None:
        if lane == "interactive":
            self._interactive_waiters.remove(ticket)
            return
        assert isinstance(ticket, _ConcurrencyTicket)
        group_waiters = self._background_waiters_by_group[ticket.group]
        group_waiters.remove(ticket)
        if not group_waiters:
            del self._background_waiters_by_group[ticket.group]
            self._background_ready_groups.remove(ticket.group)

    def _has_capacity(self, estimated_tokens: int) -> bool:
        rpm_available = (
            self._requests_per_minute is None or len(self._reservations) < self._requests_per_minute
        )
        tpm_available = (
            self._tokens_per_minute is None
            or sum(item.tokens for item in self._reservations) + estimated_tokens
            <= self._tokens_per_minute
        )
        return rpm_available and tpm_available


_concurrency_gates: dict[tuple[Path, ExecutionLane], _DesktopModelConcurrencyGate] = {}
_rate_limiters: dict[Path, _DesktopModelRateLimiter] = {}
_dispatchers_lock = threading.Lock()


def _concurrency_gate_for(
    kb_dir: Path, maximum: int, *, lane: ExecutionLane = "background"
) -> _DesktopModelConcurrencyGate:
    key = (kb_dir, lane)
    with _dispatchers_lock:
        gate = _concurrency_gates.get(key)
        if gate is None:
            gate = _DesktopModelConcurrencyGate(maximum)
            _concurrency_gates[key] = gate
        else:
            gate.configure(maximum)
        return gate


def _rate_limiter_for(
    kb_dir: Path,
    requests_per_minute: int | None,
    tokens_per_minute: int | None,
) -> _DesktopModelRateLimiter:
    with _dispatchers_lock:
        limiter = _rate_limiters.get(kb_dir)
        if limiter is None:
            limiter = _DesktopModelRateLimiter(requests_per_minute, tokens_per_minute)
            _rate_limiters[kb_dir] = limiter
        else:
            limiter.configure(requests_per_minute, tokens_per_minute)
        return limiter


class _ConcurrentDesktopModelTransport:
    """Apply concurrency and rate limits around ordinary and streaming requests."""

    def __init__(
        self,
        transport: Callable[[DesktopModelRequest, float], object],
        gate: _DesktopModelConcurrencyGate,
        *,
        rate_limiter: _DesktopModelRateLimiter | None = None,
        lane: ExecutionLane = "background",
        lane_factory: Callable[[ExecutionLane], _DesktopModelConcurrencyGate] | None = None,
    ) -> None:
        self._transport = transport
        self._gate = gate
        self._rate_limiter = rate_limiter
        self._lane = require_execution_lane(lane)
        self._lane_factory = lane_factory

    def for_lane(self, lane: ExecutionLane) -> _ConcurrentDesktopModelTransport:
        lane = require_execution_lane(lane)
        if self._lane_factory is None:
            return self
        return _ConcurrentDesktopModelTransport(
            self._transport,
            self._lane_factory(lane),
            rate_limiter=self._rate_limiter,
            lane=lane,
            lane_factory=self._lane_factory,
        )

    def __call__(self, request: DesktopModelRequest, timeout: float) -> object:
        return self._delegate_call("call_until_terminal", request, timeout)

    def release_prepared_model_attempt(self) -> None:
        self._gate.release()

    def prepare_terminal_model_attempt(
        self, is_cancelled: CancellationCallback | None
    ) -> Callable[[], None]:
        self._gate.acquire_until_cancelled(is_cancelled)
        return once(self._gate.release)

    def prepare_terminal_model_request(
        self, request: DesktopModelRequest, is_cancelled: CancellationCallback | None
    ) -> Callable[[], None]:
        dispatch_group = _request_dispatch_group(request) if self._lane == "background" else None
        self._gate.acquire_until_cancelled(is_cancelled, group=dispatch_group)
        try:
            if self._rate_limiter is not None:
                self._rate_limiter.acquire(request, lane=self._lane, is_cancelled=is_cancelled)
        except BaseException:
            self._gate.release()
            raise

        def release() -> None:
            if self._rate_limiter is not None:
                self._rate_limiter.finalize(request)
            self._gate.release()

        return once(release)

    def cancel_active_stream(self, request: DesktopModelRequest) -> bool:
        cancel = getattr(self._transport, "cancel_active_stream", None)
        return bool(cancel(request)) if callable(cancel) else False

    def prepare_active_stream(self, request: DesktopModelRequest) -> None:
        prepare = getattr(self._transport, "prepare_active_stream", None)
        if callable(prepare):
            prepare(request)

    def call_until_terminal(self, request: DesktopModelRequest, timeout: float) -> object:
        return self._delegate_call("call_until_terminal", request, timeout)

    def call_until_terminal_with_lifecycle(
        self,
        request: DesktopModelRequest,
        timeout: float,
        on_request_sent: Callable[[], None],
    ) -> object:
        call = getattr(self._transport, "call_until_terminal_with_lifecycle", None)
        if callable(call):
            response = call(request, timeout, on_request_sent)
            return self._reconcile(request, response)
        response = self._delegate_call("call_until_terminal", request, timeout)
        on_request_sent()
        return response

    def stream_until_terminal(
        self,
        request: DesktopModelRequest,
        timeout: float,
        on_delta: Callable[[str], None],
    ) -> object:
        return self._delegate_stream("stream_until_terminal", request, timeout, on_delta)

    def stream_until_terminal_with_lifecycle(
        self,
        request: DesktopModelRequest,
        timeout: float,
        on_delta: Callable[[str], None],
        on_request_sent: Callable[[], None],
    ) -> object:
        stream = getattr(self._transport, "stream_until_terminal_with_lifecycle", None)
        if callable(stream):
            response = stream(request, timeout, on_delta, on_request_sent)
            return self._reconcile(request, response)
        on_request_sent()
        return self._delegate_stream("stream_until_terminal", request, timeout, on_delta)

    def _delegate_call(self, name: str, request: DesktopModelRequest, timeout: float) -> object:
        call = getattr(self._transport, name, None)
        response = call(request, timeout) if callable(call) else self._transport(request, timeout)
        return self._reconcile(request, response)

    def _delegate_stream(
        self,
        name: str,
        request: DesktopModelRequest,
        timeout: float,
        on_delta: Callable[[str], None],
    ) -> object:
        stream = getattr(self._transport, name, None)
        if callable(stream):
            return self._reconcile(request, stream(request, timeout, on_delta))
        response = self._transport(request, timeout)
        if isinstance(response, str):
            on_delta(response)
        return self._reconcile(request, response)

    def _reconcile(self, request: DesktopModelRequest, response: object) -> object:
        if self._rate_limiter is not None:
            self._rate_limiter.reconcile(request, response)
        return response


def _estimated_request_tokens(request: DesktopModelRequest) -> int:
    try:
        contract = prompt_contract_for(request.operation)
        instructions = contract.instructions
        output_budget: object = contract.token_budget_policy.get("reserve_output_tokens")
    except KeyError:
        instructions = ""
        output_budget = 2_048
    snapshot_instructions = (
        request.prompt_contract_snapshot.get("instructions")
        if request.prompt_contract_snapshot is not None
        else None
    )
    if isinstance(snapshot_instructions, str):
        instructions = snapshot_instructions
    parameters = request.generation_parameters or {}
    output_budget = parameters.get("max_tokens", output_budget)
    if isinstance(output_budget, bool) or not isinstance(output_budget, int) or output_budget < 0:
        output_budget = 2_048
    schema = (
        json.dumps(request.response_schema, ensure_ascii=False, sort_keys=True)
        if request.response_schema is not None
        else ""
    )
    return max(
        1,
        estimate_model_tokens(instructions)
        + estimate_model_tokens(request.content)
        + estimate_model_tokens(schema)
        + output_budget,
    )


def _request_dispatch_group(request: DesktopModelRequest) -> str:
    return request.job_id or request.document_name or request.operation
