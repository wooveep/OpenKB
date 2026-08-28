"""Generated-to-user-page reconciliation admission and baseline lookup."""

from __future__ import annotations

import datetime as dt
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass

from openkb.desktop_knowledge_generations import knowledge_content_sha256
from openkb.desktop_knowledge_reconciliation_changes import IncomingKnowledgeChange


@dataclass(frozen=True)
class KnowledgeReconciliationBaseline:
    kind: str
    baseline_id: str
    title: str
    content_markdown: str
    page_id: str | None = None


@dataclass(frozen=True)
class KnowledgeWorkingDraft:
    page_id: str
    title: str
    content_markdown: str
    content_sha256: str
    updated_at: str
    published: KnowledgeReconciliationBaseline


def record_adoption_match_in(
    connection: sqlite3.Connection,
    *,
    document_id: str,
    change: IncomingKnowledgeChange,
    target_page_id: str,
    observed_generation_id: int,
    insert_candidate: Callable[..., str],
) -> str:
    """Queue one explicit generated-to-user-page match for human reconciliation."""
    document = connection.execute(
        "SELECT 1 FROM source_documents WHERE document_id = ?",
        (document_id,),
    ).fetchone()
    if document is None:
        raise ValueError("knowledge_adoption_source_unavailable")
    working_draft = working_draft_for_page_in(
        connection,
        page_id=target_page_id,
        kind=change.kind,
    )
    baseline = (
        working_draft.published
        if working_draft is not None
        else published_page_baseline_in(
            connection,
            page_id=target_page_id,
            kind=change.kind,
        )
    )
    if baseline is None:
        raise ValueError("knowledge_adoption_candidate_not_found")
    return insert_candidate(
        connection,
        document_id=document_id,
        change=change,
        classification="conflict",
        status="pending_conflict",
        baseline=baseline,
        working_draft=working_draft,
        observed_generation_id=observed_generation_id,
        now=dt.datetime.now(dt.timezone.utc).isoformat(),
    )


def candidate_has_durable_adoption_origin_in(
    connection: sqlite3.Connection,
    candidate_id: str,
) -> bool:
    """Identify review work backed by an immutable Generated Knowledge snapshot."""
    row = connection.execute(
        """
        SELECT 1
        FROM knowledge_reconciliation_candidates AS candidates
        JOIN knowledge_generation_items AS items
            ON items.generation_id = candidates.observed_generation_id
            AND items.source_document_id = candidates.document_id
            AND items.kind = candidates.kind
            AND items.normalized_title = candidates.normalized_title
            AND items.content_sha256 = candidates.content_sha256
        JOIN knowledge_origin_references AS origins
            ON origins.generation_id = items.generation_id
            AND origins.item_key = items.item_key
            AND origins.page_id = candidates.target_page_id
        WHERE candidates.candidate_id = ?
        LIMIT 1
        """,
        (candidate_id,),
    ).fetchone()
    return row is not None


def working_draft_for_page_in(
    connection: sqlite3.Connection,
    *,
    page_id: str,
    kind: str,
) -> KnowledgeWorkingDraft | None:
    row = connection.execute(
        """
        SELECT drafts.page_id, revisions.revision_id, revisions.title,
            revisions.content_markdown, drafts.title, drafts.content_markdown,
            drafts.updated_at
        FROM knowledge_page_working_drafts AS drafts
        LEFT JOIN knowledge_pages AS pages ON pages.page_id = drafts.page_id
        LEFT JOIN knowledge_page_revisions AS revisions
            ON revisions.revision_id = pages.current_revision_id
        WHERE drafts.page_id = ? AND drafts.kind = ?
        """,
        (page_id, kind),
    ).fetchone()
    if row is None:
        return None
    published = (
        KnowledgeReconciliationBaseline(
            kind="user_revision",
            baseline_id=str(row[1]),
            title=str(row[2]),
            content_markdown=str(row[3]),
            page_id=page_id,
        )
        if row[1] is not None
        else KnowledgeReconciliationBaseline(
            kind="unpublished_page",
            baseline_id=page_id,
            title="",
            content_markdown="",
            page_id=page_id,
        )
    )
    content_markdown = str(row[5])
    return KnowledgeWorkingDraft(
        page_id=page_id,
        title=str(row[4]),
        content_markdown=content_markdown,
        content_sha256=knowledge_content_sha256(content_markdown),
        updated_at=str(row[6]),
        published=published,
    )


def published_page_baseline_in(
    connection: sqlite3.Connection,
    *,
    page_id: str,
    kind: str,
) -> KnowledgeReconciliationBaseline | None:
    row = connection.execute(
        """
        SELECT revisions.revision_id, revisions.title, revisions.content_markdown
        FROM knowledge_pages AS pages
        JOIN knowledge_page_revisions AS revisions
            ON revisions.revision_id = pages.current_revision_id
        WHERE pages.page_id = ? AND pages.kind = ?
        """,
        (page_id, kind),
    ).fetchone()
    if row is None:
        return None
    return KnowledgeReconciliationBaseline(
        kind="user_revision",
        baseline_id=str(row[0]),
        title=str(row[1]),
        content_markdown=str(row[2]),
        page_id=page_id,
    )
