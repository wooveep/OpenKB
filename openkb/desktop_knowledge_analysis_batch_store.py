"""Crash-safe persistence for Knowledge Analysis batch checkpoints."""

from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from openkb.desktop_import_artifacts import DesktopImportError, DocumentIRBlock
from openkb.desktop_workspace import desktop_state_dir
from openkb.locks import kb_ingest_lock


@dataclass(frozen=True)
class KnowledgeAnalysisBatch:
    """One ordered persisted batch reconstructed against current Evidence."""

    batch_id: str
    ordinal: int
    section_paths: tuple[tuple[str, ...], ...]
    evidence: tuple[tuple[str, DocumentIRBlock], ...]
    status: str
    checkpoint: dict[str, object] | None = None


class DesktopKnowledgeAnalysisBatchStore:
    """Own batch/merge transitions below one Knowledge Analysis operation."""

    def __init__(
        self,
        kb_dir: Path,
        *,
        reanalysis: bool = False,
        execution_token: str | None = None,
    ) -> None:
        if execution_token is not None and not reanalysis:
            raise ValueError("Execution tokens belong only to Knowledge Reanalysis.")
        self._state_dir = desktop_state_dir(kb_dir)
        self._database_path = self._state_dir / "state.sqlite3"
        self._batch_table = (
            "knowledge_reanalysis_batches" if reanalysis else "knowledge_analysis_batches"
        )
        self._merge_table = (
            "knowledge_reanalysis_merges" if reanalysis else "knowledge_analysis_merges"
        )
        self._execution_token = execution_token

    def load_or_create(
        self,
        *,
        job_id: str,
        stage_run_id: str,
        evidence: tuple[tuple[str, DocumentIRBlock], ...],
    ) -> tuple[KnowledgeAnalysisBatch, ...]:
        from openkb.desktop_knowledge_analysis_batches import (
            plan_knowledge_analysis_batches,
        )

        with kb_ingest_lock(self._state_dir):
            connection = _connect(self._database_path)
            try:
                self._require_active_job_in(connection, job_id)
                rows = _batch_rows(connection, job_id, self._batch_table)
                if rows:
                    return _hydrate_batches(rows, evidence)
                planned = plan_knowledge_analysis_batches(evidence)
                if len(planned) <= 1:
                    return ()
                now = _timestamp()
                connection.execute("BEGIN IMMEDIATE")
                self._require_active_job_in(connection, job_id)
                for ordinal, batch_evidence in enumerate(planned):
                    section_paths = _section_paths(batch_evidence)
                    connection.execute(
                        f"""
                        INSERT INTO {self._batch_table} (
                            batch_id, job_id, stage_run_id, batch_ordinal,
                            section_paths_json, evidence_ids_json, status,
                            checkpoint_json, error_code, created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, 'pending', NULL, NULL, ?, ?)
                        """,
                        (
                            uuid.uuid4().hex,
                            job_id,
                            stage_run_id,
                            ordinal,
                            _json(section_paths),
                            _json([item[0] for item in batch_evidence]),
                            now,
                            now,
                        ),
                    )
                connection.execute(
                    f"""
                    INSERT INTO {self._merge_table} (
                        job_id, stage_run_id, status, checkpoint_json,
                        error_code, created_at, updated_at
                    ) VALUES (?, ?, 'pending', NULL, NULL, ?, ?)
                    """,
                    (job_id, stage_run_id, now, now),
                )
                connection.commit()
                return _hydrate_batches(
                    _batch_rows(connection, job_id, self._batch_table), evidence
                )
            except BaseException:
                connection.rollback()
                raise
            finally:
                connection.close()

    def start_batch(self, batch_id: str) -> None:
        self._transition_batch(batch_id, "running", checkpoint=None, error_code=None)

    def complete_batch(self, batch_id: str, checkpoint: dict[str, object]) -> None:
        self._transition_batch(batch_id, "completed", checkpoint=checkpoint, error_code=None)

    def fail_batch(self, batch_id: str, error_code: str) -> None:
        self._transition_batch(batch_id, "failed", checkpoint=None, error_code=error_code)

    def start_merge(self, job_id: str) -> None:
        self._transition_merge(job_id, "running", checkpoint=None, error_code=None)

    def complete_merge(self, job_id: str, checkpoint: dict[str, object]) -> None:
        self._transition_merge(job_id, "completed", checkpoint=checkpoint, error_code=None)

    def fail_merge(self, job_id: str, error_code: str) -> None:
        self._transition_merge(job_id, "failed", checkpoint=None, error_code=error_code)

    def merge_checkpoint(self, job_id: str) -> dict[str, object] | None:
        with kb_ingest_lock(self._state_dir):
            connection = _connect(self._database_path)
            try:
                self._require_active_job_in(connection, job_id)
                row = connection.execute(
                    f"""
                    SELECT status, checkpoint_json FROM {self._merge_table}
                    WHERE job_id = ?
                    """,
                    (job_id,),
                ).fetchone()
                if row is None or str(row[0]) != "completed":
                    return None
                return _checkpoint(row[1])
            finally:
                connection.close()

    def _transition_batch(
        self,
        batch_id: str,
        status: str,
        *,
        checkpoint: dict[str, object] | None,
        error_code: str | None,
    ) -> None:
        with kb_ingest_lock(self._state_dir):
            connection = _connect(self._database_path)
            try:
                with connection:
                    job_id = _job_id_for_batch_in(connection, self._batch_table, batch_id)
                    self._require_active_job_in(connection, job_id)
                    cursor = connection.execute(
                        f"""
                        UPDATE {self._batch_table}
                        SET status = ?, checkpoint_json = ?, error_code = ?, updated_at = ?
                        WHERE batch_id = ? AND status != 'completed'
                        """,
                        (
                            status,
                            _json(checkpoint) if checkpoint is not None else None,
                            error_code,
                            _timestamp(),
                            batch_id,
                        ),
                    )
                    if cursor.rowcount != 1:
                        raise _state_error("Knowledge Analysis batch state changed unexpectedly.")
            finally:
                connection.close()

    def _transition_merge(
        self,
        job_id: str,
        status: str,
        *,
        checkpoint: dict[str, object] | None,
        error_code: str | None,
    ) -> None:
        with kb_ingest_lock(self._state_dir):
            connection = _connect(self._database_path)
            try:
                with connection:
                    self._require_active_job_in(connection, job_id)
                    cursor = connection.execute(
                        f"""
                        UPDATE {self._merge_table}
                        SET status = ?, checkpoint_json = ?, error_code = ?, updated_at = ?
                        WHERE job_id = ? AND status != 'completed'
                        """,
                        (
                            status,
                            _json(checkpoint) if checkpoint is not None else None,
                            error_code,
                            _timestamp(),
                            job_id,
                        ),
                    )
                    if cursor.rowcount != 1:
                        raise _state_error("Knowledge Analysis merge state changed unexpectedly.")
            finally:
                connection.close()

    def _require_active_job_in(self, connection: sqlite3.Connection, job_id: str) -> None:
        if self._execution_token is None:
            return
        row = connection.execute(
            """
            SELECT status, execution_token FROM knowledge_reanalysis_jobs
            WHERE job_id = ?
            """,
            (job_id,),
        ).fetchone()
        if row != ("running", self._execution_token):
            raise DesktopImportError(
                "knowledge_reanalysis_interrupted",
                "Knowledge Reanalysis is no longer the active execution for this document.",
            )


def _job_id_for_batch_in(connection: sqlite3.Connection, batch_table: str, batch_id: str) -> str:
    row = connection.execute(
        f"SELECT job_id FROM {batch_table} WHERE batch_id = ?", (batch_id,)
    ).fetchone()
    if row is None:
        raise _state_error("Knowledge Analysis batch was not found.")
    return str(row[0])


def _batch_rows(
    connection: sqlite3.Connection, job_id: str, batch_table: str
) -> list[tuple[object, ...]]:
    return connection.execute(
        f"""
        SELECT batch_id, batch_ordinal, section_paths_json, evidence_ids_json,
            status, checkpoint_json
        FROM {batch_table} WHERE job_id = ? ORDER BY batch_ordinal
        """,
        (job_id,),
    ).fetchall()


def _hydrate_batches(
    rows: list[tuple[object, ...]],
    evidence: tuple[tuple[str, DocumentIRBlock], ...],
) -> tuple[KnowledgeAnalysisBatch, ...]:
    by_id = {item[0]: item for item in evidence}
    hydrated: list[KnowledgeAnalysisBatch] = []
    flattened: list[str] = []
    for expected_ordinal, row in enumerate(rows):
        try:
            section_values = json.loads(str(row[2]))
            evidence_ids = json.loads(str(row[3]))
        except json.JSONDecodeError as error:
            raise _state_error("Knowledge Analysis batch plan is invalid.") from error
        if (
            type(row[1]) is not int
            or row[1] != expected_ordinal
            or not isinstance(section_values, list)
            or not all(
                isinstance(path, list) and all(isinstance(value, str) for value in path)
                for path in section_values
            )
            or not isinstance(evidence_ids, list)
            or not evidence_ids
            or not all(isinstance(value, str) and value in by_id for value in evidence_ids)
        ):
            raise _state_error("Knowledge Analysis batch plan is invalid.")
        flattened.extend(evidence_ids)
        checkpoint = _checkpoint(row[5]) if row[5] is not None else None
        if str(row[4]) == "completed" and checkpoint is None:
            raise _state_error("Completed Knowledge Analysis batch has no checkpoint.")
        hydrated.append(
            KnowledgeAnalysisBatch(
                batch_id=str(row[0]),
                ordinal=expected_ordinal,
                section_paths=tuple(tuple(path) for path in section_values),
                evidence=tuple(by_id[value] for value in evidence_ids),
                status=str(row[4]),
                checkpoint=checkpoint,
            )
        )
    if flattened != [item[0] for item in evidence] or len(flattened) != len(set(flattened)):
        raise _state_error("Knowledge Analysis batch plan no longer matches Evidence.")
    return tuple(hydrated)


def _section_paths(
    evidence: tuple[tuple[str, DocumentIRBlock], ...],
) -> tuple[tuple[str, ...], ...]:
    return tuple(dict.fromkeys(item[1].heading_path for item in evidence))


def _checkpoint(value: object) -> dict[str, object]:
    try:
        parsed = json.loads(str(value))
    except json.JSONDecodeError as error:
        raise _state_error("Knowledge Analysis checkpoint is invalid.") from error
    if not isinstance(parsed, dict):
        raise _state_error("Knowledge Analysis checkpoint is invalid.")
    return parsed


def _connect(database_path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(database_path)
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _timestamp() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def _state_error(message: str) -> DesktopImportError:
    return DesktopImportError("desktop_import_state_invalid", message)
