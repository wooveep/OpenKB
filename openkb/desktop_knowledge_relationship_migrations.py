"""Structured, source-bound relationships emitted by corpus synthesis."""

from __future__ import annotations


def _relationship_insert(predicate: str) -> str:
    return f"""
    INSERT OR IGNORE INTO knowledge_generation_relationships (
        generation_id, source_item_key, target_item_key, relation_kind, provenance
    )
    SELECT DISTINCT source_items.generation_id, source_items.item_key,
        target_items.item_key, 'references', 'corpus_claim_title_mention'
    FROM knowledge_generation_items AS source_items
    JOIN knowledge_generation_item_sources AS source_sources
      ON source_sources.generation_id = source_items.generation_id
     AND source_sources.item_key = source_items.item_key
    JOIN knowledge_generation_items AS target_items
      ON target_items.generation_id = source_items.generation_id
     AND target_items.item_key <> source_items.item_key
    WHERE {predicate}
      AND length(trim(target_items.title)) >= 2
      AND instr(lower(source_sources.claim_text), lower(trim(target_items.title))) > 0
    """


def _source_binding_insert(predicate: str) -> str:
    return f"""
    INSERT OR IGNORE INTO knowledge_generation_relationship_sources (
        generation_id, source_item_key, target_item_key, relation_kind,
        binding_role, evidence_id
    )
    SELECT DISTINCT relationships.generation_id, relationships.source_item_key,
        relationships.target_item_key, relationships.relation_kind,
        'source', sources.evidence_id
    FROM knowledge_generation_relationships AS relationships
    JOIN knowledge_generation_items AS targets
      ON targets.generation_id = relationships.generation_id
     AND targets.item_key = relationships.target_item_key
    JOIN knowledge_generation_item_sources AS sources
      ON sources.generation_id = relationships.generation_id
     AND sources.item_key = relationships.source_item_key
    WHERE {predicate}
      AND instr(lower(sources.claim_text), lower(trim(targets.title))) > 0
    """


def _target_binding_insert(predicate: str) -> str:
    return f"""
    INSERT OR IGNORE INTO knowledge_generation_relationship_sources (
        generation_id, source_item_key, target_item_key, relation_kind,
        binding_role, evidence_id
    )
    SELECT DISTINCT relationships.generation_id, relationships.source_item_key,
        relationships.target_item_key, relationships.relation_kind,
        'target', sources.evidence_id
    FROM knowledge_generation_relationships AS relationships
    JOIN knowledge_generation_item_sources AS sources
      ON sources.generation_id = relationships.generation_id
     AND sources.item_key = relationships.target_item_key
    WHERE {predicate}
    """


KNOWLEDGE_RELATIONSHIP_MIGRATION_STATEMENTS: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS knowledge_generation_relationships (
        generation_id INTEGER NOT NULL,
        source_item_key TEXT NOT NULL,
        target_item_key TEXT NOT NULL,
        relation_kind TEXT NOT NULL CHECK(relation_kind = 'references'),
        provenance TEXT NOT NULL CHECK(provenance = 'corpus_claim_title_mention'),
        PRIMARY KEY(
            generation_id, source_item_key, target_item_key, relation_kind
        ),
        FOREIGN KEY(generation_id, source_item_key)
            REFERENCES knowledge_generation_items(generation_id, item_key)
            ON DELETE CASCADE,
        FOREIGN KEY(generation_id, target_item_key)
            REFERENCES knowledge_generation_items(generation_id, item_key)
            ON DELETE CASCADE,
        CHECK(source_item_key <> target_item_key)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS knowledge_generation_relationship_sources (
        generation_id INTEGER NOT NULL,
        source_item_key TEXT NOT NULL,
        target_item_key TEXT NOT NULL,
        relation_kind TEXT NOT NULL,
        binding_role TEXT NOT NULL CHECK(binding_role IN ('source', 'target')),
        evidence_id TEXT NOT NULL REFERENCES evidence_refs(evidence_id) ON DELETE RESTRICT,
        PRIMARY KEY(
            generation_id, source_item_key, target_item_key, relation_kind,
            binding_role, evidence_id
        ),
        FOREIGN KEY(
            generation_id, source_item_key, target_item_key, relation_kind
        ) REFERENCES knowledge_generation_relationships(
            generation_id, source_item_key, target_item_key, relation_kind
        ) ON DELETE CASCADE
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS knowledge_generation_relationship_sources_evidence_idx
        ON knowledge_generation_relationship_sources(generation_id, evidence_id)
    """,
    "DELETE FROM knowledge_generation_relationship_sources",
    "DELETE FROM knowledge_generation_relationships",
    _relationship_insert("1 = 1"),
    _source_binding_insert("1 = 1"),
    _target_binding_insert("1 = 1"),
    """
    UPDATE knowledge_catalog_state
    SET source_revision = source_revision + 1,
        is_stale = 1,
        stale_since = COALESCE(
            stale_since,
            strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
        )
    WHERE singleton = 1
    """,
    """
    INSERT INTO knowledge_catalog_rebuild_tasks (
        singleton, status, reason, requested_source_revision, execution_token,
        attempt_count, error_code, error_reason, created_at, updated_at, completed_at
    )
    SELECT 1, 'pending', 'knowledge_relationship_migration', source_revision, NULL,
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
        completed_at = NULL
    """,
)


def relationship_rebuild_statements() -> tuple[str, str, str]:
    """Return parameterized inserts for one immutable generation."""
    predicate = "source_items.generation_id = ?"
    return (
        _relationship_insert(predicate),
        _source_binding_insert("relationships.generation_id = ?"),
        _target_binding_insert("relationships.generation_id = ?"),
    )
