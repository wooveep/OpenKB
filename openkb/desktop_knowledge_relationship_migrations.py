"""Structured, source-bound relationships emitted by corpus synthesis."""

from __future__ import annotations

_MAX_BOUND_EVIDENCE_PER_ENDPOINT = 3
_MAX_RELATIONSHIP_ITEMS_PER_GENERATION = 1_024
_MAX_RELATIONSHIP_SOURCE_CLAIMS_PER_ITEM = 16
_MAX_RELATIONSHIPS_PER_SOURCE_ITEM = 12
_MAX_RELATIONSHIPS_PER_GENERATION = 4_096


def _relationship_insert(item_predicate: str) -> str:
    return f"""
    WITH bounded_items AS (
        SELECT items.generation_id, items.item_key, items.title
        FROM knowledge_generation_items AS items
        WHERE {item_predicate}
        ORDER BY items.generation_id, items.item_key
        LIMIT {_MAX_RELATIONSHIP_ITEMS_PER_GENERATION}
    ), ranked_sources AS (
        SELECT sources.generation_id, sources.item_key, sources.claim_text,
            ROW_NUMBER() OVER (
                PARTITION BY sources.generation_id, sources.item_key
                ORDER BY sources.source_id, sources.evidence_id, sources.claim_text
            ) AS source_rank
        FROM knowledge_generation_item_sources AS sources
        JOIN bounded_items AS items
          ON items.generation_id = sources.generation_id
         AND items.item_key = sources.item_key
    ), bounded_sources AS (
        SELECT generation_id, item_key, claim_text
        FROM ranked_sources
        WHERE source_rank <= {_MAX_RELATIONSHIP_SOURCE_CLAIMS_PER_ITEM}
    ), candidates AS (
        SELECT DISTINCT source_items.generation_id,
            source_items.item_key AS source_item_key,
            target_items.item_key AS target_item_key
        FROM bounded_items AS source_items
        JOIN bounded_sources AS source_sources
          ON source_sources.generation_id = source_items.generation_id
         AND source_sources.item_key = source_items.item_key
        JOIN bounded_items AS target_items
          ON target_items.generation_id = source_items.generation_id
         AND target_items.item_key <> source_items.item_key
        WHERE length(trim(target_items.title)) >= 2
          AND instr(lower(source_sources.claim_text), lower(trim(target_items.title))) > 0
    ), source_ranked AS (
        SELECT candidates.*,
            ROW_NUMBER() OVER (
                PARTITION BY generation_id, source_item_key
                ORDER BY target_item_key
            ) AS relationship_rank
        FROM candidates
    ), generation_ranked AS (
        SELECT source_ranked.*,
            ROW_NUMBER() OVER (
                PARTITION BY generation_id
                ORDER BY source_item_key, target_item_key
            ) AS generation_rank
        FROM source_ranked
        WHERE relationship_rank <= {_MAX_RELATIONSHIPS_PER_SOURCE_ITEM}
    )
    INSERT OR IGNORE INTO knowledge_generation_relationships (
        generation_id, source_item_key, target_item_key, relation_kind, provenance
    )
    SELECT generation_id, source_item_key, target_item_key,
        'references', 'corpus_claim_title_mention'
    FROM generation_ranked
    WHERE generation_rank <= {_MAX_RELATIONSHIPS_PER_GENERATION}
    """


def _source_binding_insert(predicate: str) -> str:
    return f"""
    WITH relevant_relationships AS (
        SELECT * FROM knowledge_generation_relationships AS relationships
        WHERE {predicate}
    ), relevant_items AS (
        SELECT DISTINCT generation_id, source_item_key AS item_key
        FROM relevant_relationships
    ), ranked_sources AS (
        SELECT sources.*,
            ROW_NUMBER() OVER (
                PARTITION BY sources.generation_id, sources.item_key
                ORDER BY sources.source_id, sources.evidence_id, sources.claim_text
            ) AS source_rank
        FROM knowledge_generation_item_sources AS sources
        JOIN relevant_items AS items
          ON items.generation_id = sources.generation_id
         AND items.item_key = sources.item_key
    ), bounded_sources AS (
        SELECT * FROM ranked_sources
        WHERE source_rank <= {_MAX_RELATIONSHIP_SOURCE_CLAIMS_PER_ITEM}
    ), candidates AS (
        SELECT DISTINCT relationships.generation_id,
            relationships.source_item_key, relationships.target_item_key,
            relationships.relation_kind, sources.evidence_id
        FROM relevant_relationships AS relationships
        JOIN knowledge_generation_items AS targets
          ON targets.generation_id = relationships.generation_id
         AND targets.item_key = relationships.target_item_key
        JOIN bounded_sources AS sources
          ON sources.generation_id = relationships.generation_id
         AND sources.item_key = relationships.source_item_key
        WHERE instr(lower(sources.claim_text), lower(trim(targets.title))) > 0
    ), ranked AS (
        SELECT candidates.*,
            ROW_NUMBER() OVER (
                PARTITION BY generation_id, source_item_key, target_item_key,
                    relation_kind
                ORDER BY evidence_id
            ) AS binding_rank
        FROM candidates
    )
    INSERT OR IGNORE INTO knowledge_generation_relationship_sources (
        generation_id, source_item_key, target_item_key, relation_kind,
        binding_role, evidence_id
    )
    SELECT generation_id, source_item_key, target_item_key, relation_kind,
        'source', evidence_id
    FROM ranked WHERE binding_rank <= {_MAX_BOUND_EVIDENCE_PER_ENDPOINT}
    """


def _target_binding_insert(predicate: str) -> str:
    return f"""
    WITH relevant_relationships AS (
        SELECT * FROM knowledge_generation_relationships AS relationships
        WHERE {predicate}
    ), relevant_items AS (
        SELECT DISTINCT generation_id, target_item_key AS item_key
        FROM relevant_relationships
    ), ranked_sources AS (
        SELECT sources.*,
            ROW_NUMBER() OVER (
                PARTITION BY sources.generation_id, sources.item_key
                ORDER BY sources.source_id, sources.evidence_id, sources.claim_text
            ) AS source_rank
        FROM knowledge_generation_item_sources AS sources
        JOIN relevant_items AS items
          ON items.generation_id = sources.generation_id
         AND items.item_key = sources.item_key
    ), bounded_sources AS (
        SELECT * FROM ranked_sources
        WHERE source_rank <= {_MAX_RELATIONSHIP_SOURCE_CLAIMS_PER_ITEM}
    ), candidates AS (
        SELECT DISTINCT relationships.generation_id,
            relationships.source_item_key, relationships.target_item_key,
            relationships.relation_kind, sources.evidence_id
        FROM relevant_relationships AS relationships
        JOIN bounded_sources AS sources
          ON sources.generation_id = relationships.generation_id
         AND sources.item_key = relationships.target_item_key
    ), ranked AS (
        SELECT candidates.*,
            ROW_NUMBER() OVER (
                PARTITION BY generation_id, source_item_key, target_item_key,
                    relation_kind
                ORDER BY evidence_id
            ) AS binding_rank
        FROM candidates
    )
    INSERT OR IGNORE INTO knowledge_generation_relationship_sources (
        generation_id, source_item_key, target_item_key, relation_kind,
        binding_role, evidence_id
    )
    SELECT generation_id, source_item_key, target_item_key, relation_kind,
        'target', evidence_id
    FROM ranked WHERE binding_rank <= {_MAX_BOUND_EVIDENCE_PER_ENDPOINT}
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
    _relationship_insert(
        "items.generation_id = ("
        "SELECT current_generation_id FROM knowledge_generation_state WHERE singleton = 1)"
    ),
    _source_binding_insert(
        "relationships.generation_id = ("
        "SELECT current_generation_id FROM knowledge_generation_state WHERE singleton = 1)"
    ),
    _target_binding_insert(
        "relationships.generation_id = ("
        "SELECT current_generation_id FROM knowledge_generation_state WHERE singleton = 1)"
    ),
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
    predicate = "items.generation_id = ?"
    return (
        _relationship_insert(predicate),
        _source_binding_insert("relationships.generation_id = ?"),
        _target_binding_insert("relationships.generation_id = ?"),
    )
