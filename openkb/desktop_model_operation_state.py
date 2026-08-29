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

    def mark_ready_unless_suspended(
        self,
        *,
        operation: str,
        capability_identity: str,
        prompt_contract_digest: str,
    ) -> bool:
        """Publish readiness without letting stale in-flight work clear a suspension."""
        return (
            self._write(
                operation=operation,
                capability_identity=capability_identity,
                prompt_contract_digest=prompt_contract_digest,
                status="ready",
                failure_code=None,
                reason=None,
                failure_stage=None,
                failure_signature=None,
                preserve_suspension=True,
            )
            >= 0
        )

    def mark_ready_for_retry(
        self,
        *,
        operation: str,
        capability_identity: str,
        prompt_contract_digest: str,
        retry_scope: str,
    ) -> bool:
        """Clear only the suspension revision captured by this retry action."""
        if not retry_scope:
            raise ValueError("Model operation retry scope must not be empty.")
        return (
            self._write(
                operation=operation,
                capability_identity=capability_identity,
                prompt_contract_digest=prompt_contract_digest,
                status="ready",
                failure_code=None,
                reason=None,
                failure_stage=None,
                failure_signature=None,
                retry_scope=retry_scope,
            )
            >= 0
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
        with kb_ingest_lock(self._state_dir):
            connection = self._connect()
            try:
                with connection:
                    return authorize_model_operation_retry_in(
                        connection,
                        operation=operation,
                        capability_identity=capability_identity,
                        prompt_contract_digest=prompt_contract_digest,
                        retry_scope=retry_scope,
                    )
            finally:
                connection.close()

    def authorize_retry_group(
        self,
        *,
        retry_scope: str,
        contracts: tuple[tuple[str, str, str], ...],
    ) -> None:
        """Bind every exact contract observed by one non-task user action."""
        if not retry_scope:
            raise ValueError("Model operation retry scope must not be empty.")
        with kb_ingest_lock(self._state_dir):
            connection = self._connect()
            try:
                with connection:
                    for operation, capability_identity, prompt_contract_digest in contracts:
                        authorize_model_operation_retry_in(
                            connection,
                            operation=operation,
                            capability_identity=capability_identity,
                            prompt_contract_digest=prompt_contract_digest,
                            retry_scope=retry_scope,
                        )
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
                return _dispatch_admitted_in(
                    connection,
                    operation=operation,
                    capability_identity=capability_identity,
                    prompt_contract_digest=prompt_contract_digest,
                    retry_scope=retry_scope,
                )
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
                    return _dispatch_admitted_in(
                        connection,
                        operation=operation,
                        capability_identity=capability_identity,
                        prompt_contract_digest=prompt_contract_digest,
                        retry_scope=retry_scope,
                    )
            finally:
                connection.close()

    def revoke_retry_scope(self, retry_scope: str) -> None:
        """End one explicit action so unused permits cannot leak into later work."""
        with kb_ingest_lock(self._state_dir):
            connection = self._connect()
            try:
                with connection:
                    revoke_model_operation_retry_scope_in(connection, retry_scope)
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
        preserve_suspension: bool = False,
        retry_scope: str | None = None,
    ) -> int:
        if preserve_suspension and retry_scope is not None:
            raise ValueError("Retry-scoped readiness cannot also preserve every suspension.")
        now = datetime.now(timezone.utc).isoformat()
        with kb_ingest_lock(self._state_dir):
            connection = self._connect()
            try:
                with connection:
                    if preserve_suspension or retry_scope is not None:
                        current = connection.execute(
                            """
                            SELECT status, revision FROM model_operation_contract_states
                            WHERE operation = ? AND capability_identity = ?
                                AND prompt_contract_digest = ?
                            """,
                            (operation, capability_identity, prompt_contract_digest),
                        ).fetchone()
                        if current is not None and str(current[0]) == "suspended":
                            if preserve_suspension:
                                return -1
                            permit = connection.execute(
                                """
                                SELECT 1 FROM model_operation_retry_permits
                                WHERE operation = ? AND capability_identity = ?
                                    AND prompt_contract_digest = ? AND retry_scope = ?
                                    AND suspension_revision = ?
                                """,
                                (
                                    operation,
                                    capability_identity,
                                    prompt_contract_digest,
                                    retry_scope,
                                    int(current[1]),
                                ),
                            ).fetchone()
                            if permit is None:
                                return -1
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
                    if status != "suspended":
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
                            created_at, updated_at, revision
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
                        ON CONFLICT(operation, capability_identity, prompt_contract_digest)
                        DO UPDATE SET
                            status = excluded.status,
                            failure_code = excluded.failure_code,
                            reason = excluded.reason,
                            failure_stage = excluded.failure_stage,
                            failure_signature = excluded.failure_signature,
                            updated_at = excluded.updated_at,
                            revision = model_operation_contract_states.revision + 1
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


def authorize_model_operation_retry_in(
    connection: sqlite3.Connection,
    *,
    operation: str,
    capability_identity: str,
    prompt_contract_digest: str,
    retry_scope: str,
) -> bool:
    """Bind one permit to the suspension revision in the caller's transaction."""
    if not retry_scope:
        raise ValueError("Model operation retry scope must not be empty.")
    row = connection.execute(
        """
        SELECT status, revision FROM model_operation_contract_states
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
            retry_scope, created_at, suspension_revision
        ) VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(
            operation, capability_identity, prompt_contract_digest, retry_scope
        ) DO NOTHING
        """,
        (
            operation,
            capability_identity,
            prompt_contract_digest,
            retry_scope,
            datetime.now(timezone.utc).isoformat(),
            int(row[1]),
        ),
    )
    permit = connection.execute(
        """
        SELECT suspension_revision FROM model_operation_retry_permits
        WHERE operation = ? AND capability_identity = ?
            AND prompt_contract_digest = ? AND retry_scope = ?
        """,
        (operation, capability_identity, prompt_contract_digest, retry_scope),
    ).fetchone()
    return permit is not None and int(permit[0]) == int(row[1])


def revoke_model_operation_retry_scope_in(
    connection: sqlite3.Connection, retry_scope: str
) -> None:
    """Revoke one action scope inside its owning task-state transaction."""
    connection.execute(
        "DELETE FROM model_operation_retry_permits WHERE retry_scope = ?",
        (retry_scope,),
    )


def _status(value: str) -> ModelOperationContractStatus:
    if value not in {"unverified", "ready", "suspended"}:
        raise ValueError(f"Unknown Model Operation Contract status: {value}")
    return value  # type: ignore[return-value]


def _dispatch_admitted_in(
    connection: sqlite3.Connection,
    *,
    operation: str,
    capability_identity: str,
    prompt_contract_digest: str,
    retry_scope: str | None,
) -> bool:
    """Apply the exact-contract suspension and scoped-retry admission rule."""
    contract = (operation, capability_identity, prompt_contract_digest)
    row = connection.execute(
        """
        SELECT status, revision FROM model_operation_contract_states
        WHERE operation = ? AND capability_identity = ?
            AND prompt_contract_digest = ?
        """,
        contract,
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
            AND suspension_revision = ?
        """,
        (*contract, retry_scope, int(row[1])),
    ).fetchone()
    return permit is not None
