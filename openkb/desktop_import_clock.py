"""Shared wall-clock values for durable Desktop import state."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

_LEASE_SECONDS = 30


def timestamp() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def lease_expiry() -> str:
    return (datetime.now(tz=timezone.utc) + timedelta(seconds=_LEASE_SECONDS)).isoformat()
