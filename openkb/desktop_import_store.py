"""SQLite state transitions for resumable Desktop document imports."""

from __future__ import annotations

import json
import logging
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from portalocker import LockException

from openkb.desktop_import_artifacts import DesktopImportError, DocumentIRBlock
from openkb.desktop_import_queries import (
    find_available_document_in,
    stages_for_job,
    task_from_row,
)
from openkb.desktop_import_types import (
    DesktopImportedDocument,
    DesktopImportTask,
    DesktopStageRun,
)
from openkb.desktop_workspace import desktop_state_database_path, desktop_state_dir
from openkb.locks import atomic_write_bytes, kb_ingest_lock

IMPORT_STAGES = (
    "preflight",
    "raw_asset",
    "document_ir",
    "evidence",
    "model_analysis",
    "search",
)
_STAGE_ORDER_SQL = (
    "CASE stage "
    + " ".join(
        f"WHEN '{stage}' THEN {ordinal}" for ordinal, stage in enumerate(IMPORT_STAGES, start=1)
    )
    + " END"
)
_BASE_STAGE_STATUSES = {"pending", "running", "completed", "failed", "skipped"}
StageProgressCallback = Callable[[dict[str, object]], None]
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ImportJobState:
    job_id: str
    source: Path
    status: str
    stage_ids: dict[str, str]
    recovery_run_id: str | None = None


class DesktopImportStore:
    def __init__(
        self,
        kb_dir: Path,
        *,
        on_stage_progress: StageProgressCallback | None = None,
    ) -> None:
        self.kb_dir = kb_dir.expanduser().resolve()
        self.state_dir = desktop_state_dir(self.kb_dir)
        self.database_path = desktop_state_database_path(self.kb_dir)
        self._on_stage_progress = on_stage_progress
        self.lease_owner = uuid.uuid4().hex

    def require_database(self) -> None:
        if not self.database_path.is_file():
            raise DesktopImportError(
                "desktop_knowledge_base_not_found",
                f"Not a Desktop Knowledge Base: {self.kb_dir}",
            )

    def create_job(self, source: Path) -> ImportJobState:
        job_id = uuid.uuid4().hex
        stage_ids = {stage: uuid.uuid4().hex for stage in IMPORT_STAGES}
        now = _timestamp()
        with kb_ingest_lock(self.state_dir):
            connection = self._connect()
            try:
                with connection:
                    connection.execute(
                        """
                        INSERT INTO import_jobs (
                            job_id, source_path, document_id, status, progress, error_code,
                            created_at, completed_at
                        ) VALUES (?, ?, NULL, 'running', 0, NULL, ?, NULL)
                        """,
                        (job_id, str(source), now),
                    )
                    connection.execute(
                        """
                        INSERT INTO import_job_runtime (
                            job_id, status, lease_owner, lease_expires_at, updated_at
                        ) VALUES (?, 'running', ?, ?, ?)
                        """,
                        (job_id, self.lease_owner, _lease_expiry(), now),
                    )
                    connection.executemany(
                        """
                        INSERT INTO stage_runs (
                            stage_run_id, job_id, stage, status, progress, error_code,
                            started_at, completed_at
                        ) VALUES (?, ?, ?, 'pending', 0, NULL, NULL, NULL)
                        """,
                        [(stage_ids[stage], job_id, stage) for stage in IMPORT_STAGES],
                    )
                    connection.executemany(
                        """
                        INSERT INTO stage_run_runtime (
                            stage_run_id, job_id, status, checkpoint_json, error_code, updated_at
                        ) VALUES (?, ?, 'pending', NULL, NULL, ?)
                        """,
                        [(stage_ids[stage], job_id, now) for stage in IMPORT_STAGES],
                    )
            finally:
                connection.close()
        return ImportJobState(job_id=job_id, source=source, status="running", stage_ids=stage_ids)

    def resume_job(self, job_id: str) -> ImportJobState:
        with kb_ingest_lock(self.state_dir):
            connection = self._connect()
            try:
                now = _timestamp()
                connection.execute("BEGIN IMMEDIATE")
                row = connection.execute(
                    """
                    SELECT import_jobs.source_path, import_job_runtime.status
                    FROM import_jobs
                    JOIN import_job_runtime ON import_job_runtime.job_id = import_jobs.job_id
                    WHERE import_jobs.job_id = ?
                    """,
                    (job_id,),
                ).fetchone()
                if row is None:
                    raise DesktopImportError("import_job_not_found", f"Job not found: {job_id}")
                status = str(row[1])
                if status not in {"paused", "recoverable"}:
                    raise DesktopImportError(
                        "import_job_not_resumable", f"Job {job_id} is {status}; not resumable."
                    )
                connection.execute(
                    """
                    UPDATE import_job_runtime
                    SET status = 'running', lease_owner = ?, lease_expires_at = ?, updated_at = ?
                    WHERE job_id = ?
                    """,
                    (self.lease_owner, _lease_expiry(), now, job_id),
                )
                connection.execute(
                    """
                    UPDATE stage_run_runtime
                    SET status = 'pending', error_code = NULL, updated_at = ?
                    WHERE job_id = ? AND status = 'paused'
                    """,
                    (now, job_id),
                )
                connection.commit()
                state = self._job_state_in(connection, job_id, Path(str(row[0])), "running")
            except BaseException:
                connection.rollback()
                raise
            finally:
                connection.close()
        return state

    def resumable_job_ids(self) -> tuple[str, ...]:
        connection = self._connect()
        try:
            rows = connection.execute(
                """
                SELECT import_jobs.job_id
                FROM import_jobs
                JOIN import_job_runtime ON import_job_runtime.job_id = import_jobs.job_id
                WHERE import_job_runtime.status = 'recoverable'
                    AND NOT EXISTS (
                        SELECT 1 FROM quarantined_documents
                        WHERE quarantined_documents.job_id = import_jobs.job_id
                    )
                ORDER BY import_jobs.created_at
                """
            ).fetchall()
        finally:
            connection.close()
        return tuple(str(row[0]) for row in rows)

    def list_import_jobs(self) -> dict[str, object]:
        self.require_database()
        connection = self._connect()
        try:
            rows = connection.execute(
                """
                SELECT import_jobs.job_id, COALESCE(import_job_runtime.status, import_jobs.status),
                    import_jobs.progress, import_jobs.document_id, source_documents.document_id,
                    source_documents.display_name, source_documents.source_format,
                    source_documents.asset_sha256, source_documents.availability,
                    (SELECT COUNT(*) FROM evidence_refs
                     WHERE evidence_refs.document_id = source_documents.document_id),
                    import_jobs.source_path
                FROM import_jobs
                LEFT JOIN import_job_runtime ON import_job_runtime.job_id = import_jobs.job_id
                LEFT JOIN source_documents ON source_documents.document_id = import_jobs.document_id
                ORDER BY import_jobs.created_at DESC
                """
            ).fetchall()
            tasks = tuple(task_from_row(connection, row, _STAGE_ORDER_SQL) for row in rows)
        finally:
            connection.close()
        return {"jobs": [task.as_dict() for task in tasks]}

    def task(self, job_id: str) -> DesktopImportTask:
        connection = self._connect()
        try:
            row = connection.execute(
                """
                SELECT import_jobs.job_id, COALESCE(import_job_runtime.status, import_jobs.status),
                    import_jobs.progress, import_jobs.document_id, source_documents.document_id,
                    source_documents.display_name, source_documents.source_format,
                    source_documents.asset_sha256, source_documents.availability,
                    (SELECT COUNT(*) FROM evidence_refs
                     WHERE evidence_refs.document_id = source_documents.document_id),
                    import_jobs.source_path
                FROM import_jobs
                LEFT JOIN import_job_runtime ON import_job_runtime.job_id = import_jobs.job_id
                LEFT JOIN source_documents ON source_documents.document_id = import_jobs.document_id
                WHERE import_jobs.job_id = ?
                """,
                (job_id,),
            ).fetchone()
            if row is None:
                raise DesktopImportError("import_job_not_found", f"Job not found: {job_id}")
            return task_from_row(connection, row, _STAGE_ORDER_SQL)
        finally:
            connection.close()

    def job_state(self, job_id: str) -> ImportJobState:
        connection = self._connect()
        try:
            row = connection.execute(
                """
                SELECT import_jobs.source_path,
                    COALESCE(import_job_runtime.status, import_jobs.status)
                FROM import_jobs
                LEFT JOIN import_job_runtime ON import_job_runtime.job_id = import_jobs.job_id
                WHERE import_jobs.job_id = ?
                """,
                (job_id,),
            ).fetchone()
            if row is None:
                raise DesktopImportError("import_job_not_found", f"Job not found: {job_id}")
            return self._job_state_in(connection, job_id, Path(str(row[0])), str(row[1]))
        finally:
            connection.close()

    def stage_runs(self, job_id: str) -> tuple[DesktopStageRun, ...]:
        connection = self._connect()
        try:
            return stages_for_job(connection, job_id, _STAGE_ORDER_SQL)
        finally:
            connection.close()

    def checkpoint(self, stage_run_id: str) -> object | None:
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT checkpoint_json FROM stage_run_runtime WHERE stage_run_id = ?",
                (stage_run_id,),
            ).fetchone()
        finally:
            connection.close()
        if row is None or row[0] is None:
            return None
        try:
            return json.loads(str(row[0]))
        except json.JSONDecodeError as error:
            raise DesktopImportError(
                "import_checkpoint_invalid", f"Invalid checkpoint for {stage_run_id}."
            ) from error

    def set_stage(
        self,
        state: ImportJobState,
        stage: str,
        status: str,
        progress: int,
        *,
        checkpoint: object | None = None,
        error_code: str | None = None,
    ) -> None:
        """Persist one stage transition and renew the owning job lease."""
        stage_run_id = state.stage_ids[stage]
        now = _timestamp()
        base_status = status if status in _BASE_STAGE_STATUSES else "pending"
        started_at = now if status == "running" else None
        completed_at = now if status in {"completed", "failed", "skipped"} else None
        checkpoint_json = None if checkpoint is None else json.dumps(checkpoint, ensure_ascii=False)
        with kb_ingest_lock(self.state_dir):
            connection = self._connect()
            try:
                with connection:
                    cursor = connection.execute(
                        """
                        UPDATE stage_runs
                        SET status = ?, progress = ?, error_code = ?,
                            started_at = COALESCE(started_at, ?),
                            completed_at = COALESCE(?, completed_at)
                        WHERE stage_run_id = ? AND job_id = ?
                        """,
                        (
                            base_status,
                            progress,
                            error_code,
                            started_at,
                            completed_at,
                            stage_run_id,
                            state.job_id,
                        ),
                    )
                    if cursor.rowcount != 1:
                        raise DesktopImportError(
                            "desktop_import_state_invalid",
                            f"Import stage is missing for job {state.job_id}.",
                        )
                    connection.execute(
                        "UPDATE import_jobs SET progress = ? WHERE job_id = ?",
                        (progress, state.job_id),
                    )
                    connection.execute(
                        """
                        UPDATE stage_run_runtime
                        SET status = ?, checkpoint_json = COALESCE(?, checkpoint_json),
                            error_code = ?, updated_at = ?
                        WHERE stage_run_id = ? AND job_id = ?
                        """,
                        (status, checkpoint_json, error_code, now, stage_run_id, state.job_id),
                    )
                    connection.execute(
                        """
                        UPDATE import_job_runtime
                        SET status = 'running', lease_owner = ?, lease_expires_at = ?, updated_at=?
                        WHERE job_id = ?
                        """,
                        (self.lease_owner, _lease_expiry(), now, state.job_id),
                    )
            finally:
                connection.close()
        self._emit_stage(
            state.job_id, DesktopStageRun(stage_run_id, stage, status, progress, error_code)
        )

    def write_raw_asset(self, asset_sha256: str, content: bytes) -> str:
        raw_relative_path = Path("raw") / f"{asset_sha256}.txt"
        with kb_ingest_lock(self.state_dir):
            atomic_write_bytes(self.kb_dir / raw_relative_path, content)
        return raw_relative_path.as_posix()

    def emit_stage(
        self,
        state: ImportJobState,
        stage: str,
        status: str,
        progress: int,
        *,
        document_id: str | None = None,
        error_code: str | None = None,
    ) -> None:
        self._emit_stage(
            state.job_id,
            DesktopStageRun(state.stage_ids[stage], stage, status, progress, error_code),
            document_id=document_id,
        )

    def find_available_document(self, asset_sha256: str) -> DesktopImportedDocument | None:
        connection = self._connect()
        try:
            return find_available_document_in(connection, asset_sha256)
        finally:
            connection.close()

    def complete_duplicate_job(self, state: ImportJobState, document_id: str) -> None:
        now = _timestamp()
        with kb_ingest_lock(self.state_dir):
            connection = self._connect()
            try:
                connection.execute("BEGIN IMMEDIATE")
                for stage in IMPORT_STAGES[2:]:
                    stage_run_id = state.stage_ids[stage]
                    connection.execute(
                        """
                        UPDATE stage_runs
                        SET status = 'skipped', progress = 100, error_code = NULL,
                            completed_at = COALESCE(completed_at, ?)
                        WHERE stage_run_id = ? AND job_id = ?
                        """,
                        (now, stage_run_id, state.job_id),
                    )
                    connection.execute(
                        """
                        UPDATE stage_run_runtime
                        SET status = 'skipped', error_code = NULL, updated_at = ?
                        WHERE stage_run_id = ? AND job_id = ?
                        """,
                        (now, stage_run_id, state.job_id),
                    )
                self._complete_job_in(
                    connection,
                    state.job_id,
                    state.stage_ids["raw_asset"],
                    document_id,
                    now,
                )
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
            finally:
                connection.close()
        for stage in IMPORT_STAGES[2:]:
            self._emit_stage(
                state.job_id,
                DesktopStageRun(state.stage_ids[stage], stage, "skipped", 100),
                document_id=document_id,
            )

    def publish_document(
        self,
        *,
        state: ImportJobState,
        source: Path,
        document_id: str,
        asset_sha256: str,
        raw_path: str,
        raw_size: int,
        blocks: tuple[DocumentIRBlock, ...],
        evidence: tuple[tuple[str, DocumentIRBlock], ...],
    ) -> tuple[DesktopImportedDocument, bool]:
        now = _timestamp()
        search_stage_id = state.stage_ids["search"]
        with kb_ingest_lock(self.state_dir):
            connection = self._connect()
            try:
                connection.execute("BEGIN IMMEDIATE")
                existing = find_available_document_in(connection, asset_sha256)
                if existing is not None:
                    self._complete_job_in(
                        connection, state.job_id, search_stage_id, existing.document_id, now
                    )
                    self._complete_recovery_in(connection, state, now)
                    connection.execute(
                        "DELETE FROM quarantined_documents WHERE job_id = ?", (state.job_id,)
                    )
                    connection.commit()
                    return existing, True
                connection.execute(
                    """
                    INSERT INTO raw_assets (
                        asset_sha256, byte_size, media_type, raw_path, original_name, created_at
                    ) VALUES (?, ?, 'text/plain', ?, ?, ?)
                    ON CONFLICT(asset_sha256) DO NOTHING
                    """,
                    (asset_sha256, raw_size, raw_path, source.name, now),
                )
                connection.execute(
                    """
                    INSERT INTO source_documents (
                        document_id, asset_sha256, display_name, source_format, availability,
                        created_at, available_at
                    ) VALUES (?, ?, ?, 'txt', 'available', ?, ?)
                    """,
                    (document_id, asset_sha256, source.name, now, now),
                )
                connection.executemany(
                    """
                    INSERT INTO document_ir_blocks (
                        block_id, document_id, ordinal, kind, text, heading_path, locator_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        (
                            block.block_id,
                            document_id,
                            block.ordinal,
                            block.kind,
                            block.text,
                            json.dumps(block.heading_path, ensure_ascii=False),
                            _line_locator(block),
                        )
                        for block in blocks
                    ],
                )
                connection.executemany(
                    """
                    INSERT INTO evidence_refs (
                        evidence_id, document_id, block_id, ordinal, text, locator_json
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    [
                        (
                            evidence_id,
                            document_id,
                            block.block_id,
                            block.ordinal,
                            block.text,
                            _line_locator(block),
                        )
                        for evidence_id, block in evidence
                    ],
                )
                connection.executemany(
                    "INSERT INTO evidence_fts (evidence_id, document_id, content) VALUES (?, ?, ?)",
                    [(evidence_id, document_id, block.text) for evidence_id, block in evidence],
                )
                self._complete_job_in(connection, state.job_id, search_stage_id, document_id, now)
                self._complete_recovery_in(connection, state, now)
                connection.execute(
                    "DELETE FROM quarantined_documents WHERE job_id = ?", (state.job_id,)
                )
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
            finally:
                connection.close()
        return (
            DesktopImportedDocument(
                document_id=document_id,
                name=source.name,
                source_format="txt",
                raw_asset_sha256=asset_sha256,
                evidence_count=len(evidence),
                availability="available",
            ),
            False,
        )

    def fail_job(self, state: ImportJobState, stage: str, error_code: str) -> None:
        stage_run_id = state.stage_ids[stage]
        try:
            with kb_ingest_lock(self.state_dir):
                connection = self._connect()
                try:
                    now = _timestamp()
                    connection.execute("BEGIN IMMEDIATE")
                    connection.execute(
                        """
                        UPDATE stage_runs
                        SET status = 'failed', progress = 100, error_code = ?,
                            completed_at = COALESCE(completed_at, ?)
                        WHERE stage_run_id = ? AND job_id = ?
                        """,
                        (error_code, now, stage_run_id, state.job_id),
                    )
                    connection.execute(
                        """
                        UPDATE import_jobs
                        SET status = 'failed', progress = 100, error_code = ?, completed_at = ?
                        WHERE job_id = ?
                        """,
                        (error_code, now, state.job_id),
                    )
                    connection.execute(
                        """
                        UPDATE stage_run_runtime
                        SET status = 'failed', error_code = ?, updated_at = ?
                        WHERE stage_run_id = ? AND job_id = ?
                        """,
                        (error_code, now, stage_run_id, state.job_id),
                    )
                    connection.execute(
                        """
                        UPDATE import_job_runtime
                        SET status = 'failed', lease_owner = NULL, lease_expires_at = NULL,
                            updated_at = ?
                        WHERE job_id = ?
                        """,
                        (now, state.job_id),
                    )
                    connection.commit()
                except BaseException:
                    connection.rollback()
                    raise
                finally:
                    connection.close()
            self._emit_stage(
                state.job_id,
                DesktopStageRun(stage_run_id, stage, "failed", 100, error_code),
            )
        except (OSError, sqlite3.Error, LockException, DesktopImportError):
            logger.warning("state write %s/%s/%s", state.job_id, stage, error_code, exc_info=True)

    def pause_job(self, state: ImportJobState, stage: str) -> None:
        self._set_terminal_control_state(state, stage, "paused", "import_paused")

    def cancel_job(self, state: ImportJobState, stage: str) -> None:
        self._set_terminal_control_state(state, stage, "cancelled", "import_cancelled")

    def _set_terminal_control_state(
        self, state: ImportJobState, stage: str, status: str, code: str
    ) -> None:
        stage_run_id = state.stage_ids[stage]
        now = _timestamp()
        with kb_ingest_lock(self.state_dir):
            connection = self._connect()
            try:
                connection.execute("BEGIN IMMEDIATE")
                progress_row = connection.execute(
                    "SELECT progress FROM stage_runs WHERE stage_run_id = ? AND job_id = ?",
                    (stage_run_id, state.job_id),
                ).fetchone()
                if progress_row is None:
                    raise DesktopImportError(
                        "desktop_import_state_invalid", "Import stage is missing for its job."
                    )
                stage_progress = int(progress_row[0])
                connection.execute(
                    """
                    UPDATE stage_run_runtime
                    SET status = ?, error_code = ?, updated_at = ?
                    WHERE stage_run_id = ? AND job_id = ?
                    """,
                    (status, code, now, stage_run_id, state.job_id),
                )
                if status == "cancelled":
                    connection.execute(
                        """
                        UPDATE stage_run_runtime
                        SET status = 'cancelled', error_code = ?, updated_at = ?
                        WHERE job_id = ? AND status IN ('pending', 'paused')
                        """,
                        (code, now, state.job_id),
                    )
                    connection.execute(
                        """
                        UPDATE import_jobs
                        SET status = 'failed', error_code = ?, completed_at = ?
                        WHERE job_id = ?
                        """,
                        (code, now, state.job_id),
                    )
                connection.execute(
                    """
                    UPDATE import_job_runtime
                    SET status = ?, lease_owner = NULL, lease_expires_at = NULL, updated_at = ?
                    WHERE job_id = ?
                    """,
                    (status, now, state.job_id),
                )
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
            finally:
                connection.close()
        self._emit_stage(
            state.job_id,
            DesktopStageRun(stage_run_id, stage, status, stage_progress, code),
        )

    def _complete_job_in(
        self,
        connection: sqlite3.Connection,
        job_id: str,
        stage_run_id: str,
        document_id: str,
        now: str,
    ) -> None:
        connection.execute(
            """
            UPDATE stage_runs
            SET status = 'completed', progress = 100, error_code = NULL,
                completed_at = COALESCE(completed_at, ?)
            WHERE stage_run_id = ? AND job_id = ?
            """,
            (now, stage_run_id, job_id),
        )
        connection.execute(
            """
            UPDATE stage_run_runtime
            SET status = 'completed', error_code = NULL, updated_at = ?
            WHERE stage_run_id = ? AND job_id = ?
            """,
            (now, stage_run_id, job_id),
        )
        connection.execute(
            """
            UPDATE import_jobs
            SET document_id = ?, status = 'completed', progress = 100, error_code = NULL,
                completed_at = ?
            WHERE job_id = ?
            """,
            (document_id, now, job_id),
        )
        connection.execute(
            """
            UPDATE import_job_runtime
            SET status = 'completed', lease_owner = NULL, lease_expires_at = NULL, updated_at = ?
            WHERE job_id = ?
            """,
            (now, job_id),
        )

    @staticmethod
    def _complete_recovery_in(
        connection: sqlite3.Connection, state: ImportJobState, now: str
    ) -> None:
        if state.recovery_run_id is None:
            return
        connection.execute(
            """
            UPDATE recovery_runs
            SET status = 'completed', completed_at = ?
            WHERE recovery_run_id = ?
            """,
            (now, state.recovery_run_id),
        )

    def _job_state_in(
        self, connection: sqlite3.Connection, job_id: str, source: Path, status: str
    ) -> ImportJobState:
        rows = connection.execute(
            "SELECT stage, stage_run_id FROM stage_runs WHERE job_id = ?", (job_id,)
        ).fetchall()
        stage_ids = {str(row[0]): str(row[1]) for row in rows}
        if set(stage_ids) != set(IMPORT_STAGES):
            raise DesktopImportError(
                "desktop_import_state_invalid", f"Import job {job_id} has incomplete stage state."
            )
        return ImportJobState(job_id=job_id, source=source, status=status, stage_ids=stage_ids)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path)
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def _emit_stage(
        self,
        job_id: str,
        stage_run: DesktopStageRun,
        *,
        document_id: str | None = None,
    ) -> None:
        if self._on_stage_progress is None:
            return
        data: dict[str, object] = {"job_id": job_id, **stage_run.as_dict()}
        if document_id is not None:
            data["document_id"] = document_id
        try:
            self._on_stage_progress(data)
        except Exception:
            logger.debug("Desktop import stage callback failed for job %s", job_id, exc_info=True)


def _line_locator(block: DocumentIRBlock) -> str:
    return json.dumps({"line_start": block.line_start, "line_end": block.line_end})


def _timestamp() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def _lease_expiry() -> str:
    return (datetime.now(tz=timezone.utc) + timedelta(seconds=30)).isoformat()
