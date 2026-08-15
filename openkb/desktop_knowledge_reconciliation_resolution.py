"""Durable review choices and atomic publication for knowledge conflicts."""

from __future__ import annotations

import datetime as dt
import logging
import sqlite3
import uuid
from dataclasses import dataclass
from pathlib import Path

from openkb.desktop_import_artifacts import DesktopImportError
from openkb.desktop_import_types import (
    DesktopKnowledgeReconciliationCommit,
    DesktopKnowledgeReconciliationConflict,
)
from openkb.desktop_knowledge_generations import (
    KnowledgeGenerationChange,
    activate_generation_projection,
    current_generation_id_in,
    discard_generation_projection_staging,
    knowledge_content_sha256,
    publish_generation_changes_in,
    stage_generation_projection_in,
)
from openkb.desktop_knowledge_reconciliation import DesktopKnowledgeReconciliationService
from openkb.desktop_workspace import desktop_state_database_path, desktop_state_dir
from openkb.locks import kb_ingest_lock

_DECISIONS = frozenset({"publish_incoming", "keep_current"})
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class _StagedCandidate:
    candidate_id: str
    document_id: str
    document_available: bool
    kind: str
    title: str
    normalized_title: str
    content_markdown: str
    content_sha256: str
    baseline_kind: str
    baseline_id: str
    baseline_content_markdown: str
    decision: str


class DesktopKnowledgeReconciliationResolutionService:
    """Keep review choices inert until one explicit, atomic publish action."""

    def __init__(self, kb_dir: Path) -> None:
        self._kb_dir = kb_dir.expanduser().resolve()
        self._state_dir = desktop_state_dir(self._kb_dir)
        self._database_path = desktop_state_database_path(self._kb_dir)

    def stage_decisions(
        self, candidate_ids: tuple[str, ...], decision: str | None
    ) -> tuple[DesktopKnowledgeReconciliationConflict, ...]:
        """Persist an individual or batch choice without changing publication."""
        identifiers = _candidate_ids(candidate_ids)
        if decision is not None and decision not in _DECISIONS:
            raise DesktopImportError(
                "invalid_knowledge_reconciliation_decision",
                "Choose publish_incoming or keep_current for a knowledge conflict.",
            )
        with kb_ingest_lock(self._state_dir):
            connection = self._connect()
            try:
                connection.execute("BEGIN IMMEDIATE")
                for candidate_id in identifiers:
                    _require_pending_candidate_in(connection, candidate_id)
                    connection.execute(
                        """
                        UPDATE knowledge_reconciliation_candidates
                        SET staged_decision = ?
                        WHERE candidate_id = ?
                        """,
                        (decision, candidate_id),
                    )
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
            finally:
                connection.close()
        return DesktopKnowledgeReconciliationService(self._kb_dir).list_conflicts()

    def commit_staged_decisions(self) -> DesktopKnowledgeReconciliationCommit:
        """Publish all staged choices together and erase their review-copy text."""
        with kb_ingest_lock(self._state_dir):
            connection = self._connect()
            staged_projection: Path | None = None
            try:
                connection.execute("BEGIN IMMEDIATE")
                candidates = _staged_candidates_in(connection)
                if not candidates:
                    raise DesktopImportError(
                        "knowledge_reconciliation_nothing_staged",
                        "Choose at least one conflict before committing the review queue.",
                    )
                _require_distinct_publications(candidates)
                current_generation_id = current_generation_id_in(connection)
                for candidate in candidates:
                    _require_current_baseline_in(connection, current_generation_id, candidate)
                published = tuple(
                    _generation_change(candidate)
                    for candidate in candidates
                    if candidate.decision == "publish_incoming"
                )
                generation_id = (
                    publish_generation_changes_in(
                        connection,
                        current_generation_id=current_generation_id,
                        changes=published,
                        now=_timestamp(),
                    )
                    if published
                    else None
                )
                now = _timestamp()
                for candidate in candidates:
                    resolution_id = uuid.uuid4().hex
                    connection.execute(
                        """
                        INSERT INTO knowledge_reconciliation_resolution_records (
                            resolution_id, candidate_id, document_id, kind, normalized_title,
                            decision, published_generation_id, resolved_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            resolution_id,
                            candidate.candidate_id,
                            candidate.document_id,
                            candidate.kind,
                            candidate.normalized_title,
                            candidate.decision,
                            generation_id if candidate.decision == "publish_incoming" else None,
                            now,
                        ),
                    )
                    connection.execute(
                        """
                        UPDATE knowledge_reconciliation_candidates
                        SET staged_decision = NULL,
                            resolution_status = ?,
                            resolved_at = ?,
                            content_markdown = '',
                            baseline_content_markdown = NULL
                        WHERE candidate_id = ?
                        """,
                        (
                            "published" if candidate.decision == "publish_incoming" else "kept",
                            now,
                            candidate.candidate_id,
                        ),
                    )
                if generation_id is not None:
                    staged_projection = stage_generation_projection_in(
                        connection, self._kb_dir, generation_id
                    )
                connection.commit()
                outcome = DesktopKnowledgeReconciliationCommit(
                    published_generation_id=generation_id,
                    published_count=len(published),
                    kept_count=len(candidates) - len(published),
                    resolved_candidate_ids=tuple(
                        candidate.candidate_id for candidate in candidates
                    ),
                )
            except BaseException:
                connection.rollback()
                if staged_projection is not None:
                    discard_generation_projection_staging(staged_projection)
                raise
            finally:
                connection.close()
            if staged_projection is not None:
                try:
                    activate_generation_projection(self._kb_dir, staged_projection)
                except Exception:
                    logger.exception("Could not activate Desktop knowledge Markdown projection.")
                finally:
                    discard_generation_projection_staging(staged_projection)
            return outcome

    def _connect(self) -> sqlite3.Connection:
        if not self._database_path.is_file():
            raise DesktopImportError(
                "desktop_knowledge_base_not_found",
                "Open a Desktop Knowledge Base before reviewing knowledge conflicts.",
            )
        connection = sqlite3.connect(self._database_path)
        connection.execute("PRAGMA foreign_keys = ON")
        return connection


def _candidate_ids(candidate_ids: tuple[str, ...]) -> tuple[str, ...]:
    identifiers = tuple(dict.fromkeys(candidate_ids))
    if not identifiers or any(not candidate_id.strip() for candidate_id in identifiers):
        raise DesktopImportError(
            "invalid_knowledge_reconciliation_candidates",
            "Choose one or more knowledge conflicts before staging a decision.",
        )
    return identifiers


def _require_pending_candidate_in(connection: sqlite3.Connection, candidate_id: str) -> None:
    row = connection.execute(
        """
        SELECT documents.availability
        FROM knowledge_reconciliation_candidates AS candidates
        JOIN source_documents AS documents ON documents.document_id = candidates.document_id
        WHERE candidates.candidate_id = ?
            AND candidates.status = 'pending_conflict'
            AND candidates.resolution_status IS NULL
        """,
        (candidate_id,),
    ).fetchone()
    if row is None:
        raise DesktopImportError(
            "knowledge_reconciliation_candidate_not_found",
            "The selected knowledge conflict is no longer available for review.",
        )
    if str(row[0]) != "available":
        raise DesktopImportError(
            "knowledge_reconciliation_candidate_unavailable",
            "The source document must remain available before choosing a knowledge conflict.",
        )


def _staged_candidates_in(connection: sqlite3.Connection) -> tuple[_StagedCandidate, ...]:
    rows = connection.execute(
        """
        SELECT candidates.candidate_id, candidates.document_id, documents.availability,
            candidates.kind, candidates.title, candidates.normalized_title,
            candidates.content_markdown, candidates.content_sha256,
            candidates.baseline_kind, candidates.baseline_id,
            candidates.baseline_content_markdown, candidates.staged_decision
        FROM knowledge_reconciliation_candidates AS candidates
        JOIN source_documents AS documents ON documents.document_id = candidates.document_id
        WHERE candidates.status = 'pending_conflict'
            AND candidates.resolution_status IS NULL
            AND candidates.staged_decision IS NOT NULL
        ORDER BY candidates.created_at, candidates.candidate_id
        """
    ).fetchall()
    values: list[_StagedCandidate] = []
    for row in rows:
        candidate = _StagedCandidate(
            candidate_id=str(row[0]),
            document_id=str(row[1]),
            document_available=str(row[2]) == "available",
            kind=str(row[3]),
            title=str(row[4]),
            normalized_title=str(row[5]),
            content_markdown=str(row[6]),
            content_sha256=str(row[7]),
            baseline_kind=str(row[8]),
            baseline_id=str(row[9]),
            baseline_content_markdown=str(row[10]),
            decision=str(row[11]),
        )
        if candidate.decision not in _DECISIONS:
            raise DesktopImportError(
                "knowledge_reconciliation_stage_invalid",
                "A staged knowledge conflict has an invalid decision.",
            )
        if not candidate.document_available:
            raise DesktopImportError(
                "knowledge_reconciliation_candidate_unavailable",
                "The source document must remain available before committing review choices.",
            )
        values.append(candidate)
    return tuple(values)


def _require_distinct_publications(candidates: tuple[_StagedCandidate, ...]) -> None:
    identities: set[tuple[str, str]] = set()
    for candidate in candidates:
        if candidate.decision != "publish_incoming":
            continue
        identity = candidate.kind, candidate.normalized_title
        if identity in identities:
            raise DesktopImportError(
                "knowledge_reconciliation_multiple_publications",
                "Choose only one incoming version for each Concept or Entity before committing.",
            )
        identities.add(identity)


def _require_current_baseline_in(
    connection: sqlite3.Connection,
    current_generation_id: int | None,
    candidate: _StagedCandidate,
) -> None:
    if candidate.baseline_kind == "user_revision":
        row = connection.execute(
            """
            SELECT pages.current_revision_id
            FROM knowledge_pages AS pages
            WHERE pages.kind = ? AND pages.normalized_title = ?
            """,
            (candidate.kind, candidate.normalized_title),
        ).fetchone()
        if row is not None and str(row[0]) == candidate.baseline_id:
            return
    elif candidate.baseline_kind == "published_generation" and current_generation_id is not None:
        row = connection.execute(
            """
            SELECT content_markdown FROM knowledge_generation_items
            WHERE generation_id = ? AND kind = ? AND normalized_title = ?
            """,
            (current_generation_id, candidate.kind, candidate.normalized_title),
        ).fetchone()
        if row is not None and knowledge_content_sha256(str(row[0])) == knowledge_content_sha256(
            candidate.baseline_content_markdown
        ):
            return
    raise DesktopImportError(
        "knowledge_reconciliation_baseline_changed",
        "This conflict changed after it was staged. Refresh it and choose again before committing.",
    )


def _generation_change(candidate: _StagedCandidate) -> KnowledgeGenerationChange:
    return KnowledgeGenerationChange(
        document_id=candidate.document_id,
        kind=candidate.kind,
        title=candidate.title,
        normalized_title=candidate.normalized_title,
        content_markdown=candidate.content_markdown,
        content_sha256=candidate.content_sha256,
    )


def _timestamp() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()
