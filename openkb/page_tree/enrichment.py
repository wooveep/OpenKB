"""Optional, recoverable summary overlays for immutable deterministic PageTrees."""

from __future__ import annotations

import json
import logging
import sqlite3
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from openkb.locks import kb_ingest_lock
from openkb.models.event import normalize_model_event
from openkb.models.gateway import (
    DesktopModelCallError,
    DesktopModelCancelledError,
    DesktopModelGateway,
    DesktopModelRequest,
    gateway_analysis_capability_verified,
)
from openkb.models.prompt_contracts import prompt_contract_for
from openkb.models.result_failure import (
    DesktopModelOperationCompletionAuthority,
    DesktopModelOperationSuspendedError,
    mark_structured_output_operations_ready,
    model_operation_dispatch_possible,
    require_model_operation_dispatch,
    revoke_model_operation_retry_scope_in,
    suspend_analysis_operation_failure,
    suspend_structured_model_operation,
)
from openkb.models.structured_output import (
    DesktopStructuredOutputInvalidError,
    run_structured_output,
)
from openkb.page_tree.enrichment_actions import DesktopPageTreeEnrichmentActions
from openkb.page_tree.enrichment_contract import (
    page_tree_enrichment_request_in,
    parse_page_tree_enrichment_summaries,
)
from openkb.page_tree.enrichment_control import (
    INTERRUPTED_CODE as _INTERRUPTED_CODE,
)
from openkb.page_tree.enrichment_control import (
    INTERRUPTED_REASON as _INTERRUPTED_REASON,
)
from openkb.page_tree.enrichment_control import page_tree_enrichment_queue_reason
from openkb.shared.clock import timestamp as _timestamp
from openkb.storage.sqlite import connect_database
from openkb.workspace.paths import desktop_state_database_path, desktop_state_dir

_PAGE_TREE_ENRICHMENT_CONTRACT = prompt_contract_for("page_tree_enrichment")
PAGE_TREE_ENRICHMENT_PROMPT_DIGEST = _PAGE_TREE_ENRICHMENT_CONTRACT.digest

_FAILED_CODE = "page_tree_enrichment_failed"
_UNAVAILABLE_CODE = "source_document_unavailable"
_UNAVAILABLE_REASON = "The source document is no longer Available."
_INVALID_RESPONSE_CODE = "model_response_invalid"
_INVALID_RESPONSE_REASON = "The PageTree enrichment response could not be validated."
logger = logging.getLogger(__name__)
StopCallback = Callable[[], bool]


@dataclass(frozen=True)
class _EnrichmentClaim:
    document_id: str
    document_name: str
    base_generation_id: str
    provider: str
    model: str
    prompt_digest: str
    execution_token: str
    request_content: str
    node_ids: frozenset[str]
    retry_scope: str | None


class DesktopPageTreeEnrichmentService:
    """Own enrichment task state without ever mutating deterministic PageTree nodes."""

    def __init__(self, kb_dir: Path) -> None:
        self.kb_dir = kb_dir.expanduser().resolve()
        self.state_dir = desktop_state_dir(self.kb_dir)
        self.database_path = desktop_state_database_path(self.kb_dir)
        self._actions = DesktopPageTreeEnrichmentActions(
            self.kb_dir,
            prompt_digest=PAGE_TREE_ENRICHMENT_PROMPT_DIGEST,
        )

    def queue_eligible(self, gateway: DesktopModelGateway, *, retry_failed: bool = False) -> int:
        """Queue Available current trees whose active overlay has a different identity."""
        now = _timestamp()
        queued = 0
        with kb_ingest_lock(self.state_dir):
            connection = self._connect()
            try:
                connection.execute("BEGIN IMMEDIATE")
                rows = connection.execute(
                    """
                    SELECT documents.document_id, base.generation_id,
                        enriched.base_generation_id, enriched.provider,
                        enriched.model, enriched.prompt_digest
                    FROM source_documents AS documents
                    JOIN document_page_tree_current AS current
                        ON current.document_id = documents.document_id
                    JOIN document_page_tree_generations AS base
                        ON base.generation_id = current.generation_id
                    LEFT JOIN document_page_tree_enrichment_current AS enrichment_current
                        ON enrichment_current.document_id = documents.document_id
                    LEFT JOIN document_page_tree_enrichment_generations AS enriched
                        ON enriched.enrichment_generation_id =
                            enrichment_current.enrichment_generation_id
                    WHERE documents.availability = 'available'
                    ORDER BY documents.created_at, documents.document_id
                    """
                ).fetchall()
                for row in rows:
                    target = (
                        str(row[1]),
                        gateway.provider_name,
                        gateway.model_name,
                        PAGE_TREE_ENRICHMENT_PROMPT_DIGEST,
                    )
                    current_target = (
                        str(row[2]) if row[2] is not None else None,
                        str(row[3]) if row[3] is not None else None,
                        str(row[4]) if row[4] is not None else None,
                        str(row[5]) if row[5] is not None else None,
                    )
                    document_id = str(row[0])
                    if current_target == target:
                        retry_scope = _complete_repaired_task_in(
                            connection, document_id, target, now
                        )
                        if retry_scope is not None:
                            revoke_model_operation_retry_scope_in(connection, retry_scope)
                        continue
                    task_queued, retry_scope = _queue_target_in(
                        connection,
                        document_id,
                        target,
                        page_tree_enrichment_queue_reason(current_target, target),
                        now,
                        retry_failed=retry_failed,
                    )
                    if task_queued:
                        queued += 1
                        if retry_scope is not None:
                            revoke_model_operation_retry_scope_in(connection, retry_scope)
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
            finally:
                connection.close()
        return queued

    def recover_interrupted(self) -> int:
        """Return process-owned running work to its durable pending state."""
        return self._actions.recover_interrupted()

    def request_cancel(self, document_id: str) -> bool:
        """Mark pending work interrupted; a running worker observes Engine cancellation."""
        return self._actions.request_cancel(document_id)

    def retry_document(self, document_id: str, gateway: DesktopModelGateway) -> bool:
        """Make one interrupted or failed optional task runnable after explicit user action."""
        return self._actions.retry_document(document_id, gateway)

    def pending_document_ids(self, gateway: DesktopModelGateway) -> tuple[str, ...]:
        with kb_ingest_lock(self.state_dir):
            connection = self._connect()
            try:
                with connection:
                    now = _timestamp()
                    unavailable_scopes = tuple(
                        str(row[0])
                        for row in connection.execute(
                            """
                            SELECT DISTINCT tasks.retry_scope
                            FROM document_page_tree_enrichment_tasks AS tasks
                            JOIN source_documents AS documents
                                ON documents.document_id = tasks.document_id
                            WHERE tasks.status IN ('pending', 'running')
                                AND tasks.retry_scope IS NOT NULL
                                AND documents.availability != 'available'
                            """
                        ).fetchall()
                    )
                    connection.execute(
                        """
                        UPDATE document_page_tree_enrichment_tasks AS tasks
                        SET status = 'failed', execution_token = NULL, error_code = ?,
                            error_reason = ?, retry_scope = NULL,
                            updated_at = ?, completed_at = ?
                        WHERE status IN ('pending', 'running') AND EXISTS (
                            SELECT 1 FROM source_documents AS documents
                            WHERE documents.document_id = tasks.document_id
                                AND documents.availability != 'available'
                        )
                        """,
                        (_UNAVAILABLE_CODE, _UNAVAILABLE_REASON, now, now),
                    )
                    for retry_scope in unavailable_scopes:
                        revoke_model_operation_retry_scope_in(connection, retry_scope)
                    rows = connection.execute(
                        """
                        SELECT tasks.document_id, tasks.retry_scope
                        FROM document_page_tree_enrichment_tasks AS tasks
                        JOIN source_documents AS documents
                            ON documents.document_id = tasks.document_id
                        WHERE tasks.status = 'pending' AND tasks.provider = ?
                            AND tasks.model = ? AND tasks.prompt_digest = ?
                            AND tasks.error_code IS NULL
                            AND documents.availability = 'available'
                            AND (tasks.reason != 'explicit_retry'
                                OR tasks.retry_scope IS NOT NULL)
                        ORDER BY CASE WHEN tasks.reason = 'explicit_retry' THEN 0 ELSE 1 END,
                            tasks.updated_at, tasks.document_id LIMIT 50
                        """,
                        (
                            gateway.provider_name,
                            gateway.model_name,
                            PAGE_TREE_ENRICHMENT_PROMPT_DIGEST,
                        ),
                    ).fetchall()
                    documents = tuple(
                        (str(row[0]), str(row[1]) if row[1] is not None else None) for row in rows
                    )
            finally:
                connection.close()
        return tuple(
            document_id
            for document_id, retry_scope in documents
            if model_operation_dispatch_possible(
                self.kb_dir,
                gateway,
                operation="page_tree_enrichment",
                retry_scope=retry_scope,
            )
        )

    def deterministic_work_active(self) -> bool:
        connection = self._connect()
        try:
            return (
                connection.execute(
                    "SELECT 1 FROM document_page_tree_rebuild_tasks "
                    "WHERE status IN ('pending', 'running') LIMIT 1"
                ).fetchone()
                is not None
            )
        finally:
            connection.close()

    def run_document(
        self,
        document_id: str,
        gateway: DesktopModelGateway,
        *,
        should_stop: StopCallback,
    ) -> bool:
        """Run one claimed task and atomically activate only its summary overlay."""
        if not gateway_analysis_capability_verified(gateway):
            return False
        claim = self._claim(document_id, gateway)
        if claim is None:
            return False
        if should_stop():
            self._interrupt(claim)
            return False
        if not model_operation_dispatch_possible(
            self.kb_dir,
            gateway,
            operation="page_tree_enrichment",
            retry_scope=claim.retry_scope,
        ):
            self._interrupt(claim)
            return False
        try:

            def invoke(request: DesktopModelRequest):
                require_model_operation_dispatch(
                    self.kb_dir,
                    gateway,
                    request,
                    retry_scope=claim.retry_scope,
                )
                return gateway.analyze(
                    request,
                    on_event=lambda event: self._record_attempt(claim, event),
                    is_cancelled=should_stop,
                )

            output = run_structured_output(
                operation="page_tree_enrichment",
                document_name=claim.document_name,
                source_material=claim.request_content,
                invoke=invoke,
                validate=lambda content: parse_page_tree_enrichment_summaries(
                    content, claim.node_ids
                ),
            )
            result = output.result
            summaries = output.value
            mark_structured_output_operations_ready(
                self.kb_dir,
                gateway,
                output,
                authority=DesktopModelOperationCompletionAuthority.for_retry_scope(
                    claim.retry_scope
                ),
            )
            if should_stop():
                self._interrupt(claim)
                return False
            published = self._publish(claim, summaries)
            if published:
                logger.info(
                    "page_tree_enrichment_completed document_id=%s call_id=%s summaries=%s",
                    claim.document_id,
                    result.call_id,
                    len(summaries),
                )
            return published
        except DesktopModelCancelledError:
            self._interrupt(claim)
        except DesktopModelOperationSuspendedError:
            self._interrupt(claim)
        except DesktopModelCallError as error:
            suspend_analysis_operation_failure(self.kb_dir, gateway, error)
            self._fail(claim, error.failure.code, error.failure.reason)
        except DesktopStructuredOutputInvalidError as error:
            suspend_structured_model_operation(
                self.kb_dir,
                gateway,
                error,
                operation="page_tree_enrichment",
                failure_code=_INVALID_RESPONSE_CODE,
                reason=_INVALID_RESPONSE_REASON,
            )
            self._fail(claim, _INVALID_RESPONSE_CODE, _INVALID_RESPONSE_REASON)
        except (json.JSONDecodeError, TypeError, ValueError):
            self._fail(claim, _INVALID_RESPONSE_CODE, _INVALID_RESPONSE_REASON)
        except Exception:
            logger.exception("PageTree enrichment failed for %s", claim.document_id)
            self._fail(claim, _FAILED_CODE, "PageTree enrichment could not be completed.")
        return False

    def _claim(self, document_id: str, gateway: DesktopModelGateway) -> _EnrichmentClaim | None:
        with kb_ingest_lock(self.state_dir):
            connection = self._connect()
            try:
                connection.execute("BEGIN IMMEDIATE")
                row = connection.execute(
                    """
                    SELECT tasks.base_generation_id, tasks.provider, tasks.model,
                        tasks.prompt_digest, documents.display_name, tasks.reason,
                        tasks.retry_scope
                    FROM document_page_tree_enrichment_tasks AS tasks
                    JOIN source_documents AS documents
                        ON documents.document_id = tasks.document_id
                    JOIN document_page_tree_current AS current
                        ON current.document_id = tasks.document_id
                        AND current.generation_id = tasks.base_generation_id
                    WHERE tasks.document_id = ? AND tasks.status = 'pending'
                        AND tasks.error_code IS NULL
                        AND tasks.provider = ? AND tasks.model = ?
                        AND tasks.prompt_digest = ?
                        AND documents.availability = 'available'
                        AND (tasks.reason != 'explicit_retry'
                            OR tasks.retry_scope IS NOT NULL)
                        AND NOT EXISTS (
                            SELECT 1 FROM document_page_tree_rebuild_tasks AS rebuild
                            WHERE rebuild.status IN ('pending', 'running')
                        )
                    """,
                    (
                        document_id,
                        gateway.provider_name,
                        gateway.model_name,
                        PAGE_TREE_ENRICHMENT_PROMPT_DIGEST,
                    ),
                ).fetchone()
                if row is None:
                    connection.rollback()
                    return None
                base_generation_id = str(row[0])
                request_content, node_ids = page_tree_enrichment_request_in(
                    connection, base_generation_id, str(row[4])
                )
                token = uuid.uuid4().hex
                cursor = connection.execute(
                    """
                    UPDATE document_page_tree_enrichment_tasks
                    SET status = 'running', execution_token = ?, attempt_count = attempt_count + 1,
                        model_attempt = 0, call_id = NULL, timeout_seconds = NULL,
                        remaining_seconds = NULL, error_code = NULL, error_reason = NULL,
                        updated_at = ?, completed_at = NULL
                    WHERE document_id = ? AND status = 'pending'
                        AND error_code IS NULL
                        AND base_generation_id = ? AND provider = ? AND model = ?
                        AND prompt_digest = ?
                    """,
                    (
                        token,
                        _timestamp(),
                        document_id,
                        base_generation_id,
                        str(row[1]),
                        str(row[2]),
                        str(row[3]),
                    ),
                )
                if cursor.rowcount != 1:
                    connection.rollback()
                    return None
                connection.commit()
                return _EnrichmentClaim(
                    document_id=document_id,
                    document_name=str(row[4]),
                    base_generation_id=base_generation_id,
                    provider=str(row[1]),
                    model=str(row[2]),
                    prompt_digest=str(row[3]),
                    execution_token=token,
                    request_content=request_content,
                    node_ids=node_ids,
                    retry_scope=(
                        str(row[6])
                        if str(row[5]) == "explicit_retry" and row[6] is not None
                        else None
                    ),
                )
            except BaseException:
                connection.rollback()
                raise
            finally:
                connection.close()

    def _record_attempt(self, claim: _EnrichmentClaim, event: object) -> None:
        lifecycle = normalize_model_event(event)
        with kb_ingest_lock(self.state_dir):
            connection = self._connect()
            try:
                with connection:
                    connection.execute(
                        """
                        UPDATE document_page_tree_enrichment_tasks
                        SET model_attempt = ?, call_id = ?, timeout_seconds = ?,
                            remaining_seconds = ?, error_code = ?, error_reason = ?, updated_at = ?
                        WHERE document_id = ? AND status = 'running' AND execution_token = ?
                        """,
                        (
                            lifecycle.attempt,
                            lifecycle.call_id,
                            None,
                            None,
                            lifecycle.error_code,
                            lifecycle.reason,
                            _timestamp(),
                            claim.document_id,
                            claim.execution_token,
                        ),
                    )
            finally:
                connection.close()

    def _publish(self, claim: _EnrichmentClaim, summaries: tuple[tuple[str, str], ...]) -> bool:
        with kb_ingest_lock(self.state_dir):
            connection = self._connect()
            try:
                connection.execute("BEGIN IMMEDIATE")
                current = connection.execute(
                    """
                    SELECT documents.availability, tasks.retry_scope
                    FROM document_page_tree_enrichment_tasks AS tasks
                    JOIN document_page_tree_current AS base
                        ON base.document_id = tasks.document_id
                        AND base.generation_id = tasks.base_generation_id
                    JOIN source_documents AS documents
                        ON documents.document_id = tasks.document_id
                    WHERE tasks.document_id = ? AND tasks.status = 'running'
                        AND tasks.execution_token = ? AND tasks.base_generation_id = ?
                    """,
                    (
                        claim.document_id,
                        claim.execution_token,
                        claim.base_generation_id,
                    ),
                ).fetchone()
                if current is None:
                    connection.rollback()
                    return False
                if str(current[0]) != "available":
                    now = _timestamp()
                    connection.execute(
                        """
                        UPDATE document_page_tree_enrichment_tasks
                        SET status = 'failed', execution_token = NULL, error_code = ?,
                            error_reason = ?, retry_scope = NULL,
                            updated_at = ?, completed_at = ?
                        WHERE document_id = ? AND status = 'running' AND execution_token = ?
                        """,
                        (
                            _UNAVAILABLE_CODE,
                            _UNAVAILABLE_REASON,
                            now,
                            now,
                            claim.document_id,
                            claim.execution_token,
                        ),
                    )
                    if current[1] is not None:
                        revoke_model_operation_retry_scope_in(connection, str(current[1]))
                    connection.commit()
                    return False
                enrichment_generation_id = uuid.uuid4().hex
                previous = connection.execute(
                    "SELECT enrichment_generation_id "
                    "FROM document_page_tree_enrichment_current WHERE document_id = ?",
                    (claim.document_id,),
                ).fetchone()
                if previous is not None:
                    connection.execute(
                        "UPDATE document_page_tree_enrichment_generations "
                        "SET status = 'superseded' WHERE enrichment_generation_id = ?",
                        (str(previous[0]),),
                    )
                now = _timestamp()
                connection.execute(
                    """
                    INSERT INTO document_page_tree_enrichment_generations (
                        enrichment_generation_id, document_id, base_generation_id,
                        provider, model, prompt_digest, status, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, 'current', ?)
                    """,
                    (
                        enrichment_generation_id,
                        claim.document_id,
                        claim.base_generation_id,
                        claim.provider,
                        claim.model,
                        claim.prompt_digest,
                        now,
                    ),
                )
                connection.executemany(
                    """
                    INSERT INTO document_page_tree_enrichment_summaries (
                        enrichment_generation_id, base_generation_id, node_id, summary
                    ) VALUES (?, ?, ?, ?)
                    """,
                    (
                        (
                            enrichment_generation_id,
                            claim.base_generation_id,
                            node_id,
                            summary,
                        )
                        for node_id, summary in summaries
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO document_page_tree_enrichment_current (
                        document_id, enrichment_generation_id, base_generation_id, activated_at
                    ) VALUES (?, ?, ?, ?)
                    ON CONFLICT(document_id) DO UPDATE SET
                        enrichment_generation_id = excluded.enrichment_generation_id,
                        base_generation_id = excluded.base_generation_id,
                        activated_at = excluded.activated_at
                    """,
                    (
                        claim.document_id,
                        enrichment_generation_id,
                        claim.base_generation_id,
                        now,
                    ),
                )
                connection.execute(
                    """
                    UPDATE document_page_tree_enrichment_tasks
                    SET status = 'completed', execution_token = NULL, error_code = NULL,
                        error_reason = NULL, retry_scope = NULL,
                        updated_at = ?, completed_at = ?
                    WHERE document_id = ? AND status = 'running' AND execution_token = ?
                    """,
                    (now, now, claim.document_id, claim.execution_token),
                )
                if claim.retry_scope is not None:
                    revoke_model_operation_retry_scope_in(connection, claim.retry_scope)
                connection.commit()
                return True
            except BaseException:
                connection.rollback()
                raise
            finally:
                connection.close()

    def _interrupt(self, claim: _EnrichmentClaim) -> None:
        self._finish_task(
            claim,
            status="pending",
            error_code=_INTERRUPTED_CODE,
            error_reason=_INTERRUPTED_REASON,
        )

    def _fail(self, claim: _EnrichmentClaim, error_code: str, error_reason: str) -> None:
        self._finish_task(
            claim,
            status="failed",
            error_code=error_code,
            error_reason=error_reason,
        )

    def _finish_task(
        self,
        claim: _EnrichmentClaim,
        *,
        status: str,
        error_code: str,
        error_reason: str,
    ) -> None:
        try:
            with kb_ingest_lock(self.state_dir):
                connection = self._connect()
                try:
                    with connection:
                        now = _timestamp()
                        connection.execute(
                            """
                            UPDATE document_page_tree_enrichment_tasks
                            SET status = ?, execution_token = NULL, error_code = ?,
                                error_reason = ?, retry_scope = NULL,
                                updated_at = ?, completed_at = ?
                            WHERE document_id = ? AND status = 'running'
                                AND execution_token = ?
                            """,
                            (
                                status,
                                error_code,
                                error_reason,
                                now,
                                now if status == "failed" else None,
                                claim.document_id,
                                claim.execution_token,
                            ),
                        )
                        if claim.retry_scope is not None:
                            revoke_model_operation_retry_scope_in(connection, claim.retry_scope)
                finally:
                    connection.close()
        except (OSError, sqlite3.Error):
            logger.warning(
                "Could not persist PageTree enrichment failure for %s", claim.document_id
            )

    def _connect(self) -> sqlite3.Connection:
        connection = connect_database(self.database_path)
        return connection


def active_page_tree_summaries_in(
    connection: sqlite3.Connection,
    document_id: str,
    base_generation_id: str,
) -> dict[str, str]:
    """Return summaries only when the overlay still targets the current deterministic tree."""
    rows = connection.execute(
        """
        SELECT summaries.node_id, summaries.summary
        FROM document_page_tree_enrichment_current AS current
        JOIN document_page_tree_enrichment_generations AS generations
            ON generations.enrichment_generation_id = current.enrichment_generation_id
            AND generations.status = 'current'
        JOIN document_page_tree_enrichment_summaries AS summaries
            ON summaries.enrichment_generation_id = generations.enrichment_generation_id
        WHERE current.document_id = ? AND current.base_generation_id = ?
            AND generations.base_generation_id = ?
        """,
        (document_id, base_generation_id, base_generation_id),
    ).fetchall()
    return {str(node_id): str(summary) for node_id, summary in rows}


def _queue_target_in(
    connection: sqlite3.Connection,
    document_id: str,
    target: tuple[str, str, str, str],
    reason: str,
    now: str,
    *,
    retry_failed: bool = False,
) -> tuple[bool, str | None]:
    existing = connection.execute(
        """
        SELECT base_generation_id, provider, model, prompt_digest, status, error_code,
            retry_scope
        FROM document_page_tree_enrichment_tasks WHERE document_id = ?
        """,
        (document_id,),
    ).fetchone()
    if existing is not None and tuple(str(value) for value in existing[:4]) == target:
        status = str(existing[4])
        if status in {"pending", "running"} or (
            status == "failed" and not retry_failed and str(existing[5]) != _UNAVAILABLE_CODE
        ):
            return False, None
    previous_scope = str(existing[6]) if existing is not None and existing[6] is not None else None
    connection.execute(
        """
        INSERT INTO document_page_tree_enrichment_tasks (
            document_id, base_generation_id, status, reason, provider, model,
            prompt_digest, execution_token, attempt_count, model_attempt,
            call_id, timeout_seconds, remaining_seconds, error_code, error_reason,
            created_at, updated_at, completed_at
        ) VALUES (?, ?, 'pending', ?, ?, ?, ?, NULL, 0, 0, NULL, NULL, NULL, NULL, NULL,
            ?, ?, NULL)
        ON CONFLICT(document_id) DO UPDATE SET
            base_generation_id = excluded.base_generation_id,
            status = 'pending', reason = excluded.reason, provider = excluded.provider,
            model = excluded.model, prompt_digest = excluded.prompt_digest,
            execution_token = NULL, attempt_count = 0, model_attempt = 0,
            call_id = NULL, timeout_seconds = NULL, remaining_seconds = NULL,
            error_code = NULL, error_reason = NULL, retry_scope = NULL,
            updated_at = excluded.updated_at, completed_at = NULL
        """,
        (
            document_id,
            target[0],
            reason,
            target[1],
            target[2],
            target[3],
            now,
            now,
        ),
    )
    return True, previous_scope


def _complete_repaired_task_in(
    connection: sqlite3.Connection,
    document_id: str,
    target: tuple[str, str, str, str],
    now: str,
) -> str | None:
    previous = connection.execute(
        "SELECT retry_scope FROM document_page_tree_enrichment_tasks WHERE document_id = ?",
        (document_id,),
    ).fetchone()
    cursor = connection.execute(
        """
        INSERT INTO document_page_tree_enrichment_tasks (
            document_id, base_generation_id, status, reason, provider, model,
            prompt_digest, execution_token, attempt_count, model_attempt,
            call_id, timeout_seconds, remaining_seconds, error_code, error_reason,
            created_at, updated_at, completed_at
        ) VALUES (?, ?, 'completed', 'current', ?, ?, ?, NULL, 0, 0,
            NULL, NULL, NULL, NULL, NULL, ?, ?, ?)
        ON CONFLICT(document_id) DO UPDATE SET
            base_generation_id = excluded.base_generation_id, status = 'completed',
            reason = excluded.reason, provider = excluded.provider, model = excluded.model,
            prompt_digest = excluded.prompt_digest, execution_token = NULL,
            attempt_count = 0, model_attempt = 0, call_id = NULL,
            timeout_seconds = NULL, remaining_seconds = NULL, error_code = NULL,
            error_reason = NULL, retry_scope = NULL, updated_at = excluded.updated_at,
            completed_at = excluded.completed_at
        WHERE document_page_tree_enrichment_tasks.base_generation_id
                != excluded.base_generation_id
            OR document_page_tree_enrichment_tasks.provider != excluded.provider
            OR document_page_tree_enrichment_tasks.model != excluded.model
            OR document_page_tree_enrichment_tasks.prompt_digest != excluded.prompt_digest
            OR document_page_tree_enrichment_tasks.status != 'completed'
        """,
        (document_id, *target, now, now, now),
    )
    if cursor.rowcount != 1 or previous is None or previous[0] is None:
        return None
    return str(previous[0])
