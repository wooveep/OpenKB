"""Thread-safe ownership for provider streams that support best-effort close."""

from __future__ import annotations

import threading
from collections.abc import Callable


def once(call: Callable[[], None]) -> Callable[[], None]:
    """Return a thread-safe idempotent callback for one owned resource."""
    lock = threading.Lock()
    called = False

    def invoke() -> None:
        nonlocal called
        with lock:
            if called:
                return
            called = True
        call()

    return invoke


class DesktopActiveModelStreams:
    """Track idempotent close callbacks by in-memory request identity."""

    def __init__(self) -> None:
        self._close_by_request: dict[int, Callable[[], None]] = {}
        self._pending: set[int] = set()
        self._cancelled: set[int] = set()
        self._lock = threading.Lock()

    def prepare(self, request_key: int) -> None:
        """Publish ownership before the provider worker can race cancellation."""
        with self._lock:
            if request_key not in self._close_by_request:
                self._cancelled.discard(request_key)
            self._pending.add(request_key)

    def register(self, request_key: int, close: Callable[[], None]) -> Callable[[], None]:
        with self._lock:
            self._pending.discard(request_key)
            cancelled = request_key in self._cancelled
            if not cancelled:
                self._close_by_request[request_key] = close
        if cancelled:
            close()

        def release() -> None:
            with self._lock:
                if self._close_by_request.get(request_key) is close:
                    self._close_by_request.pop(request_key, None)
                self._pending.discard(request_key)
                self._cancelled.discard(request_key)
            close()

        return release

    def close(self, request_key: int) -> bool:
        with self._lock:
            active = request_key in self._pending or request_key in self._close_by_request
            if not active:
                return False
            self._cancelled.add(request_key)
            close = self._close_by_request.get(request_key)
        if close is not None:
            close()
        return True

    def abandon(self, request_key: int) -> None:
        """Clear ownership when setup fails before a close callback exists."""
        with self._lock:
            self._pending.discard(request_key)
            self._cancelled.discard(request_key)
