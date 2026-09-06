"""Shared wall-clock values for durable Desktop import state."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from openkb.shared.clock import timestamp as timestamp

_LEASE_SECONDS = 30


def lease_expiry() -> str:
    return (datetime.now(tz=timezone.utc) + timedelta(seconds=_LEASE_SECONDS)).isoformat()
