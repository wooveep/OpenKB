"""Bounded execution for concurrent Desktop Engine requests."""

from __future__ import annotations

import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor

DEFAULT_MAX_REQUEST_WORKERS = 8


class DesktopRequestWorkers:
    """Queue arbitrary request work behind a fixed number of worker threads."""

    def __init__(self, *, maximum: int = DEFAULT_MAX_REQUEST_WORKERS) -> None:
        if isinstance(maximum, bool) or not isinstance(maximum, int) or maximum < 1:
            raise ValueError("maximum must be a positive integer")
        self._executor = ThreadPoolExecutor(
            max_workers=maximum,
            thread_name_prefix="openkb-engine-request",
        )
        self._lock = threading.Lock()
        self._closed = False

    def submit(self, task: Callable[..., None], *args: object) -> None:
        """Queue work without exposing executor lifecycle details to the caller."""
        with self._lock:
            if self._closed:
                raise RuntimeError("Desktop request workers are closed")
            self._executor.submit(task, *args)

    def close(self) -> None:
        """Stop accepting work and wait for active and queued requests to finish."""
        with self._lock:
            if self._closed:
                return
            self._closed = True
        self._executor.shutdown(wait=True)
