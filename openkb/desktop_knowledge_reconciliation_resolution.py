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
from openkb.desktop_knowledge_analysis_sources import bind_generation_sources_to_draft_in
from openkb.desktop_knowledge_generations import (
    KnowledgeGenerationChange,
    KnowledgeGenerationSource,
    activate_generation_projection,
    current_generation_id_in,
    discard_generation_projection_staging,
    knowledge_content_sha256,
    publish_generation_changes_in,
    stage_generation_projection_in,
)
from openkb.desktop_knowledge_metadata import decode_knowledge_labels
from openkb.desktop_knowledge_reconciliation import DesktopKnowledgeReconciliationService
from openkb.desktop_knowledge_reconciliation_adoption import (
    candidate_has_durable_adoption_origin_in,
)
from openkb.desktop_knowledge_sources import prune_obsolete_draft_sources_in
from openkb.desktop_knowledge_three_way_merge import apply_incoming_to_draft
from openkb.desktop_workspace import desktop_state_database_path, desktop_state_dir
from openkb.locks import kb_ingest_lock

_TWO_WAY_DECISIONS = frozenset({"publish_incoming", "keep_current"})
_THREE_WAY_DECISIONS = frozenset({"keep_draft", "apply_incoming", "replace_draft", "manual_merge"})
_DECISIONS = _TWO_WAY_DECISIONS | _THREE_WAY_DECISIONS
_DRAFT_UPDATE_DECISIONS = frozenset({"apply_incoming", "replace_draft", "manual_merge"})
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
    entity_subtype: str | None
    aliases: tuple[str, ...]
    tags: tuple[str, ...]
    analysis_provenance_json: str | None
    sources: tuple[KnowledgeGenerationSource, ...]
    baseline_kind: str
    baseline_id: str
    baseline_content_markdown: str
    reconciliation_mode: str
    target_page_id: str | None
    working_draft_title: str | None
    working_draft_content_markdown: str | None
    working_draft_content_sha256: str | None
    working_draft_updated_at: str | None
    decision: str
    staged_content_markdown: str | None


class DesktopKnowledgeReconciliationResolutionService:
    """Keep review choices inert until one explicit, atomic queue commit."""

    def __init__(self, kb_dir: Path) -> None:
        self._kb_dir = kb_dir.expanduser().resolve()
        self._state_dir = desktop_state_dir(self._kb_dir)
        self._database_path = desktop_state_database_path(self._kb_dir)

    def stage_decisions(
        self,
        candidate_ids: tuple[str, ...],
        decision: str | None,
        *,
        manual_merge_content: str | None = None,
    ) -> tuple[DesktopKnowledgeReconciliationConflict, ...]:
        """Persist an individual or batch choice without changing publication."""
        identifiers = _candidate_ids(candidate_ids)
        if decision is not None and decision not in _DECISIONS:
            raise DesktopImportError(
                "invalid_knowledge_reconciliation_decision",
                "Choose an action supported by this knowledge reconciliation mode.",
            )
        if decision == "manual_merge":
            if len(identifiers) != 1 or manual_merge_content is None:
                raise DesktopImportError(
                    "invalid_knowledge_reconciliation_manual_merge",
                    "Manual merge requires one conflict and explicit merged Markdown.",
                )
        elif manual_merge_content is not None:
            raise DesktopImportError(
                "invalid_knowledge_reconciliation_manual_merge",
                "Merged Markdown is accepted only for the manual_merge action.",
            )
        with kb_ingest_lock(self._state_dir):
            connection = self._connect()
            try:
                connection.execute("BEGIN IMMEDIATE")
                for candidate_id in identifiers:
                    _stage_candidate_in(
                        connection,
                        candidate_id=candidate_id,
                        decision=decision,
                        manual_merge_content=manual_merge_content,
                    )
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
            finally:
                connection.close()
        return DesktopKnowledgeReconciliationService(self._kb_dir).list_conflicts()

    def commit_staged_decisions(self) -> DesktopKnowledgeReconciliationCommit:
        """Commit staged publications or Draft updates and erase review-copy text."""
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
                _require_distinct_draft_updates(candidates)
                current_generation_id = current_generation_id_in(connection)
                for candidate in candidates:
                    _require_current_baseline_in(connection, current_generation_id, candidate)
                    if candidate.reconciliation_mode == "three_way":
                        _require_current_draft_in(connection, candidate)
                    else:
                        _require_no_current_draft_in(connection, candidate)
                published = tuple(
                    _generation_change(candidate)
                    for candidate in candidates
                    if candidate.reconciliation_mode == "two_way"
                    and candidate.decision == "publish_incoming"
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
                draft_results: dict[str, str] = {}
                for candidate in candidates:
                    if candidate.reconciliation_mode != "three_way":
                        continue
                    result = _three_way_result(candidate)
                    draft_results[candidate.candidate_id] = result
                    if candidate.decision in _DRAFT_UPDATE_DECISIONS:
                        connection.execute(
                            """
                            UPDATE knowledge_page_working_drafts
                            SET content_markdown = ?, updated_at = ?
                            WHERE page_id = ?
                            """,
                            (result, now, candidate.target_page_id),
                        )
                        assert candidate.target_page_id is not None
                        prune_obsolete_draft_sources_in(
                            connection,
                            candidate.target_page_id,
                            result,
                            previous_content_markdown=(
                                candidate.working_draft_content_markdown
                                if candidate.decision == "apply_incoming"
                                else None
                            ),
                        )
                        bind_generation_sources_to_draft_in(
                            connection,
                            candidate.target_page_id,
                            result,
                            candidate.sources,
                            created_at=now,
                        )
                for candidate in candidates:
                    resolution_id = uuid.uuid4().hex
                    connection.execute(
                        """
                        INSERT INTO knowledge_reconciliation_resolution_records (
                            resolution_id, candidate_id, document_id, kind, normalized_title,
                            decision, target_page_id, published_generation_id,
                            result_content_sha256, resolved_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            resolution_id,
                            candidate.candidate_id,
                            candidate.document_id,
                            candidate.kind,
                            candidate.normalized_title,
                            candidate.decision,
                            candidate.target_page_id,
                            generation_id if candidate.decision == "publish_incoming" else None,
                            (
                                knowledge_content_sha256(draft_results[candidate.candidate_id])
                                if candidate.candidate_id in draft_results
                                else None
                            ),
                            now,
                        ),
                    )
                    connection.execute(
                        """
                        DELETE FROM knowledge_reconciliation_candidate_sources
                        WHERE candidate_id = ?
                        """,
                        (candidate.candidate_id,),
                    )
                    connection.execute(
                        """
                        UPDATE knowledge_reconciliation_candidates
                        SET staged_decision = NULL,
                            resolution_status = ?,
                            resolved_at = ?,
                            content_markdown = '',
                            entity_subtype = NULL,
                            aliases_json = '[]',
                            tags_json = '[]',
                            analysis_provenance_json = NULL,
                            baseline_content_markdown = NULL,
                            working_draft_content_markdown = NULL,
                            working_draft_content_sha256 = NULL,
                            staged_content_markdown = NULL
                        WHERE candidate_id = ?
                        """,
                        (
                            _resolution_status(candidate.decision),
                            now,
                            candidate.candidate_id,
                        ),
                    )
                staged_projection = stage_generation_projection_in(
                    connection, self._kb_dir, generation_id
                )
                connection.commit()
                outcome = DesktopKnowledgeReconciliationCommit(
                    published_generation_id=generation_id,
                    published_count=len(published),
                    draft_updated_count=sum(
                        candidate.decision in _DRAFT_UPDATE_DECISIONS for candidate in candidates
                    ),
                    kept_count=sum(
                        candidate.decision in {"keep_current", "keep_draft"}
                        for candidate in candidates
                    ),
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


def _stage_candidate_in(
    connection: sqlite3.Connection,
    *,
    candidate_id: str,
    decision: str | None,
    manual_merge_content: str | None,
) -> None:
    row = connection.execute(
        """
        WITH candidate AS (
            SELECT candidates.*,
                COALESCE(
                    candidates.target_page_id,
                    (
                        SELECT matching_drafts.page_id
                        FROM knowledge_page_working_drafts AS matching_drafts
                        WHERE matching_drafts.kind = candidates.kind
                            AND matching_drafts.normalized_title = candidates.normalized_title
                        ORDER BY matching_drafts.updated_at DESC, matching_drafts.page_id
                        LIMIT 1
                    ),
                    (
                        SELECT pages.page_id FROM knowledge_pages AS pages
                        WHERE pages.kind = candidates.kind
                            AND pages.normalized_title = candidates.normalized_title
                    )
                ) AS resolved_page_id
            FROM knowledge_reconciliation_candidates AS candidates
            WHERE candidates.candidate_id = ?
                AND candidates.status = 'pending_conflict'
                AND candidates.resolution_status IS NULL
        )
        SELECT documents.availability, candidate.baseline_kind, candidate.baseline_id,
            candidate.baseline_title, candidate.baseline_content_markdown,
            candidate.resolved_page_id, revisions.revision_id, revisions.title,
            revisions.content_markdown, drafts.title, drafts.content_markdown,
            drafts.updated_at
        FROM candidate
        JOIN source_documents AS documents ON documents.document_id = candidate.document_id
        LEFT JOIN knowledge_pages AS pages ON pages.page_id = candidate.resolved_page_id
        LEFT JOIN knowledge_page_revisions AS revisions
            ON revisions.revision_id = pages.current_revision_id
        LEFT JOIN knowledge_page_working_drafts AS drafts
            ON drafts.page_id = candidate.resolved_page_id
        """,
        (candidate_id,),
    ).fetchone()
    if row is None:
        raise DesktopImportError(
            "knowledge_reconciliation_candidate_not_found",
            "The selected knowledge conflict is no longer available for review.",
        )
    if str(row[0]) != "available" and not candidate_has_durable_adoption_origin_in(
        connection, candidate_id
    ):
        raise DesktopImportError(
            "knowledge_reconciliation_candidate_unavailable",
            "The source document must remain available before choosing a knowledge conflict.",
        )
    if decision is None:
        connection.execute(
            """
            UPDATE knowledge_reconciliation_candidates
            SET staged_decision = NULL, staged_content_markdown = NULL
            WHERE candidate_id = ?
            """,
            (candidate_id,),
        )
        return

    target_page_id = str(row[5]) if row[5] is not None else None
    current_revision_id = str(row[6]) if row[6] is not None else None
    draft_content = str(row[10]) if row[10] is not None else None
    is_three_way = target_page_id is not None and draft_content is not None
    allowed = _THREE_WAY_DECISIONS if is_three_way else _TWO_WAY_DECISIONS
    if decision not in allowed:
        mode = "three-way Working Draft" if is_three_way else "two-way published"
        raise DesktopImportError(
            "invalid_knowledge_reconciliation_decision",
            f"The {decision} action is not valid for a {mode} conflict.",
        )

    baseline_kind = str(row[1]) if row[1] is not None else None
    baseline_id = str(row[2]) if row[2] is not None else None
    baseline_title = str(row[3]) if row[3] is not None else None
    baseline_content = str(row[4]) if row[4] is not None else None
    if current_revision_id is not None:
        baseline_kind = "user_revision"
        baseline_id = current_revision_id
        baseline_title = str(row[7])
        baseline_content = str(row[8])
    if None in {baseline_kind, baseline_id, baseline_title, baseline_content}:
        raise DesktopImportError(
            "knowledge_reconciliation_baseline_changed",
            "This conflict no longer has a published baseline. Refresh before choosing again.",
        )

    draft_title = str(row[9]) if is_three_way else None
    draft_updated_at = str(row[11]) if is_three_way else None
    connection.execute(
        """
        UPDATE knowledge_reconciliation_candidates
        SET reconciliation_mode = ?, target_page_id = ?, baseline_kind = ?,
            baseline_id = ?, baseline_title = ?, baseline_content_markdown = ?,
            working_draft_title = ?, working_draft_content_markdown = ?,
            working_draft_content_sha256 = ?, working_draft_updated_at = ?,
            staged_decision = ?, staged_content_markdown = ?
        WHERE candidate_id = ?
        """,
        (
            "three_way" if is_three_way else "two_way",
            target_page_id,
            baseline_kind,
            baseline_id,
            baseline_title,
            baseline_content,
            draft_title,
            draft_content if is_three_way else None,
            knowledge_content_sha256(draft_content) if draft_content is not None else None,
            draft_updated_at,
            decision,
            manual_merge_content if decision == "manual_merge" else None,
            candidate_id,
        ),
    )


def _staged_candidates_in(connection: sqlite3.Connection) -> tuple[_StagedCandidate, ...]:
    rows = connection.execute(
        """
        SELECT candidates.candidate_id, candidates.document_id, documents.availability,
            candidates.kind, candidates.title, candidates.normalized_title,
            candidates.content_markdown, candidates.content_sha256,
            candidates.entity_subtype, candidates.aliases_json, candidates.tags_json,
            candidates.analysis_provenance_json,
            candidates.baseline_kind, candidates.baseline_id,
            candidates.baseline_content_markdown, candidates.reconciliation_mode,
            candidates.target_page_id, candidates.working_draft_title,
            candidates.working_draft_content_markdown,
            candidates.working_draft_content_sha256,
            candidates.working_draft_updated_at, candidates.staged_decision,
            candidates.staged_content_markdown
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
            entity_subtype=str(row[8]) if row[8] is not None else None,
            aliases=decode_knowledge_labels(row[9]),
            tags=decode_knowledge_labels(row[10]),
            analysis_provenance_json=str(row[11]) if row[11] is not None else None,
            sources=_candidate_sources_in(connection, str(row[0])),
            baseline_kind=str(row[12]),
            baseline_id=str(row[13]),
            baseline_content_markdown=str(row[14]),
            reconciliation_mode=str(row[15]),
            target_page_id=str(row[16]) if row[16] is not None else None,
            working_draft_title=str(row[17]) if row[17] is not None else None,
            working_draft_content_markdown=str(row[18]) if row[18] is not None else None,
            working_draft_content_sha256=str(row[19]) if row[19] is not None else None,
            working_draft_updated_at=str(row[20]) if row[20] is not None else None,
            decision=str(row[21]),
            staged_content_markdown=str(row[22]) if row[22] is not None else None,
        )
        if candidate.decision not in _DECISIONS:
            raise DesktopImportError(
                "knowledge_reconciliation_stage_invalid",
                "A staged knowledge conflict has an invalid decision.",
            )
        if (
            not candidate.document_available
            and not candidate_has_durable_adoption_origin_in(
                connection, candidate.candidate_id
            )
        ):
            raise DesktopImportError(
                "knowledge_reconciliation_candidate_unavailable",
                "The source document must remain available before committing review choices.",
            )
        allowed = (
            _THREE_WAY_DECISIONS
            if candidate.reconciliation_mode == "three_way"
            else _TWO_WAY_DECISIONS
        )
        if candidate.decision not in allowed:
            raise DesktopImportError(
                "knowledge_reconciliation_stage_invalid",
                "A staged action does not match its reconciliation mode.",
            )
        values.append(candidate)
    return tuple(values)


def _candidate_sources_in(
    connection: sqlite3.Connection, candidate_id: str
) -> tuple[KnowledgeGenerationSource, ...]:
    rows = connection.execute(
        """
        SELECT sources.source_id, sources.evidence_id, sources.claim_text
        FROM knowledge_reconciliation_candidate_sources AS sources
        WHERE sources.candidate_id = ?
            AND EXISTS (
                SELECT 1
                FROM evidence_occurrences AS occurrences
                JOIN source_documents AS documents
                    ON documents.document_id = occurrences.document_id
                WHERE occurrences.evidence_id = sources.evidence_id
                    AND documents.availability = 'available'
            )
        ORDER BY sources.source_id
        """,
        (candidate_id,),
    ).fetchall()
    return tuple(KnowledgeGenerationSource(str(row[0]), str(row[1]), str(row[2])) for row in rows)


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


def _require_distinct_draft_updates(candidates: tuple[_StagedCandidate, ...]) -> None:
    pages: set[str] = set()
    for candidate in candidates:
        if candidate.decision not in _DRAFT_UPDATE_DECISIONS:
            continue
        if candidate.target_page_id is None:
            raise DesktopImportError(
                "knowledge_reconciliation_working_draft_changed",
                "The staged Working Draft is no longer available.",
            )
        if candidate.target_page_id in pages:
            raise DesktopImportError(
                "knowledge_reconciliation_multiple_draft_updates",
                "Apply only one incoming change to each Working Draft per commit.",
            )
        pages.add(candidate.target_page_id)


def _require_current_baseline_in(
    connection: sqlite3.Connection,
    current_generation_id: int | None,
    candidate: _StagedCandidate,
) -> None:
    if candidate.baseline_kind == "user_revision":
        if candidate.target_page_id is not None:
            row = connection.execute(
                "SELECT current_revision_id FROM knowledge_pages WHERE page_id = ?",
                (candidate.target_page_id,),
            ).fetchone()
        else:
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
    elif candidate.baseline_kind == "unpublished_page":
        row = connection.execute(
            """
            SELECT drafts.page_id
            FROM knowledge_page_working_drafts AS drafts
            LEFT JOIN knowledge_pages AS pages ON pages.page_id = drafts.page_id
            WHERE drafts.page_id = ? AND pages.current_revision_id IS NULL
            """,
            (candidate.target_page_id,),
        ).fetchone()
        if (
            row is not None
            and candidate.target_page_id is not None
            and candidate.baseline_id == candidate.target_page_id
        ):
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


def _require_current_draft_in(connection: sqlite3.Connection, candidate: _StagedCandidate) -> None:
    if candidate.target_page_id is None:
        raise DesktopImportError(
            "knowledge_reconciliation_working_draft_changed",
            "The staged Working Draft is no longer available.",
        )
    row = connection.execute(
        """
        SELECT title, content_markdown, updated_at
        FROM knowledge_page_working_drafts WHERE page_id = ?
        """,
        (candidate.target_page_id,),
    ).fetchone()
    if (
        row is not None
        and candidate.working_draft_title == str(row[0])
        and candidate.working_draft_content_sha256 == knowledge_content_sha256(str(row[1]))
        and candidate.working_draft_updated_at == str(row[2])
    ):
        return
    raise DesktopImportError(
        "knowledge_reconciliation_working_draft_changed",
        "This Working Draft changed after staging. Refresh it and choose again.",
    )


def _require_no_current_draft_in(
    connection: sqlite3.Connection, candidate: _StagedCandidate
) -> None:
    row = connection.execute(
        """
        SELECT drafts.page_id
        FROM knowledge_page_working_drafts AS drafts
        LEFT JOIN knowledge_pages AS pages ON pages.page_id = drafts.page_id
        WHERE drafts.page_id = ?
            OR (
                drafts.kind = ?
                AND (
                    drafts.normalized_title = ?
                    OR pages.normalized_title = ?
                )
            )
        LIMIT 1
        """,
        (
            candidate.target_page_id,
            candidate.kind,
            candidate.normalized_title,
            candidate.normalized_title,
        ),
    ).fetchone()
    if row is None:
        return
    raise DesktopImportError(
        "knowledge_reconciliation_working_draft_changed",
        "A matching Working Draft now exists. Refresh it and choose a three-way action.",
    )


def _three_way_result(candidate: _StagedCandidate) -> str:
    draft = candidate.working_draft_content_markdown
    if draft is None:
        raise DesktopImportError(
            "knowledge_reconciliation_working_draft_changed",
            "The staged Working Draft snapshot is missing.",
        )
    if candidate.decision == "keep_draft":
        return draft
    if candidate.decision == "apply_incoming":
        return apply_incoming_to_draft(
            baseline=candidate.baseline_content_markdown,
            draft=draft,
            incoming=candidate.content_markdown,
        )
    if candidate.decision == "replace_draft":
        return candidate.content_markdown
    if candidate.decision == "manual_merge" and candidate.staged_content_markdown is not None:
        return candidate.staged_content_markdown
    raise DesktopImportError(
        "knowledge_reconciliation_stage_invalid",
        "The staged three-way action has no deterministic Draft result.",
    )


def _resolution_status(decision: str) -> str:
    if decision == "publish_incoming":
        return "published"
    if decision in {"keep_current", "keep_draft"}:
        return "kept"
    return "draft_updated"


def _generation_change(candidate: _StagedCandidate) -> KnowledgeGenerationChange:
    return KnowledgeGenerationChange(
        document_id=candidate.document_id,
        kind=candidate.kind,
        title=candidate.title,
        normalized_title=candidate.normalized_title,
        content_markdown=candidate.content_markdown,
        content_sha256=candidate.content_sha256,
        entity_subtype=candidate.entity_subtype,
        aliases=candidate.aliases,
        tags=candidate.tags,
        sources=candidate.sources,
        analysis_provenance_json=candidate.analysis_provenance_json,
    )


def _timestamp() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()
