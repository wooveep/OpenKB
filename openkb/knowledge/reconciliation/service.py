"""Reconcile imported Concept/Entity candidates without changing document identity.

This is intentionally a small, deterministic first pass.  It extracts bounded
section candidates from Document IR, keeps published derived knowledge as
generation snapshots, and leaves incompatible changes in the review queue.
User-owned Knowledge Page revisions remain separate SQLite authority.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import uuid
from pathlib import Path

from openkb.importing.artifacts import DesktopImportError, DocumentIRBlock
from openkb.importing.types import DesktopKnowledgeReconciliationConflict
from openkb.knowledge.pages.generations import (
    KnowledgeGenerationChange,
    current_generation_id_in,
    knowledge_content_sha256,
    publish_additional_generation_sources_in,
    publish_generation_changes_in,
)
from openkb.knowledge.pages.metadata import encode_knowledge_labels
from openkb.knowledge.pages.okf_projection import (
    activate_okf_projection,
    discard_okf_projection_staging,
    stage_okf_projection_in,
)
from openkb.knowledge.reconciliation.adoption import (
    KnowledgeReconciliationBaseline as _Baseline,
)
from openkb.knowledge.reconciliation.adoption import (
    KnowledgeWorkingDraft as _WorkingDraft,
)
from openkb.knowledge.reconciliation.adoption import (
    candidate_has_durable_adoption_origin_in,
)
from openkb.knowledge.reconciliation.adoption import (
    record_adoption_match_in as _record_adoption_match_in,
)
from openkb.knowledge.reconciliation.changes import (
    IncomingKnowledgeChange,
    extract_incoming_knowledge_changes,
    knowledge_relationship,
)
from openkb.shared.clock import timestamp as _timestamp
from openkb.storage.sqlite import connect_database
from openkb.workspace.paths import desktop_state_database_path

logger = logging.getLogger(__name__)


class DesktopKnowledgeReconciliationService:
    """Auto-reconcile safe changes and isolate every change that meets a Working Draft."""

    def __init__(self, kb_dir: Path) -> None:
        self._kb_dir = kb_dir.expanduser().resolve()
        self._database_path = desktop_state_database_path(self._kb_dir)

    def record_document_changes(
        self, document_id: str, blocks: tuple[DocumentIRBlock, ...]
    ) -> tuple[DesktopKnowledgeReconciliationConflict, ...]:
        """Classify one published document's deterministic incoming knowledge changes.

        The import worker already owns the knowledge-base ingestion lock when it
        calls this method.  As with D3 candidate recording, this method only owns
        a short SQLite transaction and must not acquire that file lock again.
        """
        return self._record_changes(document_id, blocks=blocks)

    def record_analysis_changes(
        self, document_id: str, changes: tuple[IncomingKnowledgeChange, ...]
    ) -> tuple[DesktopKnowledgeReconciliationConflict, ...]:
        """Reconcile a validated Knowledge Analysis result after document publication."""
        return self._record_changes(document_id, changes=changes)

    def record_analysis_changes_in(
        self,
        connection: sqlite3.Connection,
        document_id: str,
        changes: tuple[IncomingKnowledgeChange, ...],
    ) -> tuple[DesktopKnowledgeReconciliationConflict, ...]:
        """Apply structured analysis inside the document publication transaction."""
        return self._record_changes_in(connection, document_id, changes=changes)

    def _record_changes(
        self,
        document_id: str,
        *,
        blocks: tuple[DocumentIRBlock, ...] | None = None,
        changes: tuple[IncomingKnowledgeChange, ...] | None = None,
    ) -> tuple[DesktopKnowledgeReconciliationConflict, ...]:
        connection = self._connect()
        staged_projection: Path | None = None
        try:
            with connection:
                initial_generation_id = current_generation_id_in(connection)
                conflicts = self._record_changes_in(
                    connection, document_id, blocks=blocks, changes=changes
                )
                if current_generation_id_in(connection) != initial_generation_id:
                    staged_projection = stage_okf_projection_in(connection, self._kb_dir)
        except BaseException:
            if staged_projection is not None:
                discard_okf_projection_staging(staged_projection)
            raise
        finally:
            connection.close()
        if staged_projection is not None:
            try:
                activate_okf_projection(self._kb_dir, staged_projection)
            except Exception:
                logger.exception("Could not activate auto-reconciled OKF projection.")
            finally:
                discard_okf_projection_staging(staged_projection)
        return tuple(conflicts)

    def _record_changes_in(
        self,
        connection: sqlite3.Connection,
        document_id: str,
        *,
        blocks: tuple[DocumentIRBlock, ...] | None = None,
        changes: tuple[IncomingKnowledgeChange, ...] | None = None,
    ) -> tuple[DesktopKnowledgeReconciliationConflict, ...]:
        document = connection.execute(
            """SELECT display_name FROM source_documents
            WHERE document_id = ? AND availability = 'available'""",
            (document_id,),
        ).fetchone()
        if document is None:
            return ()
        document_name = str(document[0])
        incoming = (
            changes
            if changes is not None
            else extract_incoming_knowledge_changes(blocks or (), document_name)
        )
        conflicts: list[DesktopKnowledgeReconciliationConflict] = []
        for parsed_change in incoming:
            generation_id = current_generation_id_in(connection)
            for change in _resolved_kinds_in(connection, generation_id, parsed_change):
                conflict = self._reconcile_change_in(
                    connection,
                    document_id=document_id,
                    document_name=document_name,
                    change=change,
                )
                if conflict is not None:
                    conflicts.append(conflict)
        return tuple(conflicts)

    def list_conflicts(self) -> tuple[DesktopKnowledgeReconciliationConflict, ...]:
        """Return pending two-way conflicts and Draft-aware three-way changes."""
        connection = self._connect()
        try:
            rows = connection.execute(
                """
                SELECT candidates.candidate_id, candidates.document_id, documents.display_name,
                    candidates.kind, candidates.title, candidates.content_markdown,
                    candidates.baseline_kind,
                    COALESCE(current_revision.title, candidates.baseline_title),
                    COALESCE(
                        current_revision.content_markdown,
                        candidates.baseline_content_markdown
                    ),
                    candidates.observed_generation_id,
                    CASE WHEN drafts.page_id IS NULL THEN 'two_way' ELSE 'three_way' END,
                    COALESCE(pages.page_id, drafts.page_id), drafts.title,
                    drafts.content_markdown,
                    drafts.updated_at, candidates.staged_decision,
                    candidates.staged_content_markdown, documents.availability
                FROM knowledge_reconciliation_candidates AS candidates
                JOIN source_documents AS documents ON documents.document_id = candidates.document_id
                LEFT JOIN knowledge_pages AS pages ON pages.page_id = COALESCE(
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
                        SELECT matching.page_id FROM knowledge_pages AS matching
                        WHERE matching.kind = candidates.kind
                            AND matching.normalized_title = candidates.normalized_title
                    )
                )
                LEFT JOIN knowledge_page_revisions AS current_revision
                    ON current_revision.revision_id = pages.current_revision_id
                LEFT JOIN knowledge_page_working_drafts AS drafts
                    ON drafts.page_id = COALESCE(
                        pages.page_id,
                        candidates.target_page_id,
                        (
                            SELECT matching_drafts.page_id
                            FROM knowledge_page_working_drafts AS matching_drafts
                            WHERE matching_drafts.kind = candidates.kind
                                AND matching_drafts.normalized_title = candidates.normalized_title
                            ORDER BY matching_drafts.updated_at DESC,
                                matching_drafts.page_id
                            LIMIT 1
                        )
                    )
                WHERE candidates.status = 'pending_conflict'
                    AND candidates.resolution_status IS NULL
                ORDER BY candidates.created_at DESC, candidates.candidate_id
                """
            ).fetchall()
            return tuple(
                _conflict_from_row(row)
                for row in rows
                if str(row[17]) == "available"
                or candidate_has_durable_adoption_origin_in(connection, str(row[0]))
            )
        finally:
            connection.close()

    def record_existing_document_changes(
        self, document_id: str
    ) -> tuple[DesktopKnowledgeReconciliationConflict, ...]:
        """Re-run only reconciliation for a D0 asset using its stored canonical IR."""
        connection = self._connect()
        try:
            canonical = connection.execute(
                """
                SELECT canonical_document_id FROM document_content_fingerprints
                WHERE document_id = ?
                """,
                (document_id,),
            ).fetchone()
            processing_document_id = (
                str(canonical[0])
                if canonical is not None and canonical[0] is not None
                else document_id
            )
            rows = connection.execute(
                """
                SELECT block_id, ordinal, kind, text, heading_path, locator_json
                FROM document_ir_blocks WHERE document_id = ? ORDER BY ordinal
                """,
                (processing_document_id,),
            ).fetchall()
            blocks = tuple(_stored_block(row) for row in rows)
        finally:
            connection.close()
        if not blocks:
            return ()
        return self.record_document_changes(document_id, blocks)

    def current_generation_id(self) -> int | None:
        """Expose the stable current generation for transport and focused checks."""
        connection = self._connect()
        try:
            return current_generation_id_in(connection)
        finally:
            connection.close()

    def record_adoption_match_in(
        self,
        connection: sqlite3.Connection,
        *,
        document_id: str,
        change: IncomingKnowledgeChange,
        target_page_id: str,
        observed_generation_id: int,
    ) -> str:
        """Queue an explicit generated-to-user-page adoption for human reconciliation."""
        return _record_adoption_match_in(
            connection,
            document_id=document_id,
            change=change,
            target_page_id=target_page_id,
            observed_generation_id=observed_generation_id,
            insert_candidate=_insert_candidate_in,
        )

    def _reconcile_change_in(
        self,
        connection: sqlite3.Connection,
        *,
        document_id: str,
        document_name: str,
        change: IncomingKnowledgeChange,
    ) -> DesktopKnowledgeReconciliationConflict | None:
        current_generation_id = current_generation_id_in(connection)
        working_draft = _working_draft_in(connection, change)
        now = _timestamp()
        if working_draft is not None:
            published_relationship = knowledge_relationship(
                change.content_markdown, working_draft.published.content_markdown
            )
            draft_relationship = knowledge_relationship(
                change.content_markdown, working_draft.content_markdown
            )
            classification = published_relationship
            status = (
                "auto_reconciled"
                if "duplicate" in {published_relationship, draft_relationship}
                else "pending_conflict"
            )
            candidate = _insert_candidate_in(
                connection,
                document_id=document_id,
                change=change,
                classification=classification,
                status=status,
                baseline=working_draft.published,
                working_draft=working_draft if status == "pending_conflict" else None,
                observed_generation_id=current_generation_id,
                now=now,
            )
            if status == "pending_conflict":
                return DesktopKnowledgeReconciliationConflict(
                    candidate_id=candidate,
                    document_id=document_id,
                    document_name=document_name,
                    kind=change.kind,
                    title=change.title,
                    content_markdown=change.content_markdown,
                    baseline_kind=working_draft.published.kind,
                    baseline_title=working_draft.published.title,
                    baseline_content_markdown=working_draft.published.content_markdown,
                    observed_generation_id=current_generation_id,
                    reconciliation_mode="three_way",
                    target_page_id=working_draft.page_id,
                    working_draft_title=working_draft.title,
                    working_draft_content_markdown=working_draft.content_markdown,
                    working_draft_updated_at=working_draft.updated_at,
                    staged_decision=None,
                    staged_content_markdown=None,
                )
            return None

        baselines = _baselines_in(connection, current_generation_id, change)
        relationships = tuple(
            knowledge_relationship(change.content_markdown, baseline.content_markdown)
            for baseline in baselines
        )
        conflict_index = next(
            (index for index, value in enumerate(relationships) if value == "conflict"), None
        )
        if conflict_index is not None:
            conflict_baseline = baselines[conflict_index]
            candidate = _insert_candidate_in(
                connection,
                document_id=document_id,
                change=change,
                classification="conflict",
                status="pending_conflict",
                baseline=conflict_baseline,
                working_draft=None,
                observed_generation_id=current_generation_id,
                now=now,
            )
            return DesktopKnowledgeReconciliationConflict(
                candidate_id=candidate,
                document_id=document_id,
                document_name=document_name,
                kind=change.kind,
                title=change.title,
                content_markdown=change.content_markdown,
                baseline_kind=conflict_baseline.kind,
                baseline_title=conflict_baseline.title,
                baseline_content_markdown=conflict_baseline.content_markdown,
                observed_generation_id=current_generation_id,
                reconciliation_mode="two_way",
                target_page_id=conflict_baseline.page_id,
                working_draft_title=None,
                working_draft_content_markdown=None,
                working_draft_updated_at=None,
                staged_decision=None,
                staged_content_markdown=None,
            )

        has_addition = not baselines or any(
            value == "compatible_addition" for value in relationships
        )
        baseline_for_record = baselines[0] if baselines else None
        if has_addition:
            published_generation_id = publish_generation_changes_in(
                connection,
                current_generation_id=current_generation_id,
                changes=(_generation_change(document_id, change),),
                now=now,
            )
            _insert_candidate_in(
                connection,
                document_id=document_id,
                change=change,
                classification="compatible_addition",
                status="auto_reconciled",
                baseline=baseline_for_record,
                working_draft=None,
                observed_generation_id=published_generation_id,
                now=now,
            )
        else:
            duplicate_generation_id = current_generation_id
            if (
                current_generation_id is not None
                and change.sources
                and any(baseline.kind == "published_generation" for baseline in baselines)
            ):
                duplicate_generation_id = publish_additional_generation_sources_in(
                    connection,
                    current_generation_id=current_generation_id,
                    kind=change.kind,
                    normalized_title=change.normalized_title,
                    sources=change.sources,
                    now=now,
                )
            _insert_candidate_in(
                connection,
                document_id=document_id,
                change=change,
                classification="duplicate",
                status="auto_reconciled",
                baseline=baseline_for_record,
                working_draft=None,
                observed_generation_id=duplicate_generation_id,
                now=now,
            )
        return None

    def _connect(self) -> sqlite3.Connection:
        if not self._database_path.is_file():
            raise DesktopImportError(
                "desktop_knowledge_base_not_found",
                "Open a Desktop Knowledge Base before reconciling imported knowledge.",
            )
        connection = connect_database(self._database_path)
        return connection


def _resolved_kinds_in(
    connection: sqlite3.Connection,
    generation_id: int | None,
    change: IncomingKnowledgeChange,
) -> tuple[IncomingKnowledgeChange, ...]:
    """Match an unprefixed heading to established Concept/Entity identities."""
    if change.is_kind_explicit:
        return (change,)
    rows = connection.execute(
        """
        SELECT kind FROM knowledge_page_working_drafts
        WHERE normalized_title = ?
        UNION
        SELECT kind FROM knowledge_pages
        WHERE normalized_title = ?
        UNION
        SELECT kind FROM knowledge_generation_items
        WHERE generation_id = ? AND normalized_title = ?
        ORDER BY kind
        """,
        (
            change.normalized_title,
            change.normalized_title,
            generation_id,
            change.normalized_title,
        ),
    ).fetchall()
    kinds = tuple(str(row[0]) for row in rows)
    if not kinds:
        return (change,)
    return tuple(
        IncomingKnowledgeChange(
            source_block_id=change.source_block_id,
            kind=kind,
            is_kind_explicit=True,
            title=change.title,
            normalized_title=change.normalized_title,
            content_markdown=change.content_markdown,
            content_sha256=change.content_sha256,
            aliases=change.aliases,
            identity_labels=change.identity_labels,
            sources=change.sources,
            analysis_provenance_json=change.analysis_provenance_json,
        )
        for kind in kinds
    )


def _baselines_in(
    connection: sqlite3.Connection,
    generation_id: int | None,
    change: IncomingKnowledgeChange,
) -> tuple[_Baseline, ...]:
    baselines: list[_Baseline] = []
    user = connection.execute(
        """
        SELECT revisions.revision_id, revisions.title, revisions.content_markdown,
            pages.page_id
        FROM knowledge_pages AS pages
        JOIN knowledge_page_revisions AS revisions
            ON revisions.revision_id = pages.current_revision_id
        WHERE pages.kind = ? AND pages.normalized_title = ?
        """,
        (change.kind, change.normalized_title),
    ).fetchone()
    if user is not None:
        baselines.append(
            _Baseline("user_revision", str(user[0]), str(user[1]), str(user[2]), str(user[3]))
        )
    if generation_id is not None:
        published = connection.execute(
            """
            SELECT item_key, title, content_markdown
            FROM knowledge_generation_items
            WHERE generation_id = ? AND kind = ? AND normalized_title = ?
            """,
            (generation_id, change.kind, change.normalized_title),
        ).fetchone()
        if published is not None:
            baselines.append(
                _Baseline(
                    "published_generation", str(published[0]), str(published[1]), str(published[2])
                )
            )
    return tuple(baselines)


def _working_draft_in(
    connection: sqlite3.Connection, change: IncomingKnowledgeChange
) -> _WorkingDraft | None:
    row = connection.execute(
        """
        SELECT drafts.page_id, revisions.revision_id, revisions.title,
            revisions.content_markdown, drafts.title, drafts.content_markdown,
            drafts.updated_at
        FROM knowledge_page_working_drafts AS drafts
        LEFT JOIN knowledge_pages AS pages ON pages.page_id = drafts.page_id
        LEFT JOIN knowledge_page_revisions AS revisions
            ON revisions.revision_id = pages.current_revision_id
        WHERE drafts.kind = ?
            AND (pages.normalized_title = ? OR drafts.normalized_title = ?)
        ORDER BY CASE WHEN drafts.normalized_title = ? THEN 0 ELSE 1 END, pages.page_id
        LIMIT 1
        """,
        (
            change.kind,
            change.normalized_title,
            change.normalized_title,
            change.normalized_title,
        ),
    ).fetchone()
    if row is None:
        return None
    page_id = str(row[0])
    published = (
        _Baseline(
            kind="user_revision",
            baseline_id=str(row[1]),
            title=str(row[2]),
            content_markdown=str(row[3]),
            page_id=page_id,
        )
        if row[1] is not None
        else _Baseline(
            kind="unpublished_page",
            baseline_id=page_id,
            title="",
            content_markdown="",
            page_id=page_id,
        )
    )
    content_markdown = str(row[5])
    return _WorkingDraft(
        page_id=page_id,
        title=str(row[4]),
        content_markdown=content_markdown,
        content_sha256=knowledge_content_sha256(content_markdown),
        updated_at=str(row[6]),
        published=published,
    )


def _generation_change(
    document_id: str, change: IncomingKnowledgeChange
) -> KnowledgeGenerationChange:
    return KnowledgeGenerationChange(
        document_id=document_id,
        kind=change.kind,
        title=change.title,
        normalized_title=change.normalized_title,
        content_markdown=change.content_markdown,
        content_sha256=change.content_sha256,
        aliases=change.aliases,
        identity_labels=change.identity_labels,
        sources=change.sources,
        analysis_provenance_json=change.analysis_provenance_json,
    )


def _insert_candidate_in(
    connection: sqlite3.Connection,
    *,
    document_id: str,
    change: IncomingKnowledgeChange,
    classification: str,
    status: str,
    baseline: _Baseline | None,
    working_draft: _WorkingDraft | None,
    observed_generation_id: int | None,
    now: str,
) -> str:
    existing = connection.execute(
        """
        SELECT candidate_id FROM knowledge_reconciliation_candidates
        WHERE document_id = ? AND kind = ? AND normalized_title = ?
            AND content_sha256 = ? AND classification = ? AND status = ?
            AND COALESCE(target_page_id, '') = COALESCE(?, '')
            AND resolution_status IS NULL
        ORDER BY created_at, candidate_id LIMIT 1
        """,
        (
            document_id,
            change.kind,
            change.normalized_title,
            change.content_sha256,
            classification,
            status,
            baseline.page_id if baseline is not None and status == "pending_conflict" else None,
        ),
    ).fetchone()
    if existing is not None:
        candidate_id = str(existing[0])
        connection.execute(
            """
            UPDATE knowledge_reconciliation_candidates
            SET aliases_json = ?, identity_labels_json = ?,
                analysis_provenance_json = ?
            WHERE candidate_id = ?
            """,
            (
                encode_knowledge_labels(change.aliases),
                encode_knowledge_labels(change.identity_labels),
                change.analysis_provenance_json,
                candidate_id,
            ),
        )
        if status == "pending_conflict":
            _replace_candidate_sources_in(connection, candidate_id, change)
        return candidate_id
    candidate_id = uuid.uuid4().hex
    source_block_id = change.source_block_id
    if source_block_id is not None:
        source_block = connection.execute(
            """
            SELECT 1 FROM document_ir_blocks
            WHERE block_id = ? AND document_id = ?
            """,
            (source_block_id, document_id),
        ).fetchone()
        if source_block is None:
            # D1 versions reuse their canonical document's IR.  The incoming
            # transient block has no row of its own, but the version must still
            # be reconciled against published knowledge.
            source_block_id = None
    connection.execute(
        """
        INSERT INTO knowledge_reconciliation_candidates (
            candidate_id, document_id, source_block_id, kind, title, normalized_title,
            content_markdown, content_sha256, classification, status,
            aliases_json, identity_labels_json, analysis_provenance_json,
            baseline_kind, baseline_id, baseline_title, baseline_content_markdown,
            observed_generation_id, reconciliation_mode, target_page_id,
            working_draft_title, working_draft_content_markdown,
            working_draft_content_sha256, working_draft_updated_at, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            candidate_id,
            document_id,
            source_block_id,
            change.kind,
            change.title,
            change.normalized_title,
            change.content_markdown,
            change.content_sha256,
            classification,
            status,
            encode_knowledge_labels(change.aliases),
            encode_knowledge_labels(change.identity_labels),
            change.analysis_provenance_json,
            baseline.kind if baseline is not None else None,
            baseline.baseline_id if baseline is not None else None,
            baseline.title if baseline is not None else None,
            baseline.content_markdown if baseline is not None else None,
            observed_generation_id,
            "three_way" if working_draft is not None else "two_way",
            baseline.page_id if baseline is not None and status == "pending_conflict" else None,
            working_draft.title if working_draft is not None else None,
            working_draft.content_markdown if working_draft is not None else None,
            working_draft.content_sha256 if working_draft is not None else None,
            working_draft.updated_at if working_draft is not None else None,
            now,
        ),
    )
    if status == "pending_conflict":
        _replace_candidate_sources_in(connection, candidate_id, change)
    return candidate_id


def _replace_candidate_sources_in(
    connection: sqlite3.Connection,
    candidate_id: str,
    change: IncomingKnowledgeChange,
) -> None:
    connection.execute(
        "DELETE FROM knowledge_reconciliation_candidate_sources WHERE candidate_id = ?",
        (candidate_id,),
    )
    connection.executemany(
        """
        INSERT INTO knowledge_reconciliation_candidate_sources (
            candidate_id, source_id, evidence_id, claim_text
        ) VALUES (?, ?, ?, ?)
        """,
        (
            (candidate_id, source.source_id, source.evidence_id, source.claim_text)
            for source in change.sources
        ),
    )


def _conflict_from_row(row: tuple[object, ...]) -> DesktopKnowledgeReconciliationConflict:
    return DesktopKnowledgeReconciliationConflict(
        candidate_id=str(row[0]),
        document_id=str(row[1]),
        document_name=str(row[2]),
        kind=str(row[3]),
        title=str(row[4]),
        content_markdown=str(row[5]),
        baseline_kind=str(row[6]),
        baseline_title=str(row[7]),
        baseline_content_markdown=str(row[8]),
        observed_generation_id=int(str(row[9])) if row[9] is not None else None,
        reconciliation_mode=str(row[10]),
        target_page_id=str(row[11]) if row[11] is not None else None,
        working_draft_title=str(row[12]) if row[12] is not None else None,
        working_draft_content_markdown=str(row[13]) if row[13] is not None else None,
        working_draft_updated_at=str(row[14]) if row[14] is not None else None,
        staged_decision=str(row[15]) if row[15] is not None else None,
        staged_content_markdown=str(row[16]) if row[16] is not None else None,
    )


def _stored_block(row: tuple[object, ...]) -> DocumentIRBlock:
    try:
        heading_path = json.loads(str(row[4]))
        locator = json.loads(str(row[5]))
    except json.JSONDecodeError as error:
        raise DesktopImportError(
            "desktop_import_state_invalid", "Stored Document IR is invalid."
        ) from error
    if (
        not isinstance(heading_path, list)
        or not all(isinstance(value, str) for value in heading_path)
        or not isinstance(locator, dict)
    ):
        raise DesktopImportError("desktop_import_state_invalid", "Stored Document IR is invalid.")
    return DocumentIRBlock(
        block_id=str(row[0]),
        ordinal=int(str(row[1])),
        kind=str(row[2]),
        text=str(row[3]),
        heading_path=tuple(heading_path),
        line_start=1,
        line_end=1,
        locator=locator,
    )
