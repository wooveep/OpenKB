"""Schema for corpus candidates, summaries, identities, and qualified generations."""

from __future__ import annotations

import re
import sqlite3


def _catalog_page_invalidation_trigger(
    name: str, event: str, reason: str, *, when: str = ""
) -> str:
    predicate = f" WHEN {when}" if when else ""
    return f"""
    CREATE TRIGGER IF NOT EXISTS {name} AFTER {event} ON knowledge_pages{predicate}
    BEGIN
        UPDATE knowledge_catalog_state
        SET source_revision = source_revision + 1,
            is_stale = 1,
            stale_since = COALESCE(
                stale_since,
                strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
            )
        WHERE singleton = 1;
        INSERT INTO knowledge_catalog_rebuild_tasks (
            singleton, status, reason, requested_source_revision, execution_token,
            attempt_count, error_code, error_reason, created_at, updated_at, completed_at
        )
        SELECT 1, 'pending', '{reason}', source_revision, NULL,
            0, NULL, NULL,
            strftime('%Y-%m-%dT%H:%M:%fZ', 'now'),
            strftime('%Y-%m-%dT%H:%M:%fZ', 'now'), NULL
        FROM knowledge_catalog_state WHERE singleton = 1
        ON CONFLICT(singleton) DO UPDATE SET
            status = 'pending',
            reason = excluded.reason,
            requested_source_revision = excluded.requested_source_revision,
            execution_token = NULL,
            attempt_count = 0,
            error_code = NULL,
            error_reason = NULL,
            updated_at = excluded.updated_at,
            completed_at = NULL;
    END
    """


def _retrieval_item_revision_trigger(event: str) -> str:
    return f"""
    CREATE TRIGGER IF NOT EXISTS desktop_retrieval_corpus_knowledge_generation_items_{event}
    AFTER {event.upper()} ON knowledge_generation_items
    BEGIN
        UPDATE desktop_retrieval_corpus_state
        SET revision = revision + 1
        WHERE singleton = 1;
    END
    """


CORPUS_KNOWLEDGE_MIGRATION_STATEMENTS: tuple[str, ...] = (
    """
    ALTER TABLE knowledge_generations ADD COLUMN qualification_state TEXT NOT NULL
        DEFAULT 'legacy_unqualified'
        CHECK(qualification_state IN (
            'legacy_unqualified', 'candidate', 'qualified', 'failed'
        ))
    """,
    """
    ALTER TABLE knowledge_generations ADD COLUMN synthesis_schema_version TEXT
    """,
    """
    ALTER TABLE knowledge_generation_items ADD COLUMN identity_id TEXT
    """,
    """
    CREATE TABLE knowledge_pages_v53 (
        page_id TEXT PRIMARY KEY,
        kind TEXT NOT NULL CHECK(kind IN ('concept', 'entity', 'procedure')),
        title TEXT NOT NULL,
        normalized_title TEXT NOT NULL,
        materialized_path TEXT NOT NULL UNIQUE,
        current_revision_id TEXT NOT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        lifecycle_state TEXT NOT NULL DEFAULT 'stable'
            CHECK(lifecycle_state IN ('stable', 'deprecated')),
        stale_after TEXT,
        UNIQUE(kind, normalized_title)
    )
    """,
    """
    INSERT INTO knowledge_pages_v53 SELECT
        page_id, kind, title, normalized_title, materialized_path,
        current_revision_id, created_at, updated_at, lifecycle_state, stale_after
    FROM knowledge_pages
    """,
    "DROP TABLE knowledge_pages",
    "ALTER TABLE knowledge_pages_v53 RENAME TO knowledge_pages",
    """
    CREATE INDEX knowledge_pages_kind_updated_idx
        ON knowledge_pages(kind, updated_at DESC)
    """,
    """
    CREATE TABLE knowledge_page_working_drafts_v53 (
        page_id TEXT PRIMARY KEY,
        kind TEXT NOT NULL CHECK(kind IN ('concept', 'entity', 'procedure')),
        title TEXT NOT NULL,
        normalized_title TEXT NOT NULL,
        content_markdown TEXT NOT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        UNIQUE(kind, normalized_title)
    )
    """,
    """
    INSERT INTO knowledge_page_working_drafts_v53 SELECT
        page_id, kind, title, normalized_title, content_markdown, created_at, updated_at
    FROM knowledge_page_working_drafts
    """,
    "DROP TABLE knowledge_page_working_drafts",
    "ALTER TABLE knowledge_page_working_drafts_v53 RENAME TO knowledge_page_working_drafts",
    """
    CREATE TABLE knowledge_generation_items_v53 (
        generation_id INTEGER NOT NULL
            REFERENCES knowledge_generations(generation_id) ON DELETE CASCADE,
        item_key TEXT NOT NULL,
        kind TEXT NOT NULL CHECK(kind IN ('concept', 'entity', 'procedure')),
        title TEXT NOT NULL,
        normalized_title TEXT NOT NULL,
        content_markdown TEXT NOT NULL,
        content_sha256 TEXT NOT NULL,
        source_document_id TEXT NOT NULL
            REFERENCES source_documents(document_id) ON DELETE RESTRICT,
        created_at TEXT NOT NULL,
        provenance_state TEXT NOT NULL DEFAULT 'structural'
            CHECK(provenance_state IN ('source_backed', 'structural')),
        analysis_provenance_json TEXT,
        aliases_json TEXT NOT NULL DEFAULT '[]',
        identity_labels_json TEXT NOT NULL DEFAULT '[]',
        identity_id TEXT,
        PRIMARY KEY(generation_id, item_key),
        UNIQUE(generation_id, kind, normalized_title)
    )
    """,
    """
    INSERT INTO knowledge_generation_items_v53 SELECT
        generation_id, item_key, kind, title, normalized_title,
        content_markdown, content_sha256, source_document_id, created_at,
        provenance_state, analysis_provenance_json,
        aliases_json, identity_labels_json, identity_id
    FROM knowledge_generation_items
    """,
    "DROP TABLE knowledge_generation_items",
    "ALTER TABLE knowledge_generation_items_v53 RENAME TO knowledge_generation_items",
    """
    CREATE INDEX knowledge_generation_items_current_lookup_idx
        ON knowledge_generation_items(generation_id, kind, normalized_title)
    """,
    """
    CREATE TABLE knowledge_reconciliation_candidates_v53 (
        candidate_id TEXT PRIMARY KEY,
        document_id TEXT NOT NULL REFERENCES source_documents(document_id) ON DELETE CASCADE,
        source_block_id TEXT REFERENCES document_ir_blocks(block_id) ON DELETE SET NULL,
        kind TEXT NOT NULL CHECK(kind IN ('concept', 'entity', 'procedure')),
        title TEXT NOT NULL,
        normalized_title TEXT NOT NULL,
        content_markdown TEXT NOT NULL,
        content_sha256 TEXT NOT NULL,
        classification TEXT NOT NULL CHECK(classification IN (
            'duplicate', 'compatible_addition', 'conflict'
        )),
        status TEXT NOT NULL CHECK(status IN ('auto_reconciled', 'pending_conflict')),
        baseline_kind TEXT CHECK(baseline_kind IN (
            'published_generation', 'user_revision', 'unpublished_page'
        )),
        baseline_id TEXT,
        baseline_title TEXT,
        baseline_content_markdown TEXT,
        observed_generation_id INTEGER
            REFERENCES knowledge_generations(generation_id) ON DELETE SET NULL,
        reconciliation_mode TEXT NOT NULL DEFAULT 'two_way'
            CHECK(reconciliation_mode IN ('two_way', 'three_way')),
        target_page_id TEXT,
        working_draft_title TEXT,
        working_draft_content_markdown TEXT,
        working_draft_content_sha256 TEXT,
        working_draft_updated_at TEXT,
        staged_decision TEXT CHECK(staged_decision IN (
            'publish_incoming', 'keep_current', 'keep_draft', 'apply_incoming',
            'replace_draft', 'manual_merge'
        )),
        staged_content_markdown TEXT,
        resolution_status TEXT CHECK(resolution_status IN (
            'published', 'kept', 'draft_updated'
        )),
        resolved_at TEXT,
        created_at TEXT NOT NULL,
        analysis_provenance_json TEXT,
        aliases_json TEXT NOT NULL DEFAULT '[]',
        identity_labels_json TEXT NOT NULL DEFAULT '[]'
    )
    """,
    """
    INSERT INTO knowledge_reconciliation_candidates_v53 SELECT
        candidate_id, document_id, source_block_id, kind, title, normalized_title,
        content_markdown, content_sha256, classification, status, baseline_kind,
        baseline_id, baseline_title, baseline_content_markdown,
        observed_generation_id, reconciliation_mode, target_page_id,
        working_draft_title, working_draft_content_markdown,
        working_draft_content_sha256, working_draft_updated_at, staged_decision,
        staged_content_markdown, resolution_status, resolved_at, created_at,
        analysis_provenance_json, aliases_json, identity_labels_json
    FROM knowledge_reconciliation_candidates
    """,
    "DROP TABLE knowledge_reconciliation_candidates",
    """
    ALTER TABLE knowledge_reconciliation_candidates_v53
        RENAME TO knowledge_reconciliation_candidates
    """,
    """
    CREATE INDEX knowledge_reconciliation_candidates_review_idx
        ON knowledge_reconciliation_candidates(status, created_at DESC)
    """,
    """
    CREATE INDEX knowledge_reconciliation_candidates_document_idx
        ON knowledge_reconciliation_candidates(document_id, created_at DESC)
    """,
    """
    CREATE INDEX knowledge_reconciliation_candidates_staged_idx
        ON knowledge_reconciliation_candidates(
            status, resolution_status, staged_decision
        )
    """,
    """
    CREATE TABLE knowledge_reconciliation_resolution_records_v53 (
        resolution_id TEXT PRIMARY KEY,
        candidate_id TEXT NOT NULL UNIQUE
            REFERENCES knowledge_reconciliation_candidates(candidate_id) ON DELETE CASCADE,
        document_id TEXT NOT NULL REFERENCES source_documents(document_id) ON DELETE CASCADE,
        kind TEXT NOT NULL CHECK(kind IN ('concept', 'entity', 'procedure')),
        normalized_title TEXT NOT NULL,
        decision TEXT NOT NULL CHECK(decision IN (
            'publish_incoming', 'keep_current', 'keep_draft', 'apply_incoming',
            'replace_draft', 'manual_merge'
        )),
        target_page_id TEXT,
        published_generation_id INTEGER
            REFERENCES knowledge_generations(generation_id) ON DELETE SET NULL,
        result_content_sha256 TEXT,
        resolved_at TEXT NOT NULL
    )
    """,
    """
    INSERT INTO knowledge_reconciliation_resolution_records_v53 SELECT *
    FROM knowledge_reconciliation_resolution_records
    """,
    "DROP TABLE knowledge_reconciliation_resolution_records",
    """
    ALTER TABLE knowledge_reconciliation_resolution_records_v53
        RENAME TO knowledge_reconciliation_resolution_records
    """,
    """
    CREATE INDEX knowledge_reconciliation_resolution_records_document_idx
        ON knowledge_reconciliation_resolution_records(document_id, resolved_at DESC)
    """,
    """
    CREATE TABLE knowledge_missing_source_candidates_v53 (
        candidate_id TEXT PRIMARY KEY,
        document_id TEXT NOT NULL
            REFERENCES source_documents(document_id) ON DELETE RESTRICT,
        kind TEXT NOT NULL CHECK(kind IN ('concept', 'entity', 'procedure')),
        title TEXT NOT NULL,
        normalized_title TEXT NOT NULL,
        aliases_json TEXT NOT NULL DEFAULT '[]',
        identity_labels_json TEXT NOT NULL DEFAULT '[]',
        claim_text TEXT NOT NULL,
        reason TEXT NOT NULL CHECK(reason IN (
            'source_not_provided', 'source_reference_unresolved'
        )),
        section TEXT NOT NULL,
        locator_json TEXT NOT NULL,
        analysis_provenance_json TEXT NOT NULL,
        created_at TEXT NOT NULL,
        UNIQUE(document_id, kind, normalized_title, claim_text)
    )
    """,
    """
    INSERT INTO knowledge_missing_source_candidates_v53 SELECT *
    FROM knowledge_missing_source_candidates
    """,
    "DROP TABLE knowledge_missing_source_candidates",
    """
    ALTER TABLE knowledge_missing_source_candidates_v53
        RENAME TO knowledge_missing_source_candidates
    """,
    """
    CREATE INDEX knowledge_missing_source_candidates_created_idx
        ON knowledge_missing_source_candidates(created_at, candidate_id)
    """,
    """
    CREATE TABLE knowledge_catalog_nodes_v53 (
        generation_id TEXT NOT NULL
            REFERENCES knowledge_catalog_generations(generation_id) ON DELETE CASCADE,
        node_id TEXT NOT NULL,
        parent_node_id TEXT,
        node_order INTEGER NOT NULL CHECK(node_order >= 0),
        depth INTEGER NOT NULL CHECK(depth >= 0),
        kind TEXT NOT NULL CHECK(
            kind IN ('root', 'group', 'concept', 'entity', 'procedure', 'source_document')
        ),
        authority TEXT NOT NULL CHECK(
            authority IN ('system', 'user_revision', 'published_generation', 'source_document')
        ),
        authority_id TEXT NOT NULL,
        title TEXT NOT NULL,
        normalized_title TEXT NOT NULL,
        search_text TEXT NOT NULL,
        lifecycle_state TEXT,
        availability TEXT,
        metadata_json TEXT NOT NULL,
        PRIMARY KEY(generation_id, node_id),
        UNIQUE(generation_id, node_order),
        FOREIGN KEY(generation_id, parent_node_id)
            REFERENCES knowledge_catalog_nodes_v53(generation_id, node_id)
    )
    """,
    """
    INSERT INTO knowledge_catalog_nodes_v53 SELECT * FROM knowledge_catalog_nodes
    """,
    "DROP TABLE knowledge_catalog_nodes",
    "ALTER TABLE knowledge_catalog_nodes_v53 RENAME TO knowledge_catalog_nodes",
    """
    CREATE INDEX knowledge_catalog_nodes_search_idx
        ON knowledge_catalog_nodes(generation_id, kind, node_order)
    """,
    _catalog_page_invalidation_trigger(
        "knowledge_catalog_pages_insert", "INSERT", "knowledge_publication"
    ),
    _catalog_page_invalidation_trigger(
        "knowledge_catalog_pages_update",
        "UPDATE",
        "knowledge_publication",
        when=(
            "OLD.current_revision_id IS NOT NEW.current_revision_id "
            "OR OLD.title IS NOT NEW.title "
            "OR OLD.lifecycle_state IS NOT NEW.lifecycle_state "
            "OR OLD.stale_after IS NOT NEW.stale_after"
        ),
    ),
    _catalog_page_invalidation_trigger(
        "knowledge_catalog_pages_delete", "DELETE", "knowledge_deletion"
    ),
    *(_retrieval_item_revision_trigger(event) for event in ("insert", "update", "delete")),
    """
    CREATE TABLE document_summaries (
        document_id TEXT PRIMARY KEY
            REFERENCES source_documents(document_id) ON DELETE CASCADE,
        provenance_state TEXT NOT NULL
            CHECK(provenance_state IN ('source_backed', 'structural')),
        section_map_json TEXT NOT NULL,
        analysis_provenance_json TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE document_summary_units (
        document_id TEXT NOT NULL REFERENCES document_summaries(document_id)
            ON DELETE CASCADE,
        unit_ordinal INTEGER NOT NULL CHECK(unit_ordinal >= 0),
        label TEXT NOT NULL,
        unit_text TEXT NOT NULL,
        PRIMARY KEY(document_id, unit_ordinal)
    )
    """,
    """
    CREATE TABLE document_summary_unit_sources (
        document_id TEXT NOT NULL,
        unit_ordinal INTEGER NOT NULL,
        evidence_id TEXT NOT NULL REFERENCES evidence_refs(evidence_id) ON DELETE RESTRICT,
        PRIMARY KEY(document_id, unit_ordinal, evidence_id),
        FOREIGN KEY(document_id, unit_ordinal)
            REFERENCES document_summary_units(document_id, unit_ordinal) ON DELETE CASCADE
    )
    """,
    """
    CREATE TABLE knowledge_document_candidates (
        candidate_id TEXT PRIMARY KEY,
        document_id TEXT NOT NULL REFERENCES source_documents(document_id) ON DELETE CASCADE,
        kind TEXT NOT NULL CHECK(kind IN ('concept', 'entity', 'procedure')),
        title TEXT NOT NULL,
        normalized_title TEXT NOT NULL,
        aliases_json TEXT NOT NULL,
        identity_labels_json TEXT NOT NULL,
        admission_state TEXT NOT NULL CHECK(admission_state IN ('admit', 'review', 'exclude')),
        admission_reason TEXT NOT NULL,
        analysis_provenance_json TEXT NOT NULL,
        created_at TEXT NOT NULL,
        UNIQUE(document_id, kind, normalized_title)
    )
    """,
    """
    CREATE INDEX knowledge_document_candidates_admission_idx
        ON knowledge_document_candidates(admission_state, kind, normalized_title)
    """,
    """
    CREATE TABLE knowledge_document_candidate_claims (
        candidate_id TEXT NOT NULL REFERENCES knowledge_document_candidates(candidate_id)
            ON DELETE CASCADE,
        claim_ordinal INTEGER NOT NULL CHECK(claim_ordinal >= 0),
        claim_text TEXT NOT NULL,
        applicability_json TEXT NOT NULL,
        PRIMARY KEY(candidate_id, claim_ordinal)
    )
    """,
    """
    CREATE TABLE knowledge_document_candidate_claim_sources (
        candidate_id TEXT NOT NULL,
        claim_ordinal INTEGER NOT NULL,
        evidence_id TEXT NOT NULL REFERENCES evidence_refs(evidence_id) ON DELETE RESTRICT,
        PRIMARY KEY(candidate_id, claim_ordinal, evidence_id),
        FOREIGN KEY(candidate_id, claim_ordinal)
            REFERENCES knowledge_document_candidate_claims(candidate_id, claim_ordinal)
            ON DELETE CASCADE
    )
    """,
    """
    CREATE TABLE knowledge_identities (
        identity_id TEXT PRIMARY KEY,
        kind TEXT NOT NULL CHECK(kind IN ('concept', 'entity', 'procedure')),
        canonical_title TEXT NOT NULL,
        normalized_title TEXT NOT NULL,
        status TEXT NOT NULL CHECK(status IN ('active', 'deferred')),
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        UNIQUE(kind, normalized_title)
    )
    """,
    """
    CREATE TABLE knowledge_identity_aliases (
        identity_id TEXT NOT NULL REFERENCES knowledge_identities(identity_id)
            ON DELETE CASCADE,
        alias TEXT NOT NULL,
        normalized_alias TEXT NOT NULL,
        created_at TEXT NOT NULL,
        PRIMARY KEY(identity_id, normalized_alias)
    )
    """,
    """
    CREATE INDEX knowledge_identity_aliases_lookup_idx
        ON knowledge_identity_aliases(normalized_alias, identity_id)
    """,
    """
    CREATE TABLE knowledge_identity_candidates (
        identity_id TEXT NOT NULL REFERENCES knowledge_identities(identity_id)
            ON DELETE CASCADE,
        candidate_id TEXT NOT NULL REFERENCES knowledge_document_candidates(candidate_id)
            ON DELETE CASCADE,
        match_basis TEXT NOT NULL,
        created_at TEXT NOT NULL,
        PRIMARY KEY(identity_id, candidate_id)
    )
    """,
    """
    CREATE TABLE knowledge_generation_documents (
        generation_id INTEGER NOT NULL REFERENCES knowledge_generations(generation_id)
            ON DELETE CASCADE,
        document_id TEXT NOT NULL REFERENCES source_documents(document_id) ON DELETE RESTRICT,
        PRIMARY KEY(generation_id, document_id)
    )
    """,
    """
    CREATE TABLE knowledge_identity_review_items (
        review_id TEXT PRIMARY KEY,
        kind TEXT NOT NULL CHECK(kind IN ('concept', 'entity', 'procedure')),
        reason TEXT NOT NULL,
        candidate_ids_json TEXT NOT NULL,
        status TEXT NOT NULL CHECK(status IN ('pending', 'resolved')),
        created_at TEXT NOT NULL,
        resolved_at TEXT
    )
    """,
)

_KIND_REBUILD_GROUPS = (
    (3, 8, "knowledge_pages"),
    (8, 12, "knowledge_page_working_drafts"),
    (12, 17, "knowledge_generation_items"),
    (17, 24, "knowledge_reconciliation_candidates"),
    (24, 29, "knowledge_reconciliation_resolution_records"),
    (29, 34, "knowledge_missing_source_candidates"),
    (34, 39, "knowledge_catalog_nodes"),
)
_CREATE_OBJECT = re.compile(r"^CREATE\s+(?:TABLE|INDEX)\s+([A-Za-z0-9_]+)", re.I)


def pending_corpus_knowledge_migration_statements(
    connection: sqlite3.Connection,
) -> tuple[str, ...]:
    """Repair an interrupted pre-ledger v53 migration without repeating DDL."""
    skipped: set[int] = set()
    for start, end, table in _KIND_REBUILD_GROUPS:
        row = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)
        ).fetchone()
        if row is not None and "'procedure'" in str(row[0]):
            skipped.update(range(start, end))
    column_targets = (
        (0, "knowledge_generations", "qualification_state"),
        (1, "knowledge_generations", "synthesis_schema_version"),
        (2, "knowledge_generation_items", "identity_id"),
    )
    for index, table, column in column_targets:
        if connection.execute(
            "SELECT 1 FROM pragma_table_info(?) WHERE name = ?", (table, column)
        ).fetchone():
            skipped.add(index)
    pending: list[str] = []
    for index, statement in enumerate(CORPUS_KNOWLEDGE_MIGRATION_STATEMENTS):
        if index in skipped:
            continue
        match = _CREATE_OBJECT.match(statement.strip())
        if match is not None:
            name = match.group(1)
            if connection.execute("SELECT 1 FROM sqlite_master WHERE name = ?", (name,)).fetchone():
                continue
        pending.append(statement)
    return tuple(pending)
