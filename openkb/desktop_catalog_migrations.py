"""SQLite schema and authority triggers for deterministic Catalog generations."""

from __future__ import annotations


def _invalidate_trigger(
    name: str,
    event: str,
    table: str,
    reason: str,
    *,
    when: str = "",
) -> str:
    predicate = f" WHEN {when}" if when else ""
    return f"""
    CREATE TRIGGER {name} AFTER {event} ON {table}{predicate}
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
            0,
            NULL, NULL,
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


CATALOG_MIGRATION_STATEMENTS: tuple[str, ...] = (
    """
    CREATE TABLE knowledge_catalog_generations (
        generation_id TEXT PRIMARY KEY,
        source_revision INTEGER NOT NULL CHECK(source_revision >= 0),
        snapshot_digest TEXT NOT NULL,
        status TEXT NOT NULL CHECK(status IN ('current', 'recent')),
        node_count INTEGER NOT NULL CHECK(node_count >= 0),
        link_count INTEGER NOT NULL CHECK(link_count >= 0),
        created_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE knowledge_catalog_nodes (
        generation_id TEXT NOT NULL
            REFERENCES knowledge_catalog_generations(generation_id) ON DELETE CASCADE,
        node_id TEXT NOT NULL,
        parent_node_id TEXT,
        node_order INTEGER NOT NULL CHECK(node_order >= 0),
        depth INTEGER NOT NULL CHECK(depth >= 0),
        kind TEXT NOT NULL CHECK(
            kind IN ('root', 'group', 'concept', 'entity', 'source_document')
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
            REFERENCES knowledge_catalog_nodes(generation_id, node_id)
    )
    """,
    """
    CREATE TABLE knowledge_catalog_node_sources (
        generation_id TEXT NOT NULL,
        node_id TEXT NOT NULL,
        source_id TEXT NOT NULL,
        evidence_id TEXT NOT NULL,
        document_id TEXT NOT NULL,
        availability TEXT NOT NULL CHECK(availability IN ('available', 'failed')),
        association_order INTEGER NOT NULL CHECK(association_order >= 0),
        PRIMARY KEY(generation_id, node_id, evidence_id),
        FOREIGN KEY(generation_id, node_id)
            REFERENCES knowledge_catalog_nodes(generation_id, node_id) ON DELETE CASCADE
    )
    """,
    """
    CREATE TABLE knowledge_catalog_links (
        generation_id TEXT NOT NULL,
        from_node_id TEXT NOT NULL,
        to_node_id TEXT NOT NULL,
        weight REAL NOT NULL CHECK(weight > 0 AND weight <= 1),
        PRIMARY KEY(generation_id, from_node_id, to_node_id),
        FOREIGN KEY(generation_id, from_node_id)
            REFERENCES knowledge_catalog_nodes(generation_id, node_id) ON DELETE CASCADE,
        FOREIGN KEY(generation_id, to_node_id)
            REFERENCES knowledge_catalog_nodes(generation_id, node_id) ON DELETE CASCADE
    )
    """,
    """
    CREATE TABLE knowledge_catalog_state (
        singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
        source_revision INTEGER NOT NULL CHECK(source_revision >= 0),
        current_generation_id TEXT
            REFERENCES knowledge_catalog_generations(generation_id) ON DELETE RESTRICT,
        is_stale INTEGER NOT NULL CHECK(is_stale IN (0, 1)),
        stale_since TEXT,
        activated_at TEXT
    )
    """,
    """
    CREATE TABLE knowledge_catalog_rebuild_tasks (
        singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
        status TEXT NOT NULL CHECK(status IN ('pending', 'running', 'failed', 'completed')),
        reason TEXT NOT NULL,
        requested_source_revision INTEGER NOT NULL CHECK(requested_source_revision >= 0),
        execution_token TEXT,
        attempt_count INTEGER NOT NULL DEFAULT 0 CHECK(attempt_count >= 0),
        error_code TEXT,
        error_reason TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        completed_at TEXT
    )
    """,
    """
    CREATE INDEX knowledge_catalog_generations_status_idx
        ON knowledge_catalog_generations(status, created_at DESC)
    """,
    """
    CREATE INDEX knowledge_catalog_nodes_search_idx
        ON knowledge_catalog_nodes(generation_id, kind, node_order)
    """,
    """
    CREATE INDEX knowledge_catalog_sources_evidence_idx
        ON knowledge_catalog_node_sources(generation_id, evidence_id, association_order)
    """,
    """
    INSERT INTO knowledge_catalog_state (
        singleton, source_revision, current_generation_id, is_stale, stale_since, activated_at
    ) VALUES (
        1, 1, NULL, 1, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'), NULL
    )
    """,
    """
    INSERT INTO knowledge_catalog_rebuild_tasks (
        singleton, status, reason, requested_source_revision, execution_token,
        attempt_count, error_code, error_reason, created_at, updated_at, completed_at
    ) VALUES (
        1, 'pending', 'schema_upgrade', 1, NULL, 0, NULL, NULL,
        strftime('%Y-%m-%dT%H:%M:%fZ', 'now'),
        strftime('%Y-%m-%dT%H:%M:%fZ', 'now'), NULL
    )
    """,
    _invalidate_trigger(
        "knowledge_catalog_pages_insert", "INSERT", "knowledge_pages", "knowledge_publication"
    ),
    _invalidate_trigger(
        "knowledge_catalog_pages_update",
        "UPDATE",
        "knowledge_pages",
        "knowledge_publication",
        when=(
            "OLD.current_revision_id IS NOT NEW.current_revision_id "
            "OR OLD.title IS NOT NEW.title "
            "OR OLD.lifecycle_state IS NOT NEW.lifecycle_state "
            "OR OLD.stale_after IS NOT NEW.stale_after"
        ),
    ),
    _invalidate_trigger(
        "knowledge_catalog_pages_delete", "DELETE", "knowledge_pages", "knowledge_deletion"
    ),
    _invalidate_trigger(
        "knowledge_catalog_revision_sources_insert",
        "INSERT",
        "knowledge_page_revision_sources",
        "source_map_change",
    ),
    _invalidate_trigger(
        "knowledge_catalog_revision_sources_update",
        "UPDATE",
        "knowledge_page_revision_sources",
        "source_map_change",
    ),
    _invalidate_trigger(
        "knowledge_catalog_revision_sources_delete",
        "DELETE",
        "knowledge_page_revision_sources",
        "source_map_change",
    ),
    _invalidate_trigger(
        "knowledge_catalog_generation_state_insert",
        "INSERT",
        "knowledge_generation_state",
        "knowledge_publication",
    ),
    _invalidate_trigger(
        "knowledge_catalog_generation_state_update",
        "UPDATE",
        "knowledge_generation_state",
        "knowledge_publication",
        when="OLD.current_generation_id IS NOT NEW.current_generation_id",
    ),
    _invalidate_trigger(
        "knowledge_catalog_documents_insert",
        "INSERT",
        "source_documents",
        "document_availability_change",
    ),
    _invalidate_trigger(
        "knowledge_catalog_documents_update",
        "UPDATE",
        "source_documents",
        "document_availability_change",
        when=(
            "OLD.availability IS NOT NEW.availability OR OLD.display_name IS NOT NEW.display_name"
        ),
    ),
    _invalidate_trigger(
        "knowledge_catalog_documents_delete",
        "DELETE",
        "source_documents",
        "document_availability_change",
    ),
    _invalidate_trigger(
        "knowledge_catalog_verifications_insert",
        "INSERT",
        "knowledge_page_verifications",
        "knowledge_metadata_change",
    ),
    _invalidate_trigger(
        "knowledge_catalog_verifications_update",
        "UPDATE",
        "knowledge_page_verifications",
        "knowledge_metadata_change",
    ),
    _invalidate_trigger(
        "knowledge_catalog_verifications_delete",
        "DELETE",
        "knowledge_page_verifications",
        "knowledge_metadata_change",
    ),
)
