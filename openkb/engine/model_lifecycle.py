"""Sanitized Model Call lifecycle events shared by Desktop interactive workloads."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import TYPE_CHECKING

from openkb.models.terminal import DesktopTerminalModelEvent
from openkb.models.usage import DesktopModelUsageStore

if TYPE_CHECKING:
    from openkb.engine.server import DesktopEngineServer


def emit_model_lifecycle(
    server: DesktopEngineServer,
    *,
    kb_dir: Path,
    request_id: str | int,
    event: DesktopTerminalModelEvent,
) -> None:
    """Forward identity and timing only; raw model chunks stay on answer.delta."""
    payload = event.as_dict()
    payload["request_id"] = request_id
    try:
        threshold = DesktopModelUsageStore(kb_dir).long_wait_threshold_seconds(
            event.model_role,
            event.model_name,
        )
    except (OSError, sqlite3.Error):
        threshold = 300.0
    payload["long_wait_threshold_seconds"] = threshold
    server._emit_event("model.call_lifecycle", payload)
