"""The first Desktop-native document import vertical slice.

This module deliberately owns only TXT.  It turns one user-selected source
into the durable artifacts that later format adapters and LLM stages share:
one raw asset, a small Document IR, evidence rows, and an FTS5 baseline.
Legacy CLI/Web mutation paths are intentionally not involved.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from portalocker import LockException

from openkb.desktop_workspace import desktop_state_database_path, desktop_state_dir
from openkb.locks import atomic_write_bytes, kb_ingest_lock

_STAGES = ("preflight", "raw_asset", "document_ir", "evidence", "search")
_STAGE_ORDER_SQL = (
    "CASE stage "
    + " ".join(f"WHEN '{stage}' THEN {ordinal}" for ordinal, stage in enumerate(_STAGES, start=1))
    + " END"
)
_HEADING_PATTERN = re.compile(r"^(#{1,6})[ \t]+(.+?)\s*$")


class DesktopImportError(RuntimeError):
    """A stable domain error for Desktop document import."""

    code: str

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class DocumentIRBlock:
    """A normalized structured block retained in the SQLite Document IR."""

    block_id: str
    ordinal: int
    kind: str
    text: str
    heading_path: tuple[str, ...]
    line_start: int
    line_end: int


@dataclass(frozen=True)
class DesktopStageRun:
    """One durable import stage, suitable for the task center and Bridge events."""

    stage_run_id: str
    stage: str
    status: str
    progress: int
    error_code: str | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "stage_run_id": self.stage_run_id,
            "stage": self.stage,
            "status": self.status,
            "progress": self.progress,
            "error_code": self.error_code,
        }


@dataclass(frozen=True)
class DesktopImportedDocument:
    """The Available Knowledge record exposed once the final transaction commits."""

    document_id: str
    name: str
    source_format: str
    raw_asset_sha256: str
    evidence_count: int
    availability: str

    def as_dict(self) -> dict[str, object]:
        return {
            "document_id": self.document_id,
            "name": self.name,
            "source_format": self.source_format,
            "raw_asset_sha256": self.raw_asset_sha256,
            "evidence_count": self.evidence_count,
            "availability": self.availability,
        }


@dataclass(frozen=True)
class DesktopImportJob:
    """The persisted task-center record for one selected source."""

    job_id: str
    status: str
    progress: int
    document_id: str | None
    deduplicated: bool

    def as_dict(self) -> dict[str, object]:
        return {
            "job_id": self.job_id,
            "status": self.status,
            "progress": self.progress,
            "document_id": self.document_id,
            "deduplicated": self.deduplicated,
        }


@dataclass(frozen=True)
class DesktopTextImportResult:
    """The successful result of importing one TXT file."""

    document: DesktopImportedDocument
    job: DesktopImportJob
    stages: tuple[DesktopStageRun, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "document": self.document.as_dict(),
            "job": self.job.as_dict(),
            "stages": [stage.as_dict() for stage in self.stages],
        }


@dataclass(frozen=True)
class DesktopImportTask:
    """One persisted task-center record, including failed crash recovery work."""

    job: DesktopImportJob
    document: DesktopImportedDocument | None
    stages: tuple[DesktopStageRun, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "job": self.job.as_dict(),
            "document": self.document.as_dict() if self.document is not None else None,
            "stages": [stage.as_dict() for stage in self.stages],
        }


StageProgressCallback = Callable[[dict[str, object]], None]


class DesktopTextImportService:
    """Import a selected TXT file into one Desktop Knowledge Base.

    Intermediate stage status is durable so the Desktop task center can show
    progress.  Only the final SQLite transaction makes a source document
    available, so a crash may leave a recoverable raw object or running job but
    can never make a partially indexed document searchable.
    """

    def __init__(
        self,
        kb_dir: Path,
        *,
        on_stage_progress: StageProgressCallback | None = None,
    ) -> None:
        self._kb_dir = kb_dir.expanduser().resolve()
        self._state_dir = desktop_state_dir(self._kb_dir)
        self._database_path = desktop_state_database_path(self._kb_dir)
        self._on_stage_progress = on_stage_progress

    def import_text(self, source_path: Path) -> DesktopTextImportResult:
        """Run the TXT vertical slice and return one Available Knowledge record."""
        try:
            # A live import owns this lock, so reopening cannot falsely recover it.
            with kb_ingest_lock(self._state_dir):
                return self._import_text_locked(source_path)
        except DesktopImportError:
            raise
        except (OSError, sqlite3.Error, LockException) as error:
            raise DesktopImportError(
                "desktop_import_failed", f"Could not import {source_path.name}: {error}"
            ) from error

    def _import_text_locked(self, source_path: Path) -> DesktopTextImportResult:
        source = _validate_source(source_path)
        self._require_database()
        job_id = uuid.uuid4().hex
        stage_ids = {stage: uuid.uuid4().hex for stage in _STAGES}
        self._create_job(job_id, source, stage_ids)
        active_stage = "preflight"
        terminal_state_committed = False
        try:
            self._set_stage(job_id, stage_ids[active_stage], active_stage, "running", 0)
            raw_bytes = source.read_bytes()
            text = _decode_text(raw_bytes, source)
            asset_sha256 = hashlib.sha256(raw_bytes).hexdigest()
            self._set_stage(job_id, stage_ids[active_stage], active_stage, "completed", 20)

            active_stage = "raw_asset"
            self._set_stage(job_id, stage_ids[active_stage], active_stage, "running", 25)
            duplicate = self._find_available_document(asset_sha256)
            if duplicate is not None:
                self._set_stage(job_id, stage_ids[active_stage], active_stage, "completed", 35)
                self._complete_duplicate_job(job_id, stage_ids, duplicate.document_id)
                terminal_state_committed = True
                return self._result(job_id, duplicate, deduplicated=True)

            raw_path = self._write_raw_asset(asset_sha256, raw_bytes)
            self._set_stage(job_id, stage_ids[active_stage], active_stage, "completed", 35)

            document_id = uuid.uuid4().hex
            active_stage = "document_ir"
            self._set_stage(job_id, stage_ids[active_stage], active_stage, "running", 40)
            blocks = _build_document_ir(text)
            self._set_stage(job_id, stage_ids[active_stage], active_stage, "completed", 55)

            active_stage = "evidence"
            self._set_stage(job_id, stage_ids[active_stage], active_stage, "running", 60)
            evidence = _build_evidence(blocks)
            self._set_stage(job_id, stage_ids[active_stage], active_stage, "completed", 75)

            active_stage = "search"
            self._set_stage(job_id, stage_ids[active_stage], active_stage, "running", 80)
            document, deduplicated = self._publish_document(
                job_id=job_id,
                stage_run_id=stage_ids[active_stage],
                source=source,
                document_id=document_id,
                asset_sha256=asset_sha256,
                raw_path=raw_path,
                raw_size=len(raw_bytes),
                blocks=blocks,
                evidence=evidence,
            )
            terminal_state_committed = True
            self._emit_stage(
                job_id,
                DesktopStageRun(stage_ids[active_stage], active_stage, "completed", 100),
                document_id=document.document_id,
            )
            return self._result(job_id, document, deduplicated=deduplicated)
        except DesktopImportError as error:
            if not terminal_state_committed:
                self._fail_job(job_id, stage_ids[active_stage], active_stage, error.code)
            raise
        except (OSError, sqlite3.Error, LockException) as error:
            wrapped = DesktopImportError(
                "desktop_import_failed", f"Could not import {source.name}: {error}"
            )
            if not terminal_state_committed:
                self._fail_job(job_id, stage_ids[active_stage], active_stage, wrapped.code)
            raise wrapped from error

    def list_import_jobs(self) -> dict[str, object]:
        """Return durable task-center records for the active Desktop knowledge base."""
        self._require_database()
        connection = self._connect()
        try:
            rows = connection.execute(
                """
                SELECT import_jobs.job_id, import_jobs.status, import_jobs.progress,
                    import_jobs.document_id, source_documents.document_id,
                    source_documents.display_name, source_documents.source_format,
                    source_documents.asset_sha256, source_documents.availability,
                    (SELECT COUNT(*) FROM evidence_refs
                     WHERE evidence_refs.document_id = source_documents.document_id)
                FROM import_jobs
                LEFT JOIN source_documents
                    ON source_documents.document_id = import_jobs.document_id
                ORDER BY import_jobs.created_at DESC
                """
            ).fetchall()
            tasks = tuple(self._task_from_row(connection, row) for row in rows)
        finally:
            connection.close()
        return {"jobs": [task.as_dict() for task in tasks]}

    def _require_database(self) -> None:
        if not self._database_path.is_file():
            raise DesktopImportError(
                "desktop_knowledge_base_not_found",
                f"Not a Desktop Knowledge Base: {self._kb_dir}",
            )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._database_path)
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def _create_job(self, job_id: str, source: Path, stage_ids: dict[str, str]) -> None:
        now = _timestamp()
        with kb_ingest_lock(self._state_dir):
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
                    connection.executemany(
                        """
                        INSERT INTO stage_runs (
                            stage_run_id, job_id, stage, status, progress, error_code,
                            started_at, completed_at
                        ) VALUES (?, ?, ?, 'pending', 0, NULL, NULL, NULL)
                        """,
                        [(stage_ids[stage], job_id, stage) for stage in _STAGES],
                    )
            finally:
                connection.close()

    def _set_stage(
        self,
        job_id: str,
        stage_run_id: str,
        stage: str,
        status: str,
        progress: int,
        *,
        error_code: str | None = None,
    ) -> None:
        now = _timestamp()
        started_at = now if status == "running" else None
        completed_at = now if status in {"completed", "failed", "skipped"} else None
        with kb_ingest_lock(self._state_dir):
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
                            status,
                            progress,
                            error_code,
                            started_at,
                            completed_at,
                            stage_run_id,
                            job_id,
                        ),
                    )
                    if cursor.rowcount != 1:
                        raise DesktopImportError(
                            "desktop_import_state_invalid",
                            f"Import stage is missing for job {job_id}.",
                        )
                    connection.execute(
                        "UPDATE import_jobs SET progress = ? WHERE job_id = ?",
                        (progress, job_id),
                    )
            finally:
                connection.close()
        self._emit_stage(
            job_id,
            DesktopStageRun(stage_run_id, stage, status, progress, error_code),
        )

    def _write_raw_asset(self, asset_sha256: str, content: bytes) -> str:
        raw_relative_path = Path("raw") / f"{asset_sha256}.txt"
        with kb_ingest_lock(self._state_dir):
            atomic_write_bytes(self._kb_dir / raw_relative_path, content)
        return str(raw_relative_path)

    def _find_available_document(self, asset_sha256: str) -> DesktopImportedDocument | None:
        connection = self._connect()
        try:
            return self._find_available_document_in(connection, asset_sha256)
        finally:
            connection.close()

    def _publish_document(
        self,
        *,
        job_id: str,
        stage_run_id: str,
        source: Path,
        document_id: str,
        asset_sha256: str,
        raw_path: str,
        raw_size: int,
        blocks: tuple[DocumentIRBlock, ...],
        evidence: tuple[tuple[str, DocumentIRBlock], ...],
    ) -> tuple[DesktopImportedDocument, bool]:
        now = _timestamp()
        with kb_ingest_lock(self._state_dir):
            connection = self._connect()
            try:
                connection.execute("BEGIN IMMEDIATE")
                existing = self._find_available_document_in(connection, asset_sha256)
                if existing is not None:
                    self._complete_job_in(
                        connection, job_id, stage_run_id, existing.document_id, now
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
                            json.dumps(
                                {"line_start": block.line_start, "line_end": block.line_end}
                            ),
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
                            json.dumps(
                                {"line_start": block.line_start, "line_end": block.line_end}
                            ),
                        )
                        for evidence_id, block in evidence
                    ],
                )
                connection.executemany(
                    "INSERT INTO evidence_fts (evidence_id, document_id, content) VALUES (?, ?, ?)",
                    [(evidence_id, document_id, block.text) for evidence_id, block in evidence],
                )
                self._complete_job_in(connection, job_id, stage_run_id, document_id, now)
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

    def _find_available_document_in(
        self, connection: sqlite3.Connection, asset_sha256: str
    ) -> DesktopImportedDocument | None:
        row = connection.execute(
            """
            SELECT document_id, display_name, source_format, asset_sha256, availability,
                (SELECT COUNT(*) FROM evidence_refs
                 WHERE evidence_refs.document_id = source_documents.document_id)
            FROM source_documents
            WHERE asset_sha256 = ? AND availability = 'available'
            """,
            (asset_sha256,),
        ).fetchone()
        return _document_from_row(row) if row is not None else None

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
            UPDATE import_jobs
            SET document_id = ?, status = 'completed', progress = 100, error_code = NULL,
                completed_at = ?
            WHERE job_id = ?
            """,
            (document_id, now, job_id),
        )

    def _complete_duplicate_job(
        self, job_id: str, stage_ids: dict[str, str], document_id: str
    ) -> None:
        now = _timestamp()
        with kb_ingest_lock(self._state_dir):
            connection = self._connect()
            try:
                connection.execute("BEGIN IMMEDIATE")
                for stage in _STAGES[2:]:
                    connection.execute(
                        """
                        UPDATE stage_runs
                        SET status = 'skipped', progress = 100, error_code = NULL,
                            completed_at = COALESCE(completed_at, ?)
                        WHERE stage_run_id = ? AND job_id = ?
                        """,
                        (now, stage_ids[stage], job_id),
                    )
                connection.execute(
                    """
                    UPDATE import_jobs
                    SET document_id = ?, status = 'completed', progress = 100,
                        error_code = NULL, completed_at = ?
                    WHERE job_id = ?
                    """,
                    (document_id, now, job_id),
                )
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
            finally:
                connection.close()
        for stage in _STAGES[2:]:
            self._emit_stage(
                job_id,
                DesktopStageRun(stage_ids[stage], stage, "skipped", 100),
                document_id=document_id,
            )

    def _fail_job(self, job_id: str, stage_run_id: str, stage: str, error_code: str) -> None:
        try:
            with kb_ingest_lock(self._state_dir):
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
                        (error_code, now, stage_run_id, job_id),
                    )
                    connection.execute(
                        """
                        UPDATE import_jobs
                        SET status = 'failed', progress = 100, error_code = ?, completed_at = ?
                        WHERE job_id = ?
                        """,
                        (error_code, now, job_id),
                    )
                    connection.commit()
                except BaseException:
                    connection.rollback()
                    raise
                finally:
                    connection.close()
            self._emit_stage(
                job_id,
                DesktopStageRun(stage_run_id, stage, "failed", 100, error_code),
            )
        except (OSError, sqlite3.Error, LockException, DesktopImportError):
            # Preserve the original import failure even when its bookkeeping is unavailable.
            return

    def _result(
        self, job_id: str, document: DesktopImportedDocument, *, deduplicated: bool
    ) -> DesktopTextImportResult:
        connection = self._connect()
        try:
            job_row = connection.execute(
                "SELECT status, progress, document_id FROM import_jobs WHERE job_id = ?", (job_id,)
            ).fetchone()
            stages = self._stages_for_job(connection, job_id)
        finally:
            connection.close()
        if job_row is None:
            raise DesktopImportError(
                "desktop_import_state_invalid", f"Import job is missing: {job_id}."
            )
        job = DesktopImportJob(
            job_id=job_id,
            status=str(job_row[0]),
            progress=int(job_row[1]),
            document_id=str(job_row[2]) if job_row[2] is not None else None,
            deduplicated=deduplicated,
        )
        return DesktopTextImportResult(document=document, job=job, stages=stages)

    def _task_from_row(
        self, connection: sqlite3.Connection, row: tuple[object, ...]
    ) -> DesktopImportTask:
        stages = self._stages_for_job(connection, str(row[0]))
        job = DesktopImportJob(
            job_id=str(row[0]),
            status=str(row[1]),
            progress=int(str(row[2])),
            document_id=str(row[3]) if row[3] is not None else None,
            deduplicated=any(
                stage.stage == "document_ir" and stage.status == "skipped" for stage in stages
            ),
        )
        document = _document_from_row(row[4:]) if row[4] is not None else None
        return DesktopImportTask(
            job=job,
            document=document,
            stages=stages,
        )

    def _stages_for_job(
        self, connection: sqlite3.Connection, job_id: str
    ) -> tuple[DesktopStageRun, ...]:
        stage_rows = connection.execute(
            f"""
            SELECT stage_run_id, stage, status, progress, error_code
            FROM stage_runs
            WHERE job_id = ?
            ORDER BY {_STAGE_ORDER_SQL}
            """,
            (job_id,),
        ).fetchall()
        return tuple(
            DesktopStageRun(
                stage_run_id=str(row[0]),
                stage=str(row[1]),
                status=str(row[2]),
                progress=int(str(row[3])),
                error_code=str(row[4]) if row[4] is not None else None,
            )
            for row in stage_rows
        )

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
            # The SQLite task must remain authoritative if the owning window has gone away.
            return


def _validate_source(source_path: Path) -> Path:
    source = source_path.expanduser().resolve()
    if source.suffix.lower() != ".txt":
        raise DesktopImportError(
            "unsupported_import_format", "The first Desktop import path supports TXT files only."
        )
    if not source.is_file():
        raise DesktopImportError("import_source_not_found", f"TXT source was not found: {source}")
    return source


def _decode_text(content: bytes, source: Path) -> str:
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError as error:
        raise DesktopImportError(
            "invalid_text_document", f"TXT source is not valid UTF-8 text: {source.name}"
        ) from error
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    if not text.strip():
        raise DesktopImportError("empty_text_document", f"TXT source is empty: {source.name}")
    return text


def _build_document_ir(text: str) -> tuple[DocumentIRBlock, ...]:
    lines = text.split("\n")
    blocks: list[DocumentIRBlock] = []
    heading_path: list[str] = []
    paragraph_lines: list[str] = []
    paragraph_start = 0

    def flush_paragraph(end_line: int) -> None:
        nonlocal paragraph_lines
        if not paragraph_lines:
            return
        blocks.append(
            DocumentIRBlock(
                block_id=uuid.uuid4().hex,
                ordinal=len(blocks),
                kind="paragraph",
                text="\n".join(paragraph_lines),
                heading_path=tuple(heading_path),
                line_start=paragraph_start,
                line_end=end_line,
            )
        )
        paragraph_lines = []

    for line_number, line in enumerate(lines, start=1):
        heading_match = _HEADING_PATTERN.match(line)
        if heading_match:
            flush_paragraph(line_number - 1)
            level = len(heading_match.group(1))
            title = heading_match.group(2)
            heading_path[level - 1 :] = [title]
            blocks.append(
                DocumentIRBlock(
                    block_id=uuid.uuid4().hex,
                    ordinal=len(blocks),
                    kind="heading",
                    text=title,
                    heading_path=tuple(heading_path),
                    line_start=line_number,
                    line_end=line_number,
                )
            )
            continue
        if not line.strip():
            flush_paragraph(line_number - 1)
            continue
        if not paragraph_lines:
            paragraph_start = line_number
        paragraph_lines.append(line)

    flush_paragraph(len(lines))
    if not blocks:
        raise DesktopImportError(
            "empty_text_document", "TXT source did not contain usable text blocks."
        )
    return tuple(blocks)


def _build_evidence(blocks: tuple[DocumentIRBlock, ...]) -> tuple[tuple[str, DocumentIRBlock], ...]:
    return tuple((uuid.uuid4().hex, block) for block in blocks)


def _document_from_row(row: tuple[object, ...]) -> DesktopImportedDocument:
    return DesktopImportedDocument(
        document_id=str(row[0]),
        name=str(row[1]),
        source_format=str(row[2]),
        raw_asset_sha256=str(row[3]),
        availability=str(row[4]),
        evidence_count=int(str(row[5])),
    )


def _timestamp() -> str:
    from datetime import datetime, timezone

    return datetime.now(tz=timezone.utc).isoformat()
