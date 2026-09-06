"""Interruptible wait boundary around synchronous provider calls."""

from __future__ import annotations

import threading
from collections.abc import Callable

from openkb.models.gateway import DesktopModelCancelledError


def wait_for_model_response(
    call: Callable[[], object],
    *,
    is_cancelled: Callable[[], bool] | None,
    on_wait: Callable[[], None] | None = None,
    on_cancel: Callable[[], None] | None = None,
) -> object:
    """Wait for a daemon provider worker until completion or explicit cancellation."""
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
        cancelled = is_cancelled is not None and is_cancelled()
        if cancelled:
            if on_cancel is not None:
                on_cancel()
            raise DesktopModelCancelledError()
    if on_wait is not None:
        on_wait()
    if is_cancelled is not None and is_cancelled():
        raise DesktopModelCancelledError()
    error = outcome.get("error")
    if isinstance(error, Exception):
        raise error
    return outcome.get("response")
