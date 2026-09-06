"""Additive schema for confirmed Document Lineages, catalog revisions, and diffs."""

from __future__ import annotations


def _catalog_invalidation_trigger(name: str, event: str, table: str) -> str:
    return f"""
    CREATE TRIGGER IF NOT EXISTS {name} AFTER {event} ON {table}
    BEGIN
        UPDATE knowledge_catalog_state
        SET source_revision = source_revision + 1,
            is_stale = 1,
            stale_since = COALESCE(
                stale_since, strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
            )
        WHERE singleton = 1;
        INSERT INTO knowledge_catalog_rebuild_tasks (
            singleton, status, reason, requested_source_revision, execution_token,
            attempt_count, error_code, error_reason, created_at, updated_at, completed_at
        )
        SELECT 1, 'pending', 'document_version_metadata', source_revision, NULL,
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


DOCUMENT_VERSION_CATALOG_MIGRATION_STATEMENTS: tuple[str, ...] = (
    "ALTER TABLE knowledge_candidate_generations "
    "ADD COLUMN candidate_payload_digest TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE knowledge_candidate_generations "
    "ADD COLUMN document_ir_digest TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE knowledge_candidate_generations "
    "ADD COLUMN evidence_digest TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE knowledge_candidate_generations ADD COLUMN page_tree_generation_id TEXT",
    "ALTER TABLE knowledge_candidate_generations ADD COLUMN page_tree_digest TEXT",
    "ALTER TABLE knowledge_candidate_generations "
    "ADD COLUMN analysis_operation TEXT NOT NULL DEFAULT 'knowledge_analysis'",
    "ALTER TABLE knowledge_candidate_generations "
    "ADD COLUMN analysis_contract_digest TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE knowledge_candidate_generations "
    "ADD COLUMN analysis_prompt_digest TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE knowledge_candidate_generations "
    "ADD COLUMN model_capability_provenance_json TEXT NOT NULL DEFAULT '{}'",
    "ALTER TABLE knowledge_candidate_generations "
    "ADD COLUMN claim_count INTEGER NOT NULL DEFAULT 0 CHECK(claim_count >= 0)",
    "ALTER TABLE knowledge_candidate_generations "
    "ADD COLUMN completion_state TEXT NOT NULL DEFAULT 'ready' "
    "CHECK(completion_state IN ('ready', 'empty'))",
    """
    CREATE TABLE IF NOT EXISTS knowledge_generation_graph_inputs (
        generation_id INTEGER NOT NULL
            REFERENCES knowledge_generation_manifests(generation_id) ON DELETE CASCADE,
        document_id TEXT NOT NULL,
        candidate_generation_id TEXT NOT NULL,
        candidate_generation_digest TEXT NOT NULL,
        result_id TEXT REFERENCES knowledge_graph_results(result_id) ON DELETE RESTRICT,
        graph_state TEXT NOT NULL CHECK(graph_state IN (
            'ready', 'completed_empty', 'degraded', 'unavailable_optional'
        )),
        PRIMARY KEY(generation_id, document_id),
        FOREIGN KEY(generation_id, document_id)
            REFERENCES knowledge_generation_candidate_inputs(generation_id, document_id)
            ON DELETE CASCADE
    )
    """,
    "ALTER TABLE document_version_sources ADD COLUMN display_name TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE document_version_sources ADD COLUMN normalized_name TEXT NOT NULL DEFAULT ''",
    """
    ALTER TABLE document_version_sources ADD COLUMN lineage_state TEXT NOT NULL DEFAULT 'singleton'
        CHECK(lineage_state IN ('singleton', 'confirmed', 'needs_order_review'))
    """,
    """
    ALTER TABLE document_version_sources ADD COLUMN version_scheme TEXT NOT NULL DEFAULT 'opaque'
        CHECK(version_scheme IN ('numeric_dotted', 'semver', 'calendar', 'opaque'))
    """,
    """
    ALTER TABLE document_version_sources ADD COLUMN current_document_id TEXT
        REFERENCES source_documents(document_id) ON DELETE SET NULL
    """,
    "ALTER TABLE document_version_sources ADD COLUMN current_set_origin TEXT",
    "ALTER TABLE document_version_sources ADD COLUMN current_set_at TEXT",
    """
    ALTER TABLE document_version_sources ADD COLUMN metadata_revision INTEGER NOT NULL DEFAULT 0
        CHECK(metadata_revision >= 0)
    """,
    "ALTER TABLE document_version_sources ADD COLUMN updated_at TEXT",
    "ALTER TABLE document_version_members ADD COLUMN version_label TEXT",
    "ALTER TABLE document_version_members ADD COLUMN normalized_version_label TEXT",
    "ALTER TABLE document_version_members ADD COLUMN version_key_json TEXT",
    "ALTER TABLE document_version_members ADD COLUMN branch_label TEXT",
    """
    ALTER TABLE document_version_members ADD COLUMN predecessor_document_id TEXT
        REFERENCES source_documents(document_id) ON DELETE SET NULL
    """,
    """
    ALTER TABLE document_version_members ADD COLUMN snapshot_kind TEXT NOT NULL DEFAULT 'unknown'
        CHECK(snapshot_kind IN ('full_snapshot', 'delta', 'unknown'))
    """,
    "ALTER TABLE document_version_members ADD COLUMN metadata_origin TEXT",
    "ALTER TABLE document_version_members ADD COLUMN metadata_confidence REAL",
    "ALTER TABLE document_version_members ADD COLUMN confirmed_at TEXT",
    """
    CREATE TABLE IF NOT EXISTS document_lineage_aliases (
        lineage_id TEXT NOT NULL
            REFERENCES document_version_sources(source_id) ON DELETE CASCADE,
        alias TEXT NOT NULL,
        normalized_alias TEXT NOT NULL,
        origin TEXT NOT NULL,
        confirmed_at TEXT,
        PRIMARY KEY(lineage_id, normalized_alias)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS document_lineage_aliases_lookup_idx
        ON document_lineage_aliases(normalized_alias, lineage_id)
    """,
    """
    ALTER TABLE document_version_candidates ADD COLUMN proposal_group_id TEXT
    """,
    "ALTER TABLE document_version_candidates ADD COLUMN proposed_lineage_name TEXT",
    "ALTER TABLE document_version_candidates ADD COLUMN proposed_document_label TEXT",
    "ALTER TABLE document_version_candidates ADD COLUMN proposed_candidate_label TEXT",
    "ALTER TABLE document_version_candidates ADD COLUMN proposed_predecessor_document_id TEXT",
    "ALTER TABLE document_version_candidates ADD COLUMN proposed_snapshot_kind TEXT",
    "ALTER TABLE document_version_candidates ADD COLUMN label_origin TEXT",
    "ALTER TABLE document_version_candidates ADD COLUMN label_confidence REAL",
    "ALTER TABLE document_version_candidates ADD COLUMN diff_preview_json TEXT",
    "ALTER TABLE document_version_candidates ADD COLUMN candidate_algorithm_version TEXT",
    "ALTER TABLE grounded_answer_citations ADD COLUMN version_label TEXT",
    "ALTER TABLE grounded_answer_citations ADD COLUMN version_side TEXT",
    """
    CREATE TABLE IF NOT EXISTS document_version_catalog_revisions (
        revision_id TEXT PRIMARY KEY,
        source_revision INTEGER NOT NULL CHECK(source_revision >= 0),
        snapshot_digest TEXT NOT NULL,
        snapshot_json TEXT NOT NULL,
        created_at TEXT NOT NULL,
        UNIQUE(source_revision, snapshot_digest)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS document_version_catalog_state (
        singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
        current_revision_id TEXT NOT NULL
            REFERENCES document_version_catalog_revisions(revision_id) ON DELETE RESTRICT,
        activated_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS document_version_diffs (
        diff_id TEXT PRIMARY KEY,
        lineage_id TEXT NOT NULL
            REFERENCES document_version_sources(source_id) ON DELETE CASCADE,
        from_document_id TEXT NOT NULL
            REFERENCES source_documents(document_id) ON DELETE CASCADE,
        to_document_id TEXT NOT NULL
            REFERENCES source_documents(document_id) ON DELETE CASCADE,
        algorithm_version TEXT NOT NULL,
        status TEXT NOT NULL CHECK(status IN ('ready', 'stale', 'failed')),
        stats_json TEXT NOT NULL,
        created_at TEXT NOT NULL,
        UNIQUE(lineage_id, from_document_id, to_document_id, algorithm_version),
        CHECK(from_document_id <> to_document_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS document_version_diff_items (
        item_id TEXT PRIMARY KEY,
        diff_id TEXT NOT NULL
            REFERENCES document_version_diffs(diff_id) ON DELETE CASCADE,
        item_order INTEGER NOT NULL CHECK(item_order >= 0),
        old_block_id TEXT REFERENCES document_ir_blocks(block_id) ON DELETE RESTRICT,
        new_block_id TEXT REFERENCES document_ir_blocks(block_id) ON DELETE RESTRICT,
        old_evidence_id TEXT REFERENCES evidence_refs(evidence_id) ON DELETE RESTRICT,
        new_evidence_id TEXT REFERENCES evidence_refs(evidence_id) ON DELETE RESTRICT,
        content_change_kind TEXT NOT NULL CHECK(content_change_kind IN (
            'unchanged', 'modified', 'added', 'removed'
        )),
        location_change_kind TEXT NOT NULL CHECK(location_change_kind IN (
            'same', 'moved', 'unknown'
        )),
        similarity_score REAL NOT NULL CHECK(similarity_score >= 0 AND similarity_score <= 1),
        reason_json TEXT NOT NULL,
        UNIQUE(diff_id, item_order),
        CHECK(old_block_id IS NOT NULL OR new_block_id IS NOT NULL)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS document_version_diff_pair_idx
        ON document_version_diffs(lineage_id, from_document_id, to_document_id, status)
    """,
    "DROP TRIGGER source_documents_create_version_source",
    """
    CREATE TRIGGER IF NOT EXISTS source_documents_create_version_source
    AFTER INSERT ON source_documents
    BEGIN
        INSERT INTO document_version_sources (
            source_id, created_at, display_name, normalized_name, lineage_state,
            version_scheme, current_document_id, current_set_origin,
            current_set_at, metadata_revision, updated_at
        ) VALUES (
            NEW.document_id, NEW.created_at, NEW.display_name, lower(trim(NEW.display_name)),
            'singleton', 'opaque', NEW.document_id, 'import', NEW.created_at, 1, NEW.created_at
        );
        INSERT INTO document_version_members (
            document_id, source_id, linked_at, snapshot_kind,
            metadata_origin, confirmed_at
        ) VALUES (
            NEW.document_id, NEW.document_id, NEW.created_at,
            'full_snapshot', 'import', NEW.created_at
        );
    END
    """,
    _catalog_invalidation_trigger(
        "knowledge_catalog_version_sources_insert", "INSERT", "document_version_sources"
    ),
    _catalog_invalidation_trigger(
        "knowledge_catalog_version_sources_update", "UPDATE", "document_version_sources"
    ),
    _catalog_invalidation_trigger(
        "knowledge_catalog_version_sources_delete", "DELETE", "document_version_sources"
    ),
    _catalog_invalidation_trigger(
        "knowledge_catalog_version_members_insert", "INSERT", "document_version_members"
    ),
    _catalog_invalidation_trigger(
        "knowledge_catalog_version_members_update", "UPDATE", "document_version_members"
    ),
    _catalog_invalidation_trigger(
        "knowledge_catalog_version_members_delete", "DELETE", "document_version_members"
    ),
    _catalog_invalidation_trigger(
        "knowledge_catalog_lineage_aliases_insert", "INSERT", "document_lineage_aliases"
    ),
    _catalog_invalidation_trigger(
        "knowledge_catalog_lineage_aliases_update", "UPDATE", "document_lineage_aliases"
    ),
    _catalog_invalidation_trigger(
        "knowledge_catalog_lineage_aliases_delete", "DELETE", "document_lineage_aliases"
    ),
)
