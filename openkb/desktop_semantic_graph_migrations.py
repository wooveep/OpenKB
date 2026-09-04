"""Schema migration from evidence-local graph rows to the Knowledge Identity Graph."""

SEMANTIC_GRAPH_MIGRATION_STATEMENTS: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS knowledge_document_relationships (
        document_id TEXT NOT NULL REFERENCES source_documents(document_id) ON DELETE CASCADE,
        source_candidate_id TEXT NOT NULL
            REFERENCES knowledge_document_candidates(candidate_id) ON DELETE CASCADE,
        target_candidate_id TEXT NOT NULL
            REFERENCES knowledge_document_candidates(candidate_id) ON DELETE CASCADE,
        relation_kind TEXT NOT NULL CHECK(relation_kind IN (
            'IS_A', 'PART_OF', 'RELATED_TO', 'DEPENDS_ON', 'USES', 'PRODUCES',
            'LOCATED_IN', 'CREATED_BY', 'PRECEDES', 'REPLACES'
        )),
        applicability_json TEXT NOT NULL,
        provenance TEXT NOT NULL CHECK(provenance = 'semantic_relation_analysis'),
        PRIMARY KEY(
            document_id, source_candidate_id, target_candidate_id, relation_kind
        ),
        CHECK(source_candidate_id <> target_candidate_id)
    )
    """,
    """
    CREATE TRIGGER IF NOT EXISTS knowledge_document_relationship_endpoint_guard
    BEFORE INSERT ON knowledge_document_relationships
    WHEN NOT EXISTS (
        SELECT 1 FROM knowledge_document_candidates AS source
        JOIN knowledge_document_candidates AS target
          ON target.candidate_id = NEW.target_candidate_id
        WHERE source.candidate_id = NEW.source_candidate_id
          AND source.document_id = NEW.document_id
          AND target.document_id = NEW.document_id
          AND source.admission_state = 'admitted'
          AND target.admission_state = 'admitted'
    )
    BEGIN
        SELECT RAISE(ABORT, 'semantic relationship endpoints must be admitted document candidates');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS knowledge_document_relationship_endpoint_update_guard
    BEFORE UPDATE ON knowledge_document_relationships
    WHEN NOT EXISTS (
        SELECT 1 FROM knowledge_document_candidates AS source
        JOIN knowledge_document_candidates AS target
          ON target.candidate_id = NEW.target_candidate_id
        WHERE source.candidate_id = NEW.source_candidate_id
          AND source.document_id = NEW.document_id
          AND target.document_id = NEW.document_id
          AND source.admission_state = 'admitted'
          AND target.admission_state = 'admitted'
    )
    BEGIN
        SELECT RAISE(ABORT, 'semantic relationship endpoints must be admitted document candidates');
    END
    """,
    """
    CREATE TABLE IF NOT EXISTS knowledge_document_relationship_claims (
        document_id TEXT NOT NULL,
        source_candidate_id TEXT NOT NULL,
        target_candidate_id TEXT NOT NULL,
        relation_kind TEXT NOT NULL,
        support_candidate_id TEXT NOT NULL,
        claim_ordinal INTEGER NOT NULL CHECK(claim_ordinal >= 0),
        PRIMARY KEY(
            document_id, source_candidate_id, target_candidate_id, relation_kind,
            support_candidate_id, claim_ordinal
        ),
        FOREIGN KEY(
            document_id, source_candidate_id, target_candidate_id, relation_kind
        ) REFERENCES knowledge_document_relationships(
            document_id, source_candidate_id, target_candidate_id, relation_kind
        ) ON DELETE CASCADE,
        FOREIGN KEY(support_candidate_id, claim_ordinal)
            REFERENCES knowledge_document_candidate_claims(candidate_id, claim_ordinal)
            ON DELETE CASCADE,
        CHECK(
            support_candidate_id = source_candidate_id
            OR support_candidate_id = target_candidate_id
        )
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS knowledge_document_relationships_endpoint_idx
        ON knowledge_document_relationships(
            source_candidate_id, target_candidate_id, relation_kind
        )
    """,
    """
    CREATE INDEX IF NOT EXISTS knowledge_document_relationship_claims_support_idx
        ON knowledge_document_relationship_claims(support_candidate_id, claim_ordinal)
    """,
    "DROP TABLE IF EXISTS knowledge_generation_relationship_sources",
    "DROP TABLE IF EXISTS knowledge_generation_relationships",
    """
    CREATE TABLE knowledge_generation_relationships (
        generation_id INTEGER NOT NULL,
        source_item_key TEXT NOT NULL,
        target_item_key TEXT NOT NULL,
        relation_kind TEXT NOT NULL CHECK(relation_kind IN (
            'IS_A', 'PART_OF', 'RELATED_TO', 'DEPENDS_ON', 'USES', 'PRODUCES',
            'LOCATED_IN', 'CREATED_BY', 'PRECEDES', 'REPLACES'
        )),
        applicability_json TEXT NOT NULL,
        provenance TEXT NOT NULL CHECK(provenance = 'semantic_relation_analysis'),
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
    CREATE TABLE knowledge_generation_relationship_sources (
        generation_id INTEGER NOT NULL,
        source_item_key TEXT NOT NULL,
        target_item_key TEXT NOT NULL,
        relation_kind TEXT NOT NULL,
        binding_role TEXT NOT NULL CHECK(binding_role IN ('source', 'target', 'assertion')),
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
    CREATE INDEX knowledge_generation_relationship_sources_evidence_idx
        ON knowledge_generation_relationship_sources(generation_id, evidence_id)
    """,
    """
    CREATE TRIGGER IF NOT EXISTS desktop_retrieval_corpus_semantic_relationship_insert
    AFTER INSERT ON knowledge_generation_relationships
    BEGIN
        UPDATE desktop_retrieval_corpus_state
        SET revision = revision + 1 WHERE singleton = 1;
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS desktop_retrieval_corpus_semantic_relationship_update
    AFTER UPDATE ON knowledge_generation_relationships
    BEGIN
        UPDATE desktop_retrieval_corpus_state
        SET revision = revision + 1 WHERE singleton = 1;
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS desktop_retrieval_corpus_semantic_relationship_delete
    AFTER DELETE ON knowledge_generation_relationships
    BEGIN
        UPDATE desktop_retrieval_corpus_state
        SET revision = revision + 1 WHERE singleton = 1;
    END
    """,
    """
    UPDATE knowledge_graph_extraction_tasks
    SET status = 'pending', reason = 'semantic_identity_graph_migration',
        execution_token = NULL, retry_scope = NULL, error_code = NULL,
        error_reason = NULL, completed_at = NULL,
        updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
    WHERE EXISTS (
        SELECT 1 FROM knowledge_document_candidates AS candidates
        WHERE candidates.document_id = knowledge_graph_extraction_tasks.document_id
          AND candidates.admission_state = 'admitted'
    )
    """,
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
    SELECT 1, 'pending', 'semantic_identity_graph_migration', source_revision, NULL,
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
