"""No-TTL capability evidence for immutable Model Execution Profiles."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from openkb.desktop_model_execution_profile import DesktopModelExecutionProfile
from openkb.desktop_workspace import desktop_state_database_path, desktop_state_dir
from openkb.locks import kb_ingest_lock

CapabilityStatus = Literal["unchecked", "checking", "verified", "failed", "cancelled"]


@dataclass(frozen=True)
class DesktopModelCapabilityState:
    profile_identity: str
    status: CapabilityStatus
    failure_code: str | None = None
    reason: str | None = None
    checked_at: str | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "profile_identity": self.profile_identity,
            "status": self.status,
            "failure_code": self.failure_code,
            "reason": self.reason,
            "checked_at": self.checked_at,
        }


class DesktopModelCapabilityStore:
    """Own capability state transitions without retaining check prompts or results."""

    def __init__(self, kb_dir: Path) -> None:
        resolved = kb_dir.expanduser().resolve()
        self._state_dir = desktop_state_dir(resolved)
        self._database_path = desktop_state_database_path(resolved)

    def state(self, profile: DesktopModelExecutionProfile) -> DesktopModelCapabilityState:
        with kb_ingest_lock(self._state_dir):
            connection = _connect(self._database_path)
            try:
                row = connection.execute(
                    """
                    SELECT status, failure_code, reason, checked_at
                    FROM model_capability_checks WHERE profile_identity = ?
                    """,
                    (profile.identity,),
                ).fetchone()
            finally:
                connection.close()
        if row is None:
            return DesktopModelCapabilityState(profile.identity, "unchecked")
        return DesktopModelCapabilityState(
            profile_identity=profile.identity,
            status=_status(str(row[0])),
            failure_code=str(row[1]) if row[1] is not None else None,
            reason=str(row[2]) if row[2] is not None else None,
            checked_at=str(row[3]) if row[3] is not None else None,
        )

    def is_verified(self, profile: DesktopModelExecutionProfile) -> bool:
        return self.state(profile).status == "verified"

    def begin(self, profile: DesktopModelExecutionProfile) -> None:
        self._write(profile, "checking")

    def mark_verified(self, profile: DesktopModelExecutionProfile) -> None:
        self._write(profile, "verified", checked=True)

    def mark_failed(
        self,
        profile: DesktopModelExecutionProfile,
        *,
        failure_code: str,
        reason: str,
    ) -> None:
        self._write(
            profile,
            "failed",
            failure_code=failure_code,
            reason=reason,
            checked=True,
        )

    def mark_cancelled(self, profile: DesktopModelExecutionProfile) -> None:
        self._write(
            profile,
            "cancelled",
            failure_code="request_cancelled",
            reason="Model Capability Check cancelled.",
            checked=True,
        )

    def invalidate(
        self,
        profile: DesktopModelExecutionProfile,
        *,
        failure_code: str,
        reason: str,
    ) -> None:
        self._write(
            profile,
            "unchecked",
            failure_code=failure_code,
            reason=reason,
        )

    def _write(
        self,
        profile: DesktopModelExecutionProfile,
        status: CapabilityStatus,
        *,
        failure_code: str | None = None,
        reason: str | None = None,
        checked: bool = False,
    ) -> None:
        now = _timestamp()
        checked_at = now if checked else None
        with kb_ingest_lock(self._state_dir):
            connection = _connect(self._database_path)
            try:
                with connection:
                    connection.execute(
                        """
                        INSERT INTO model_capability_checks (
                            profile_identity, profile_json, status, failure_code,
                            reason, checked_at, created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(profile_identity) DO UPDATE SET
                            profile_json = excluded.profile_json,
                            status = excluded.status,
                            failure_code = excluded.failure_code,
                            reason = excluded.reason,
                            checked_at = excluded.checked_at,
                            updated_at = excluded.updated_at
                        """,
                        (
                            profile.identity,
                            _json(profile.as_dict()),
                            status,
                            failure_code,
                            reason,
                            checked_at,
                            now,
                            now,
                        ),
                    )
            finally:
                connection.close()


def _status(value: str) -> CapabilityStatus:
    if value not in {"unchecked", "checking", "verified", "failed", "cancelled"}:
        raise ValueError(f"Unknown Model Capability Check status: {value}")
    return value  # type: ignore[return-value]


def _connect(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _timestamp() -> str:
    return datetime.now(tz=timezone.utc).isoformat()
