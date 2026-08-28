"""Knowledge-base-local readiness for one exact model operation contract."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from openkb.desktop_workspace import desktop_state_database_path, desktop_state_dir
from openkb.locks import kb_ingest_lock

ModelOperationContractStatus = Literal["unverified", "ready", "suspended"]


@dataclass(frozen=True)
class DesktopModelOperationContractState:
    operation: str
    capability_identity: str
    prompt_contract_digest: str
    status: ModelOperationContractStatus
    failure_code: str | None = None
    reason: str | None = None
    failure_stage: str | None = None
    failure_signature: str | None = None


class DesktopModelOperationContractStore:
    """Own operation-local readiness without changing shared role verification."""

    def __init__(self, kb_dir: Path) -> None:
        resolved = kb_dir.expanduser().resolve()
        self._state_dir = desktop_state_dir(resolved)
        self._database_path = desktop_state_database_path(resolved)

    def state(
        self,
        *,
        operation: str,
        capability_identity: str,
        prompt_contract_digest: str,
    ) -> DesktopModelOperationContractState:
        with kb_ingest_lock(self._state_dir):
            connection = self._connect()
            try:
                row = connection.execute(
                    """
                    SELECT status, failure_code, reason, failure_stage, failure_signature
                    FROM model_operation_contract_states
                    WHERE operation = ? AND capability_identity = ?
                        AND prompt_contract_digest = ?
                    """,
                    (operation, capability_identity, prompt_contract_digest),
                ).fetchone()
            finally:
                connection.close()
        if row is None:
            return DesktopModelOperationContractState(
                operation,
                capability_identity,
                prompt_contract_digest,
                "unverified",
            )
        return DesktopModelOperationContractState(
            operation,
            capability_identity,
            prompt_contract_digest,
            _status(str(row[0])),
            str(row[1]) if row[1] is not None else None,
            str(row[2]) if row[2] is not None else None,
            str(row[3]) if row[3] is not None else None,
            str(row[4]) if row[4] is not None else None,
        )

    def suspend(
        self,
        *,
        operation: str,
        capability_identity: str,
        prompt_contract_digest: str,
        failure_code: str,
        reason: str,
        failure_stage: str,
        failure_signature: str | None = None,
    ) -> int:
        return self._write(
            operation=operation,
            capability_identity=capability_identity,
            prompt_contract_digest=prompt_contract_digest,
            status="suspended",
            failure_code=failure_code,
            reason=reason,
            failure_stage=failure_stage,
            failure_signature=failure_signature,
        )

    def mark_ready(
        self,
        *,
        operation: str,
        capability_identity: str,
        prompt_contract_digest: str,
    ) -> None:
        """Record a successful validation for only this exact operation contract."""
        self._write(
            operation=operation,
            capability_identity=capability_identity,
            prompt_contract_digest=prompt_contract_digest,
            status="ready",
            failure_code=None,
            reason=None,
            failure_stage=None,
            failure_signature=None,
        )

    def mark_unverified(
        self,
        *,
        operation: str,
        capability_identity: str,
        prompt_contract_digest: str,
    ) -> None:
        """Authorize one explicit retry without claiming its result is already valid."""
        self._write(
            operation=operation,
            capability_identity=capability_identity,
            prompt_contract_digest=prompt_contract_digest,
            status="unverified",
            failure_code=None,
            reason=None,
            failure_stage=None,
            failure_signature=None,
        )

    def authorize_retry(
        self,
        *,
        operation: str,
        capability_identity: str,
        prompt_contract_digest: str,
        retry_scope: str,
    ) -> bool:
        """Persist one scoped permit without changing the suspended contract state."""
        if not retry_scope:
            raise ValueError("Model operation retry scope must not be empty.")
        now = datetime.now(timezone.utc).isoformat()
        with kb_ingest_lock(self._state_dir):
            connection = self._connect()
            try:
                with connection:
                    row = connection.execute(
                        """
                        SELECT status FROM model_operation_contract_states
                        WHERE operation = ? AND capability_identity = ?
                            AND prompt_contract_digest = ?
                        """,
                        (operation, capability_identity, prompt_contract_digest),
                    ).fetchone()
                    if row is None or str(row[0]) != "suspended":
                        return False
                    connection.execute(
                        """
                        INSERT INTO model_operation_retry_permits (
                            operation, capability_identity, prompt_contract_digest,
                            retry_scope, created_at
                        ) VALUES (?, ?, ?, ?, ?)
                        ON CONFLICT(
                            operation, capability_identity, prompt_contract_digest, retry_scope
                        ) DO UPDATE SET created_at = excluded.created_at
                        """,
                        (
                            operation,
                            capability_identity,
                            prompt_contract_digest,
                            retry_scope,
                            now,
                        ),
                    )
                    return True
            finally:
                connection.close()

    def dispatch_possible(
        self,
        *,
        operation: str,
        capability_identity: str,
        prompt_contract_digest: str,
        retry_scope: str | None = None,
    ) -> bool:
        """Check readiness or a matching scoped permit without consuming it."""
        with kb_ingest_lock(self._state_dir):
            connection = self._connect()
            try:
                row = connection.execute(
                    """
                    SELECT status FROM model_operation_contract_states
                    WHERE operation = ? AND capability_identity = ?
                        AND prompt_contract_digest = ?
                    """,
                    (operation, capability_identity, prompt_contract_digest),
                ).fetchone()
                if row is None or str(row[0]) != "suspended":
                    return True
                if retry_scope is None:
                    return False
                permit = connection.execute(
                    """
                    SELECT 1 FROM model_operation_retry_permits
                    WHERE operation = ? AND capability_identity = ?
                        AND prompt_contract_digest = ? AND retry_scope = ?
                    """,
                    (
                        operation,
                        capability_identity,
                        prompt_contract_digest,
                        retry_scope,
                    ),
                ).fetchone()
                return permit is not None
            finally:
                connection.close()

    def claim_dispatch(
        self,
        *,
        operation: str,
        capability_identity: str,
        prompt_contract_digest: str,
        retry_scope: str | None = None,
    ) -> bool:
        """Join one scoped retry round while the contract remains suspended."""
        with kb_ingest_lock(self._state_dir):
            connection = self._connect()
            try:
                with connection:
                    row = connection.execute(
                        """
                        SELECT status FROM model_operation_contract_states
                        WHERE operation = ? AND capability_identity = ?
                            AND prompt_contract_digest = ?
                        """,
                        (operation, capability_identity, prompt_contract_digest),
                    ).fetchone()
                    if row is None or str(row[0]) != "suspended":
                        return True
                    if retry_scope is None:
                        return False
                    permit = connection.execute(
                        """
                        SELECT 1 FROM model_operation_retry_permits
                        WHERE operation = ? AND capability_identity = ?
                            AND prompt_contract_digest = ? AND retry_scope = ?
                        """,
                        (
                            operation,
                            capability_identity,
                            prompt_contract_digest,
                            retry_scope,
                        ),
                    ).fetchone()
                    return permit is not None
            finally:
                connection.close()

    def revoke_retry_scope(self, retry_scope: str) -> None:
        """End one explicit action so unused permits cannot leak into later work."""
        with kb_ingest_lock(self._state_dir):
            connection = self._connect()
            try:
                with connection:
                    connection.execute(
                        "DELETE FROM model_operation_retry_permits WHERE retry_scope = ?",
                        (retry_scope,),
                    )
            finally:
                connection.close()

    def _write(
        self,
        *,
        operation: str,
        capability_identity: str,
        prompt_contract_digest: str,
        status: ModelOperationContractStatus,
        failure_code: str | None,
        reason: str | None,
        failure_stage: str | None,
        failure_signature: str | None,
    ) -> int:
        now = datetime.now(timezone.utc).isoformat()
        with kb_ingest_lock(self._state_dir):
            connection = self._connect()
            try:
                with connection:
                    connection.execute(
                        """
                        UPDATE model_operation_contract_states
                        SET failure_signature = NULL
                        WHERE operation = ? AND capability_identity = ?
                            AND prompt_contract_digest != ?
                            AND failure_signature IS NOT NULL
                        """,
                        (operation, capability_identity, prompt_contract_digest),
                    )
                    connection.execute(
                        """
                        DELETE FROM model_operation_retry_permits
                        WHERE operation = ? AND capability_identity = ?
                            AND prompt_contract_digest = ?
                        """,
                        (operation, capability_identity, prompt_contract_digest),
                    )
                    connection.execute(
                        """
                        INSERT INTO model_operation_contract_states (
                            operation, capability_identity, prompt_contract_digest, status,
                            failure_code, reason, failure_stage, failure_signature,
                            created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(operation, capability_identity, prompt_contract_digest)
                        DO UPDATE SET
                            status = excluded.status,
                            failure_code = excluded.failure_code,
                            reason = excluded.reason,
                            failure_stage = excluded.failure_stage,
                            failure_signature = excluded.failure_signature,
                            updated_at = excluded.updated_at
                        """,
                        (
                            operation,
                            capability_identity,
                            prompt_contract_digest,
                            status,
                            failure_code,
                            reason,
                            failure_stage,
                            failure_signature,
                            now,
                            now,
                        ),
                    )
                    connection.execute(
                        """
                        INSERT INTO model_operation_contract_events (
                            operation, capability_identity, prompt_contract_digest,
                            status, failure_code, failure_stage, failure_signature, created_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            operation,
                            capability_identity,
                            prompt_contract_digest,
                            status,
                            failure_code,
                            failure_stage,
                            failure_signature,
                            now,
                        ),
                    )
                    if failure_signature is None:
                        return 0
                    row = connection.execute(
                        """
                        SELECT COUNT(DISTINCT operation)
                        FROM model_operation_contract_states
                        WHERE capability_identity = ? AND failure_signature = ?
                            AND status = 'suspended'
                        """,
                        (capability_identity, failure_signature),
                    ).fetchone()
                    return int(row[0]) if row is not None else 0
            finally:
                connection.close()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._database_path)
        connection.execute("PRAGMA foreign_keys = ON")
        return connection


def _status(value: str) -> ModelOperationContractStatus:
    if value not in {"unverified", "ready", "suspended"}:
        raise ValueError(f"Unknown Model Operation Contract status: {value}")
    return value  # type: ignore[return-value]
