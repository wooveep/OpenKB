"""No-TTL capability evidence for immutable Model Execution Profiles."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol

from openkb.locks import kb_ingest_lock
from openkb.shared.clock import timestamp as _timestamp
from openkb.storage.sqlite import connect_database
from openkb.workspace.paths import desktop_state_database_path, desktop_state_dir

CapabilityStatus = Literal["unchecked", "checking", "verified", "failed", "cancelled"]


class DesktopCapabilityEvidenceProfile(Protocol):
    """Credential-free immutable identity accepted by the capability ledger."""

    @property
    def identity(self) -> str: ...

    def as_dict(self) -> dict[str, object]: ...


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

    def state(self, profile: DesktopCapabilityEvidenceProfile) -> DesktopModelCapabilityState:
        profile = _shared_evidence_profile(profile)
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

    def is_verified(self, profile: DesktopCapabilityEvidenceProfile) -> bool:
        return self.state(profile).status == "verified"

    def begin(self, profile: DesktopCapabilityEvidenceProfile) -> None:
        self._write(profile, "checking")

    def mark_verified(self, profile: DesktopCapabilityEvidenceProfile) -> None:
        self._write(profile, "verified", checked=True)

    def mark_failed(
        self,
        profile: DesktopCapabilityEvidenceProfile,
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

    def mark_cancelled(self, profile: DesktopCapabilityEvidenceProfile) -> None:
        self._write(
            profile,
            "cancelled",
            failure_code="request_cancelled",
            reason="Model Capability Check cancelled.",
            checked=True,
        )

    def invalidate(
        self,
        profile: DesktopCapabilityEvidenceProfile,
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

    def invalidate_identity(
        self,
        profile_identity: str,
        *,
        failure_code: str,
        reason: str,
    ) -> bool:
        """Invalidate only durable evidence for an already-known exact identity."""
        with kb_ingest_lock(self._state_dir):
            connection = _connect(self._database_path)
            try:
                with connection:
                    cursor = connection.execute(
                        """
                        UPDATE model_capability_checks
                        SET status = 'unchecked', failure_code = ?, reason = ?,
                            checked_at = NULL, updated_at = ?
                        WHERE profile_identity = ?
                        """,
                        (failure_code, reason, _timestamp(), profile_identity),
                    )
                    return cursor.rowcount == 1
            finally:
                connection.close()

    def _write(
        self,
        profile: DesktopCapabilityEvidenceProfile,
        status: CapabilityStatus,
        *,
        failure_code: str | None = None,
        reason: str | None = None,
        checked: bool = False,
    ) -> None:
        profile = _shared_evidence_profile(profile)
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


def _shared_evidence_profile(
    profile: DesktopCapabilityEvidenceProfile,
) -> DesktopCapabilityEvidenceProfile:
    """Normalize legacy callers that pass a complete Analysis execution profile."""
    shared = getattr(profile, "capability_evidence_profile", None)
    if shared is not None and hasattr(shared, "identity") and hasattr(shared, "as_dict"):
        return shared
    return profile


def _status(value: str) -> CapabilityStatus:
    if value not in {"unchecked", "checking", "verified", "failed", "cancelled"}:
        raise ValueError(f"Unknown Model Capability Check status: {value}")
    return value  # type: ignore[return-value]


def _connect(path: Path) -> sqlite3.Connection:
    connection = connect_database(path)
    return connection


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
