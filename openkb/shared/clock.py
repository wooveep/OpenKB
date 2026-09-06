"""UTC wall-clock serialization for durable state, separate from elapsed timers."""

from __future__ import annotations

from datetime import datetime, timezone


def timestamp() -> str:
    """Return the existing ISO 8601 format, including the explicit UTC offset."""
    return datetime.now(tz=timezone.utc).isoformat()
