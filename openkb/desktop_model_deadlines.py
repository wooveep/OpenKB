"""Request-scoped response deadlines for bounded interactive model workflows."""

from __future__ import annotations

import time
from dataclasses import replace

from openkb.desktop_model_gateway import DesktopModelCancelledError, DesktopModelRequest


def request_with_response_deadline(
    request: DesktopModelRequest,
    deadline: float | None,
) -> DesktopModelRequest:
    """Attach the remaining duration of one caller-owned absolute deadline."""
    if deadline is None:
        return request
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise DesktopModelCancelledError()
    return replace(request, response_timeout_seconds=remaining)
