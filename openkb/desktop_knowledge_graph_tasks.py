"""Durable lifecycle and Task Center projection for optional graph extraction."""

from __future__ import annotations

import logging
import sqlite3
import uuid
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path

from openkb.desktop_candidate_registry import (
    CandidateRegistryStatus,
    candidate_registry_outcome_in,
)
from openkb.desktop_corpus_knowledge_pipeline import CorpusKnowledgeSynthesisPipeline
from openkb.desktop_knowledge_graph import DesktopKnowledgeGraphService
from openkb.desktop_model_event import normalize_model_event
from openkb.desktop_model_gateway import DesktopModelGateway
from openkb.desktop_model_result_failure import (
    authorize_model_operation_retry_group_in,
    model_operation_dispatch_possible,
    revoke_model_operation_retry_scope_in,
)
from openkb.desktop_model_usage import model_activity_for_call_in
from openkb.desktop_prompt_contracts import prompt_contract_for
from openkb.desktop_semantic_graph import semantic_graph_operation_for_document_in
from openkb.desktop_structured_output import structured_output_repair_contract_digest
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
                    registry = candidate_registry_outcome_in(connection, document_id)
                    operation = semantic_graph_operation_for_document_in(connection, document_id)
                    prompt_digest = prompt_contract_for(operation).digest
                    generation = registry.generation
                    input_provenance = (
                        "semantic" if registry.status in {"ready", "empty"} else registry.status
                    )
                    cursor = connection.execute(
                        """
                        INSERT INTO knowledge_graph_extraction_tasks (
                            document_id, status, reason, provider, model, prompt_digest,
                            created_at, updated_at, input_provenance,
                            candidate_generation_id, candidate_generation_digest
                        )
                        SELECT document_id, 'pending', ?, ?, ?, ?, ?, ?, ?, ?, ?
                        FROM source_documents
                        WHERE document_id = ? AND availability = 'available'
                        ON CONFLICT(document_id) DO UPDATE SET
                            status = 'pending', reason = excluded.reason,
                            provider = excluded.provider, model = excluded.model,
                            prompt_digest = excluded.prompt_digest,
                            execution_token = NULL, retry_scope = NULL,
                            error_code = NULL, error_reason = NULL,
                            updated_at = excluded.updated_at, completed_at = NULL,
                            input_provenance = excluded.input_provenance,
                            candidate_generation_id = excluded.candidate_generation_id,
                            candidate_generation_digest = excluded.candidate_generation_digest
                        WHERE knowledge_graph_extraction_tasks.input_provenance
                                IS NOT excluded.input_provenance
                           OR knowledge_graph_extraction_tasks.candidate_generation_id
                                IS NOT excluded.candidate_generation_id
                           OR knowledge_graph_extraction_tasks.candidate_generation_digest
                                IS NOT excluded.candidate_generation_digest
                        """,
                        (
                            (
                                "candidate_generation_unavailable"
                                if registry.status == "dependency_unavailable"
                                else "initial"
                            ),
                            gateway.provider_name,
                            gateway.model_name,
                            prompt_digest,
                            now,
                            now,
                            input_provenance,
                            generation.generation_id if generation is not None else None,
                            generation.registry_digest if generation is not None else None,
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
                    retry_scopes = tuple(
                        str(row[0])
                        for row in connection.execute(
                            """
                            SELECT DISTINCT retry_scope
                            FROM knowledge_graph_extraction_tasks
                            WHERE status IN ('pending', 'running') AND retry_scope IS NOT NULL
                            """
                        ).fetchall()
                    )
                    for retry_scope in retry_scopes:
                        revoke_model_operation_retry_scope_in(connection, retry_scope)
                    cursor = connection.execute(
                        """
                        UPDATE knowledge_graph_extraction_tasks
                        SET status = 'pending', execution_token = NULL,
                            retry_scope = NULL, error_code = ?, error_reason = ?,
                            updated_at = ?, completed_at = NULL
                        WHERE status IN ('pending', 'running')
                        """,
                        (INTERRUPTED_CODE, INTERRUPTED_REASON, _timestamp()),
                    )
                    recovered = cursor.rowcount
            finally:
                connection.close()
        return recovered

    def request_cancel(self, document_id: str) -> bool:
        """Invalidate the durable claim before signalling a live model call."""
        retry_scope: str | None = None
        with kb_ingest_lock(self.state_dir):
            connection = self._connect()
            try:
                with connection:
                    row = connection.execute(
                        """
                        SELECT retry_scope FROM knowledge_graph_extraction_tasks
                        WHERE document_id = ? AND status IN ('pending', 'running')
                        """,
                        (document_id,),
                    ).fetchone()
                    retry_scope = str(row[0]) if row is not None and row[0] is not None else None
                    cursor = connection.execute(
                        """
                        UPDATE knowledge_graph_extraction_tasks
                        SET status = 'pending', execution_token = NULL,
                            retry_scope = NULL, error_code = ?, error_reason = ?,
                            updated_at = ?, completed_at = NULL
                        WHERE document_id = ? AND status IN ('pending', 'running')
                        """,
                        (INTERRUPTED_CODE, INTERRUPTED_REASON, _timestamp(), document_id),
                    )
                    cancelled = cursor.rowcount == 1
                    if cancelled and retry_scope is not None:
                        revoke_model_operation_retry_scope_in(connection, retry_scope)
            finally:
                connection.close()
        return cancelled

    def retry(self, document_id: str, gateway: DesktopModelGateway) -> bool:
        """Make interrupted or failed work runnable only after a user action."""
        accepted = False
        previous_scope: str | None = None
        retry_scope = _new_retry_scope(document_id)
        with kb_ingest_lock(self.state_dir):
            connection = self._connect()
            try:
                with connection:
                    registry = candidate_registry_outcome_in(connection, document_id)
                    if registry.status == "dependency_unavailable":
                        return False
                    operation = semantic_graph_operation_for_document_in(connection, document_id)
                    prompt_digest = prompt_contract_for(operation).digest
                    generation = registry.generation
                    input_provenance = (
                        "semantic" if registry.status in {"ready", "empty"} else registry.status
                    )
                    previous = connection.execute(
                        """
                        SELECT retry_scope FROM knowledge_graph_extraction_tasks
                        WHERE document_id = ?
                        """,
                        (document_id,),
                    ).fetchone()
                    previous_scope = (
                        str(previous[0])
                        if previous is not None and previous[0] is not None
                        else None
                    )
                    cursor = connection.execute(
                        """
                        UPDATE knowledge_graph_extraction_tasks
                        SET status = 'pending', reason = 'explicit_retry',
                            provider = ?, model = ?, prompt_digest = ?,
                            execution_token = NULL, retry_scope = ?, error_code = NULL,
                            error_reason = NULL, updated_at = ?, completed_at = NULL,
                            input_provenance = ?, candidate_generation_id = ?,
                            candidate_generation_digest = ?
                        WHERE document_id = ? AND (
                                status IN ('pending', 'failed')
                                OR (
                                    status = 'completed'
                                    AND (
                                        SELECT latest.quality
                                        FROM knowledge_graph_results AS latest
                                        WHERE latest.document_id =
                                            knowledge_graph_extraction_tasks.document_id
                                        ORDER BY latest.created_at DESC, latest.result_id DESC
                                        LIMIT 1
                                    ) = 'degraded'
                                )
                            )
                            AND EXISTS (
                                SELECT 1 FROM source_documents
                                WHERE source_documents.document_id = ?
                                    AND source_documents.availability = 'available'
                            )
                        """,
                        (
                            gateway.provider_name,
                            gateway.model_name,
                            prompt_digest,
                            retry_scope,
                            _timestamp(),
                            input_provenance,
                            generation.generation_id if generation is not None else None,
                            generation.registry_digest if generation is not None else None,
                            document_id,
                            document_id,
                        ),
                    )
                    accepted = cursor.rowcount == 1
                    if accepted:
                        authorize_model_operation_retry_group_in(
                            connection,
                            gateway,
                            retry_scope=retry_scope,
                            contracts=(
                                (operation, None),
                                (
                                    "structured_output_repair",
                                    structured_output_repair_contract_digest(operation),
                                ),
                                ("entity_dossier_planning", None),
                                (
                                    "structured_output_repair",
                                    structured_output_repair_contract_digest(
                                        "entity_dossier_planning"
                                    ),
                                ),
                            ),
                        )
                        if previous_scope is not None:
                            revoke_model_operation_retry_scope_in(connection, previous_scope)
            finally:
                connection.close()
        return accepted

    def pending_document_ids(self, gateway: DesktopModelGateway) -> tuple[str, ...]:
        connection = self._connect()
        try:
            rows = connection.execute(
                """
                SELECT tasks.document_id, tasks.retry_scope, tasks.input_provenance,
                    COALESCE(generations.admitted_count, 0)
                FROM knowledge_graph_extraction_tasks AS tasks
                JOIN source_documents AS documents ON documents.document_id = tasks.document_id
                LEFT JOIN knowledge_candidate_registry_state AS registry
                  ON registry.document_id = tasks.document_id
                LEFT JOIN knowledge_candidate_generations AS generations
                  ON generations.candidate_generation_id = tasks.candidate_generation_id
                WHERE tasks.status = 'pending'
                    AND (tasks.error_code IS NULL
                        OR tasks.error_code = 'model_operation_suspended')
                    AND tasks.provider = ? AND tasks.model = ?
                    AND documents.availability = 'available'
                    AND tasks.input_provenance != 'dependency_unavailable'
                    AND (
                        (tasks.input_provenance = 'explicit_legacy'
                            AND registry.provenance_state = 'explicit_legacy')
                        OR
                        (tasks.input_provenance = 'semantic'
                            AND registry.provenance_state = 'semantic'
                            AND registry.current_candidate_generation_id
                                = tasks.candidate_generation_id
                            AND generations.registry_digest
                                = tasks.candidate_generation_digest)
                    )
                    AND (tasks.reason != 'explicit_retry' OR tasks.retry_scope IS NOT NULL)
                ORDER BY CASE WHEN tasks.reason = 'explicit_retry' THEN 0 ELSE 1 END,
                    tasks.updated_at, tasks.document_id LIMIT 50
                """,
                (gateway.provider_name, gateway.model_name),
            ).fetchall()
            documents = tuple(
                (
                    str(row[0]),
                    str(row[1]) if row[1] is not None else None,
                    (
                        "knowledge_graph_extraction"
                        if str(row[2]) == "explicit_legacy"
                        else "knowledge_relation_analysis"
                    ),
                    "empty" if str(row[2]) == "semantic" and int(row[3]) == 0 else "ready",
                )
                for row in rows
            )
        finally:
            connection.close()
        return tuple(
            document_id
            for document_id, retry_scope, operation, registry_status in documents
            if registry_status == "empty"
            or model_operation_dispatch_possible(
                self.kb_dir,
                gateway,
                operation=operation,
                retry_scope=retry_scope,
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
        token, document_name, retry_scope, operation, registry_status = claim

        def claim_stopped() -> bool:
            return should_stop() or not self._claim_is_active(document_id, token)

        if claim_stopped():
            self._finish(document_id, token, "pending", INTERRUPTED_CODE, INTERRUPTED_REASON)
            return False
        if registry_status != "empty" and not model_operation_dispatch_possible(
            self.kb_dir,
            gateway,
            operation=operation,
            retry_scope=retry_scope,
        ):
            self._finish(
                document_id,
                token,
                "pending",
                "model_operation_suspended",
                "The Knowledge Graph extraction contract is suspended.",
            )
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
                if registry_status != "explicit_legacy":
                    CorpusKnowledgeSynthesisPipeline(self.kb_dir).run_generation(
                        should_stop=should_stop,
                        force_generation=True,
                        gateway=gateway,
                        retry_scope=retry_scope,
                    )
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
                    SELECT tasks.retry_scope
                    FROM knowledge_graph_extraction_tasks AS tasks
                    LEFT JOIN knowledge_candidate_registry_state AS registry
                      ON registry.document_id = tasks.document_id
                    LEFT JOIN knowledge_candidate_generations AS generations
                      ON generations.candidate_generation_id = tasks.candidate_generation_id
                    WHERE tasks.document_id = ? AND tasks.status = 'running'
                        AND tasks.execution_token = ?
                        AND (
                            (tasks.input_provenance = 'explicit_legacy'
                                AND registry.provenance_state = 'explicit_legacy')
                            OR
                            (tasks.input_provenance = 'semantic'
                                AND registry.provenance_state = 'semantic'
                                AND registry.current_candidate_generation_id
                                    = tasks.candidate_generation_id
                                AND generations.registry_digest
                                    = tasks.candidate_generation_digest)
                        )
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
                        error_reason = NULL, retry_scope = NULL,
                        updated_at = ?, completed_at = ?
                    WHERE document_id = ? AND status = 'running' AND execution_token = ?
                    """,
                    (now, now, document_id, token),
                )
                if cursor.rowcount != 1:
                    connection.rollback()
                    return False
                if owned[0] is not None:
                    revoke_model_operation_retry_scope_in(connection, str(owned[0]))
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
    ) -> tuple[str, str, str | None, str, CandidateRegistryStatus] | None:
        with kb_ingest_lock(self.state_dir):
            connection = self._connect()
            try:
                connection.execute("BEGIN IMMEDIATE")
                row = connection.execute(
                    """
                    SELECT documents.display_name, tasks.reason, tasks.retry_scope,
                        tasks.input_provenance, COALESCE(generations.admitted_count, 0)
                    FROM knowledge_graph_extraction_tasks AS tasks
                    JOIN source_documents AS documents ON documents.document_id = tasks.document_id
                    LEFT JOIN knowledge_candidate_registry_state AS registry
                      ON registry.document_id = tasks.document_id
                    LEFT JOIN knowledge_candidate_generations AS generations
                      ON generations.candidate_generation_id = tasks.candidate_generation_id
                    WHERE tasks.document_id = ? AND tasks.status = 'pending'
                        AND (tasks.error_code IS NULL
                            OR tasks.error_code = 'model_operation_suspended')
                        AND tasks.provider = ?
                        AND tasks.model = ?
                        AND documents.availability = 'available'
                        AND tasks.input_provenance != 'dependency_unavailable'
                        AND (
                            (tasks.input_provenance = 'explicit_legacy'
                                AND registry.provenance_state = 'explicit_legacy')
                            OR
                            (tasks.input_provenance = 'semantic'
                                AND registry.provenance_state = 'semantic'
                                AND registry.current_candidate_generation_id
                                    = tasks.candidate_generation_id
                                AND generations.registry_digest
                                    = tasks.candidate_generation_digest)
                        )
                        AND (tasks.reason != 'explicit_retry' OR tasks.retry_scope IS NOT NULL)
                    """,
                    (document_id, gateway.provider_name, gateway.model_name),
                ).fetchone()
                if row is None:
                    connection.rollback()
                    return None
                operation = (
                    "knowledge_graph_extraction"
                    if str(row[3]) == "explicit_legacy"
                    else "knowledge_relation_analysis"
                )
                prompt_digest = prompt_contract_for(operation).digest
                token = uuid.uuid4().hex
                cursor = connection.execute(
                    """
                    UPDATE knowledge_graph_extraction_tasks
                    SET status = 'running', execution_token = ?,
                        attempt_count = attempt_count + 1, model_attempt = 0,
                        call_id = NULL, error_code = NULL, error_reason = NULL,
                        prompt_digest = ?, updated_at = ?, completed_at = NULL
                    WHERE document_id = ? AND status = 'pending'
                        AND (error_code IS NULL OR error_code = 'model_operation_suspended')
                    """,
                    (token, prompt_digest, _timestamp(), document_id),
                )
                if cursor.rowcount != 1:
                    connection.rollback()
                    return None
                connection.commit()
                retry_scope = (
                    str(row[2]) if str(row[1]) == "explicit_retry" and row[2] is not None else None
                )
                registry_status: CandidateRegistryStatus = (
                    "explicit_legacy"
                    if str(row[3]) == "explicit_legacy"
                    else ("empty" if int(row[4]) == 0 else "ready")
                )
                return token, str(row[0]), retry_scope, operation, registry_status
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
                        SELECT 1
                        FROM knowledge_graph_extraction_tasks AS tasks
                        LEFT JOIN knowledge_candidate_registry_state AS registry
                          ON registry.document_id = tasks.document_id
                        LEFT JOIN knowledge_candidate_generations AS generations
                          ON generations.candidate_generation_id = tasks.candidate_generation_id
                        WHERE tasks.document_id = ? AND tasks.status = 'running'
                            AND tasks.execution_token = ?
                            AND (
                                (tasks.input_provenance = 'explicit_legacy'
                                    AND registry.provenance_state = 'explicit_legacy')
                                OR
                                (tasks.input_provenance = 'semantic'
                                    AND registry.provenance_state = 'semantic'
                                    AND registry.current_candidate_generation_id
                                        = tasks.candidate_generation_id
                                    AND generations.registry_digest
                                        = tasks.candidate_generation_digest)
                            )
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
                        owned = connection.execute(
                            """
                            SELECT retry_scope FROM knowledge_graph_extraction_tasks
                            WHERE document_id = ? AND status = 'running'
                                AND execution_token = ?
                            """,
                            (document_id, token),
                        ).fetchone()
                        cursor = connection.execute(
                            """
                            UPDATE knowledge_graph_extraction_tasks
                            SET status = ?, execution_token = NULL, error_code = ?,
                                error_reason = ?, retry_scope = NULL,
                                updated_at = ?, completed_at = ?
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
                        if cursor.rowcount == 1 and owned is not None and owned[0] is not None:
                            revoke_model_operation_retry_scope_in(connection, str(owned[0]))
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
            CASE WHEN tasks.status = 'completed' THEN results.quality ELSE NULL END,
            CASE WHEN tasks.status = 'completed'
                THEN COALESCE(results.retained_count, 0) ELSE 0 END,
            CASE WHEN tasks.status = 'completed'
                THEN COALESCE(results.weakened_count, 0) ELSE 0 END,
            CASE WHEN tasks.status = 'completed'
                THEN COALESCE(results.rejected_count, 0) ELSE 0 END,
            tasks.reason, tasks.provider, tasks.model, tasks.attempt_count, tasks.model_attempt,
            tasks.call_id, tasks.error_code, tasks.error_reason,
            tasks.updated_at, tasks.completed_at
        FROM knowledge_graph_extraction_tasks AS tasks
        JOIN source_documents AS documents ON documents.document_id = tasks.document_id
        LEFT JOIN knowledge_graph_results AS results ON results.result_id = (
            SELECT latest.result_id FROM knowledge_graph_results AS latest
            WHERE latest.document_id = tasks.document_id
            ORDER BY latest.created_at DESC, latest.result_id DESC LIMIT 1
        )
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
            "quality": str(row[5]) if row[5] is not None else None,
            "retained_count": int(row[6]),
            "weakened_count": int(row[7]),
            "rejected_count": int(row[8]),
            "reason": str(row[9]),
            "provider": str(row[10]),
            "model": str(row[11]),
            "attempt_count": int(row[12]),
            "model_attempt": int(row[13]),
            "call_id": str(row[14]) if row[14] is not None else None,
            "error_code": str(row[15]) if row[15] is not None else None,
            "error_reason": str(row[16]) if row[16] is not None else None,
            "updated_at": str(row[17]),
            "completed_at": str(row[18]) if row[18] is not None else None,
            "model_activity": (
                model_activity_for_call_in(connection, str(row[14]))
                if row[14] is not None
                else None
            ),
        }
        for row in rows
    ]


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()


def _new_retry_scope(document_id: str) -> str:
    return f"knowledge_graph_extraction:{document_id}:{uuid.uuid4().hex}"
