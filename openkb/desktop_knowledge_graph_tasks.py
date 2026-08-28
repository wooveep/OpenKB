"""Durable lifecycle and Task Center projection for optional graph extraction."""

from __future__ import annotations

import logging
import sqlite3
import uuid
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path

from openkb.desktop_knowledge_graph import DesktopKnowledgeGraphService
from openkb.desktop_model_event import normalize_model_event
from openkb.desktop_model_gateway import DesktopModelGateway
from openkb.desktop_model_result_failure import (
    authorize_model_operation_retry,
    model_operation_dispatch_possible,
    revoke_model_operation_retry_scope,
)
from openkb.desktop_model_usage import model_activity_for_call_in
from openkb.desktop_prompt_contracts import prompt_contract_for
from openkb.desktop_workspace import desktop_state_database_path, desktop_state_dir
from openkb.locks import kb_ingest_lock

INTERRUPTED_CODE = "knowledge_graph_extraction_interrupted"
INTERRUPTED_REASON = "Knowledge Graph extraction was interrupted and can resume."
FAILED_CODE = "knowledge_graph_extraction_failed"
FAILED_REASON = "Knowledge Graph extraction could not be completed."
PROMPT_DIGEST = prompt_contract_for("knowledge_graph_extraction").digest

StopCallback = Callable[[], bool]
logger = logging.getLogger(__name__)


class DesktopKnowledgeGraphExtractionTasks:
    """Own optional graph work as durable, explicitly controllable tasks."""

    def __init__(self, kb_dir: Path) -> None:
        self.kb_dir = kb_dir.expanduser().resolve()
        self.state_dir = desktop_state_dir(self.kb_dir)
        self.database_path = desktop_state_database_path(self.kb_dir)

    def queue(self, document_id: str, gateway: DesktopModelGateway) -> bool:
        """Create one task for a newly published Available document."""
        now = _timestamp()
        with kb_ingest_lock(self.state_dir):
            connection = self._connect()
            try:
                with connection:
                    cursor = connection.execute(
                        """
                        INSERT INTO knowledge_graph_extraction_tasks (
                            document_id, status, reason, provider, model, prompt_digest,
                            created_at, updated_at
                        )
                        SELECT document_id, 'pending', 'initial', ?, ?, ?, ?, ?
                        FROM source_documents
                        WHERE document_id = ? AND availability = 'available'
                        ON CONFLICT(document_id) DO NOTHING
                        """,
                        (
                            gateway.provider_name,
                            gateway.model_name,
                            PROMPT_DIGEST,
                            now,
                            now,
                            document_id,
                        ),
                    )
                    return cursor.rowcount == 1
            finally:
                connection.close()

    def recover_interrupted(self) -> int:
        """Make every unowned queue item actionable without starting model work."""
        with kb_ingest_lock(self.state_dir):
            connection = self._connect()
            try:
                with connection:
                    cursor = connection.execute(
                        """
                        UPDATE knowledge_graph_extraction_tasks
                        SET status = 'pending', execution_token = NULL,
                            error_code = ?, error_reason = ?, updated_at = ?, completed_at = NULL
                        WHERE status IN ('pending', 'running')
                        """,
                        (INTERRUPTED_CODE, INTERRUPTED_REASON, _timestamp()),
                    )
                    return cursor.rowcount
            finally:
                connection.close()

    def request_cancel(self, document_id: str) -> bool:
        """Invalidate the durable claim before signalling a live model call."""
        with kb_ingest_lock(self.state_dir):
            connection = self._connect()
            try:
                with connection:
                    cursor = connection.execute(
                        """
                        UPDATE knowledge_graph_extraction_tasks
                        SET status = 'pending', execution_token = NULL,
                            error_code = ?, error_reason = ?, updated_at = ?, completed_at = NULL
                        WHERE document_id = ? AND status IN ('pending', 'running')
                        """,
                        (INTERRUPTED_CODE, INTERRUPTED_REASON, _timestamp(), document_id),
                    )
                    return cursor.rowcount == 1
            finally:
                connection.close()

    def retry(self, document_id: str, gateway: DesktopModelGateway) -> bool:
        """Make interrupted or failed work runnable only after a user action."""
        accepted = False
        with kb_ingest_lock(self.state_dir):
            connection = self._connect()
            try:
                with connection:
                    cursor = connection.execute(
                        """
                        UPDATE knowledge_graph_extraction_tasks
                        SET status = 'pending', reason = 'explicit_retry',
                            provider = ?, model = ?, prompt_digest = ?,
                            execution_token = NULL, error_code = NULL, error_reason = NULL,
                            updated_at = ?, completed_at = NULL
                        WHERE document_id = ? AND status IN ('pending', 'failed')
                            AND EXISTS (
                                SELECT 1 FROM source_documents
                                WHERE source_documents.document_id = ?
                                    AND source_documents.availability = 'available'
                            )
                        """,
                        (
                            gateway.provider_name,
                            gateway.model_name,
                            PROMPT_DIGEST,
                            _timestamp(),
                            document_id,
                            document_id,
                        ),
                    )
                    accepted = cursor.rowcount == 1
            finally:
                connection.close()
        if accepted:
            for operation in ("knowledge_graph_extraction", "structured_output_repair"):
                authorize_model_operation_retry(
                    self.kb_dir,
                    gateway,
                    operation=operation,
                    retry_scope=_retry_scope(document_id),
                )
        return accepted

    def pending_document_ids(self, gateway: DesktopModelGateway) -> tuple[str, ...]:
        connection = self._connect()
        try:
            rows = connection.execute(
                """
                SELECT tasks.document_id, tasks.reason
                FROM knowledge_graph_extraction_tasks AS tasks
                JOIN source_documents AS documents ON documents.document_id = tasks.document_id
                WHERE tasks.status = 'pending'
                    AND (tasks.error_code IS NULL
                        OR tasks.error_code = 'model_operation_suspended')
                    AND tasks.provider = ? AND tasks.model = ?
                    AND tasks.prompt_digest = ? AND documents.availability = 'available'
                ORDER BY CASE WHEN tasks.reason = 'explicit_retry' THEN 0 ELSE 1 END,
                    tasks.updated_at, tasks.document_id LIMIT 50
                """,
                (gateway.provider_name, gateway.model_name, PROMPT_DIGEST),
            ).fetchall()
            documents = tuple((str(row[0]), str(row[1])) for row in rows)
        finally:
            connection.close()
        return tuple(
            document_id
            for document_id, reason in documents
            if model_operation_dispatch_possible(
                self.kb_dir,
                gateway,
                operation="knowledge_graph_extraction",
                retry_scope=(
                    _retry_scope(document_id) if reason == "explicit_retry" else None
                ),
            )
        )

    def run_document(
        self,
        document_id: str,
        gateway: DesktopModelGateway,
        *,
        should_stop: StopCallback,
    ) -> bool:
        """Claim and finish one task while preserving an interrupted checkpoint."""
        claim = self._claim(document_id, gateway)
        if claim is None:
            return False
        token, document_name, retry_scope = claim

        def claim_stopped() -> bool:
            return should_stop() or not self._claim_is_active(document_id, token)

        if claim_stopped():
            self._finish(document_id, token, "pending", INTERRUPTED_CODE, INTERRUPTED_REASON)
            if retry_scope is not None:
                revoke_model_operation_retry_scope(self.kb_dir, retry_scope)
            return False
        if not model_operation_dispatch_possible(
            self.kb_dir,
            gateway,
            operation="knowledge_graph_extraction",
            retry_scope=retry_scope,
        ):
            self._finish(
                document_id,
                token,
                "pending",
                "model_operation_suspended",
                "The Knowledge Graph extraction contract is suspended.",
            )
            if retry_scope is not None:
                revoke_model_operation_retry_scope(self.kb_dir, retry_scope)
            return False
        failures: list[tuple[str, str]] = []
        published_claim = False

        def publish_claim(persist: Callable[[sqlite3.Connection], bool]) -> bool:
            nonlocal published_claim
            published_claim = self._publish_and_complete(document_id, token, persist)
            return published_claim

        try:
            DesktopKnowledgeGraphService(self.kb_dir, model_gateway=gateway).extract_document(
                document_id,
                is_cancelled=claim_stopped,
                on_model_event=lambda event: self._record_attempt(document_id, token, event),
                on_failure=lambda code, reason: failures.append((code, reason)),
                publish_transaction=publish_claim,
                retry_scope=retry_scope,
            )
            if published_claim:
                logger.info("knowledge_graph_extraction_completed document_id=%s", document_id)
                return True
            if claim_stopped():
                self._finish(document_id, token, "pending", INTERRUPTED_CODE, INTERRUPTED_REASON)
                return False
            if failures:
                code, reason = failures[0]
                self._finish(document_id, token, "failed", code, reason)
                return False
            completed = self._finish(document_id, token, "completed", None, None)
            if completed:
                logger.info("knowledge_graph_extraction_completed document_id=%s", document_id)
            return completed
        except Exception:
            logger.exception("Knowledge Graph extraction failed for %s", document_name)
            self._finish(document_id, token, "failed", FAILED_CODE, FAILED_REASON)
            return False
        finally:
            if retry_scope is not None:
                revoke_model_operation_retry_scope(self.kb_dir, retry_scope)

    def _publish_and_complete(
        self,
        document_id: str,
        token: str,
        persist: Callable[[sqlite3.Connection], bool],
    ) -> bool:
        """Publish graph rows and consume exactly one live claim in one transaction."""
        with kb_ingest_lock(self.state_dir):
            connection = self._connect()
            try:
                connection.execute("BEGIN IMMEDIATE")
                owned = connection.execute(
                    """
                    SELECT 1 FROM knowledge_graph_extraction_tasks
                    WHERE document_id = ? AND status = 'running' AND execution_token = ?
                    """,
                    (document_id, token),
                ).fetchone()
                if owned is None:
                    connection.rollback()
                    return False
                persist(connection)
                now = _timestamp()
                cursor = connection.execute(
                    """
                    UPDATE knowledge_graph_extraction_tasks
                    SET status = 'completed', execution_token = NULL, error_code = NULL,
                        error_reason = NULL, updated_at = ?, completed_at = ?
                    WHERE document_id = ? AND status = 'running' AND execution_token = ?
                    """,
                    (now, now, document_id, token),
                )
                if cursor.rowcount != 1:
                    connection.rollback()
                    return False
                connection.commit()
                return True
            except BaseException:
                connection.rollback()
                raise
            finally:
                connection.close()

    def _claim(
        self,
        document_id: str,
        gateway: DesktopModelGateway,
    ) -> tuple[str, str, str | None] | None:
        with kb_ingest_lock(self.state_dir):
            connection = self._connect()
            try:
                connection.execute("BEGIN IMMEDIATE")
                row = connection.execute(
                    """
                    SELECT documents.display_name, tasks.reason
                    FROM knowledge_graph_extraction_tasks AS tasks
                    JOIN source_documents AS documents ON documents.document_id = tasks.document_id
                    WHERE tasks.document_id = ? AND tasks.status = 'pending'
                        AND (tasks.error_code IS NULL
                            OR tasks.error_code = 'model_operation_suspended')
                        AND tasks.provider = ?
                        AND tasks.model = ? AND tasks.prompt_digest = ?
                        AND documents.availability = 'available'
                    """,
                    (document_id, gateway.provider_name, gateway.model_name, PROMPT_DIGEST),
                ).fetchone()
                if row is None:
                    connection.rollback()
                    return None
                token = uuid.uuid4().hex
                cursor = connection.execute(
                    """
                    UPDATE knowledge_graph_extraction_tasks
                    SET status = 'running', execution_token = ?,
                        attempt_count = attempt_count + 1, model_attempt = 0,
                        call_id = NULL, error_code = NULL, error_reason = NULL,
                        updated_at = ?, completed_at = NULL
                    WHERE document_id = ? AND status = 'pending'
                        AND (error_code IS NULL OR error_code = 'model_operation_suspended')
                    """,
                    (token, _timestamp(), document_id),
                )
                if cursor.rowcount != 1:
                    connection.rollback()
                    return None
                connection.commit()
                retry_scope = (
                    _retry_scope(document_id) if str(row[1]) == "explicit_retry" else None
                )
                return token, str(row[0]), retry_scope
            except BaseException:
                connection.rollback()
                raise
            finally:
                connection.close()

    def _claim_is_active(self, document_id: str, token: str) -> bool:
        """Keep a retried task from reviving a superseded provider attempt."""
        try:
            connection = self._connect()
            try:
                return (
                    connection.execute(
                        """
                        SELECT 1 FROM knowledge_graph_extraction_tasks
                        WHERE document_id = ? AND status = 'running' AND execution_token = ?
                        """,
                        (document_id, token),
                    ).fetchone()
                    is not None
                )
            finally:
                connection.close()
        except (OSError, sqlite3.Error):
            return False

    def _record_attempt(self, document_id: str, token: str, event: object) -> None:
        lifecycle = normalize_model_event(event)
        with kb_ingest_lock(self.state_dir):
            connection = self._connect()
            try:
                with connection:
                    connection.execute(
                        """
                        UPDATE knowledge_graph_extraction_tasks
                        SET model_attempt = ?, call_id = ?, error_code = ?,
                            error_reason = ?, updated_at = ?
                        WHERE document_id = ? AND status = 'running' AND execution_token = ?
                        """,
                        (
                            lifecycle.attempt,
                            lifecycle.call_id,
                            lifecycle.error_code,
                            lifecycle.reason,
                            _timestamp(),
                            document_id,
                            token,
                        ),
                    )
            finally:
                connection.close()

    def _finish(
        self,
        document_id: str,
        token: str,
        status: str,
        error_code: str | None,
        error_reason: str | None,
    ) -> bool:
        try:
            with kb_ingest_lock(self.state_dir):
                connection = self._connect()
                try:
                    with connection:
                        now = _timestamp()
                        cursor = connection.execute(
                            """
                            UPDATE knowledge_graph_extraction_tasks
                            SET status = ?, execution_token = NULL, error_code = ?,
                                error_reason = ?, updated_at = ?, completed_at = ?
                            WHERE document_id = ? AND status = 'running' AND execution_token = ?
                            """,
                            (
                                status,
                                error_code,
                                error_reason,
                                now,
                                now if status in {"failed", "completed"} else None,
                                document_id,
                                token,
                            ),
                        )
                        return cursor.rowcount == 1
                finally:
                    connection.close()
        except (OSError, sqlite3.Error):
            logger.warning("Could not persist Knowledge Graph task state for %s", document_id)
            return False

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path)
        connection.execute("PRAGMA foreign_keys = ON")
        return connection


def knowledge_graph_extraction_tasks_in(
    connection: sqlite3.Connection,
) -> list[dict[str, object]]:
    """Project content-free graph task state for the global Task Center."""
    rows = connection.execute(
        """
        SELECT tasks.document_id, documents.display_name,
            CASE
                WHEN tasks.status = 'completed' THEN COALESCE(results.status, tasks.status)
                ELSE tasks.status
            END,
            CASE WHEN tasks.status = 'completed' THEN COALESCE(results.node_count, 0) ELSE 0 END,
            CASE WHEN tasks.status = 'completed' THEN COALESCE(results.edge_count, 0) ELSE 0 END,
            tasks.reason, tasks.provider, tasks.model, tasks.attempt_count, tasks.model_attempt,
            tasks.call_id, tasks.error_code, tasks.error_reason,
            tasks.updated_at, tasks.completed_at
        FROM knowledge_graph_extraction_tasks AS tasks
        JOIN source_documents AS documents ON documents.document_id = tasks.document_id
        LEFT JOIN knowledge_graph_current AS current
            ON current.document_id = tasks.document_id
        LEFT JOIN knowledge_graph_results AS results ON results.result_id = current.result_id
        ORDER BY tasks.updated_at DESC, tasks.document_id LIMIT 50
        """
    ).fetchall()
    return [
        {
            "document_id": str(row[0]),
            "document_name": str(row[1]),
            "status": str(row[2]),
            "node_count": int(row[3]),
            "edge_count": int(row[4]),
            "reason": str(row[5]),
            "provider": str(row[6]),
            "model": str(row[7]),
            "attempt_count": int(row[8]),
            "model_attempt": int(row[9]),
            "call_id": str(row[10]) if row[10] is not None else None,
            "error_code": str(row[11]) if row[11] is not None else None,
            "error_reason": str(row[12]) if row[12] is not None else None,
            "updated_at": str(row[13]),
            "completed_at": str(row[14]) if row[14] is not None else None,
            "model_activity": (
                model_activity_for_call_in(connection, str(row[10]))
                if row[10] is not None
                else None
            ),
        }
        for row in rows
    ]


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()


def _retry_scope(document_id: str) -> str:
    return f"knowledge_graph_extraction:{document_id}"
