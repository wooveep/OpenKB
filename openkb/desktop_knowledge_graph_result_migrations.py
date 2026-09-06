"""Immutable current-epoch semantic relation result provenance and selection."""

KNOWLEDGE_GRAPH_RESULT_MIGRATION_STATEMENTS: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS knowledge_graph_results (
        result_id TEXT PRIMARY KEY,
        document_id TEXT NOT NULL REFERENCES source_documents(document_id) ON DELETE CASCADE,
        status TEXT NOT NULL CHECK(status IN ('completed', 'completed_empty')),
        capability_identity TEXT,
        prompt_contract_digest TEXT,
        node_count INTEGER NOT NULL CHECK(node_count >= 0),
        edge_count INTEGER NOT NULL CHECK(edge_count >= 0),
        created_at TEXT NOT NULL,
        quality TEXT NOT NULL CHECK(quality IN ('full', 'degraded')),
        retained_count INTEGER NOT NULL CHECK(retained_count >= 0),
        weakened_count INTEGER NOT NULL CHECK(weakened_count >= 0),
        rejected_count INTEGER NOT NULL CHECK(rejected_count >= 0),
        document_version TEXT NOT NULL,
        evidence_snapshot_digest TEXT NOT NULL,
        canonical_schema_version TEXT NOT NULL,
        normalizer_version TEXT NOT NULL,
        verification_policy_version TEXT NOT NULL,
        CHECK(
            (status = 'completed_empty' AND node_count = 0 AND edge_count = 0)
            OR (status = 'completed' AND node_count > 0)
        )
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS knowledge_graph_results_document_idx
        ON knowledge_graph_results(document_id, created_at DESC, result_id)
    """,
    """
    CREATE TABLE IF NOT EXISTS knowledge_graph_current (
        document_id TEXT PRIMARY KEY REFERENCES source_documents(document_id) ON DELETE CASCADE,
        result_id TEXT NOT NULL UNIQUE
            REFERENCES knowledge_graph_results(result_id) ON DELETE RESTRICT
    )
    """,
)


KNOWLEDGE_GRAPH_CURRENT_REVISION_MIGRATION_STATEMENTS: tuple[str, ...] = (
    """
    CREATE TRIGGER IF NOT EXISTS desktop_retrieval_corpus_knowledge_graph_current_insert
    AFTER INSERT ON knowledge_graph_current
    BEGIN
        UPDATE desktop_retrieval_corpus_state
        SET revision = revision + 1 WHERE singleton = 1;
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS desktop_retrieval_corpus_knowledge_graph_current_update
    AFTER UPDATE ON knowledge_graph_current
    BEGIN
        UPDATE desktop_retrieval_corpus_state
        SET revision = revision + 1 WHERE singleton = 1;
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS desktop_retrieval_corpus_knowledge_graph_current_delete
    AFTER DELETE ON knowledge_graph_current
    BEGIN
        UPDATE desktop_retrieval_corpus_state
        SET revision = revision + 1 WHERE singleton = 1;
    END
    """,
    """
    UPDATE desktop_retrieval_corpus_state
    SET revision = revision + 1 WHERE singleton = 1
    """,
)
