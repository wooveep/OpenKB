"""In-memory control signals shared by Desktop import workers."""

from __future__ import annotations

import threading


class DesktopImportControl:
    """Expose pause/cancel intent while durable transitions stay stage-boundary owned."""

    def __init__(self) -> None:
        self._pause = threading.Event()
        self._cancel = threading.Event()

    def request_pause(self) -> None:
        self._pause.set()

    def request_cancel(self) -> None:
        self._cancel.set()

    @property
    def action(self) -> str | None:
        if self._cancel.is_set():
            return "cancelled"
        if self._pause.is_set():
            return "paused"
        return None
